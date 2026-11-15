//! A stand-in exchange: a local websocket server that speaks the shape of Binance spot's depth stream
//! and its WebSocket API `depth` request, written from Binance's public documentation. It is not Binance.
//!
//! It keeps its own order book (a plain `BTreeMap` model that shares no code with [`crate::book`], so the
//! tests do not check the client against itself), publishes deltas with update ids that rise without a hole,
//! answers `depth` requests with a snapshot, sends websocket ping frames, and can be told to misbehave:
//! drop a connection without a close frame, silently drop the next forwarded delta, go quiet, or refuse
//! connections.

use std::collections::BTreeMap;
use std::net::SocketAddr;
use std::sync::atomic::{AtomicBool, AtomicU32, AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

use futures_util::{SinkExt, StreamExt};
use tokio::net::{TcpListener, TcpStream};
use tokio::sync::{broadcast, watch};
use tokio::task::JoinHandle;
use tokio::time::{interval, Instant};
use tokio_tungstenite::tungstenite::Message;

use crate::book::{Delta, Fixed, Level, SCALE};
use crate::proto;

#[derive(Clone, Debug)]
pub struct StandInConfig {
    /// Upper case, for example `BTCUSDT`. The stream path uses it lower cased.
    pub symbol: String,
    pub seed: u64,
    /// Price of the first level on each side is `mid` minus or plus half of `spread_ticks` ticks.
    pub mid: Fixed,
    pub tick: Fixed,
    pub spread_ticks: u64,
    pub initial_levels: usize,
    /// Deltas per second the built in publisher sends. Zero means publish only when told to.
    pub rate_per_sec: u32,
    /// Interval between ping frames to each client.
    pub heartbeat_interval: Duration,
}

impl Default for StandInConfig {
    fn default() -> Self {
        StandInConfig {
            symbol: "BTCUSDT".into(),
            seed: 7,
            mid: Fixed(100 * SCALE),
            tick: Fixed(SCALE / 100),
            spread_ticks: 2,
            initial_levels: 50,
            rate_per_sec: 0,
            heartbeat_interval: Duration::from_millis(200),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PublishError {
    /// The levels would leave the best bid at or above the best ask.
    Crossed,
}

#[derive(Debug, Clone, Copy)]
pub struct Published {
    pub first_id: u64,
    pub final_id: u64,
    /// Taken right before the frame was handed to the connections.
    pub sent_at: Instant,
}

struct SplitMix(u64);

impl SplitMix {
    fn next(&mut self) -> u64 {
        self.0 = self.0.wrapping_add(0x9E37_79B9_7F4A_7C15);
        let mut z = self.0;
        z = (z ^ (z >> 30)).wrapping_mul(0xBF58_476D_1CE4_E5B9);
        z = (z ^ (z >> 27)).wrapping_mul(0x94D0_49BB_1331_11EB);
        z ^ (z >> 31)
    }
    fn below(&mut self, n: u64) -> u64 {
        self.next() % n.max(1)
    }
}

struct Exchange {
    bids: BTreeMap<u64, u64>,
    asks: BTreeMap<u64, u64>,
    last_id: u64,
    rng: SplitMix,
}

impl Exchange {
    fn best_bid(&self) -> Option<u64> {
        self.bids.keys().next_back().copied()
    }
    fn best_ask(&self) -> Option<u64> {
        self.asks.keys().next().copied()
    }
    fn set(&mut self, is_bid: bool, price: u64, qty: u64) {
        let side = if is_bid { &mut self.bids } else { &mut self.asks };
        if qty == 0 {
            side.remove(&price);
        } else {
            side.insert(price, qty);
        }
    }
    fn levels(&self, limit: usize) -> (Vec<Level>, Vec<Level>) {
        let bids = self.bids.iter().rev().take(limit).map(|(p, q)| Level { price: Fixed(*p), qty: Fixed(*q) }).collect();
        let asks = self.asks.iter().take(limit).map(|(p, q)| Level { price: Fixed(*p), qty: Fixed(*q) }).collect();
        (bids, asks)
    }
}

struct Frame {
    text: String,
}

struct State {
    cfg: StandInConfig,
    exchange: Mutex<Exchange>,
    frames: broadcast::Sender<Arc<Frame>>,
    kill: watch::Sender<u64>,
    drop_budget: AtomicU32,
    silent: AtomicBool,
    paused: AtomicBool,
    accepting: AtomicBool,
    connections: AtomicU64,
    pongs: AtomicU64,
    snapshots_served: AtomicU64,
}

pub struct StandIn {
    addr: SocketAddr,
    state: Arc<State>,
    tasks: Vec<JoinHandle<()>>,
}

impl StandIn {
    /// Binds `127.0.0.1` on a free port and starts serving.
    pub async fn start(cfg: StandInConfig) -> std::io::Result<StandIn> {
        Self::start_on("127.0.0.1:0", cfg).await
    }

    pub async fn start_on(bind: &str, cfg: StandInConfig) -> std::io::Result<StandIn> {
        let listener = TcpListener::bind(bind).await?;
        let addr = listener.local_addr()?;
        let mut ex = Exchange { bids: BTreeMap::new(), asks: BTreeMap::new(), last_id: 1_000, rng: SplitMix(cfg.seed) };
        let half = cfg.spread_ticks / 2;
        for i in 0..cfg.initial_levels as u64 {
            let q_bid = 1_000_000 + ex.rng.below(9_000_000);
            let q_ask = 1_000_000 + ex.rng.below(9_000_000);
            ex.bids.insert(cfg.mid.0 - (half + i) * cfg.tick.0, q_bid);
            ex.asks.insert(cfg.mid.0 + (cfg.spread_ticks - half + i) * cfg.tick.0, q_ask);
        }
        let (frames, _) = broadcast::channel(65_536);
        let (kill, _) = watch::channel(0u64);
        let state = Arc::new(State {
            cfg: cfg.clone(),
            exchange: Mutex::new(ex),
            frames,
            kill,
            drop_budget: AtomicU32::new(0),
            silent: AtomicBool::new(false),
            paused: AtomicBool::new(false),
            accepting: AtomicBool::new(true),
            connections: AtomicU64::new(0),
            pongs: AtomicU64::new(0),
            snapshots_served: AtomicU64::new(0),
        });
        let mut tasks = Vec::new();
        let st = Arc::clone(&state);
        tasks.push(tokio::spawn(async move {
            loop {
                let Ok((stream, _)) = listener.accept().await else { continue };
                if !st.accepting.load(Ordering::Relaxed) {
                    drop(stream);
                    continue;
                }
                st.connections.fetch_add(1, Ordering::Relaxed);
                tokio::spawn(handle_conn(Arc::clone(&st), stream));
            }
        }));
        if cfg.rate_per_sec > 0 {
            let st = Arc::clone(&state);
            tasks.push(tokio::spawn(async move {
                let mut tick = interval(Duration::from_secs_f64(1.0 / cfg.rate_per_sec as f64));
                loop {
                    tick.tick().await;
                    if !st.paused.load(Ordering::Relaxed) {
                        publish_random_on(&st);
                    }
                }
            }));
        }
        Ok(StandIn { addr, state, tasks })
    }

    pub fn addr(&self) -> SocketAddr {
        self.addr
    }

    pub fn url(&self) -> String {
        format!("ws://{}/ws/{}@depth", self.addr, self.state.cfg.symbol.to_lowercase())
    }

    pub fn symbol(&self) -> &str {
        &self.state.cfg.symbol
    }

    /// Publishes one random valid delta. Returns what was sent.
    pub fn publish_random(&self) -> Published {
        publish_random_on(&self.state)
    }

    /// Publishes a delta made of exactly these levels (size zero removes a level). Refused, and nothing sent,
    /// if it would cross the book.
    pub fn publish_levels(&self, bids: &[Level], asks: &[Level]) -> Result<Published, PublishError> {
        let mut ex = self.state.exchange.lock().unwrap();
        let mut undo: Vec<(bool, u64, Option<u64>)> = Vec::with_capacity(bids.len() + asks.len());
        for (is_bid, levels) in [(true, bids), (false, asks)] {
            for l in levels {
                let old = if is_bid { ex.bids.get(&l.price.0).copied() } else { ex.asks.get(&l.price.0).copied() };
                undo.push((is_bid, l.price.0, old));
                ex.set(is_bid, l.price.0, l.qty.0);
            }
        }
        if let (Some(b), Some(a)) = (ex.best_bid(), ex.best_ask()) {
            if b >= a {
                for (is_bid, price, old) in undo.into_iter().rev() {
                    ex.set(is_bid, price, old.unwrap_or(0));
                }
                return Err(PublishError::Crossed);
            }
        }
        let first = ex.last_id + 1;
        let fin = first;
        ex.last_id = fin;
        let d = Delta { first_id: first, final_id: fin, bids: bids.to_vec(), asks: asks.to_vec() };
        Ok(broadcast_delta(&self.state, &d))
    }

    /// Sends a text frame to every connection as it is, without touching the stand-in's book. For testing how a
    /// client treats a frame the exchange would never send (a crossing delta, junk).
    pub fn publish_raw(&self, text: &str) {
        let _ = self.state.frames.send(Arc::new(Frame { text: text.to_string() }));
    }

    /// The stand-in's own book: update id, bids from the highest price down, asks from the lowest up.
    pub fn book(&self) -> (u64, Vec<Level>, Vec<Level>) {
        let ex = self.state.exchange.lock().unwrap();
        let (b, a) = ex.levels(usize::MAX);
        (ex.last_id, b, a)
    }

    pub fn last_update_id(&self) -> u64 {
        self.state.exchange.lock().unwrap().last_id
    }

    /// Closes every open client connection without a close frame. New connections are still accepted.
    pub fn kill_connections(&self) {
        self.state.kill.send_modify(|g| *g += 1);
    }

    /// The next `n` deltas that a connection would have forwarded are silently dropped, so a client sees a gap.
    pub fn drop_next_forwarded(&self, n: u32) {
        self.state.drop_budget.store(n, Ordering::Relaxed);
    }

    /// While true, connections forward nothing and send no pings or snapshots: a frozen server.
    pub fn set_silent(&self, silent: bool) {
        self.state.silent.store(silent, Ordering::Relaxed);
    }

    /// Stops or restarts the built in publisher.
    pub fn set_paused(&self, paused: bool) {
        self.state.paused.store(paused, Ordering::Relaxed);
    }

    /// While false, a new TCP connection is closed before the websocket handshake.
    pub fn set_accepting(&self, accepting: bool) {
        self.state.accepting.store(accepting, Ordering::Relaxed);
    }

    pub fn connections(&self) -> u64 {
        self.state.connections.load(Ordering::Relaxed)
    }

    pub fn pongs_received(&self) -> u64 {
        self.state.pongs.load(Ordering::Relaxed)
    }

    pub fn snapshots_served(&self) -> u64 {
        self.state.snapshots_served.load(Ordering::Relaxed)
    }
}

impl Drop for StandIn {
    fn drop(&mut self) {
        self.state.kill.send_modify(|g| *g += 1);
        for t in &self.tasks {
            t.abort();
        }
    }
}

fn broadcast_delta(st: &State, d: &Delta) -> Published {
    let text = proto::depth_update_json(&st.cfg.symbol, 0, d);
    let sent_at = Instant::now();
    // No receivers is fine: nobody is connected.
    let _ = st.frames.send(Arc::new(Frame { text }));
    Published { first_id: d.first_id, final_id: d.final_id, sent_at }
}

fn publish_random_on(st: &State) -> Published {
    let mut ex = st.exchange.lock().unwrap();
    let first = ex.last_id + 1;
    let fin = first + ex.rng.below(3);
    let tick = st.cfg.tick.0;
    let mut bids = Vec::new();
    let mut asks = Vec::new();
    let updates = 1 + ex.rng.below(4);
    for _ in 0..updates {
        let is_bid = ex.rng.below(2) == 0;
        let action = ex.rng.below(10);
        let (own_len, other_best) = if is_bid { (ex.bids.len(), ex.best_ask()) } else { (ex.asks.len(), ex.best_bid()) };
        let (price, qty) = if action < 2 && own_len > 5 {
            // remove a random level
            let idx = ex.rng.below(own_len as u64) as usize;
            let side = if is_bid { &ex.bids } else { &ex.asks };
            let p = *side.keys().nth(idx).unwrap();
            (p, 0)
        } else if action < 6 {
            // a new level just behind the touch of the other side, always on the right side of it
            let off = 1 + ex.rng.below(40);
            let q = 1_000_000 + ex.rng.below(9_000_000);
            match other_best {
                Some(ob) => {
                    let p = if is_bid { ob.saturating_sub(off * tick) } else { ob + off * tick };
                    if p == 0 {
                        continue;
                    }
                    (p, q)
                }
                None => continue,
            }
        } else {
            // resize an existing level
            let idx = ex.rng.below(own_len.max(1) as u64) as usize;
            let side = if is_bid { &ex.bids } else { &ex.asks };
            let Some(p) = side.keys().nth(idx).copied() else { continue };
            (p, 1_000_000 + ex.rng.below(9_000_000))
        };
        ex.set(is_bid, price, qty);
        let l = Level { price: Fixed(price), qty: Fixed(qty) };
        if is_bid {
            bids.push(l);
        } else {
            asks.push(l);
        }
    }
    // Keep each side from growing without bound: drop the level furthest from the touch.
    for is_bid in [true, false] {
        let len = if is_bid { ex.bids.len() } else { ex.asks.len() };
        if len > 400 {
            let p = if is_bid { *ex.bids.keys().next().unwrap() } else { *ex.asks.keys().next_back().unwrap() };
            ex.set(is_bid, p, 0);
            let l = Level { price: Fixed(p), qty: Fixed::ZERO };
            if is_bid {
                bids.push(l);
            } else {
                asks.push(l);
            }
        }
    }
    ex.last_id = fin;
    let d = Delta { first_id: first, final_id: fin, bids, asks };
    broadcast_delta(st, &d)
}

async fn handle_conn(st: Arc<State>, stream: TcpStream) {
    let Ok(ws) = tokio_tungstenite::accept_async(stream).await else { return };
    let (mut sink, mut source) = ws.split();
    let mut frames = st.frames.subscribe();
    let mut kill = st.kill.subscribe();
    let mut beat = interval(st.cfg.heartbeat_interval);
    loop {
        tokio::select! {
            // Returning drops the socket: the peer sees the connection end with no close frame.
            _ = kill.changed() => return,
            _ = beat.tick() => {
                if !st.silent.load(Ordering::Relaxed) && sink.send(Message::Ping(Vec::new().into())).await.is_err() {
                    return;
                }
            }
            frame = frames.recv() => match frame {
                Ok(f) => {
                    if st.silent.load(Ordering::Relaxed) {
                        continue;
                    }
                    let skip = take_one(&st.drop_budget);
                    if skip {
                        continue;
                    }
                    if sink.send(Message::text(f.text.clone())).await.is_err() {
                        return;
                    }
                }
                // A client that falls this far behind is disconnected, as a real exchange would.
                Err(_) => return,
            },
            msg = source.next() => match msg {
                Some(Ok(Message::Text(t))) => {
                    if st.silent.load(Ordering::Relaxed) {
                        continue;
                    }
                    if let Some(reply) = answer(&st, t.as_str()) {
                        if sink.send(Message::text(reply)).await.is_err() {
                            return;
                        }
                    }
                }
                Some(Ok(Message::Pong(_))) => {
                    st.pongs.fetch_add(1, Ordering::Relaxed);
                }
                Some(Ok(Message::Close(_))) | None | Some(Err(_)) => return,
                Some(Ok(_)) => {}
            },
        }
    }
}

fn answer(st: &State, text: &str) -> Option<String> {
    let v: serde_json::Value = serde_json::from_str(text).ok()?;
    let id = match v.get("id")? {
        serde_json::Value::String(s) => s.clone(),
        other => other.to_string(),
    };
    if v.get("method").and_then(|m| m.as_str()) != Some("depth") {
        return Some(proto::failure_reply_json(&id, 400, "unknown method"));
    }
    let symbol = v.pointer("/params/symbol").and_then(|s| s.as_str()).unwrap_or("");
    if !symbol.eq_ignore_ascii_case(&st.cfg.symbol) {
        return Some(proto::failure_reply_json(&id, 400, "unknown symbol"));
    }
    let limit = v.pointer("/params/limit").and_then(|l| l.as_u64()).unwrap_or(100).clamp(1, 5000) as usize;
    let ex = st.exchange.lock().unwrap();
    let (bids, asks) = ex.levels(limit);
    st.snapshots_served.fetch_add(1, Ordering::Relaxed);
    Some(proto::snapshot_reply_json(&id, ex.last_id, &bids, &asks))
}

/// Takes one from the counter if it is above zero. True if it did.
fn take_one(counter: &AtomicU32) -> bool {
    let mut cur = counter.load(Ordering::Relaxed);
    while cur > 0 {
        match counter.compare_exchange(cur, cur - 1, Ordering::Relaxed, Ordering::Relaxed) {
            Ok(_) => return true,
            Err(seen) => cur = seen,
        }
    }
    false
}
