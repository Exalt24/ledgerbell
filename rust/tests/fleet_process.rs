//! Real process kills. The C2s and the workers here are separate operating system processes (the `bookwatch`
//! binary), killed with the operating system's kill, and a new process is started afterwards. The CLOB
//! stand-in runs in the test process.

mod common;

use std::path::Path;
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

use bookwatch::clob::{http_request, ClobConfig, ClobStandIn};
use bookwatch::eip712::{hex_decode, Wallet};
use bookwatch::store::Store;
use common::*;

const LIMIT: Duration = Duration::from_secs(60);
const BIN: &str = env!("CARGO_BIN_EXE_bookwatch");

fn free_port() -> u16 {
    std::net::TcpListener::bind("127.0.0.1:0")
        .unwrap()
        .local_addr()
        .unwrap()
        .port()
}

/// A child process that is killed if the test panics before it is.
struct Proc(Child);

impl Proc {
    fn start(args: &[&str]) -> Proc {
        Proc(
            Command::new(BIN)
                .args(args)
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .spawn()
                .unwrap(),
        )
    }

    fn kill(&mut self) {
        let _ = self.0.kill();
        let _ = self.0.wait();
    }

    fn id(&self) -> u32 {
        self.0.id()
    }
}

impl Drop for Proc {
    fn drop(&mut self) {
        self.kill();
    }
}

async fn clob_with_latency(ms: u64) -> ClobStandIn {
    let c = ClobStandIn::start(ClobConfig {
        post_latency: Duration::from_millis(ms),
        fill_plan: vec![100],
    })
    .await
    .unwrap();
    seed_book(&c);
    c
}

fn c2_proc(port: u16, name: &str, n: u64) -> Proc {
    Proc::start(&[
        "c2",
        "--listen",
        &format!("127.0.0.1:{port}"),
        "--id",
        name,
        "--generate",
        &n.to_string(),
    ])
}

fn worker_proc(db: &Path, key: &Path, clob: &str, urls: &[String], extra: &[&str]) -> Proc {
    let mut args: Vec<String> = vec!["worker".into(), "--id".into(), "bot-p".into()];
    for u in urls {
        args.push("--c2".into());
        args.push(u.clone());
    }
    for (k, v) in [
        ("--db", db.to_str().unwrap()),
        ("--clob", clob),
        ("--key-file", key.to_str().unwrap()),
        ("--stabilize-ms", "5"),
        ("--poll-ms", "2"),
        ("--heartbeat-ms", "100"),
    ] {
        args.push(k.into());
        args.push(v.into());
    }
    args.extend(extra.iter().map(|s| s.to_string()));
    let refs: Vec<&str> = args.iter().map(String::as_str).collect();
    Proc::start(&refs)
}

struct KeyFile(std::path::PathBuf);

impl KeyFile {
    fn new(tag: &str) -> KeyFile {
        let db = TempDb::new(tag);
        let p = db.path().with_extension("key");
        drop(db);
        KeyFile(p)
    }

    /// The worker writes a throwaway key here on its first start; the address is read back from it.
    async fn wallet(&self) -> String {
        wait_until("the key file", Duration::from_secs(20), || self.0.exists()).await;
        let text = std::fs::read_to_string(&self.0).unwrap();
        let secret: [u8; 32] = hex_decode(text.trim()).unwrap().try_into().unwrap();
        Wallet::from_secret(&secret).unwrap().address_hex()
    }
}

impl Drop for KeyFile {
    fn drop(&mut self) {
        let _ = std::fs::remove_file(&self.0);
    }
}

#[tokio::test]
async fn a_killed_worker_process_is_replaced_and_resumes_from_its_stored_ack() {
    const N: u64 = 120;
    let clob = clob_with_latency(2).await;
    let port = free_port();
    let _c2 = c2_proc(port, "A", N);
    let url = format!("ws://127.0.0.1:{port}/ws");
    let db = TempDb::new("kill-worker");
    let key = KeyFile::new("kill-worker");

    let mut w1 = worker_proc(db.path(), &key.0, &clob.url(), std::slice::from_ref(&url), &[]);
    wait_until("15 commands stored", LIMIT, || last_acked(db.path()) >= 15).await;
    w1.kill();
    let at_kill = last_acked(db.path());
    assert!(at_kill < N, "killed with work left: {at_kill} of {N}");
    println!("killed worker process {} with last_acked {at_kill}", w1.id());

    let _w2 = worker_proc(db.path(), &key.0, &clob.url(), std::slice::from_ref(&url), &[]);
    wait_until("all commands stored", LIMIT, || last_acked(db.path()) == N).await;
    let wallet = key.wallet().await;
    // The process was killed wherever it was, so one order may have been posted twice with the same bytes.
    assert_applied_exactly_once(db.path(), &wallet, &clob, N, 1);
    assert_eq!(clob.rejected(), 0);
}

#[tokio::test]
async fn a_worker_that_dies_after_the_exchange_took_its_order_posts_the_same_bytes_again_on_restart() {
    const N: u64 = 30;
    let clob = clob_with_latency(0).await;
    let port = free_port();
    let _c2 = c2_proc(port, "A", N);
    let url = format!("ws://127.0.0.1:{port}/ws");
    let db = TempDb::new("crash");
    let key = KeyFile::new("crash");

    let mut w1 = worker_proc(
        db.path(),
        &key.0,
        &clob.url(),
        std::slice::from_ref(&url),
        &["--crash-after-send", "7"],
    );
    let start = Instant::now();
    let code = loop {
        if let Some(status) = w1.0.try_wait().unwrap() {
            break status.code();
        }
        assert!(
            start.elapsed() < Duration::from_secs(30),
            "the worker never reached command 7"
        );
        tokio::time::sleep(Duration::from_millis(10)).await;
    };
    assert_eq!(code, Some(86), "the worker exited at the crash hook");
    let s = Store::open(db.path(), false).unwrap();
    assert_eq!(s.last_acked().unwrap(), 6, "command 7 was not acknowledged");
    let stored = s.order_for(7).unwrap().unwrap();
    assert_eq!(
        stored.state, "pending",
        "the worker never wrote down that the exchange had it"
    );
    assert_eq!(clob.received().len(), 7, "but the exchange had the order");
    drop(s);

    let _w2 = worker_proc(db.path(), &key.0, &clob.url(), std::slice::from_ref(&url), &[]);
    wait_until("all commands stored", LIMIT, || last_acked(db.path()) == N).await;
    let wallet = key.wallet().await;
    assert_applied_exactly_once(db.path(), &wallet, &clob, N, 1);
    let seventh = clob.received().into_iter().find(|r| r.salt == stored.nonce).unwrap();
    assert_eq!(
        seventh.posts, 2,
        "the same signed order was posted again, and the exchange kept one"
    );
    let s = Store::open(db.path(), false).unwrap();
    assert_eq!(s.order_for(7).unwrap().unwrap().body, stored.body, "the same bytes");
    assert_eq!(s.result_for(7).unwrap().unwrap().0, "filled");
}

#[tokio::test]
async fn a_killed_c2_process_makes_the_worker_fail_over_to_the_second_c2_and_finish() {
    const N: u64 = 120;
    let clob = clob_with_latency(2).await;
    let (pa, pb) = (free_port(), free_port());
    let mut a = c2_proc(pa, "A", N);
    let _b = c2_proc(pb, "B", N);
    let urls = vec![format!("ws://127.0.0.1:{pa}/ws"), format!("ws://127.0.0.1:{pb}/ws")];
    let db = TempDb::new("kill-c2");
    let key = KeyFile::new("kill-c2");

    let _w = worker_proc(db.path(), &key.0, &clob.url(), &urls, &[]);
    wait_until("15 commands through A", LIMIT, || last_acked(db.path()) >= 15).await;
    let base_a = format!("http://127.0.0.1:{pa}");
    let (_, body) = http_request(&base_a, "GET", "/workers", None, Duration::from_secs(2))
        .await
        .unwrap();
    assert!(
        body.contains("\"connected\":true"),
        "the worker is on A before the kill: {body}"
    );
    a.kill();
    let at_kill = last_acked(db.path());
    assert!(at_kill < N, "killed with work left: {at_kill} of {N}");

    wait_until("all commands stored", LIMIT, || last_acked(db.path()) == N).await;
    let base_b = format!("http://127.0.0.1:{pb}");
    let (status, body) = http_request(&base_b, "GET", "/workers/bot-p", None, Duration::from_secs(2))
        .await
        .unwrap();
    assert_eq!(status, 200, "B knows the worker: {body}");
    let v: serde_json::Value = serde_json::from_str(&body).unwrap();
    assert!(v["connections"].as_u64().unwrap() >= 1);
    assert_eq!(v["last_acked"], N);
    let wallet = key.wallet().await;
    assert_applied_exactly_once(db.path(), &wallet, &clob, N, 0);
}
