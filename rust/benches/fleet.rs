//! Criterion benchmarks for the fleet pieces: the sequence id check, a command's SQLite writes (WAL, with the
//! default and with full sync) and EIP-712 signing of a CLOB order. One iteration is one operation.

use std::hint::black_box;

use bookwatch::clob::Order;
use bookwatch::eip712::{keccak256, recover_address, Wallet};
use bookwatch::follow::OrderSide;
use bookwatch::seq::classify;
use bookwatch::store::Store;
use criterion::{criterion_group, criterion_main, Criterion};

fn temp_db(tag: &str) -> std::path::PathBuf {
    std::env::temp_dir().join(format!("bookwatch-bench-{tag}-{}.db", std::process::id()))
}

fn cleanup(p: &std::path::Path) {
    for ext in ["", "-wal", "-shm"] {
        let mut s = p.as_os_str().to_owned();
        s.push(ext);
        let _ = std::fs::remove_file(s);
    }
}

fn sample_order(w: &Wallet) -> Order {
    let a = w.address();
    Order {
        salt: 42,
        maker: a,
        signer: a,
        token_id: "71321045679252212594626385532706912750332728571942532289631379312455583992563".into(),
        maker_amount: 5_200_000,
        taker_amount: 10_000_000,
        side: OrderSide::Buy,
        signature_type: 0,
        timestamp_ms: 1_700_000_000_000,
        metadata: [0; 32],
        builder: [0; 32],
        expiration: 0,
    }
}

fn bench_seq(c: &mut Criterion) {
    // The worker's per frame decision: apply, duplicate or gap.
    c.bench_function("seq/classify the next id", |b| {
        let mut last = 0u64;
        b.iter(|| {
            let v = classify(black_box(last), black_box(last + 1));
            last += 1;
            black_box(v)
        })
    });
}

fn bench_store(c: &mut Criterion) {
    for (name, full) in [("wal normal sync", false), ("wal full sync", true)] {
        let path = temp_db(if full { "full" } else { "normal" });
        cleanup(&path);
        let mut store = Store::open(&path, full).unwrap();
        let mut seq = 0u64;
        c.bench_function(
            &format!("sqlite/store a command result and move last_acked ({name})"),
            |b| {
                b.iter(|| {
                    seq += 1;
                    store.finish(black_box(seq), "filled", "bench").unwrap();
                })
            },
        );
        let mut seq = 0u64;
        c.bench_function(
            &format!("sqlite/take a nonce and store an order, one transaction ({name})"),
            |b| {
                b.iter(|| {
                    seq += 1;
                    store
                        .begin_order(black_box(seq), "0xbench", |n| {
                            Ok((format!("{{\"salt\":{n}}}"), "BUY".into(), "1".into(), "0.5".into()))
                        })
                        .unwrap();
                })
            },
        );
        drop(store);
        cleanup(&path);
    }
    // The whole per command write path: the order, the fill and the result.
    let path = temp_db("command");
    cleanup(&path);
    let mut store = Store::open(&path, false).unwrap();
    let mut seq = 0u64;
    c.bench_function(
        "sqlite/one whole command: order, fill and result (wal normal sync)",
        |b| {
            b.iter(|| {
                seq += 1;
                store
                    .begin_order(seq, "0xbench", |n| {
                        Ok((format!("{{\"salt\":{n}}}"), "BUY".into(), "1".into(), "0.5".into()))
                    })
                    .unwrap();
                store.mark_submitted(seq, "id").unwrap();
                store.record_fill(seq, "1", true).unwrap();
                store.finish(seq, "filled", "bench").unwrap();
            })
        },
    );
    drop(store);
    cleanup(&path);
}

fn bench_sign(c: &mut Criterion) {
    let (w, _) = Wallet::throwaway();
    let order = sample_order(&w);
    c.bench_function("eip712/hash a Polymarket shaped order (domain, struct, digest)", |b| {
        b.iter(|| black_box(order.digest().unwrap()))
    });
    let digest = order.digest().unwrap();
    c.bench_function("eip712/sign a 32 byte digest (secp256k1)", |b| {
        b.iter(|| black_box(w.sign_digest(black_box(&digest)).unwrap()))
    });
    c.bench_function("eip712/hash and sign an order", |b| {
        b.iter(|| black_box(order.sign(&w).unwrap()))
    });
    let sig = w.sign_digest(&digest).unwrap();
    c.bench_function("eip712/recover the signer from a signature", |b| {
        b.iter(|| black_box(recover_address(black_box(&digest), black_box(&sig)).unwrap()))
    });
    c.bench_function("eip712/keccak256 of 64 bytes", |b| {
        b.iter(|| black_box(keccak256(black_box(&[7u8; 64]))))
    });
}

criterion_group!(fleet, bench_seq, bench_store, bench_sign);
criterion_main!(fleet);
