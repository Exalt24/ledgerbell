//! Latency from the stand-in sending a delta to the client's best bid changing, and burst throughput.
//!
//! Both ends run in this one process over a loopback websocket, so the clock is shared. Run it with
//! `cargo run --release --example latency`.
//!
//! * Paced: send one delta that raises the best bid one tick, wait until the client's event shows that bid,
//!   record the time, repeat. Each number is one delta alone in the system, with no queue in front of it.
//! * Burst: send every delta at once and time until the client holds the last one.
//!
//! The time includes serializing the frame, the loopback TCP hop, a websocket frame each way, JSON parsing,
//! the book update and the event reaching this task. It is not a network latency and not an exchange's.

use std::time::{Duration, Instant};

use bookwatch::book::{Fixed, Level, SCALE};
use bookwatch::client::{spawn, ClientConfig};
use bookwatch::standin::{StandIn, StandInConfig};

const N: u64 = 5_000;

fn pct(sorted: &[Duration], p: f64) -> Duration {
    let idx = ((sorted.len() as f64 - 1.0) * p).round() as usize;
    sorted[idx]
}

#[tokio::main]
async fn main() {
    let cfg = StandInConfig {
        spread_ticks: 2 * (N + 10),
        initial_levels: 200,
        ..StandInConfig::default()
    };
    let tick = cfg.tick.0;
    let ex = StandIn::start(cfg).await.unwrap();
    let mut ccfg = ClientConfig::new(ex.url(), ex.symbol());
    ccfg.channel_capacity = 16_384;
    let mut c = spawn(ccfg);
    while c.best_bid().is_none() {
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
    // Drain the Synced event.
    while c.events.try_recv().is_ok() {}
    let first_bid = c.best_bid().unwrap().price.0;

    // Paced.
    let mut lat = Vec::with_capacity(N as usize);
    for k in 1..=N {
        let price = Fixed(first_bid + k * tick);
        let sent = ex
            .publish_levels(
                &[Level {
                    price,
                    qty: Fixed(SCALE),
                }],
                &[],
            )
            .unwrap()
            .sent_at;
        loop {
            let ev = c.events.recv().await.unwrap();
            if ev.best_bid.map(|b| b.price) == Some(price) {
                lat.push(sent.elapsed());
                break;
            }
        }
    }
    lat.sort();
    let mean = lat.iter().sum::<Duration>() / lat.len() as u32;
    println!("paced: {N} deltas, each one tick above the last best bid");
    println!(
        "  send to best bid changed: min {:?}  p50 {:?}  p90 {:?}  p99 {:?}  max {:?}  mean {:?}",
        lat[0],
        pct(&lat, 0.5),
        pct(&lat, 0.9),
        pct(&lat, 0.99),
        lat[lat.len() - 1],
        mean
    );

    // Burst.
    let base = first_bid + (N + 1) * tick;
    let start = Instant::now();
    let mut last = Fixed(0);
    for k in 1..=N {
        last = Fixed(base + k * tick);
        // Asks sit above every bid, so this cannot cross: the spread was made wide enough for both runs.
        ex.publish_levels(
            &[Level {
                price: last,
                qty: Fixed(SCALE),
            }],
            &[],
        )
        .unwrap();
    }
    let sent_all = start.elapsed();
    while c.best_bid().map(|b| b.price) != Some(last) {
        tokio::time::sleep(Duration::from_micros(200)).await;
    }
    let total = start.elapsed();
    println!("burst: {N} deltas sent in {sent_all:?}, last one in the client's book after {total:?}");
    println!("  {:.0} deltas per second end to end", N as f64 / total.as_secs_f64());
    println!(
        "  events dropped (channel of 16384): {}",
        c.stats().dropped_events.load(std::sync::atomic::Ordering::Relaxed)
    );
}
