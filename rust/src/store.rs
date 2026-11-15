//! The worker's own state in SQLite, in WAL mode: the highest command id whose result is stored, the orders
//! it sent, the fills it counted and a monotonic nonce per wallet.
//!
//! Every change that must not be half done is one `BEGIN IMMEDIATE` transaction: taking a nonce and writing
//! the order that uses it, and storing a command's result together with moving `last_acked`. A process that
//! is killed between two steps leaves the database at one of the two whole states.

use std::path::Path;
use std::time::Duration;

use rusqlite::{params, Connection, OptionalExtension, TransactionBehavior};

#[derive(Debug)]
pub enum StoreError {
    Sql(rusqlite::Error),
    /// `finish` was called for a command that is not the next one.
    OutOfOrder {
        last_acked: u64,
        seq: u64,
    },
    /// The order could not be built (nothing was stored).
    Build(String),
}

impl std::fmt::Display for StoreError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            StoreError::Sql(e) => write!(f, "sqlite: {e}"),
            StoreError::OutOfOrder { last_acked, seq } => {
                write!(f, "command {seq} finished while the last stored result is {last_acked}")
            }
            StoreError::Build(e) => write!(f, "order not built: {e}"),
        }
    }
}

impl std::error::Error for StoreError {}

impl From<rusqlite::Error> for StoreError {
    fn from(e: rusqlite::Error) -> Self {
        StoreError::Sql(e)
    }
}

type Result<T> = std::result::Result<T, StoreError>;

/// An order as stored: everything needed to send it again byte for byte after a restart.
#[derive(Clone, Debug, PartialEq, Eq)]
pub struct StoredOrder {
    pub seq: u64,
    pub wallet: String,
    pub nonce: u64,
    /// The JSON body that is POSTed.
    pub body: String,
    pub side: String,
    pub qty: String,
    pub price: String,
    pub state: String,
    pub clob_id: Option<String>,
}

pub struct Store {
    conn: Connection,
}

const SCHEMA: &str = "
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS nonces (wallet TEXT PRIMARY KEY, next INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS orders (
    seq INTEGER PRIMARY KEY,
    wallet TEXT NOT NULL,
    nonce INTEGER NOT NULL,
    body TEXT NOT NULL,
    side TEXT NOT NULL,
    qty TEXT NOT NULL,
    price TEXT NOT NULL,
    state TEXT NOT NULL,
    clob_id TEXT,
    UNIQUE (wallet, nonce)
);
CREATE TABLE IF NOT EXISTS fills (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    seq INTEGER NOT NULL,
    matched TEXT NOT NULL,
    counted INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS results (
    seq INTEGER PRIMARY KEY,
    outcome TEXT NOT NULL,
    detail TEXT NOT NULL
);
";

impl Store {
    /// Opens (creating if needed) the database at `path` in WAL mode. `full_sync` asks SQLite to flush to disk
    /// on every commit; without it WAL uses `synchronous=NORMAL`, which survives a killed process but can lose
    /// the newest commits if the machine loses power.
    pub fn open(path: &Path, full_sync: bool) -> Result<Store> {
        let conn = Connection::open(path)?;
        conn.busy_timeout(Duration::from_secs(10))?;
        let mode: String = conn.query_row("PRAGMA journal_mode=WAL", [], |r| r.get(0))?;
        debug_assert!(mode.eq_ignore_ascii_case("wal") || mode.eq_ignore_ascii_case("memory"));
        conn.pragma_update(None, "synchronous", if full_sync { "FULL" } else { "NORMAL" })?;
        conn.execute_batch(SCHEMA)?;
        Ok(Store { conn })
    }

    /// Reads the journal mode back from the database.
    pub fn journal_mode(&self) -> Result<String> {
        Ok(self.conn.query_row("PRAGMA journal_mode", [], |r| r.get(0))?)
    }

    /// The highest command id whose result is stored. 0 before the first.
    pub fn last_acked(&self) -> Result<u64> {
        let v: Option<i64> = self
            .conn
            .query_row("SELECT v FROM meta WHERE k = 'last_acked'", [], |r| r.get(0))
            .optional()?;
        Ok(v.unwrap_or(0) as u64)
    }

    /// The next nonce for `wallet`: 1, 2, 3 ... and never the same twice, even from two connections.
    pub fn next_nonce(&mut self, wallet: &str) -> Result<u64> {
        let tx = self.conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let n = take_nonce(&tx, wallet)?;
        tx.commit()?;
        Ok(n)
    }

    pub fn order_for(&self, seq: u64) -> Result<Option<StoredOrder>> {
        Ok(self
            .conn
            .query_row(
                "SELECT seq, wallet, nonce, body, side, qty, price, state, clob_id FROM orders WHERE seq = ?1",
                [seq as i64],
                |r| {
                    Ok(StoredOrder {
                        seq: r.get::<_, i64>(0)? as u64,
                        wallet: r.get(1)?,
                        nonce: r.get::<_, i64>(2)? as u64,
                        body: r.get(3)?,
                        side: r.get(4)?,
                        qty: r.get(5)?,
                        price: r.get(6)?,
                        state: r.get(7)?,
                        clob_id: r.get(8)?,
                    })
                },
            )
            .optional()?)
    }

    /// Takes the next nonce for `wallet`, lets `build` make the order for it, and stores the order as
    /// `pending`, all in one transaction. If the order for `seq` already exists (a restart after a crash) it is
    /// returned untouched and `build` is not called, so the same nonce and the same bytes are sent again.
    pub fn begin_order<F>(&mut self, seq: u64, wallet: &str, build: F) -> Result<(StoredOrder, bool)>
    where
        F: FnOnce(u64) -> std::result::Result<(String, String, String, String), String>,
    {
        let tx = self.conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let existing = tx
            .query_row(
                "SELECT seq, wallet, nonce, body, side, qty, price, state, clob_id FROM orders WHERE seq = ?1",
                [seq as i64],
                |r| {
                    Ok(StoredOrder {
                        seq: r.get::<_, i64>(0)? as u64,
                        wallet: r.get(1)?,
                        nonce: r.get::<_, i64>(2)? as u64,
                        body: r.get(3)?,
                        side: r.get(4)?,
                        qty: r.get(5)?,
                        price: r.get(6)?,
                        state: r.get(7)?,
                        clob_id: r.get(8)?,
                    })
                },
            )
            .optional()?;
        if let Some(o) = existing {
            return Ok((o, true));
        }
        let nonce = take_nonce(&tx, wallet)?;
        let (body, side, qty, price) = build(nonce).map_err(StoreError::Build)?;
        tx.execute(
            "INSERT INTO orders (seq, wallet, nonce, body, side, qty, price, state) VALUES (?1, ?2, ?3, ?4, ?5, ?6, ?7, 'pending')",
            params![seq as i64, wallet, nonce as i64, body, side, qty, price],
        )?;
        tx.commit()?;
        Ok((
            StoredOrder {
                seq,
                wallet: wallet.to_string(),
                nonce,
                body,
                side,
                qty,
                price,
                state: "pending".into(),
                clob_id: None,
            },
            false,
        ))
    }

    pub fn mark_submitted(&mut self, seq: u64, clob_id: &str) -> Result<()> {
        self.conn.execute(
            "UPDATE orders SET state = 'submitted', clob_id = ?2 WHERE seq = ?1",
            params![seq as i64, clob_id],
        )?;
        Ok(())
    }

    pub fn mark_failed(&mut self, seq: u64) -> Result<()> {
        self.conn
            .execute("UPDATE orders SET state = 'failed' WHERE seq = ?1", params![seq as i64])?;
        Ok(())
    }

    pub fn record_fill(&mut self, seq: u64, matched: &str, counted: bool) -> Result<()> {
        self.conn.execute(
            "INSERT INTO fills (seq, matched, counted) VALUES (?1, ?2, ?3)",
            params![seq as i64, matched, counted as i64],
        )?;
        Ok(())
    }

    /// Stores command `seq`'s result and moves `last_acked` to it, in one transaction. `seq` must be the next
    /// command: `last_acked + 1`.
    pub fn finish(&mut self, seq: u64, outcome: &str, detail: &str) -> Result<()> {
        let tx = self.conn.transaction_with_behavior(TransactionBehavior::Immediate)?;
        let last: Option<i64> = tx
            .query_row("SELECT v FROM meta WHERE k = 'last_acked'", [], |r| r.get(0))
            .optional()?;
        let last = last.unwrap_or(0) as u64;
        if seq != last + 1 {
            return Err(StoreError::OutOfOrder { last_acked: last, seq });
        }
        tx.execute(
            "INSERT INTO results (seq, outcome, detail) VALUES (?1, ?2, ?3)",
            params![seq as i64, outcome, detail],
        )?;
        tx.execute(
            "UPDATE orders SET state = CASE WHEN state = 'failed' THEN 'failed' ELSE 'done' END WHERE seq = ?1",
            params![seq as i64],
        )?;
        tx.execute(
            "INSERT INTO meta (k, v) VALUES ('last_acked', ?1) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
            [seq as i64],
        )?;
        tx.commit()?;
        Ok(())
    }

    /// The stored outcome and detail of command `seq`.
    pub fn result_for(&self, seq: u64) -> Result<Option<(String, String)>> {
        Ok(self
            .conn
            .query_row(
                "SELECT outcome, detail FROM results WHERE seq = ?1",
                [seq as i64],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .optional()?)
    }

    /// Orders that went to the exchange (everything past `pending`).
    pub fn orders_sent(&self) -> Result<u64> {
        let n: i64 = self.conn.query_row(
            "SELECT COUNT(*) FROM orders WHERE state IN ('submitted', 'done')",
            [],
            |r| r.get(0),
        )?;
        Ok(n as u64)
    }

    pub fn fills_seen(&self) -> Result<u64> {
        let n: i64 = self
            .conn
            .query_row("SELECT COUNT(*) FROM fills WHERE counted = 1", [], |r| r.get(0))?;
        Ok(n as u64)
    }

    pub fn order_count(&self) -> Result<u64> {
        let n: i64 = self.conn.query_row("SELECT COUNT(*) FROM orders", [], |r| r.get(0))?;
        Ok(n as u64)
    }

    pub fn result_count(&self) -> Result<u64> {
        let n: i64 = self.conn.query_row("SELECT COUNT(*) FROM results", [], |r| r.get(0))?;
        Ok(n as u64)
    }

    /// Every stored nonce for `wallet`, in order of the command that used it.
    pub fn nonces_used(&self, wallet: &str) -> Result<Vec<u64>> {
        let mut st = self
            .conn
            .prepare("SELECT nonce FROM orders WHERE wallet = ?1 ORDER BY seq")?;
        let rows = st.query_map([wallet], |r| r.get::<_, i64>(0))?;
        Ok(rows
            .collect::<std::result::Result<Vec<_>, _>>()?
            .into_iter()
            .map(|n| n as u64)
            .collect())
    }
}

fn take_nonce(tx: &rusqlite::Transaction<'_>, wallet: &str) -> Result<u64> {
    let n: i64 = tx.query_row(
        "INSERT INTO nonces (wallet, next) VALUES (?1, 2) ON CONFLICT(wallet) DO UPDATE SET next = next + 1 RETURNING next - 1",
        [wallet],
        |r| r.get(0),
    )?;
    Ok(n as u64)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeSet;

    fn tmp(name: &str) -> std::path::PathBuf {
        let mut p = std::env::temp_dir();
        p.push(format!(
            "bookwatch-store-{}-{}-{}.db",
            name,
            std::process::id(),
            rand_tag()
        ));
        p
    }

    fn rand_tag() -> u64 {
        let mut b = [0u8; 8];
        getrandom::fill(&mut b).unwrap();
        u64::from_le_bytes(b)
    }

    fn cleanup(p: &Path) {
        for ext in ["", "-wal", "-shm"] {
            let mut s = p.as_os_str().to_owned();
            s.push(ext);
            let _ = std::fs::remove_file(s);
        }
    }

    fn build(nonce: u64) -> std::result::Result<(String, String, String, String), String> {
        Ok((format!("{{\"nonce\":{nonce}}}"), "BUY".into(), "1".into(), "0.5".into()))
    }

    #[test]
    fn a_builder_that_fails_stores_nothing_and_spends_no_nonce() {
        let p = tmp("buildfail");
        let mut s = Store::open(&p, false).unwrap();
        let r = s.begin_order(1, "w", |_| Err("no".into()));
        assert!(matches!(r, Err(StoreError::Build(_))));
        assert!(s.order_for(1).unwrap().is_none());
        assert_eq!(s.next_nonce("w").unwrap(), 1);
        drop(s);
        cleanup(&p);
    }

    #[test]
    fn the_database_is_in_wal_mode_read_back_from_sqlite() {
        let p = tmp("wal");
        let s = Store::open(&p, false).unwrap();
        assert_eq!(s.journal_mode().unwrap(), "wal");
        // A second connection reads the same mode from the file, not from a default.
        let s2 = Store::open(&p, false).unwrap();
        assert_eq!(s2.journal_mode().unwrap(), "wal");
        drop((s, s2));
        cleanup(&p);
    }

    #[test]
    fn last_acked_starts_at_zero_and_only_the_next_command_can_finish() {
        let p = tmp("ack");
        let mut s = Store::open(&p, false).unwrap();
        assert_eq!(s.last_acked().unwrap(), 0);
        assert!(matches!(
            s.finish(2, "filled", ""),
            Err(StoreError::OutOfOrder { last_acked: 0, seq: 2 })
        ));
        s.finish(1, "skipped", "x").unwrap();
        assert_eq!(s.last_acked().unwrap(), 1);
        assert!(matches!(
            s.finish(1, "filled", ""),
            Err(StoreError::OutOfOrder { last_acked: 1, seq: 1 })
        ));
        s.finish(2, "filled", "").unwrap();
        assert_eq!(s.last_acked().unwrap(), 2);
        assert_eq!(s.result_count().unwrap(), 2);
        drop(s);
        cleanup(&p);
    }

    #[test]
    fn state_survives_reopening_the_file() {
        let p = tmp("reopen");
        {
            let mut s = Store::open(&p, false).unwrap();
            s.begin_order(1, "0xw", build).unwrap();
            s.mark_submitted(1, "id1").unwrap();
            s.record_fill(1, "1", true).unwrap();
            s.finish(1, "filled", "").unwrap();
        }
        let s = Store::open(&p, false).unwrap();
        assert_eq!(s.last_acked().unwrap(), 1);
        assert_eq!(s.orders_sent().unwrap(), 1);
        assert_eq!(s.fills_seen().unwrap(), 1);
        let o = s.order_for(1).unwrap().unwrap();
        assert_eq!((o.state.as_str(), o.clob_id.as_deref()), ("done", Some("id1")));
        drop(s);
        cleanup(&p);
    }

    #[test]
    fn nonces_are_monotonic_per_wallet() {
        let p = tmp("nonce");
        let mut s = Store::open(&p, false).unwrap();
        assert_eq!(s.next_nonce("a").unwrap(), 1);
        assert_eq!(s.next_nonce("a").unwrap(), 2);
        assert_eq!(s.next_nonce("b").unwrap(), 1);
        assert_eq!(s.next_nonce("a").unwrap(), 3);
        drop(s);
        cleanup(&p);
    }

    #[test]
    fn beginning_an_order_twice_returns_the_stored_one_and_spends_no_nonce() {
        let p = tmp("resume");
        let mut s = Store::open(&p, false).unwrap();
        let (o1, resumed1) = s.begin_order(5, "0xw", build).unwrap();
        assert!(!resumed1);
        assert_eq!(o1.nonce, 1);
        let (o2, resumed2) = s
            .begin_order(5, "0xw", |_| panic!("the builder must not run on a resume"))
            .unwrap();
        assert!(resumed2);
        assert_eq!(o2, o1);
        assert_eq!(s.next_nonce("0xw").unwrap(), 2, "the resume did not use up a nonce");
        drop(s);
        cleanup(&p);
    }

    #[test]
    fn a_failed_order_stays_failed_after_finish() {
        let p = tmp("failed");
        let mut s = Store::open(&p, false).unwrap();
        s.begin_order(1, "w", build).unwrap();
        s.mark_failed(1).unwrap();
        s.finish(1, "rejected", "no").unwrap();
        assert_eq!(s.order_for(1).unwrap().unwrap().state, "failed");
        assert_eq!(s.orders_sent().unwrap(), 0);
        drop(s);
        cleanup(&p);
    }

    #[test]
    fn two_connections_on_two_threads_never_get_the_same_nonce() {
        let p = tmp("race");
        Store::open(&p, false).unwrap();
        let per_thread = 300;
        let handles: Vec<_> = (0..2)
            .map(|_| {
                let p = p.clone();
                std::thread::spawn(move || {
                    let mut s = Store::open(&p, false).unwrap();
                    (0..per_thread)
                        .map(|_| s.next_nonce("0xwallet").unwrap())
                        .collect::<Vec<u64>>()
                })
            })
            .collect();
        let mut all: Vec<u64> = Vec::new();
        for h in handles {
            let got = h.join().unwrap();
            assert!(got.windows(2).all(|w| w[0] < w[1]), "one sender saw its nonces rise");
            all.extend(got);
        }
        let set: BTreeSet<u64> = all.iter().copied().collect();
        assert_eq!(all.len(), set.len(), "no nonce was handed out twice");
        assert_eq!(set.len(), 2 * per_thread);
        assert_eq!(*set.first().unwrap(), 1);
        assert_eq!(*set.last().unwrap(), 2 * per_thread as u64, "and none was skipped");
        cleanup(&p);
    }

    #[test]
    fn two_connections_beginning_different_orders_never_share_a_nonce() {
        let p = tmp("race-orders");
        Store::open(&p, false).unwrap();
        let handles: Vec<_> = (0..2u64)
            .map(|t| {
                let p = p.clone();
                std::thread::spawn(move || {
                    let mut s = Store::open(&p, false).unwrap();
                    for i in 0..100u64 {
                        let seq = t * 1000 + i + 1;
                        s.begin_order(seq, "0xw", build).unwrap();
                    }
                })
            })
            .collect();
        for h in handles {
            h.join().unwrap();
        }
        let s = Store::open(&p, false).unwrap();
        let mut n = s.nonces_used("0xw").unwrap();
        assert_eq!(n.len(), 200);
        n.sort_unstable();
        n.dedup();
        assert_eq!(n.len(), 200);
        drop(s);
        cleanup(&p);
    }
}
