//! The fleet subcommands of the `bookwatch` binary: `c2`, `worker`, `clob` and `fleet-demo`.

use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::sync::atomic::Ordering;
use std::time::{Duration, Instant};

use bookwatch::c2::{self, C2Config};
use bookwatch::clob::{http_request, ClobConfig, ClobStandIn};
use bookwatch::eip712::{hex_decode, hex_encode, Wallet};
use bookwatch::store::Store;
use bookwatch::worker::{self, WorkerConfig};

use super::flags;

fn flag(args: &[String], name: &str) -> Option<String> {
    flags(args, name).into_iter().next()
}

fn num<T: std::str::FromStr>(args: &[String], name: &str, default: T) -> T {
    flag(args, name).and_then(|v| v.parse().ok()).unwrap_or(default)
}

/// Seeds the stand-in CLOB with one deep book for the token the generated commands trade.
pub fn seed_book(clob: &ClobStandIn) {
    clob.set_book(
        "1001",
        &[("0.499", "1000"), ("0.498", "1000"), ("0.497", "1000")],
        &[("0.50", "1000"), ("0.501", "1000"), ("0.502", "1000")],
    );
}

pub async fn c2(args: &[String]) {
    let listen = flag(args, "--listen").unwrap_or_else(|| "127.0.0.1:9100".into());
    let cfg = C2Config {
        id: flag(args, "--id").unwrap_or_else(|| "c2".into()),
        generate: num(args, "--generate", 0),
        window: num(args, "--window", 32),
        skip_once: flags(args, "--skip-seq")
            .iter()
            .filter_map(|v| v.parse().ok())
            .collect(),
        dup_once: flags(args, "--dup-seq").iter().filter_map(|v| v.parse().ok()).collect(),
        ..C2Config::default()
    };
    let id = cfg.id.clone();
    let h = c2::spawn(listen.parse().expect("listen address"), cfg)
        .await
        .expect("bind");
    println!("c2 {id} listening on {}", h.ws_url());
    let _ = tokio::signal::ctrl_c().await;
}

pub async fn clob(args: &[String]) {
    let port: u16 = num(args, "--port", 9200);
    let plan: Vec<u32> = flag(args, "--fill-plan")
        .map(|p| p.split(',').filter_map(|v| v.trim().parse().ok()).collect())
        .filter(|v: &Vec<u32>| !v.is_empty())
        .unwrap_or_else(|| vec![100]);
    let cfg = ClobConfig {
        post_latency: Duration::from_millis(num(args, "--latency-ms", 0)),
        fill_plan: plan,
    };
    let s = ClobStandIn::start_on(format!("127.0.0.1:{port}").parse().unwrap(), cfg)
        .await
        .expect("bind");
    seed_book(&s);
    println!(
        "stand-in CLOB (written from Polymarket's public docs, not Polymarket) at {}",
        s.url()
    );
    let _ = tokio::signal::ctrl_c().await;
}

/// Reads a hex secret from `path`, or makes a throwaway one and writes it there. Only the address is printed.
fn load_or_make_key(path: &str) -> [u8; 32] {
    if let Ok(text) = std::fs::read_to_string(path) {
        let raw = hex_decode(text.trim()).expect("key file holds 64 hex characters");
        return raw.try_into().expect("key file holds 32 bytes");
    }
    let (_, secret) = Wallet::throwaway();
    std::fs::write(path, hex_encode(&secret)).expect("write throwaway key file");
    secret
}

pub async fn worker(args: &[String]) {
    let (Some(id), Some(db), Some(clob_url), Some(key_file)) = (
        flag(args, "--id"),
        flag(args, "--db"),
        flag(args, "--clob"),
        flag(args, "--key-file"),
    ) else {
        eprintln!("worker needs --id, --c2, --db, --clob and --key-file");
        std::process::exit(2);
    };
    let c2_urls = flags(args, "--c2");
    if c2_urls.is_empty() {
        eprintln!("worker needs at least one --c2");
        std::process::exit(2);
    }
    let secret = load_or_make_key(&key_file);
    let mut cfg = WorkerConfig::new(&id, c2_urls, PathBuf::from(db), &clob_url, secret);
    cfg.crash_after_send = flag(args, "--crash-after-send").and_then(|v| v.parse().ok());
    cfg.full_sync = args.iter().any(|a| a == "--full-sync");
    cfg.heartbeat = Duration::from_millis(num(args, "--heartbeat-ms", 250));
    cfg.stabilize_window = Duration::from_millis(num(args, "--stabilize-ms", 30));
    cfg.poll_interval = Duration::from_millis(num(args, "--poll-ms", 5));
    let h = worker::spawn(cfg).expect("start worker");
    println!("worker {id} wallet {} (a throwaway key)", h.wallet);
    let mut tick = tokio::time::interval(Duration::from_secs(1));
    loop {
        tokio::select! {
            _ = tick.tick() => {
                let s = &h.stats;
                println!(
                    "worker {id}: applied {} | duplicates dropped {} | gaps {} | replays asked {} | connections {} | connect failures {} | failovers {} | on address {}",
                    s.applied.load(Ordering::Relaxed),
                    s.duplicates_dropped.load(Ordering::Relaxed),
                    s.gaps_detected.load(Ordering::Relaxed),
                    s.replays_requested.load(Ordering::Relaxed),
                    s.connections.load(Ordering::Relaxed),
                    s.connect_failures.load(Ordering::Relaxed),
                    s.failovers.load(Ordering::Relaxed),
                    s.current_address.load(Ordering::Relaxed),
                );
            }
            _ = tokio::signal::ctrl_c() => break,
        }
    }
}

// ---------------------------------------------------------------- the demo

fn free_port() -> u16 {
    let l = std::net::TcpListener::bind("127.0.0.1:0").expect("free port");
    l.local_addr().unwrap().port()
}

fn spawn_self(args: &[&str]) -> Child {
    Command::new(std::env::current_exe().expect("own path"))
        .args(args)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .expect("start child process")
}

async fn wait_acked(db: &std::path::Path, at_least: u64) -> u64 {
    let start = Instant::now();
    loop {
        if let Ok(s) = Store::open(db, false) {
            if let Ok(n) = s.last_acked() {
                if n >= at_least {
                    return n;
                }
            }
        }
        if start.elapsed() > Duration::from_secs(60) {
            eprintln!("timed out waiting for command {at_least}");
            std::process::exit(1);
        }
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
}

pub async fn demo() {
    const N: u64 = 80;
    let dir = std::env::temp_dir().join(format!("bookwatch-fleet-demo-{}", std::process::id()));
    std::fs::create_dir_all(&dir).unwrap();
    let db = dir.join("worker.db");
    let key = dir.join("worker.key");

    let clob = ClobStandIn::start(ClobConfig {
        post_latency: Duration::from_millis(4),
        fill_plan: vec![50, 100],
    })
    .await
    .expect("bind");
    seed_book(&clob);
    println!(
        "stand-in CLOB (from Polymarket's public docs, not Polymarket) at {}",
        clob.url()
    );

    let (pa, pb) = (free_port(), free_port());
    let gen = N.to_string();
    let mut c2a = spawn_self(&[
        "c2",
        "--listen",
        &format!("127.0.0.1:{pa}"),
        "--id",
        "A",
        "--generate",
        &gen,
    ]);
    let mut c2b = spawn_self(&[
        "c2",
        "--listen",
        &format!("127.0.0.1:{pb}"),
        "--id",
        "B",
        "--generate",
        &gen,
    ]);
    println!("C2 A on port {pa}, C2 B on port {pb}, each with the same {N} commands for every worker");
    let urls = [format!("ws://127.0.0.1:{pa}/ws"), format!("ws://127.0.0.1:{pb}/ws")];
    let clob_addr = clob.url();
    let start_worker = |extra: &[&str]| -> Child {
        let mut a = vec![
            "worker",
            "--id",
            "bot-1",
            "--c2",
            &urls[0],
            "--c2",
            &urls[1],
            "--db",
            db.to_str().unwrap(),
            "--clob",
            &clob_addr,
            "--key-file",
            key.to_str().unwrap(),
        ];
        a.extend_from_slice(extra);
        spawn_self(&a)
    };

    let mut w = start_worker(&[]);
    let n = wait_acked(&db, 20).await;
    println!("\n-- worker running, {n} commands stored; killing the worker process now");
    w.kill().unwrap();
    let _ = w.wait();
    let at_kill = Store::open(&db, false).unwrap().last_acked().unwrap();
    println!("worker killed, last stored command id {at_kill}; starting a new worker process on the same database");
    let mut w = start_worker(&[]);
    let n = wait_acked(&db, at_kill + 10).await;
    println!("new worker is past it: last stored command id {n}");

    println!("\n-- killing C2 A while the worker is connected to it");
    c2a.kill().unwrap();
    let _ = c2a.wait();
    let n = wait_acked(&db, N).await;
    println!("worker failed over to C2 B and finished: last stored command id {n}");
    let (status, body) = http_request(
        &format!("http://127.0.0.1:{pb}"),
        "GET",
        "/workers",
        None,
        Duration::from_secs(2),
    )
    .await
    .unwrap();
    println!("C2 B GET /workers answered {status}: {body}");

    let s = Store::open(&db, false).unwrap();
    let wallet = format!(
        "0x{}",
        hex_encode(
            &Wallet::from_secret(
                &hex_decode(std::fs::read_to_string(&key).unwrap().trim())
                    .unwrap()
                    .try_into()
                    .unwrap()
            )
            .unwrap()
            .address()
        )
    );
    let nonces = s.nonces_used(&wallet).unwrap();
    let received = clob.received();
    let posts: u32 = received.iter().map(|r| r.posts).sum();
    println!(
        "\nworker database: journal mode {}, {} results, {} orders, nonces {}..{}",
        s.journal_mode().unwrap(),
        s.result_count().unwrap(),
        s.order_count().unwrap(),
        nonces.first().unwrap_or(&0),
        nonces.last().unwrap_or(&0)
    );
    let nonces_ok = nonces.windows(2).all(|p| p[1] == p[0] + 1) && nonces.first() == Some(&1);
    println!(
        "CLOB stand-in saw {} POSTs for {} distinct orders; every nonce used once: {}",
        posts,
        received.len(),
        nonces_ok
    );
    let ok = nonces_ok
        && received.len() == nonces.len()
        && s.result_count().unwrap() == N
        && s.journal_mode().unwrap() == "wal";
    let _ = w.kill();
    let _ = w.wait();
    let _ = c2b.kill();
    let _ = c2b.wait();
    drop(s);
    let _ = std::fs::remove_dir_all(&dir);
    if !ok {
        eprintln!("the demo's own checks failed");
        std::process::exit(1);
    }
}
