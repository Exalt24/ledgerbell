//! A bot worker. It connects to a C2 over a WebSocket, says Hello with the highest command id it has stored,
//! and then handles `PositionDelta` commands one at a time:
//!
//! 1. read the exchange's book (stand-in CLOB, `GET /book`),
//! 2. refuse if the circuit breaker is open or the book already moved against it ([`guard`]),
//! 3. size the order with `delta_position` under a cap from the slippage ladder,
//! 4. take the next nonce for its wallet, build and sign the order (EIP-712) and store it, in one SQLite
//!    transaction,
//! 5. `POST /order`, then poll `GET /data/order/{id}` until the matched size has been unchanged for the
//!    stabilization window,
//! 6. store the result and move `last_acked` in one transaction, and send the `Ack`.
//!
//! A command with an id above the next one is a gap: the worker asks for a replay after its last stored id and
//! ignores later ones until it arrives. A command at or below the stored id is a duplicate and is dropped. The
//! connection is retried with a growing wait, and after `failover_threshold` failures in a row on one address
//! the next address in the list is tried.

use std::path::PathBuf;
use std::sync::atomic::{AtomicU64, AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use futures_util::{SinkExt, StreamExt};
use tokio::time::sleep;
use tokio_tungstenite::tungstenite::Message;

use crate::book::{Fixed, Level, OrderBook};
use crate::clob::{amounts, http_request, Order};
use crate::eip712::{hex_encode, Wallet};
use crate::follow::{delta_position, FollowConfig, OrderSide};
use crate::guard::{
    momentum_check, BreakerConfig, CircuitBreaker, FillStabilizer, FillState, LadderInput, SlippageLadder,
};
use crate::seq::{classify, SeqVerdict};
use crate::store::{Store, StoreError};
use crate::wire::{self, Ack, Body, Heartbeat, Hello, Outcome, PositionDelta, ReplayRequest};

#[derive(Clone, Debug)]
pub struct WorkerConfig {
    pub worker_id: String,
    /// C2 WebSocket addresses, the first one preferred.
    pub c2_urls: Vec<String>,
    pub db_path: PathBuf,
    pub full_sync: bool,
    /// Base URL of the CLOB stand-in, `http://host:port`.
    pub clob_url: String,
    /// The wallet's secret. Tests and the demo use a throwaway one.
    pub wallet_secret: [u8; 32],
    pub backoff_base: Duration,
    pub backoff_cap: Duration,
    /// Failures in a row on one address before the next address is tried.
    pub failover_threshold: u32,
    pub connect_timeout: Duration,
    pub heartbeat: Duration,
    /// No frame from the C2 (a pong counts) for this long ends the connection.
    pub idle_timeout: Duration,
    pub follow: FollowConfig,
    pub ladder: SlippageLadder,
    pub momentum_max_bps: u32,
    pub stabilize_window: Duration,
    pub poll_interval: Duration,
    pub fill_timeout: Duration,
    pub breaker: BreakerConfig,
    /// Extra tries of `POST /order` after a network error or a 5xx answer; the same bytes are sent each time.
    pub post_retries: u32,
    pub http_timeout: Duration,
    /// Test hook: exit the process (code 86) right after the CLOB accepted the order for this command id and
    /// before the worker wrote that down, to prove the restart path (the same bytes are posted again).
    pub crash_after_send: Option<u64>,
}

impl WorkerConfig {
    pub fn new(
        worker_id: &str,
        c2_urls: Vec<String>,
        db_path: PathBuf,
        clob_url: &str,
        wallet_secret: [u8; 32],
    ) -> Self {
        WorkerConfig {
            worker_id: worker_id.to_string(),
            c2_urls,
            db_path,
            full_sync: false,
            clob_url: clob_url.to_string(),
            wallet_secret,
            backoff_base: Duration::from_millis(50),
            backoff_cap: Duration::from_secs(2),
            failover_threshold: 2,
            connect_timeout: Duration::from_secs(2),
            heartbeat: Duration::from_millis(250),
            idle_timeout: Duration::from_secs(3),
            follow: FollowConfig {
                ratio_num: 1,
                ratio_den: 10,
                max_slippage_bps: 50,
                min_qty: Fixed(1_000_000),
            },
            ladder: SlippageLadder {
                base_bps: 50,
                min_bps: 10,
                max_bps: 200,
                miss_step_bps: 25,
                fill_step_bps: 5,
                max_steps: 4,
                thin_step_bps: 40,
                deep_step_bps: 20,
            },
            momentum_max_bps: 100,
            stabilize_window: Duration::from_millis(30),
            poll_interval: Duration::from_millis(5),
            fill_timeout: Duration::from_secs(2),
            breaker: BreakerConfig {
                failure_threshold: 3,
                cooldown_ms: 500,
            },
            post_retries: 2,
            http_timeout: Duration::from_secs(2),
            crash_after_send: None,
        }
    }
}

// ---------------------------------------------------------------- reconnect and failover

/// Which C2 address to use next and how long to wait, as a pure state machine.
#[derive(Clone, Debug)]
pub struct Failover {
    n: usize,
    threshold: u32,
    base: Duration,
    cap: Duration,
    current: usize,
    on_current: u32,
}

impl Failover {
    pub fn new(addresses: usize, threshold: u32, base: Duration, cap: Duration) -> Failover {
        Failover {
            n: addresses.max(1),
            threshold: threshold.max(1),
            base,
            cap,
            current: 0,
            on_current: 0,
        }
    }

    /// The index of the address to try now.
    pub fn current(&self) -> usize {
        self.current
    }

    /// A connection was made: failures start counting again from zero.
    pub fn on_connected(&mut self) {
        self.on_current = 0;
    }

    /// A connection failed. Returns how long to wait and whether the next try uses another address. Waits are
    /// `base * 2^(failures in a row on this address - 1)` up to `cap`; a switch to another address waits `base`.
    pub fn on_failure(&mut self) -> (Duration, bool) {
        self.on_current += 1;
        if self.n > 1 && self.on_current >= self.threshold {
            self.current = (self.current + 1) % self.n;
            self.on_current = 0;
            return (self.base, true);
        }
        let shift = (self.on_current - 1).min(20);
        let wait = self.base.saturating_mul(1u32 << shift);
        (wait.min(self.cap), false)
    }
}

// ---------------------------------------------------------------- stats and handle

#[derive(Default)]
pub struct WorkerStats {
    pub connections: AtomicU64,
    pub connect_failures: AtomicU64,
    pub failovers: AtomicU64,
    pub current_address: AtomicUsize,
    pub applied: AtomicU64,
    pub duplicates_dropped: AtomicU64,
    pub gaps_detected: AtomicU64,
    pub replays_requested: AtomicU64,
    pub filled: AtomicU64,
    pub partial: AtomicU64,
    pub skipped: AtomicU64,
    pub rejected: AtomicU64,
}

pub struct WorkerHandle {
    pub stats: Arc<WorkerStats>,
    pub wallet: String,
    task: tokio::task::JoinHandle<()>,
}

impl WorkerHandle {
    /// Stops the worker task wherever it is. The database is left as a killed process would leave it.
    pub fn abort(&self) {
        self.task.abort();
    }
}

impl Drop for WorkerHandle {
    fn drop(&mut self) {
        self.task.abort();
    }
}

pub fn spawn(cfg: WorkerConfig) -> Result<WorkerHandle, StoreError> {
    let store = Store::open(&cfg.db_path, cfg.full_sync)?;
    let wallet = Wallet::from_secret(&cfg.wallet_secret).map_err(|e| StoreError::Build(e.to_string()))?;
    let wallet_hex = wallet.address_hex();
    let stats = Arc::new(WorkerStats::default());
    let worker = Worker {
        breaker: CircuitBreaker::new(cfg.breaker),
        cfg,
        store,
        wallet,
        wallet_hex: wallet_hex.clone(),
        misses: 0,
        fills: 0,
        started: Instant::now(),
        stats: stats.clone(),
    };
    let task = tokio::spawn(worker.run());
    Ok(WorkerHandle {
        stats,
        wallet: wallet_hex,
        task,
    })
}

// ---------------------------------------------------------------- the worker

struct Worker {
    cfg: WorkerConfig,
    store: Store,
    wallet: Wallet,
    wallet_hex: String,
    breaker: CircuitBreaker,
    misses: u32,
    fills: u32,
    started: Instant,
    stats: Arc<WorkerStats>,
}

fn unix_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map_or(0, |d| d.as_millis() as u64)
}

/// What one command came to, and whether the exchange answered (for the circuit breaker).
struct Done {
    outcome: Outcome,
    detail: String,
    exchange_ok: bool,
}

fn done(outcome: Outcome, detail: impl Into<String>, exchange_ok: bool) -> Done {
    Done {
        outcome,
        detail: detail.into(),
        exchange_ok,
    }
}

impl Worker {
    fn now_ms(&self) -> u64 {
        self.started.elapsed().as_millis() as u64
    }

    async fn run(mut self) {
        let mut fo = Failover::new(
            self.cfg.c2_urls.len(),
            self.cfg.failover_threshold,
            self.cfg.backoff_base,
            self.cfg.backoff_cap,
        );
        loop {
            let idx = fo.current();
            self.stats.current_address.store(idx, Ordering::Relaxed);
            let url = self.cfg.c2_urls[idx].clone();
            let connect =
                tokio::time::timeout(self.cfg.connect_timeout, tokio_tungstenite::connect_async(url.as_str())).await;
            match connect {
                Ok(Ok((ws, _))) => {
                    fo.on_connected();
                    self.stats.connections.fetch_add(1, Ordering::Relaxed);
                    let _ = self.session(ws).await;
                    sleep(self.cfg.backoff_base).await;
                }
                _ => {
                    self.stats.connect_failures.fetch_add(1, Ordering::Relaxed);
                    let (wait, switched) = fo.on_failure();
                    if switched {
                        self.stats.failovers.fetch_add(1, Ordering::Relaxed);
                    }
                    sleep(wait).await;
                }
            }
        }
    }

    async fn session(
        &mut self,
        ws: tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>,
    ) -> Result<(), String> {
        let (mut tx, mut rx) = ws.split();
        let mut out_seq: u64 = 1;
        let mut acked = self.store.last_acked().map_err(|e| e.to_string())?;
        let hello = wire::encode(
            out_seq,
            Body::Hello(Hello {
                worker_id: self.cfg.worker_id.clone(),
                last_acked: acked,
                wallet: self.wallet_hex.clone(),
            }),
        );
        tx.send(Message::Binary(hello.into()))
            .await
            .map_err(|e| e.to_string())?;
        let mut hb = tokio::time::interval(self.cfg.heartbeat);
        let mut last_rx = Instant::now();
        let mut replay_after: Option<u64> = None;

        loop {
            tokio::select! {
                _ = hb.tick() => {
                    if last_rx.elapsed() > self.cfg.idle_timeout {
                        return Err("the C2 went quiet".into());
                    }
                    out_seq += 1;
                    let beat = Heartbeat {
                        unix_ms: unix_ms(),
                        orders_sent: self.store.orders_sent().map_err(|e| e.to_string())?,
                        fills_seen: self.store.fills_seen().map_err(|e| e.to_string())?,
                        last_acked: acked,
                    };
                    tx.send(Message::Ping(Vec::new().into())).await.map_err(|e| e.to_string())?;
                    tx.send(Message::Binary(wire::encode(out_seq, Body::Heartbeat(beat)).into()))
                        .await
                        .map_err(|e| e.to_string())?;
                }
                msg = rx.next() => {
                    let msg = match msg {
                        None => return Err("closed".into()),
                        Some(Err(e)) => return Err(e.to_string()),
                        Some(Ok(m)) => m,
                    };
                    last_rx = Instant::now();
                    let Message::Binary(bytes) = msg else {
                        if matches!(msg, Message::Close(_)) {
                            return Err("closed by the C2".into());
                        }
                        continue;
                    };
                    let Ok(env) = wire::decode(&bytes) else { continue };
                    let Some(Body::Command(cmd)) = env.body else { continue };
                    match classify(acked, env.seq) {
                        SeqVerdict::Apply => {
                            replay_after = None;
                            let d = self.execute(env.seq, &cmd).await.map_err(|e| e.to_string())?;
                            self.store.finish(env.seq, d.outcome.label(), &d.detail).map_err(|e| e.to_string())?;
                            acked = env.seq;
                            self.stats.applied.fetch_add(1, Ordering::Relaxed);
                            let counter = match d.outcome {
                                Outcome::Filled => &self.stats.filled,
                                Outcome::Partial => &self.stats.partial,
                                Outcome::Skipped => &self.stats.skipped,
                                _ => &self.stats.rejected,
                            };
                            counter.fetch_add(1, Ordering::Relaxed);
                            out_seq += 1;
                            let ack = Ack { acked_seq: env.seq, outcome: d.outcome as i32, detail: d.detail };
                            tx.send(Message::Binary(wire::encode(out_seq, Body::Ack(ack)).into()))
                                .await
                                .map_err(|e| e.to_string())?;
                        }
                        SeqVerdict::Duplicate => {
                            self.stats.duplicates_dropped.fetch_add(1, Ordering::Relaxed);
                        }
                        SeqVerdict::Gap { .. } => {
                            self.stats.gaps_detected.fetch_add(1, Ordering::Relaxed);
                            // Ask once per hole; everything after it is ignored until the missing one arrives.
                            if replay_after != Some(acked) {
                                replay_after = Some(acked);
                                self.stats.replays_requested.fetch_add(1, Ordering::Relaxed);
                                out_seq += 1;
                                let r = ReplayRequest { after_seq: acked };
                                tx.send(Message::Binary(wire::encode(out_seq, Body::Replay(r)).into()))
                                    .await
                                    .map_err(|e| e.to_string())?;
                            }
                        }
                        SeqVerdict::Invalid => {}
                    }
                }
            }
        }
    }

    /// Runs one command to its result. The circuit breaker sees one success or one failure per command.
    async fn execute(&mut self, seq: u64, cmd: &PositionDelta) -> Result<Done, StoreError> {
        let d = match self.store.order_for(seq)? {
            // A restart after the order was stored: do not decide again, finish the same order.
            Some(o) => self.complete(seq, o).await?,
            None => {
                let now = self.now_ms();
                if !self.breaker.allow(now) {
                    return Ok(done(Outcome::Skipped, "circuit breaker open", true));
                }
                let d = self.decide_and_send(seq, cmd).await?;
                let now = self.now_ms();
                if d.exchange_ok {
                    self.breaker.record_success(now);
                } else {
                    self.breaker.record_failure(now);
                }
                d
            }
        };
        match d.outcome {
            Outcome::Filled => {
                self.misses = 0;
                self.fills = self.fills.saturating_add(1);
            }
            Outcome::Partial | Outcome::Rejected => {
                self.fills = 0;
                self.misses = self.misses.saturating_add(1);
            }
            _ => {}
        }
        Ok(d)
    }

    async fn decide_and_send(&mut self, seq: u64, cmd: &PositionDelta) -> Result<Done, StoreError> {
        let Ok(leader_price) = Fixed::parse(&cmd.leader_price) else {
            return Ok(done(Outcome::Rejected, "the leader price is not a decimal", true));
        };
        let book = match Self::fetch_book(&self.cfg, &cmd.token_id).await {
            Ok(b) => b,
            Err(e) => return Ok(done(Outcome::Rejected, format!("book: {e}"), false)),
        };
        let side = match cmd.leader_after.cmp(&cmd.leader_before) {
            std::cmp::Ordering::Greater => OrderSide::Buy,
            std::cmp::Ordering::Less => OrderSide::Sell,
            std::cmp::Ordering::Equal => {
                return Ok(done(Outcome::Skipped, "the leader's position did not change", true))
            }
        };
        let (bids, asks) = book.depth(usize::MAX);
        let take_side = if side == OrderSide::Buy { &asks } else { &bids };
        let Some(best) = take_side.first().copied() else {
            return Ok(done(Outcome::Skipped, "the side to take is empty", true));
        };
        if let Err(r) = momentum_check(side, leader_price, best.price, self.cfg.momentum_max_bps) {
            return Ok(done(
                Outcome::Skipped,
                format!(
                    "momentum: price moved {} bps against us, limit {}",
                    r.moved_bps, r.max_bps
                ),
                true,
            ));
        }
        let change = (cmd.leader_after as i128 - cmd.leader_before as i128).unsigned_abs();
        let wanted = Fixed(
            u64::try_from(change * self.cfg.follow.ratio_num as u128 / self.cfg.follow.ratio_den.max(1) as u128)
                .unwrap_or(u64::MAX),
        );
        let depth = depth_within(take_side, side, best.price, self.cfg.ladder.max_bps);
        let bps = self.cfg.ladder.allowed_bps(&LadderInput {
            depth,
            wanted,
            recent_misses: self.misses,
            recent_fills: self.fills,
        });
        let follow = FollowConfig {
            max_slippage_bps: bps,
            ..self.cfg.follow
        };
        let fo = match delta_position(cmd.leader_before, cmd.leader_after, &book, &follow) {
            Ok(o) => o,
            Err(e) => return Ok(done(Outcome::Skipped, e.to_string(), true)),
        };
        let (maker_amount, taker_amount) = match amounts(side, fo.qty, fo.limit_price) {
            Ok(a) => a,
            Err(e) => return Ok(done(Outcome::Skipped, e.to_string(), true)),
        };
        let template = Order {
            salt: 0,
            maker: self.wallet.address(),
            signer: self.wallet.address(),
            token_id: cmd.token_id.clone(),
            maker_amount,
            taker_amount,
            side,
            signature_type: 0,
            timestamp_ms: unix_ms(),
            metadata: [0; 32],
            builder: [0; 32],
            expiration: 0,
        };
        // Check the order can be hashed (a token id that is not a decimal cannot) before a nonce is spent.
        if let Err(e) = template.digest() {
            return Ok(done(Outcome::Rejected, e.to_string(), true));
        }
        let wallet = &self.wallet;
        let wallet_hex = self.wallet_hex.clone();
        let (stored, _) = self.store.begin_order(seq, &wallet_hex, |nonce| {
            // The nonce is the salt: unique per wallet, so no two orders from this wallet share one.
            let order = Order {
                salt: nonce,
                ..template
            };
            let sig = order.sign(wallet).map_err(|e| e.to_string())?;
            Ok((
                order.to_wire(&sig).to_string(),
                if side == OrderSide::Buy { "BUY" } else { "SELL" }.to_string(),
                order.shares().to_string(),
                order.implied_price().to_string(),
            ))
        })?;
        self.complete(seq, stored).await
    }

    /// From a stored order to a result: post it if it was not accepted yet, then verify the fill.
    async fn complete(&mut self, seq: u64, mut order: crate::store::StoredOrder) -> Result<Done, StoreError> {
        if order.state == "failed" {
            return Ok(done(Outcome::Rejected, "an earlier attempt at this order failed", true));
        }
        if order.state == "pending" {
            let mut last_error = String::new();
            let mut accepted: Option<String> = None;
            for attempt in 0..=self.cfg.post_retries {
                if attempt > 0 {
                    sleep(Duration::from_millis(10)).await;
                }
                match http_request(
                    &self.cfg.clob_url,
                    "POST",
                    "/order",
                    Some(&order.body),
                    self.cfg.http_timeout,
                )
                .await
                {
                    Ok((200, body)) => match serde_json::from_str::<serde_json::Value>(&body) {
                        Ok(v) if v["success"] == true => {
                            accepted = v["orderID"].as_str().map(str::to_string);
                            break;
                        }
                        _ => {
                            last_error = format!("unreadable answer: {body}");
                            break;
                        }
                    },
                    Ok((status, body)) if (400..500).contains(&status) => {
                        // The exchange looked at it and said no: a final answer, not a broken exchange.
                        self.store.mark_failed(seq)?;
                        let msg = serde_json::from_str::<serde_json::Value>(&body)
                            .ok()
                            .and_then(|v| v["errorMsg"].as_str().map(str::to_string))
                            .unwrap_or(body);
                        return Ok(done(Outcome::Rejected, format!("rejected: {msg}"), true));
                    }
                    Ok((status, _)) => last_error = format!("HTTP {status}"),
                    Err(e) => last_error = e,
                }
            }
            let Some(id) = accepted else {
                self.store.mark_failed(seq)?;
                return Ok(done(Outcome::Rejected, format!("post failed: {last_error}"), false));
            };
            // The worst place to die: the exchange has the order and this worker has not written that down.
            if self.cfg.crash_after_send == Some(seq) {
                std::process::exit(86);
            }
            self.store.mark_submitted(seq, &id)?;
            order.clob_id = Some(id);
        }
        let Some(id) = order.clob_id.clone() else {
            return Ok(done(Outcome::Rejected, "stored order has no exchange id", true));
        };
        let want = Fixed::parse(&order.qty).unwrap_or(Fixed::ZERO);
        let (matched, counted, reachable) = Self::verify_fill(&self.cfg, &id).await;
        self.store.record_fill(seq, &matched.to_string(), counted)?;
        let d = if counted && matched >= want && !want.is_zero() {
            done(Outcome::Filled, format!("filled {matched} of {want}"), true)
        } else if counted {
            done(Outcome::Partial, format!("filled {matched} of {want}"), true)
        } else {
            done(
                Outcome::Partial,
                format!("fill not stable after the timeout (last read {matched} of {want})"),
                reachable,
            )
        };
        Ok(d)
    }

    /// Polls the order until the matched size stays unchanged for the window. Returns the size, whether it
    /// counts (it was stable), and whether the exchange answered at least once.
    async fn verify_fill(cfg: &WorkerConfig, id: &str) -> (Fixed, bool, bool) {
        let mut stab = FillStabilizer::new(cfg.stabilize_window.as_millis() as u64);
        let start = Instant::now();
        let mut last = Fixed::ZERO;
        let mut reachable = false;
        loop {
            let path = format!("/data/order/{id}");
            if let Ok((200, body)) = http_request(&cfg.clob_url, "GET", &path, None, cfg.http_timeout).await {
                if let Some(m) = serde_json::from_str::<serde_json::Value>(&body)
                    .ok()
                    .and_then(|v| v["size_matched"].as_str().and_then(|s| Fixed::parse(s).ok()))
                {
                    reachable = true;
                    last = m;
                    if let FillState::Stable(q) = stab.observe(start.elapsed().as_millis() as u64, m) {
                        return (q, true, true);
                    }
                }
            }
            if start.elapsed() >= cfg.fill_timeout {
                return (last, false, reachable);
            }
            sleep(cfg.poll_interval).await;
        }
    }

    async fn fetch_book(cfg: &WorkerConfig, token_id: &str) -> Result<OrderBook, String> {
        let path = format!("/book?token_id={token_id}");
        let (status, body) = http_request(&cfg.clob_url, "GET", &path, None, cfg.http_timeout).await?;
        if status != 200 {
            return Err(format!("HTTP {status}"));
        }
        let v: serde_json::Value = serde_json::from_str(&body).map_err(|e| e.to_string())?;
        let levels = |k: &str| -> Result<Vec<Level>, String> {
            v[k].as_array()
                .ok_or_else(|| format!("{k} is not a list"))?
                .iter()
                .map(|l| {
                    let p = l["price"].as_str().ok_or("price is not a string")?;
                    let s = l["size"].as_str().ok_or("size is not a string")?;
                    Ok(Level {
                        price: Fixed::parse(p).map_err(|e| e.to_string())?,
                        qty: Fixed::parse(s).map_err(|e| e.to_string())?,
                    })
                })
                .collect()
        };
        let bids: Vec<Level> = levels("bids")?.into_iter().filter(|l| !l.qty.is_zero()).collect();
        let asks: Vec<Level> = levels("asks")?.into_iter().filter(|l| !l.qty.is_zero()).collect();
        OrderBook::from_snapshot(0, &bids, &asks).map_err(|e| e.to_string())
    }
}

/// The size resting on the taking side at prices no worse than `max_bps` from the best price.
pub fn depth_within(levels: &[Level], side: OrderSide, best: Fixed, max_bps: u32) -> Fixed {
    let b = best.0 as u128;
    let reach = b * max_bps as u128 / 10_000;
    let total: u128 = levels
        .iter()
        .filter(|l| match side {
            OrderSide::Buy => (l.price.0 as u128) <= b + reach,
            OrderSide::Sell => (l.price.0 as u128) + reach >= b,
        })
        .map(|l| l.qty.0 as u128)
        .sum();
    Fixed(total.min(u64::MAX as u128) as u64)
}

pub fn wallet_hex_of(secret: &[u8; 32]) -> Option<String> {
    Wallet::from_secret(secret)
        .ok()
        .map(|w| format!("0x{}", hex_encode(&w.address())))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn f(s: &str) -> Fixed {
        Fixed::parse(s).unwrap()
    }

    fn lv(p: &str, q: &str) -> Level {
        Level { price: f(p), qty: f(q) }
    }

    fn ms(n: u64) -> Duration {
        Duration::from_millis(n)
    }

    #[test]
    fn failover_waits_grow_and_the_address_switches_at_the_threshold() {
        let mut fo = Failover::new(2, 3, ms(100), ms(1000));
        assert_eq!(fo.current(), 0);
        assert_eq!(fo.on_failure(), (ms(100), false));
        assert_eq!(fo.on_failure(), (ms(200), false));
        assert_eq!(fo.current(), 0);
        assert_eq!(
            fo.on_failure(),
            (ms(100), true),
            "the third failure switches and waits the base"
        );
        assert_eq!(fo.current(), 1);
        assert_eq!(
            fo.on_failure(),
            (ms(100), false),
            "the count starts again on the new address"
        );
    }

    #[test]
    fn failover_wraps_around_the_list() {
        let mut fo = Failover::new(2, 1, ms(10), ms(100));
        fo.on_failure();
        assert_eq!(fo.current(), 1);
        fo.on_failure();
        assert_eq!(fo.current(), 0);
    }

    #[test]
    fn a_single_address_never_switches_and_the_wait_is_capped() {
        let mut fo = Failover::new(1, 2, ms(100), ms(450));
        let waits: Vec<_> = (0..6).map(|_| fo.on_failure()).collect();
        assert_eq!(waits[0], (ms(100), false));
        assert_eq!(waits[1], (ms(200), false));
        assert_eq!(waits[2], (ms(400), false));
        assert_eq!(waits[3], (ms(450), false));
        assert_eq!(waits[5], (ms(450), false));
        assert_eq!(fo.current(), 0);
    }

    #[test]
    fn a_connection_resets_the_failure_count() {
        let mut fo = Failover::new(2, 2, ms(100), ms(1000));
        fo.on_failure();
        fo.on_connected();
        assert_eq!(
            fo.on_failure(),
            (ms(100), false),
            "one failure after a connection is the first again"
        );
        assert_eq!(fo.current(), 0);
    }

    #[test]
    fn depth_counts_only_levels_within_the_reach() {
        let asks = [lv("0.50", "10"), lv("0.504", "5"), lv("0.51", "100")];
        // 100 bps of 0.50 is 0.005: the third level at 0.51 is outside.
        assert_eq!(depth_within(&asks, OrderSide::Buy, f("0.50"), 100), f("15"));
        assert_eq!(depth_within(&asks, OrderSide::Buy, f("0.50"), 0), f("10"));
        let bids = [lv("0.50", "10"), lv("0.496", "5"), lv("0.49", "100")];
        assert_eq!(depth_within(&bids, OrderSide::Sell, f("0.50"), 100), f("15"));
        assert_eq!(depth_within(&bids, OrderSide::Sell, f("0.50"), 0), f("10"));
    }

    #[test]
    fn a_wallet_address_comes_from_a_valid_secret_only() {
        assert!(wallet_hex_of(&[0u8; 32]).is_none());
        let a = wallet_hex_of(&crate::eip712::keccak256(b"cow")).unwrap();
        assert_eq!(a, "0xcd2a3d9f938e13cd947ec05abc7fe734df8dd826");
    }
}
