//! The control server for a worker fleet: an Axum app with `GET /health`, `GET /status`, `GET /workers`,
//! `GET /workers/{id}`, `POST /workers/{id}/commands`, and `GET /ws`, a WebSocket that carries the Protobuf
//! frames of `proto/fleet.proto` between this C2 and a bot worker.
//!
//! Each worker has a command log. A command's sequence id is its position in that log (1, 2, 3 ...). On
//! `Hello { last_acked }` the C2 sends the commands after `last_acked`, keeping at most `window` commands
//! unacknowledged, and on `ReplayRequest { after_seq }` it sends them again from there. The log lives in this
//! process's memory: a second C2 given the same `generate` count produces the same commands, which is what the
//! failover tests rely on. There is no replication between C2s.

use std::collections::{BTreeMap, HashSet};
use std::net::SocketAddr;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use axum::extract::ws::{Message, WebSocket, WebSocketUpgrade};
use axum::extract::{Path, State};
use axum::http::StatusCode;
use axum::response::IntoResponse;
use axum::routing::get;
use axum::{Json, Router};
use serde_json::{json, Value as Json_};
use tokio::net::TcpListener;
use tokio::sync::watch;

use crate::seq::{classify, SeqVerdict};
use crate::wire::{self, Body, PositionDelta};

#[derive(Clone, Debug)]
pub struct C2Config {
    pub id: String,
    /// Every new worker starts with this many generated commands in its log.
    pub generate: u64,
    /// The most commands that may be sent and not yet acknowledged.
    pub window: u64,
    /// A worker that sends nothing (not even a heartbeat) for this long is dropped.
    pub idle_timeout: Duration,
    /// Test hooks: leave these command ids out of the first send (a hole on the wire).
    pub skip_once: Vec<u64>,
    /// Test hooks: send these command ids twice, once.
    pub dup_once: Vec<u64>,
}

impl Default for C2Config {
    fn default() -> Self {
        C2Config {
            id: "c2".into(),
            generate: 0,
            window: 32,
            idle_timeout: Duration::from_secs(15),
            skip_once: Vec::new(),
            dup_once: Vec::new(),
        }
    }
}

/// The `i`th generated command (1 based). The same on every C2, so two C2s agree on a worker's log.
pub fn generated_command(i: u64) -> PositionDelta {
    const UNIT: i64 = 100_000_000;
    // The leader's position walks: +1..+5 units, with every third step a sell of the same kind of size.
    let step = (((i * 7) % 5) + 1) as i64 * UNIT;
    let signed = if i.is_multiple_of(3) { -step } else { step };
    let before: i64 = (1..i)
        .map(|j| {
            let s = (((j * 7) % 5) + 1) as i64 * UNIT;
            if j.is_multiple_of(3) {
                -s
            } else {
                s
            }
        })
        .sum();
    PositionDelta {
        token_id: "1001".into(),
        leader_before: before,
        leader_after: before + signed,
        leader_price: "0.50".into(),
    }
}

struct WorkerEntry {
    wallet: String,
    connected: bool,
    connections: u64,
    last_seen_ms: u64,
    last_acked: u64,
    orders_sent: u64,
    fills_seen: u64,
    acks: u64,
    replays_served: u64,
    anomalies: u64,
    protocol_errors: u64,
    log: Vec<PositionDelta>,
    skipped: HashSet<u64>,
    duplicated: HashSet<u64>,
    outcomes: BTreeMap<String, u64>,
}

struct C2State {
    cfg: C2Config,
    started: Instant,
    workers: Mutex<BTreeMap<String, WorkerEntry>>,
    kill: watch::Sender<bool>,
    commands_sent: AtomicU64,
}

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_millis() as u64)
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct WorkerInfo {
    pub id: String,
    pub wallet: String,
    pub connected: bool,
    pub connections: u64,
    pub last_acked: u64,
    pub orders_sent: u64,
    pub fills_seen: u64,
    pub acks: u64,
    pub replays_served: u64,
    pub anomalies: u64,
    pub log_len: u64,
    pub outcomes: BTreeMap<String, u64>,
}

impl WorkerInfo {
    fn json(&self) -> Json_ {
        json!({
            "id": self.id,
            "wallet": self.wallet,
            "connected": self.connected,
            "connections": self.connections,
            "last_acked": self.last_acked,
            "orders_sent": self.orders_sent,
            "fills_seen": self.fills_seen,
            "acks": self.acks,
            "replays_served": self.replays_served,
            "inbound_seq_anomalies": self.anomalies,
            "commands_in_log": self.log_len,
            "outcomes": self.outcomes,
        })
    }
}

fn new_entry(generate: u64) -> WorkerEntry {
    WorkerEntry {
        wallet: String::new(),
        connected: false,
        connections: 0,
        last_seen_ms: 0,
        last_acked: 0,
        orders_sent: 0,
        fills_seen: 0,
        acks: 0,
        replays_served: 0,
        anomalies: 0,
        protocol_errors: 0,
        log: (1..=generate).map(generated_command).collect(),
        skipped: HashSet::new(),
        duplicated: HashSet::new(),
        outcomes: BTreeMap::new(),
    }
}

impl C2State {
    fn info(&self, id: &str, e: &WorkerEntry) -> WorkerInfo {
        let _ = self;
        WorkerInfo {
            id: id.to_string(),
            wallet: e.wallet.clone(),
            connected: e.connected,
            connections: e.connections,
            last_acked: e.last_acked,
            orders_sent: e.orders_sent,
            fills_seen: e.fills_seen,
            acks: e.acks,
            replays_served: e.replays_served,
            anomalies: e.anomalies + e.protocol_errors,
            log_len: e.log.len() as u64,
            outcomes: e.outcomes.clone(),
        }
    }
}

pub struct C2Handle {
    addr: SocketAddr,
    state: Arc<C2State>,
    task: tokio::task::JoinHandle<()>,
}

pub async fn spawn(addr: SocketAddr, cfg: C2Config) -> std::io::Result<C2Handle> {
    let listener = TcpListener::bind(addr).await?;
    let addr = listener.local_addr()?;
    let (kill, _) = watch::channel(false);
    let state = Arc::new(C2State {
        cfg,
        started: Instant::now(),
        workers: Mutex::new(BTreeMap::new()),
        kill,
        commands_sent: AtomicU64::new(0),
    });
    let task = tokio::spawn({
        let app = router(state.clone());
        async move {
            let _ = axum::serve(listener, app).await;
        }
    });
    Ok(C2Handle { addr, state, task })
}

fn router(state: Arc<C2State>) -> Router {
    Router::new()
        .route("/health", get(health))
        .route("/status", get(status))
        .route("/workers", get(workers))
        .route("/workers/{id}", get(worker))
        .route("/workers/{id}/commands", axum::routing::post(add_command))
        .route("/ws", get(ws_upgrade))
        .with_state(state)
}

impl C2Handle {
    pub fn addr(&self) -> SocketAddr {
        self.addr
    }

    pub fn ws_url(&self) -> String {
        format!("ws://{}/ws", self.addr)
    }

    pub fn http_url(&self) -> String {
        format!("http://{}", self.addr)
    }

    pub fn workers(&self) -> Vec<WorkerInfo> {
        let w = self.state.workers.lock().unwrap();
        w.iter().map(|(id, e)| self.state.info(id, e)).collect()
    }

    pub fn worker(&self, id: &str) -> Option<WorkerInfo> {
        let w = self.state.workers.lock().unwrap();
        w.get(id).map(|e| self.state.info(id, e))
    }

    pub fn commands_sent(&self) -> u64 {
        self.state.commands_sent.load(Ordering::Relaxed)
    }

    /// Stops accepting and drops every worker connection without a close frame, as a dead process would.
    pub fn kill(&self) {
        let _ = self.state.kill.send(true);
        self.task.abort();
    }
}

impl Drop for C2Handle {
    fn drop(&mut self) {
        self.kill();
    }
}

async fn health(State(st): State<Arc<C2State>>) -> Json<Json_> {
    Json(json!({"status": "ok", "c2_id": st.cfg.id}))
}

async fn status(State(st): State<Arc<C2State>>) -> Json<Json_> {
    let w = st.workers.lock().unwrap();
    Json(json!({
        "c2_id": st.cfg.id,
        "uptime_ms": st.started.elapsed().as_millis() as u64,
        "workers_known": w.len(),
        "workers_connected": w.values().filter(|e| e.connected).count(),
        "commands_sent": st.commands_sent.load(Ordering::Relaxed),
        "window": st.cfg.window,
    }))
}

async fn workers(State(st): State<Arc<C2State>>) -> Json<Json_> {
    let w = st.workers.lock().unwrap();
    Json(Json_::Array(w.iter().map(|(id, e)| st.info(id, e).json()).collect()))
}

async fn worker(State(st): State<Arc<C2State>>, Path(id): Path<String>) -> (StatusCode, Json<Json_>) {
    let w = st.workers.lock().unwrap();
    match w.get(&id) {
        Some(e) => (StatusCode::OK, Json(st.info(&id, e).json())),
        None => (StatusCode::NOT_FOUND, Json(json!({"error": "unknown worker"}))),
    }
}

#[derive(serde::Deserialize)]
struct NewCommand {
    token_id: String,
    leader_before: i64,
    leader_after: i64,
    leader_price: String,
}

async fn add_command(State(st): State<Arc<C2State>>, Path(id): Path<String>, Json(c): Json<NewCommand>) -> Json<Json_> {
    let mut w = st.workers.lock().unwrap();
    let e = w.entry(id).or_insert_with(|| new_entry(st.cfg.generate));
    e.log.push(PositionDelta {
        token_id: c.token_id,
        leader_before: c.leader_before,
        leader_after: c.leader_after,
        leader_price: c.leader_price,
    });
    Json(json!({"seq": e.log.len()}))
}

async fn ws_upgrade(ws: WebSocketUpgrade, State(st): State<Arc<C2State>>) -> impl IntoResponse {
    ws.on_upgrade(move |socket| serve_worker(socket, st))
}

async fn send_bin(sock: &mut WebSocket, seq: u64, body: Body) -> bool {
    sock.send(Message::Binary(wire::encode(seq, body).into())).await.is_ok()
}

async fn serve_worker(mut sock: WebSocket, st: Arc<C2State>) {
    // The first frame must be a Hello numbered 1.
    let first = tokio::time::timeout(Duration::from_secs(5), sock.recv()).await;
    let Ok(Some(Ok(Message::Binary(bytes)))) = first else {
        return;
    };
    let Ok(env) = wire::decode(&bytes) else {
        return;
    };
    let Some(Body::Hello(hello)) = env.body else {
        return;
    };
    if classify(0, env.seq) != SeqVerdict::Apply {
        return;
    }
    let id = hello.worker_id.clone();
    {
        let mut w = st.workers.lock().unwrap();
        let e = w.entry(id.clone()).or_insert_with(|| new_entry(st.cfg.generate));
        e.connected = true;
        e.connections += 1;
        e.wallet = hello.wallet.clone();
        e.last_acked = e.last_acked.max(hello.last_acked);
        e.last_seen_ms = now_ms();
    }
    let mut next = hello.last_acked + 1;
    let mut inbound_last = env.seq;
    let mut last_rx = Instant::now();
    let mut kill = st.kill.subscribe();
    let mut tick = tokio::time::interval(Duration::from_millis(5));

    'conn: loop {
        // Send what the window allows.
        loop {
            let send = {
                let mut w = st.workers.lock().unwrap();
                let e = w.get_mut(&id).expect("registered");
                if next > e.log.len() as u64 || next > e.last_acked + st.cfg.window {
                    None
                } else if st.cfg.skip_once.contains(&next) && e.skipped.insert(next) {
                    next += 1;
                    continue;
                } else {
                    let dup = st.cfg.dup_once.contains(&next) && e.duplicated.insert(next);
                    Some((e.log[(next - 1) as usize].clone(), dup))
                }
            };
            let Some((cmd, dup)) = send else { break };
            if !send_bin(&mut sock, next, Body::Command(cmd.clone())).await {
                break 'conn;
            }
            st.commands_sent.fetch_add(1, Ordering::Relaxed);
            if dup {
                if !send_bin(&mut sock, next, Body::Command(cmd)).await {
                    break 'conn;
                }
                st.commands_sent.fetch_add(1, Ordering::Relaxed);
            }
            next += 1;
        }
        if last_rx.elapsed() > st.cfg.idle_timeout {
            break;
        }
        tokio::select! {
            biased;
            _ = kill.changed() => break,
            msg = sock.recv() => {
                let Some(Ok(msg)) = msg else { break };
                last_rx = Instant::now();
                match msg {
                    Message::Binary(bytes) => {
                        let mut w = st.workers.lock().unwrap();
                        let e = w.get_mut(&id).expect("registered");
                        e.last_seen_ms = now_ms();
                        let Ok(env) = wire::decode(&bytes) else {
                            e.protocol_errors += 1;
                            continue;
                        };
                        match classify(inbound_last, env.seq) {
                            SeqVerdict::Apply => {}
                            _ => e.anomalies += 1,
                        }
                        inbound_last = inbound_last.max(env.seq);
                        match env.body {
                            Some(Body::Heartbeat(h)) => {
                                e.orders_sent = h.orders_sent;
                                e.fills_seen = h.fills_seen;
                                e.last_acked = e.last_acked.max(h.last_acked);
                            }
                            Some(Body::Ack(a)) => {
                                e.acks += 1;
                                e.last_acked = e.last_acked.max(a.acked_seq);
                                let label = wire::Outcome::try_from(a.outcome)
                                    .unwrap_or(wire::Outcome::Unspecified)
                                    .label();
                                *e.outcomes.entry(label.to_string()).or_insert(0) += 1;
                            }
                            Some(Body::Replay(r)) => {
                                e.replays_served += 1;
                                next = r.after_seq + 1;
                            }
                            _ => e.anomalies += 1,
                        }
                    }
                    Message::Close(_) => break,
                    Message::Ping(_) | Message::Pong(_) => {}
                    Message::Text(_) => {
                        let mut w = st.workers.lock().unwrap();
                        w.get_mut(&id).expect("registered").protocol_errors += 1;
                    }
                }
            }
            _ = tick.tick() => {}
        }
    }
    let mut w = st.workers.lock().unwrap();
    if let Some(e) = w.get_mut(&id) {
        e.connected = false;
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn generated_commands_chain_each_one_starts_where_the_last_ended() {
        let mut prev_after = 0;
        for i in 1..=50 {
            let c = generated_command(i);
            assert_eq!(c.leader_before, prev_after, "command {i}");
            assert_ne!(c.leader_before, c.leader_after);
            prev_after = c.leader_after;
        }
    }

    #[test]
    fn generated_commands_are_the_same_every_time() {
        assert_eq!(generated_command(7), generated_command(7));
        assert_ne!(generated_command(7), generated_command(8));
    }

    #[test]
    fn a_new_entry_holds_the_generated_log() {
        let e = new_entry(5);
        assert_eq!(e.log.len(), 5);
        assert_eq!(e.log[2], generated_command(3));
    }
}
