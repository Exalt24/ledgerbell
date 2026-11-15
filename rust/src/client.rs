//! The client: one task owns the connection and the book, and publishes what it learns two ways.
//!
//! * The book itself, behind a `RwLock` in [`Handle`]: always the latest state, readable at any time.
//!   It is `None` while the client has no trustworthy book (connecting, resyncing), never a wrong one.
//! * A bounded `mpsc` channel of [`BookEvent`]s for a consumer that wants to be told about changes.
//!   The reader task never waits on that consumer: when the channel is full the event is dropped, counted
//!   in [`Stats::dropped_events`], and the next event that does get through says how many came before it
//!   in `missed_before`. The book is unaffected, so a slow consumer loses notifications, not state.
//!
//! The sync procedure follows Binance's "How to manage a local order book correctly": buffer the stream,
//! take a snapshot, drop buffered deltas the snapshot already covers, apply the rest, and rebuild from a
//! new snapshot on any gap. On a lost connection the client reconnects with exponential backoff and goes
//! through the same procedure, so a delta is never applied twice and none is skipped.

use std::sync::atomic::{AtomicBool, AtomicU64, Ordering};
use std::sync::{Arc, Mutex, RwLock};
use std::time::Duration;

use futures_util::{SinkExt, StreamExt};
use tokio::sync::mpsc;
use tokio::task::JoinHandle;
use tokio::time::{sleep, timeout, Instant};
use tokio_tungstenite::tungstenite::Message;

use crate::book::{ApplyError, Delta, Level, OrderBook};
use crate::proto::{self, Inbound};

#[derive(Clone, Debug)]
pub struct ClientConfig {
    /// For example `ws://127.0.0.1:9001/ws/btcusdt@depth`.
    pub url: String,
    /// Upper case symbol used in the snapshot request, for example `BTCUSDT`.
    pub symbol: String,
    pub snapshot_limit: u32,
    /// No frame of any kind (data or ping) for this long means the connection is dead.
    pub idle_timeout: Duration,
    /// How long to wait for a snapshot reply.
    pub snapshot_timeout: Duration,
    pub backoff_base: Duration,
    pub backoff_max: Duration,
    /// Capacity of the event channel. The consumer lags past this and loses events (see module docs).
    pub channel_capacity: usize,
    /// Keep the (first, final) ids of every applied delta in [`Handle::applied_log`], for tests.
    pub record_applied: bool,
}

impl ClientConfig {
    pub fn new(url: impl Into<String>, symbol: impl Into<String>) -> Self {
        ClientConfig {
            url: url.into(),
            symbol: symbol.into(),
            snapshot_limit: 1000,
            idle_timeout: Duration::from_secs(60),
            snapshot_timeout: Duration::from_secs(10),
            backoff_base: Duration::from_millis(250),
            backoff_max: Duration::from_secs(30),
            channel_capacity: 1024,
            record_applied: false,
        }
    }
}

/// The wait before reconnect attempt number `attempt` (0 is the first retry): base doubled each time, capped.
pub fn backoff_delay(attempt: u32, base: Duration, max: Duration) -> Duration {
    let factor = 1u32.checked_shl(attempt.min(30)).unwrap_or(u32::MAX);
    base.checked_mul(factor).map_or(max, |d| d.min(max))
}

#[derive(Debug, Default)]
pub struct Stats {
    /// Deltas applied to the book.
    pub applied: AtomicU64,
    /// Deltas dropped as already applied or older than the snapshot.
    pub stale: AtomicU64,
    /// Sequence gaps (and refused overlaps and crossed books) that forced a new snapshot.
    pub resyncs: AtomicU64,
    /// Snapshots requested.
    pub snapshots: AtomicU64,
    /// Connections that were established, the first one included.
    pub connections: AtomicU64,
    /// Connection attempts that failed to connect.
    pub connect_failures: AtomicU64,
    /// Sessions that ended after being synced at least once (a dropped connection, an idle timeout).
    pub disconnects: AtomicU64,
    /// Events the consumer's channel had no room for.
    pub dropped_events: AtomicU64,
    /// Events the client tried to send, delivered or not.
    pub event_attempts: AtomicU64,
    pub synced: AtomicBool,
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub enum EventKind {
    /// A new book was built from a snapshot.
    Synced,
    /// A delta was applied.
    Update,
    /// The book was thrown away and is being rebuilt; the text says why.
    Resyncing(String),
}

#[derive(Clone, Debug, PartialEq, Eq)]
pub struct BookEvent {
    pub kind: EventKind,
    pub last_update_id: u64,
    pub best_bid: Option<Level>,
    pub best_ask: Option<Level>,
    /// How many events were dropped, because the channel was full, since the last one delivered.
    pub missed_before: u64,
}

struct Shared {
    book: RwLock<Option<OrderBook>>,
    stats: Stats,
    applied_log: Mutex<Vec<(u64, u64)>>,
}

pub struct Handle {
    pub events: mpsc::Receiver<BookEvent>,
    shared: Arc<Shared>,
    task: JoinHandle<()>,
}

impl Handle {
    pub fn best_bid(&self) -> Option<Level> {
        self.shared.book.read().unwrap().as_ref().and_then(OrderBook::best_bid)
    }

    pub fn best_ask(&self) -> Option<Level> {
        self.shared.book.read().unwrap().as_ref().and_then(OrderBook::best_ask)
    }

    /// The best `n` levels per side, or `None` while there is no trustworthy book.
    pub fn depth(&self, n: usize) -> Option<(Vec<Level>, Vec<Level>)> {
        self.shared.book.read().unwrap().as_ref().map(|b| b.depth(n))
    }

    /// A copy of the whole book, or `None` while there is none.
    pub fn book(&self) -> Option<OrderBook> {
        self.shared.book.read().unwrap().clone()
    }

    pub fn last_update_id(&self) -> Option<u64> {
        self.shared.book.read().unwrap().as_ref().map(OrderBook::last_update_id)
    }

    pub fn stats(&self) -> &Stats {
        &self.shared.stats
    }

    pub fn applied_log(&self) -> Vec<(u64, u64)> {
        self.shared.applied_log.lock().unwrap().clone()
    }

    /// Stops the task. The connection is dropped without a close frame.
    pub fn shutdown(&self) {
        self.task.abort();
    }
}

impl Drop for Handle {
    fn drop(&mut self) {
        self.task.abort();
    }
}

/// Starts the client task. Must be called inside a tokio runtime.
pub fn spawn(cfg: ClientConfig) -> Handle {
    let (tx, rx) = mpsc::channel(cfg.channel_capacity.max(1));
    let shared = Arc::new(Shared {
        book: RwLock::new(None),
        stats: Stats::default(),
        applied_log: Mutex::new(Vec::new()),
    });
    let task = tokio::spawn(run(cfg, Arc::clone(&shared), tx));
    Handle { events: rx, shared, task }
}

struct Emitter {
    tx: mpsc::Sender<BookEvent>,
    pending_missed: u64,
}

impl Emitter {
    fn send(&mut self, shared: &Shared, kind: EventKind, book: Option<&OrderBook>, id: u64) {
        let bid = book.and_then(OrderBook::best_bid);
        let ask = book.and_then(OrderBook::best_ask);
        self.send_tops(shared, kind, id, bid, ask);
    }

    fn send_tops(&mut self, shared: &Shared, kind: EventKind, id: u64, bid: Option<Level>, ask: Option<Level>) {
        shared.stats.event_attempts.fetch_add(1, Ordering::Relaxed);
        let ev = BookEvent { kind, last_update_id: id, best_bid: bid, best_ask: ask, missed_before: self.pending_missed };
        match self.tx.try_send(ev) {
            Ok(()) => self.pending_missed = 0,
            Err(mpsc::error::TrySendError::Full(_)) => {
                self.pending_missed += 1;
                shared.stats.dropped_events.fetch_add(1, Ordering::Relaxed);
            }
            // The consumer is gone. The book is still kept; nobody is told.
            Err(mpsc::error::TrySendError::Closed(_)) => {}
        }
    }
}

/// How one session (one connection) ended.
struct SessionEnd {
    reason: String,
    /// True if the session reached a live book at least once.
    was_synced: bool,
}

async fn run(cfg: ClientConfig, shared: Arc<Shared>, tx: mpsc::Sender<BookEvent>) {
    let mut emitter = Emitter { tx, pending_missed: 0 };
    let mut attempt: u32 = 0;
    loop {
        let connect = timeout(cfg.snapshot_timeout, tokio_tungstenite::connect_async(cfg.url.as_str())).await;
        match connect {
            Ok(Ok((ws, _))) => {
                shared.stats.connections.fetch_add(1, Ordering::Relaxed);
                let end = session(&cfg, &shared, &mut emitter, ws).await;
                *shared.book.write().unwrap() = None;
                shared.stats.synced.store(false, Ordering::Relaxed);
                if end.was_synced {
                    shared.stats.disconnects.fetch_add(1, Ordering::Relaxed);
                    attempt = 0;
                }
                emitter.send(&shared, EventKind::Resyncing(format!("connection ended: {}", end.reason)), None, 0);
            }
            _ => {
                shared.stats.connect_failures.fetch_add(1, Ordering::Relaxed);
            }
        }
        sleep(backoff_delay(attempt, cfg.backoff_base, cfg.backoff_max)).await;
        attempt = attempt.saturating_add(1);
    }
}

type Ws = tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>;

enum Step {
    Frame(Message),
    Ended(String),
}

async fn next_frame(ws: &mut Ws, idle: Duration) -> Step {
    match timeout(idle, ws.next()).await {
        Err(_) => Step::Ended(format!("no frame for {} ms", idle.as_millis())),
        Ok(None) => Step::Ended("stream ended".into()),
        Ok(Some(Err(e))) => Step::Ended(format!("read error: {e}")),
        Ok(Some(Ok(m))) => Step::Frame(m),
    }
}

/// The snapshot half of the procedure: ask for a snapshot, buffer the deltas that arrive meanwhile.
/// Returns the snapshot and the buffered deltas, or why the connection must go.
async fn take_snapshot(
    cfg: &ClientConfig,
    shared: &Shared,
    ws: &mut Ws,
    req_no: &mut u64,
) -> Result<(proto::SnapshotReply, Vec<Delta>), String> {
    let mut buffered: Vec<Delta> = Vec::new();
    *req_no += 1;
    let id = format!("snap-{}", *req_no);
    shared.stats.snapshots.fetch_add(1, Ordering::Relaxed);
    ws.send(Message::text(proto::depth_request(&id, &cfg.symbol, cfg.snapshot_limit)))
        .await
        .map_err(|e| format!("send error: {e}"))?;
    let deadline = Instant::now() + cfg.snapshot_timeout;
    loop {
        let left = deadline.saturating_duration_since(Instant::now());
        if left.is_zero() {
            return Err("no snapshot reply in time".into());
        }
        match next_frame(ws, left.min(cfg.idle_timeout)).await {
            Step::Ended(r) => return Err(r),
            Step::Frame(Message::Text(t)) => match proto::parse(t.as_str()) {
                Ok(Inbound::Depth(d)) => buffered.push(d),
                Ok(Inbound::Snapshot(s)) if s.id == id => return Ok((s, buffered)),
                Ok(Inbound::Failure { id: fid, status, message }) if fid == id => {
                    return Err(format!("snapshot request failed: {status} {message}"))
                }
                Ok(_) => {}
                Err(e) => return Err(e.to_string()),
            },
            Step::Frame(Message::Close(_)) => return Err("closed by server".into()),
            Step::Frame(_) => {}
        }
    }
}

async fn session(cfg: &ClientConfig, shared: &Shared, emitter: &mut Emitter, mut ws: Ws) -> SessionEnd {
    let mut was_synced = false;
    let mut req_no: u64 = 0;
    let mut failed_syncs: u32 = 0;
    'resync: loop {
        if failed_syncs >= 5 {
            return SessionEnd { reason: "five snapshots in a row did not line up with the stream".into(), was_synced };
        }
        let (snap, buffered) = match take_snapshot(cfg, shared, &mut ws, &mut req_no).await {
            Ok(x) => x,
            Err(reason) => return SessionEnd { reason, was_synced },
        };
        // Binance step 4: a snapshot older than the first buffered event cannot be joined to the stream.
        if let Some(first) = buffered.first() {
            if snap.last_update_id < first.first_id {
                failed_syncs += 1;
                continue 'resync;
            }
        }
        let mut book = match OrderBook::from_snapshot(snap.last_update_id, &snap.bids, &snap.asks) {
            Ok(b) => b,
            Err(e) => return SessionEnd { reason: e.to_string(), was_synced },
        };
        for d in &buffered {
            match book.apply(d) {
                Ok(()) => {
                    record(cfg, shared, d);
                    shared.stats.applied.fetch_add(1, Ordering::Relaxed);
                }
                Err(ApplyError::Stale { .. }) => {
                    shared.stats.stale.fetch_add(1, Ordering::Relaxed);
                }
                Err(_) => {
                    failed_syncs += 1;
                    shared.stats.resyncs.fetch_add(1, Ordering::Relaxed);
                    continue 'resync;
                }
            }
        }
        let id = book.last_update_id();
        emitter.send(shared, EventKind::Synced, Some(&book), id);
        *shared.book.write().unwrap() = Some(book);
        shared.stats.synced.store(true, Ordering::Relaxed);
        was_synced = true;

        loop {
            let msg = match next_frame(&mut ws, cfg.idle_timeout).await {
                Step::Ended(reason) => return SessionEnd { reason, was_synced },
                Step::Frame(m) => m,
            };
            let text = match msg {
                Message::Text(t) => t,
                Message::Close(_) => return SessionEnd { reason: "closed by server".into(), was_synced },
                _ => continue, // pings are answered by the library on the next read, pongs ignored
            };
            let delta = match proto::parse(text.as_str()) {
                Ok(Inbound::Depth(d)) => d,
                Ok(_) => continue,
                Err(e) => return SessionEnd { reason: e.to_string(), was_synced },
            };
            let outcome = {
                let mut guard = shared.book.write().unwrap();
                let book = guard.as_mut().expect("book is set while live");
                match book.apply(&delta) {
                    Ok(()) => Ok((book.last_update_id(), book.best_bid(), book.best_ask())),
                    Err(e) => Err(e),
                }
            };
            match outcome {
                Ok((id, bid, ask)) => {
                    record(cfg, shared, &delta);
                    shared.stats.applied.fetch_add(1, Ordering::Relaxed);
                    failed_syncs = 0;
                    emitter.send_tops(shared, EventKind::Update, id, bid, ask);
                }
                Err(ApplyError::Stale { .. }) => {
                    shared.stats.stale.fetch_add(1, Ordering::Relaxed);
                }
                Err(e) => {
                    // A gap, an overlap, a malformed or a crossing delta: the book can no longer be trusted.
                    *shared.book.write().unwrap() = None;
                    shared.stats.synced.store(false, Ordering::Relaxed);
                    shared.stats.resyncs.fetch_add(1, Ordering::Relaxed);
                    emitter.send(shared, EventKind::Resyncing(e.to_string()), None, 0);
                    continue 'resync;
                }
            }
        }
    }
}

fn record(cfg: &ClientConfig, shared: &Shared, d: &Delta) {
    if cfg.record_applied {
        shared.applied_log.lock().unwrap().push((d.first_id, d.final_id));
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn backoff_doubles_then_stays_at_the_cap() {
        let base = Duration::from_millis(100);
        let max = Duration::from_millis(1000);
        let got: Vec<u128> = (0..6).map(|a| backoff_delay(a, base, max).as_millis()).collect();
        assert_eq!(got, vec![100, 200, 400, 800, 1000, 1000]);
    }

    #[test]
    fn backoff_survives_a_huge_attempt_count() {
        let d = backoff_delay(u32::MAX, Duration::from_millis(250), Duration::from_secs(30));
        assert_eq!(d, Duration::from_secs(30));
    }
}
