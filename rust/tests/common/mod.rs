//! Helpers shared by the fleet integration tests.
#![allow(dead_code)]

use std::path::{Path, PathBuf};
use std::time::{Duration, Instant};

use bookwatch::clob::{ClobConfig, ClobStandIn};
use bookwatch::eip712::{hex_encode, Wallet};
use bookwatch::store::Store;
use bookwatch::worker::WorkerConfig;

/// A database path in the temp directory that is removed (with its WAL files) when dropped.
pub struct TempDb(pub PathBuf);

impl TempDb {
    pub fn new(tag: &str) -> TempDb {
        let mut b = [0u8; 6];
        getrandom::fill(&mut b).unwrap();
        TempDb(std::env::temp_dir().join(format!("bw-{tag}-{}-{}.db", std::process::id(), hex_encode(&b))))
    }

    pub fn path(&self) -> &Path {
        &self.0
    }

    pub fn open(&self) -> Store {
        Store::open(&self.0, false).unwrap()
    }
}

impl Drop for TempDb {
    fn drop(&mut self) {
        for ext in ["", "-wal", "-shm"] {
            let mut s = self.0.as_os_str().to_owned();
            s.push(ext);
            let _ = std::fs::remove_file(s);
        }
    }
}

/// The book the generated commands trade against: a buy is taken at 0.50, a sell at 0.499, and the leader
/// traded at 0.50, so neither side has moved against the follower by more than a few basis points.
pub fn seed_book(clob: &ClobStandIn) {
    clob.set_book(
        "1001",
        &[("0.499", "1000"), ("0.498", "1000"), ("0.497", "1000")],
        &[("0.50", "1000"), ("0.501", "1000"), ("0.502", "1000")],
    );
}

pub async fn clob() -> ClobStandIn {
    let c = ClobStandIn::start(ClobConfig::default()).await.unwrap();
    seed_book(&c);
    c
}

/// A worker configuration with short waits, for tests.
pub fn fast_cfg(id: &str, urls: Vec<String>, db: &Path, clob_url: &str) -> WorkerConfig {
    let (_, secret) = Wallet::throwaway();
    let mut c = WorkerConfig::new(id, urls, db.to_path_buf(), clob_url, secret);
    c.backoff_base = Duration::from_millis(20);
    c.backoff_cap = Duration::from_millis(200);
    c.connect_timeout = Duration::from_millis(500);
    c.heartbeat = Duration::from_millis(50);
    c.idle_timeout = Duration::from_secs(2);
    c.stabilize_window = Duration::from_millis(10);
    c.poll_interval = Duration::from_millis(2);
    c.fill_timeout = Duration::from_secs(1);
    c
}

/// Polls `cond` every 5 ms until it is true; panics with `what` after `limit`.
pub async fn wait_until(what: &str, limit: Duration, mut cond: impl FnMut() -> bool) {
    let start = Instant::now();
    while !cond() {
        assert!(start.elapsed() < limit, "timed out waiting for: {what}");
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
}

pub fn last_acked(db: &Path) -> u64 {
    Store::open(db, false).and_then(|s| s.last_acked()).unwrap_or(0)
}

/// Every command was applied once: `n` results, each order stored once, nonces 1..=orders with no repeat or
/// hole, and the CLOB saw each distinct order, with `allowed_resends` posts beyond the first in total.
pub fn assert_applied_exactly_once(db: &Path, wallet: &str, clob: &ClobStandIn, n: u64, allowed_resends: u32) {
    let s = Store::open(db, false).unwrap();
    assert_eq!(s.last_acked().unwrap(), n);
    assert_eq!(s.result_count().unwrap(), n, "one result per command");
    let nonces = s.nonces_used(wallet).unwrap();
    let want: Vec<u64> = (1..=nonces.len() as u64).collect();
    assert_eq!(nonces, want, "nonces rise by one with no repeat and no hole");
    let received = clob.received();
    assert_eq!(received.len(), nonces.len(), "the CLOB saw each stored order once");
    let mut salts: Vec<u64> = received.iter().map(|r| r.salt).collect();
    salts.sort_unstable();
    salts.dedup();
    assert_eq!(salts.len(), received.len(), "no salt twice");
    let extra: u32 = received.iter().map(|r| r.posts - 1).sum();
    assert!(
        extra <= allowed_resends,
        "{extra} resent posts, at most {allowed_resends} expected"
    );
}
