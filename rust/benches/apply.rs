//! How fast the book applies deltas, and how fast it parses and applies them from JSON text.
//!
//! `cargo bench` prints the time for 10,000 deltas on a book of 1,000 levels a side, and the throughput.

use std::hint::black_box;

use bookwatch::book::{Delta, Fixed, Level, OrderBook};
use bookwatch::proto;
use criterion::{criterion_group, criterion_main, BatchSize, Criterion, Throughput};

const TICK: u64 = 1_000_000;
const LEVELS: u64 = 1_000;
const DELTAS: usize = 10_000;

struct Lcg(u64);
impl Lcg {
    fn next(&mut self) -> u64 {
        self.0 = self
            .0
            .wrapping_mul(6364136223846793005)
            .wrapping_add(1442695040888963407);
        self.0 >> 33
    }
}

fn level(price_step: u64, qty: u64) -> Level {
    Level {
        price: Fixed(price_step * TICK),
        qty: Fixed(qty * TICK),
    }
}

fn book() -> OrderBook {
    let bids: Vec<Level> = (1..=LEVELS).map(|s| level(s, 5)).collect();
    let asks: Vec<Level> = (LEVELS + 1..=2 * LEVELS).map(|s| level(s, 5)).collect();
    OrderBook::from_snapshot(0, &bids, &asks).unwrap()
}

/// Deltas of four updates each, bids in the lower half of the price range and asks in the upper half so none crosses.
fn deltas() -> Vec<Delta> {
    let mut rng = Lcg(42);
    (0..DELTAS as u64)
        .map(|i| {
            let mut d = Delta {
                first_id: i + 1,
                final_id: i + 1,
                bids: vec![],
                asks: vec![],
            };
            for _ in 0..4 {
                let qty = rng.next() % 6;
                if rng.next().is_multiple_of(2) {
                    d.bids.push(level(1 + rng.next() % LEVELS, qty));
                } else {
                    d.asks.push(level(LEVELS + 1 + rng.next() % LEVELS, qty));
                }
            }
            d
        })
        .collect()
}

fn apply_benches(c: &mut Criterion) {
    let ds = deltas();
    let frames: Vec<String> = ds.iter().map(|d| proto::depth_update_json("BTCUSDT", 0, d)).collect();

    let mut g = c.benchmark_group("book");
    g.throughput(Throughput::Elements(DELTAS as u64));
    g.bench_function("apply_10000_deltas", |b| {
        b.iter_batched(
            book,
            |mut bk| {
                for d in &ds {
                    bk.apply(black_box(d)).unwrap();
                }
                bk
            },
            BatchSize::LargeInput,
        )
    });
    g.bench_function("parse_json_and_apply_10000_deltas", |b| {
        b.iter_batched(
            book,
            |mut bk| {
                for f in &frames {
                    let proto::Inbound::Depth(d) = proto::parse(black_box(f)).unwrap() else {
                        unreachable!()
                    };
                    bk.apply(&d).unwrap();
                }
                bk
            },
            BatchSize::LargeInput,
        )
    });
    g.finish();
}

criterion_group!(benches, apply_benches);
criterion_main!(benches);
