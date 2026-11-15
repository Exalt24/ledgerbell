//! The worker fleet against its stand-ins: the C2 server, the CLOB stand-in and a worker, all in one process
//! over real sockets on loopback. Real process kills are in `fleet_process.rs`.

mod common;

use std::net::SocketAddr;
use std::sync::atomic::Ordering;
use std::time::Duration;

use bookwatch::c2::{self, C2Config, C2Handle};
use bookwatch::clob::{http_request, ClobConfig};
use bookwatch::eip712::{recover_address, Wallet};
use bookwatch::wire::{self, Ack, Body, Hello, Outcome};
use bookwatch::worker;
use common::*;
use futures_util::{SinkExt, StreamExt};
use tokio_tungstenite::tungstenite::Message;

const LIMIT: Duration = Duration::from_secs(20);

async fn c2_with(cfg: C2Config) -> C2Handle {
    c2::spawn("127.0.0.1:0".parse().unwrap(), cfg).await.unwrap()
}

fn gen(id: &str, n: u64) -> C2Config {
    C2Config {
        id: id.into(),
        generate: n,
        ..C2Config::default()
    }
}

async fn get_json(base: &str, path: &str) -> serde_json::Value {
    let (status, body) = http_request(base, "GET", path, None, Duration::from_secs(2))
        .await
        .unwrap();
    assert_eq!(status, 200, "{path}");
    serde_json::from_str(&body).unwrap()
}

// ------------------------------------------------------------------ the C2 server

#[tokio::test]
async fn the_c2_answers_health_status_and_the_worker_registry() {
    let c2 = c2_with(gen("c2-test", 3)).await;
    let base = c2.http_url();
    let h = get_json(&base, "/health").await;
    assert_eq!(h["status"], "ok");
    assert_eq!(h["c2_id"], "c2-test");
    assert_eq!(get_json(&base, "/status").await["workers_known"], 0);
    assert_eq!(get_json(&base, "/workers").await, serde_json::json!([]));
    let (status, _) = http_request(&base, "GET", "/workers/nobody", None, Duration::from_secs(2))
        .await
        .unwrap();
    assert_eq!(status, 404);

    // A command posted for a worker creates its registry entry and is numbered after the generated ones.
    let body = r#"{"token_id":"1001","leader_before":0,"leader_after":100000000,"leader_price":"0.50"}"#;
    let (status, answer) = http_request(
        &base,
        "POST",
        "/workers/bot-9/commands",
        Some(body),
        Duration::from_secs(2),
    )
    .await
    .unwrap();
    assert_eq!(status, 200);
    assert_eq!(serde_json::from_str::<serde_json::Value>(&answer).unwrap()["seq"], 4);
    let w = get_json(&base, "/workers/bot-9").await;
    assert_eq!(w["connected"], false);
    assert_eq!(w["commands_in_log"], 4);
}

#[tokio::test]
async fn the_websocket_carries_protobuf_with_rising_sequence_ids_and_a_window() {
    let c2 = c2_with(C2Config {
        generate: 10,
        window: 4,
        ..gen("c2", 10)
    })
    .await;
    let (mut ws, _) = tokio_tungstenite::connect_async(c2.ws_url()).await.unwrap();
    let hello = wire::encode(
        1,
        Body::Hello(Hello {
            worker_id: "raw".into(),
            last_acked: 0,
            wallet: "0xabc".into(),
        }),
    );
    ws.send(Message::Binary(hello.into())).await.unwrap();

    async fn next_command(
        ws: &mut tokio_tungstenite::WebSocketStream<tokio_tungstenite::MaybeTlsStream<tokio::net::TcpStream>>,
    ) -> Option<u64> {
        let m = tokio::time::timeout(Duration::from_millis(300), ws.next())
            .await
            .ok()??
            .ok()?;
        let Message::Binary(b) = m else { return None };
        let env = wire::decode(&b).ok()?;
        matches!(env.body, Some(Body::Command(_))).then_some(env.seq)
    }

    // Four commands arrive and then the C2 waits for acknowledgements.
    let mut got = Vec::new();
    while let Some(s) = next_command(&mut ws).await {
        got.push(s);
    }
    assert_eq!(got, vec![1, 2, 3, 4], "the window of 4 holds the rest back");

    // Acking 1 and 2 opens room for 5 and 6.
    for (i, s) in [1u64, 2].into_iter().enumerate() {
        let ack = wire::encode(
            2 + i as u64,
            Body::Ack(Ack {
                acked_seq: s,
                outcome: Outcome::Filled as i32,
                detail: String::new(),
            }),
        );
        ws.send(Message::Binary(ack.into())).await.unwrap();
    }
    let mut more = Vec::new();
    while let Some(s) = next_command(&mut ws).await {
        more.push(s);
    }
    assert_eq!(more, vec![5, 6]);
    let w = c2.worker("raw").unwrap();
    assert_eq!((w.connected, w.last_acked, w.acks), (true, 2, 2));
    assert_eq!(w.outcomes.get("filled"), Some(&2));
}

#[tokio::test]
async fn a_first_frame_that_is_not_a_hello_is_dropped() {
    let c2 = c2_with(gen("c2", 3)).await;
    let (mut ws, _) = tokio_tungstenite::connect_async(c2.ws_url()).await.unwrap();
    ws.send(Message::Binary(vec![1, 2, 3].into())).await.unwrap();
    let r = tokio::time::timeout(Duration::from_secs(2), ws.next()).await.unwrap();
    assert!(matches!(r, None | Some(Err(_)) | Some(Ok(Message::Close(_)))));
    assert!(c2.workers().is_empty());
}

// ------------------------------------------------------------------ the worker, in order

#[tokio::test]
async fn a_worker_applies_every_command_once_in_order_and_acks_it() {
    let clob = clob().await;
    let c2 = c2_with(gen("A", 30)).await;
    let db = TempDb::new("order");
    let w = worker::spawn(fast_cfg("bot-1", vec![c2.ws_url()], db.path(), &clob.url())).unwrap();
    wait_until("30 commands stored", LIMIT, || last_acked(db.path()) == 30).await;
    assert_applied_exactly_once(db.path(), &w.wallet, &clob, 30, 0);

    // Every order the CLOB took carries a signature that recovers to the worker's wallet, so a signed order
    // really went over the wire and was checked.
    assert_eq!(clob.rejected(), 0);
    let s = db.open();
    for seq in 1..=30 {
        let (outcome, _) = s.result_for(seq).unwrap().unwrap();
        assert_eq!(outcome, "filled", "command {seq}");
    }
    assert_eq!(s.fills_seen().unwrap(), 30);
    assert_eq!(s.journal_mode().unwrap(), "wal");
    wait_until("the C2 registry shows the acks", LIMIT, || {
        c2.worker("bot-1").is_some_and(|i| i.last_acked == 30 && i.acks == 30)
    })
    .await;
    let info = c2.worker("bot-1").unwrap();
    assert_eq!(info.outcomes.get("filled"), Some(&30));
    assert_eq!(info.anomalies, 0, "the worker's own sequence ids rose without a hole");
    assert_eq!(w.stats.gaps_detected.load(Ordering::Relaxed), 0);
    assert_eq!(w.stats.duplicates_dropped.load(Ordering::Relaxed), 0);
}

#[tokio::test]
async fn heartbeats_reach_the_c2_with_the_workers_counts() {
    let clob = clob().await;
    let c2 = c2_with(gen("A", 5)).await;
    let db = TempDb::new("hb");
    let _w = worker::spawn(fast_cfg("bot-hb", vec![c2.ws_url()], db.path(), &clob.url())).unwrap();
    wait_until("heartbeat with five orders", LIMIT, || {
        c2.worker("bot-hb")
            .is_some_and(|i| i.orders_sent == 5 && i.fills_seen == 5)
    })
    .await;
}

// ------------------------------------------------------------------ gaps and duplicates

#[tokio::test]
async fn a_hole_in_the_sequence_is_detected_and_replayed() {
    let clob = clob().await;
    let c2 = c2_with(C2Config {
        skip_once: vec![5],
        ..gen("A", 12)
    })
    .await;
    let db = TempDb::new("gap");
    let w = worker::spawn(fast_cfg("bot-gap", vec![c2.ws_url()], db.path(), &clob.url())).unwrap();
    wait_until("12 commands stored", LIMIT, || last_acked(db.path()) == 12).await;
    assert_eq!(
        w.stats.replays_requested.load(Ordering::Relaxed),
        1,
        "one replay request for one hole"
    );
    assert!(w.stats.gaps_detected.load(Ordering::Relaxed) >= 1);
    assert_eq!(c2.worker("bot-gap").unwrap().replays_served, 1);
    assert_applied_exactly_once(db.path(), &w.wallet, &clob, 12, 0);
}

#[tokio::test]
async fn a_repeated_frame_is_dropped_and_not_applied_twice() {
    let clob = clob().await;
    let c2 = c2_with(C2Config {
        dup_once: vec![4, 7],
        ..gen("A", 10)
    })
    .await;
    let db = TempDb::new("dup");
    let w = worker::spawn(fast_cfg("bot-dup", vec![c2.ws_url()], db.path(), &clob.url())).unwrap();
    wait_until("10 commands stored", LIMIT, || last_acked(db.path()) == 10).await;
    assert_eq!(w.stats.duplicates_dropped.load(Ordering::Relaxed), 2);
    assert_eq!(w.stats.applied.load(Ordering::Relaxed), 10);
    assert_applied_exactly_once(db.path(), &w.wallet, &clob, 10, 0);
}

// ------------------------------------------------------------------ reconnect and failover

async fn rebind(addr: SocketAddr, cfg: C2Config) -> C2Handle {
    for _ in 0..200 {
        if let Ok(h) = c2::spawn(addr, cfg.clone()).await {
            return h;
        }
        tokio::time::sleep(Duration::from_millis(10)).await;
    }
    panic!("could not bind {addr} again");
}

#[tokio::test]
async fn the_worker_reconnects_when_its_c2_dies_and_comes_back_on_the_same_address() {
    let clob = clob().await;
    clob.set_post_latency(Duration::from_millis(3));
    let c2 = c2_with(gen("A", 80)).await;
    let addr = c2.addr();
    let db = TempDb::new("reconnect");
    let w = worker::spawn(fast_cfg("bot-rc", vec![c2.ws_url()], db.path(), &clob.url())).unwrap();
    wait_until("10 commands", LIMIT, || last_acked(db.path()) >= 10).await;
    c2.kill();
    drop(c2);
    wait_until("the worker notices", LIMIT, || {
        w.stats.connect_failures.load(Ordering::Relaxed) >= 1
    })
    .await;
    let before = last_acked(db.path());
    assert!(before < 80, "the C2 died with work left ({before} of 80)");
    let _c2b = rebind(addr, gen("A2", 80)).await;
    wait_until("all 80 commands", LIMIT, || last_acked(db.path()) == 80).await;
    assert!(w.stats.connections.load(Ordering::Relaxed) >= 2);
    assert_applied_exactly_once(db.path(), &w.wallet, &clob, 80, 0);
}

#[tokio::test]
async fn the_worker_fails_over_to_the_second_address_when_the_first_stays_down() {
    let clob = clob().await;
    clob.set_post_latency(Duration::from_millis(3));
    let a = c2_with(gen("A", 80)).await;
    let b = c2_with(gen("B", 80)).await;
    let db = TempDb::new("failover");
    let w = worker::spawn(fast_cfg("bot-fo", vec![a.ws_url(), b.ws_url()], db.path(), &clob.url())).unwrap();
    wait_until("10 commands from A", LIMIT, || last_acked(db.path()) >= 10).await;
    assert_eq!(w.stats.current_address.load(Ordering::Relaxed), 0);
    a.kill();
    drop(a);
    wait_until("all 80 commands", LIMIT, || last_acked(db.path()) == 80).await;
    assert!(w.stats.failovers.load(Ordering::Relaxed) >= 1);
    assert_eq!(w.stats.current_address.load(Ordering::Relaxed), 1);
    let on_b = b.worker("bot-fo").unwrap();
    assert!(on_b.connections >= 1);
    assert_eq!(on_b.last_acked, 80);
    assert_applied_exactly_once(db.path(), &w.wallet, &clob, 80, 0);
}

#[tokio::test]
async fn a_server_that_accepts_and_never_speaks_is_given_up_on_and_the_next_address_is_used() {
    // A TCP listener that takes the connection and says nothing: the WebSocket handshake never finishes.
    let silent = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let silent_addr = silent.local_addr().unwrap();
    let keep = tokio::spawn(async move {
        let mut held = Vec::new();
        while let Ok((s, _)) = silent.accept().await {
            held.push(s);
        }
    });
    let clob = clob().await;
    let b = c2_with(gen("B", 10)).await;
    let db = TempDb::new("silent");
    let mut cfg = fast_cfg(
        "bot-silent",
        vec![format!("ws://{silent_addr}/ws"), b.ws_url()],
        db.path(),
        &clob.url(),
    );
    cfg.connect_timeout = Duration::from_millis(150);
    let w = worker::spawn(cfg).unwrap();
    wait_until("10 commands from B", LIMIT, || last_acked(db.path()) == 10).await;
    assert!(
        w.stats.connect_failures.load(Ordering::Relaxed) >= 2,
        "two timeouts at the threshold"
    );
    assert!(w.stats.failovers.load(Ordering::Relaxed) >= 1);
    keep.abort();
}

#[tokio::test]
async fn nothing_is_lost_or_doubled_when_the_worker_task_is_stopped_and_a_new_one_opens_the_same_database() {
    let clob = clob().await;
    clob.set_post_latency(Duration::from_millis(3));
    let c2 = c2_with(gen("A", 60)).await;
    let db = TempDb::new("restart");
    let cfg = fast_cfg("bot-rs", vec![c2.ws_url()], db.path(), &clob.url());
    let first = worker::spawn(cfg.clone()).unwrap();
    wait_until("10 commands", LIMIT, || last_acked(db.path()) >= 10).await;
    first.abort();
    drop(first);
    let stopped_at = last_acked(db.path());
    assert!(stopped_at < 60);
    let second = worker::spawn(cfg).unwrap();
    wait_until("all 60", LIMIT, || last_acked(db.path()) == 60).await;
    // The task was cut off wherever it was, so one order may have been posted again with the same bytes.
    assert_applied_exactly_once(db.path(), &second.wallet, &clob, 60, 1);
}

// ------------------------------------------------------------------ execution protection in the worker

#[tokio::test]
async fn a_buy_after_the_book_ran_away_is_skipped_by_the_momentum_filter() {
    let clob = clob().await;
    // The leader bought at 0.50; the ask is now 0.55 (1000 bps) and the bid is 0.54.
    clob.set_book("1001", &[("0.54", "1000")], &[("0.55", "1000")]);
    let c2 = c2_with(gen("A", 3)).await;
    let db = TempDb::new("momentum");
    let _w = worker::spawn(fast_cfg("bot-mo", vec![c2.ws_url()], db.path(), &clob.url())).unwrap();
    wait_until("3 commands", LIMIT, || last_acked(db.path()) == 3).await;
    let s = db.open();
    for seq in [1, 2] {
        let (outcome, detail) = s.result_for(seq).unwrap().unwrap();
        assert_eq!(outcome, "skipped");
        assert!(detail.contains("momentum"), "{detail}");
    }
    // Command 3 is a sell, and a sell into a higher bid is a move in the follower's favour.
    assert_eq!(s.result_for(3).unwrap().unwrap().0, "filled");
    assert_eq!(clob.received().len(), 1);
}

#[tokio::test]
async fn a_fill_that_keeps_changing_does_not_count_until_it_settles() {
    let clob = clob().await;
    // The matched size reads 10, 20, ... 100 percent on successive polls, then stays at 100.
    clob.set_fill_plan((1..=10).map(|n| n * 10).collect());
    let c2 = c2_with(gen("A", 2)).await;
    let db = TempDb::new("settle");
    let mut cfg = fast_cfg("bot-st", vec![c2.ws_url()], db.path(), &clob.url());
    cfg.stabilize_window = Duration::from_millis(40);
    cfg.poll_interval = Duration::from_millis(5);
    let _w = worker::spawn(cfg).unwrap();
    wait_until("2 commands", LIMIT, || last_acked(db.path()) == 2).await;
    let s = db.open();
    assert_eq!(s.result_for(1).unwrap().unwrap().0, "filled");
    // Ten changes, five milliseconds apart, then a 40 ms window: the fill was not counted before ~90 ms.
    assert!(clob.polls() >= 10 + 40 / 5 - 1, "polled {} times", clob.polls());
}

#[tokio::test]
async fn a_fill_that_never_settles_is_recorded_as_not_counted() {
    let clob = clob().await;
    clob.set_fill_plan((1..=99).collect());
    let c2 = c2_with(gen("A", 1)).await;
    let db = TempDb::new("unsettled");
    let mut cfg = fast_cfg("bot-un", vec![c2.ws_url()], db.path(), &clob.url());
    cfg.stabilize_window = Duration::from_millis(50);
    cfg.poll_interval = Duration::from_millis(2);
    cfg.fill_timeout = Duration::from_millis(150);
    let _w = worker::spawn(cfg).unwrap();
    wait_until("1 command", LIMIT, || last_acked(db.path()) == 1).await;
    let s = db.open();
    let (outcome, detail) = s.result_for(1).unwrap().unwrap();
    assert_eq!(outcome, "partial");
    assert!(detail.contains("not stable"), "{detail}");
    assert_eq!(s.fills_seen().unwrap(), 0, "a fill that never settled is not counted");
}

#[tokio::test]
async fn a_stable_partial_fill_counts_as_partial() {
    let clob = clob().await;
    clob.set_fill_plan(vec![40]);
    let c2 = c2_with(gen("A", 1)).await;
    let db = TempDb::new("partial");
    let _w = worker::spawn(fast_cfg("bot-pa", vec![c2.ws_url()], db.path(), &clob.url())).unwrap();
    wait_until("1 command", LIMIT, || last_acked(db.path()) == 1).await;
    let s = db.open();
    assert_eq!(s.result_for(1).unwrap().unwrap().0, "partial");
    assert_eq!(s.fills_seen().unwrap(), 1);
}

#[tokio::test]
async fn the_circuit_breaker_opens_after_failed_posts_and_closes_again_after_the_cooldown() {
    let clob = clob().await;
    clob.fail_next_posts(3);
    let c2 = c2_with(gen("A", 6)).await;
    let db = TempDb::new("breaker");
    let mut cfg = fast_cfg("bot-br", vec![c2.ws_url()], db.path(), &clob.url());
    cfg.post_retries = 0;
    cfg.breaker.failure_threshold = 3;
    cfg.breaker.cooldown_ms = 400;
    let _w = worker::spawn(cfg).unwrap();
    wait_until("6 commands", LIMIT, || last_acked(db.path()) == 6).await;
    let s = db.open();
    for seq in 1..=3 {
        assert_eq!(s.result_for(seq).unwrap().unwrap().0, "rejected", "command {seq}");
    }
    for seq in 4..=6 {
        let (outcome, detail) = s.result_for(seq).unwrap().unwrap();
        assert_eq!(
            (outcome.as_str(), detail.as_str()),
            ("skipped", "circuit breaker open"),
            "command {seq}"
        );
    }
    // After the cooldown the next command is the probe; the CLOB works again, so it goes through and closes it.
    tokio::time::sleep(Duration::from_millis(450)).await;
    let body = r#"{"token_id":"1001","leader_before":0,"leader_after":300000000,"leader_price":"0.50"}"#;
    let (status, _) = http_request(
        &c2.http_url(),
        "POST",
        "/workers/bot-br/commands",
        Some(body),
        Duration::from_secs(2),
    )
    .await
    .unwrap();
    assert_eq!(status, 200);
    wait_until("the probe command", LIMIT, || last_acked(db.path()) == 7).await;
    assert_eq!(s.result_for(7).unwrap().unwrap().0, "filled");
}

#[tokio::test]
async fn a_thin_book_widens_the_cap_so_the_order_reaches_the_second_level() {
    // Command 1 is a buy of 0.3 shares (the leader's +3 units at a ratio of 1/10). The ask side holds 0.1 at
    // 0.50 and 0.05 at 0.5035. The base cap of 50 bps stops at 0.5025, which would leave only 0.1. The book
    // inside the ladder's reach holds 0.15, under twice the wanted size, so the ladder adds its thin step and
    // the cap becomes 90 bps (0.5045): both levels are inside it and the order is for 0.15.
    let clob = clob().await;
    clob.set_book("1001", &[("0.499", "1000")], &[("0.50", "0.1"), ("0.5035", "0.05")]);
    let c2 = c2_with(gen("A", 1)).await;
    let db = TempDb::new("ladder");
    let _w = worker::spawn(fast_cfg("bot-lad", vec![c2.ws_url()], db.path(), &clob.url())).unwrap();
    wait_until("1 command", LIMIT, || last_acked(db.path()) == 1).await;
    let s = db.open();
    assert_eq!(s.order_for(1).unwrap().unwrap().qty, "0.15000000");
    assert_eq!(s.result_for(1).unwrap().unwrap().0, "filled");
}

// ------------------------------------------------------------------ the CLOB stand-in itself

#[tokio::test]
async fn the_clob_checks_signatures_and_treats_a_resend_of_the_same_order_as_one_order() {
    use bookwatch::clob::{amounts, Order};
    use bookwatch::follow::OrderSide;
    let clob = clob().await;
    let (w, _) = Wallet::throwaway();
    let a = w.address();
    let (maker_amount, taker_amount) = amounts(
        OrderSide::Buy,
        bookwatch::book::Fixed::parse("10").unwrap(),
        bookwatch::book::Fixed::parse("0.52").unwrap(),
    )
    .unwrap();
    let order = Order {
        salt: 1,
        maker: a,
        signer: a,
        token_id: "1001".into(),
        maker_amount,
        taker_amount,
        side: OrderSide::Buy,
        signature_type: 0,
        timestamp_ms: 1,
        metadata: [0; 32],
        builder: [0; 32],
        expiration: 0,
    };
    let sig = order.sign(&w).unwrap();
    assert_eq!(recover_address(&order.digest().unwrap(), &sig).unwrap(), a);
    let post = |body: String| {
        let url = clob.url();
        async move {
            http_request(&url, "POST", "/order", Some(&body), Duration::from_secs(2))
                .await
                .unwrap()
        }
    };
    let (s1, b1) = post(order.to_wire(&sig).to_string()).await;
    let (s2, b2) = post(order.to_wire(&sig).to_string()).await;
    assert_eq!((s1, s2), (200, 200));
    assert_eq!(b1, b2, "the same order twice gets the same answer");
    assert_eq!(clob.received().len(), 1);
    assert_eq!(clob.received()[0].posts, 2);

    // Another key's signature on the same fields is refused.
    let (other, _) = Wallet::throwaway();
    let forged = order.sign(&other).unwrap();
    let (s3, b3) = post(
        Order {
            salt: 2,
            ..order.clone()
        }
        .to_wire(&forged)
        .to_string(),
    )
    .await;
    assert_eq!(s3, 400, "{b3}");
    assert!(b3.contains("invalid signature"));

    // A second order with the same salt from the same maker is refused (the stand-in's rule).
    let changed = Order {
        timestamp_ms: 2,
        ..order.clone()
    };
    let (s4, b4) = post(changed.to_wire(&changed.sign(&w).unwrap()).to_string()).await;
    assert_eq!(s4, 400, "{b4}");
    assert!(b4.contains("salt"));
    assert_eq!(clob.received().len(), 1);
    assert_eq!(clob.rejected(), 2);
}

#[tokio::test]
async fn the_clob_serves_a_book_in_the_documented_shape() {
    let clob = clob().await;
    let v = get_json(&clob.url(), "/book?token_id=1001").await;
    for k in [
        "market",
        "asset_id",
        "timestamp",
        "hash",
        "bids",
        "asks",
        "min_order_size",
        "tick_size",
        "neg_risk",
        "last_trade_price",
    ] {
        assert!(v.get(k).is_some(), "{k}");
    }
    let bids: Vec<f64> = v["bids"]
        .as_array()
        .unwrap()
        .iter()
        .map(|l| l["price"].as_str().unwrap().parse().unwrap())
        .collect();
    assert!(bids.windows(2).all(|w| w[0] > w[1]), "bids best first");
    let (status, _) = http_request(&clob.url(), "GET", "/book?token_id=nope", None, Duration::from_secs(2))
        .await
        .unwrap();
    assert_eq!(status, 404);
    let _ = ClobConfig::default();
}
