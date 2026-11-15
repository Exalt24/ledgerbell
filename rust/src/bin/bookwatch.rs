//! `bookwatch standin | watch | demo`: run the stand-in exchange, watch a depth stream, or run both in one
//! process, break the connection twice and show the book coming back identical.

use std::sync::atomic::Ordering;
use std::time::Duration;

use bookwatch::book::Level;
use bookwatch::client::{spawn, ClientConfig, Handle};
use bookwatch::standin::{StandIn, StandInConfig};

fn usage() -> ! {
    eprintln!(
        "usage:\n  bookwatch standin [--port N] [--rate N] [--symbol BTCUSDT]\n  bookwatch watch <ws-url> <SYMBOL> [--depth N]\n  bookwatch demo"
    );
    std::process::exit(2)
}

fn flag(args: &[String], name: &str) -> Option<String> {
    args.iter()
        .position(|a| a == name)
        .and_then(|i| args.get(i + 1))
        .cloned()
}

fn fmt_level(l: Option<Level>) -> String {
    match l {
        Some(l) => format!("{} x {}", l.price, l.qty),
        None => "none".into(),
    }
}

fn print_top(label: &str, c: &Handle) {
    println!(
        "{label:<28} bid {:<26} ask {:<26} update id {}",
        fmt_level(c.best_bid()),
        fmt_level(c.best_ask()),
        c.last_update_id().map_or("-".to_string(), |i| i.to_string())
    );
}

fn print_stats(c: &Handle) {
    let s = c.stats();
    println!(
        "stats: applied {} | stale dropped {} | resyncs {} | snapshots {} | connections {} | disconnects {} | events dropped {}",
        s.applied.load(Ordering::Relaxed),
        s.stale.load(Ordering::Relaxed),
        s.resyncs.load(Ordering::Relaxed),
        s.snapshots.load(Ordering::Relaxed),
        s.connections.load(Ordering::Relaxed),
        s.disconnects.load(Ordering::Relaxed),
        s.dropped_events.load(Ordering::Relaxed),
    );
}

async fn wait_for(mut cond: impl FnMut() -> bool) {
    let start = std::time::Instant::now();
    while !cond() {
        if start.elapsed() > Duration::from_secs(10) {
            eprintln!("timed out");
            std::process::exit(1);
        }
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
}

#[tokio::main]
async fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    match args.first().map(String::as_str) {
        Some("standin") => {
            let port = flag(&args, "--port").unwrap_or_else(|| "9001".into());
            let rate: u32 = flag(&args, "--rate").and_then(|r| r.parse().ok()).unwrap_or(50);
            let symbol = flag(&args, "--symbol").unwrap_or_else(|| "BTCUSDT".into());
            let cfg = StandInConfig {
                symbol,
                rate_per_sec: rate,
                ..StandInConfig::default()
            };
            let ex = StandIn::start_on(&format!("127.0.0.1:{port}"), cfg)
                .await
                .expect("bind");
            println!(
                "stand-in exchange (not Binance) at {}  {} deltas per second",
                ex.url(),
                rate
            );
            let _ = tokio::signal::ctrl_c().await;
        }
        Some("watch") => {
            let (Some(url), Some(symbol)) = (args.get(1), args.get(2)) else {
                usage()
            };
            let depth: usize = flag(&args, "--depth").and_then(|d| d.parse().ok()).unwrap_or(3);
            let c = spawn(ClientConfig::new(url.clone(), symbol.clone()));
            let mut tick = tokio::time::interval(Duration::from_secs(1));
            loop {
                tokio::select! {
                    _ = tick.tick() => {
                        match c.depth(depth) {
                            Some((bids, asks)) => {
                                println!("update id {}", c.last_update_id().unwrap_or(0));
                                for (b, a) in bids.iter().zip(asks.iter()) {
                                    println!("  {:>20} x {:<20} | {:>20} x {:<20}", b.price.to_string(), b.qty.to_string(), a.price.to_string(), a.qty.to_string());
                                }
                            }
                            None => println!("no book yet (connecting or resyncing)"),
                        }
                    }
                    _ = tokio::signal::ctrl_c() => break,
                }
            }
        }
        Some("demo") => demo().await,
        _ => usage(),
    }
}

async fn demo() {
    let ex = StandIn::start(StandInConfig {
        rate_per_sec: 400,
        ..StandInConfig::default()
    })
    .await
    .expect("bind");
    println!(
        "stand-in exchange (written from Binance's public docs, not Binance) on {}",
        ex.addr()
    );
    let mut cfg = ClientConfig::new(ex.url(), ex.symbol());
    cfg.backoff_base = Duration::from_millis(100);
    cfg.record_applied = true;
    let c = spawn(cfg);
    wait_for(|| c.stats().synced.load(Ordering::Relaxed)).await;
    print_top("synced", &c);
    tokio::time::sleep(Duration::from_millis(500)).await;
    print_top("streaming", &c);

    println!("\n-- the stand-in drops one delta on its way to the client");
    ex.drop_next_forwarded(1);
    let before = c.stats().resyncs.load(Ordering::Relaxed);
    wait_for(|| c.stats().resyncs.load(Ordering::Relaxed) > before).await;
    wait_for(|| c.stats().synced.load(Ordering::Relaxed)).await;
    println!("gap seen, snapshot requested again on the same connection, book rebuilt");
    print_top("after the gap", &c);

    println!("\n-- the stand-in closes the connection without a close frame, twice");
    for n in 1..=2 {
        let conns = c.stats().connections.load(Ordering::Relaxed);
        ex.kill_connections();
        wait_for(|| c.stats().connections.load(Ordering::Relaxed) > conns).await;
        wait_for(|| c.stats().synced.load(Ordering::Relaxed)).await;
        print_top(&format!("reconnected ({n})"), &c);
        tokio::time::sleep(Duration::from_millis(300)).await;
    }

    ex.set_paused(true);
    wait_for(|| c.last_update_id() == Some(ex.last_update_id())).await;
    let (id, bids, asks) = ex.book();
    let book = c.book().expect("book");
    let (cb, ca) = book.all_levels();
    println!("\nexchange update id {id}, client update id {}", book.last_update_id());
    println!("client book equals the exchange's book: {}", cb == bids && ca == asks);
    let log = c.applied_log();
    let ordered = log.windows(2).all(|w| w[1].1 > w[0].1);
    println!("deltas applied {}, none twice or out of order: {ordered}", log.len());
    print_stats(&c);
}
