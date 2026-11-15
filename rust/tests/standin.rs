//! The client against the stand-in exchange, over real websockets on loopback.

use std::sync::atomic::Ordering;
use std::time::{Duration, Instant};

use bookwatch::book::{Fixed, Level, SCALE};
use bookwatch::client::{spawn, BookEvent, ClientConfig, EventKind, Handle};
use bookwatch::proto;
use bookwatch::standin::{PublishError, StandIn, StandInConfig};

async fn wait_until(what: &str, mut cond: impl FnMut() -> bool) {
    let start = Instant::now();
    while !cond() {
        assert!(start.elapsed() < Duration::from_secs(15), "timed out waiting for: {what}");
        tokio::time::sleep(Duration::from_millis(5)).await;
    }
}

fn fast(url: String, symbol: &str) -> ClientConfig {
    let mut c = ClientConfig::new(url, symbol);
    c.idle_timeout = Duration::from_secs(5);
    c.snapshot_timeout = Duration::from_secs(5);
    c.backoff_base = Duration::from_millis(20);
    c.backoff_max = Duration::from_millis(200);
    c.record_applied = true;
    c
}

fn ex_cfg() -> StandInConfig {
    StandInConfig { heartbeat_interval: Duration::from_millis(30), ..StandInConfig::default() }
}

/// Waits until the client holds the stand-in's exact book, then compares every level.
async fn assert_converged(c: &Handle, ex: &StandIn) {
    wait_until("the client to reach the exchange's update id", || c.last_update_id() == Some(ex.last_update_id())).await;
    let (id, bids, asks) = ex.book();
    let book = c.book().expect("a synced client has a book");
    assert_eq!(book.last_update_id(), id);
    let (cb, ca) = book.all_levels();
    assert_eq!(cb, bids, "bids differ");
    assert_eq!(ca, asks, "asks differ");
}

fn finals_strictly_increase(log: &[(u64, u64)]) {
    for w in log.windows(2) {
        assert!(w[1].1 > w[0].1, "a delta was applied twice or out of order: {:?} then {:?}", w[0], w[1]);
        assert!(w[1].0 > w[0].0, "a delta started at or before the previous one: {:?} then {:?}", w[0], w[1]);
    }
}

#[tokio::test]
async fn it_syncs_tracks_the_stream_and_answers_pings() {
    let ex = StandIn::start(ex_cfg()).await.unwrap();
    for _ in 0..50 {
        ex.publish_random();
    }
    let c = spawn(fast(ex.url(), ex.symbol()));
    wait_until("the first sync", || c.stats().synced.load(Ordering::Relaxed)).await;
    for _ in 0..300 {
        ex.publish_random();
    }
    assert_converged(&c, &ex).await;
    let (bids, asks) = c.depth(5).unwrap();
    assert_eq!(bids.len(), 5);
    assert_eq!(asks.len(), 5);
    assert!(c.best_bid().unwrap().price < c.best_ask().unwrap().price);
    assert_eq!(c.best_bid(), bids.first().copied());
    wait_until("a pong from the client", || ex.pongs_received() > 0).await;
    assert_eq!(c.stats().connections.load(Ordering::Relaxed), 1);
    finals_strictly_increase(&c.applied_log());
}

#[tokio::test]
async fn a_dropped_delta_is_a_gap_and_a_new_snapshot_fixes_it_on_the_same_connection() {
    let ex = StandIn::start(ex_cfg()).await.unwrap();
    let c = spawn(fast(ex.url(), ex.symbol()));
    wait_until("the first sync", || c.stats().synced.load(Ordering::Relaxed)).await;
    let snapshots_before = c.stats().snapshots.load(Ordering::Relaxed);
    ex.drop_next_forwarded(1);
    ex.publish_random(); // lost on the way to the client
    for _ in 0..20 {
        ex.publish_random();
    }
    wait_until("a resync", || c.stats().resyncs.load(Ordering::Relaxed) >= 1).await;
    assert_converged(&c, &ex).await;
    assert!(c.stats().snapshots.load(Ordering::Relaxed) > snapshots_before);
    assert_eq!(c.stats().connections.load(Ordering::Relaxed), 1, "a gap must not cost a reconnect");
    finals_strictly_increase(&c.applied_log());
}

#[tokio::test]
async fn killing_the_connection_mid_stream_reconnects_and_the_book_equals_the_servers() {
    let ex = StandIn::start(StandInConfig { rate_per_sec: 1000, ..ex_cfg() }).await.unwrap();
    let c = spawn(fast(ex.url(), ex.symbol()));
    wait_until("the first sync and some deltas", || c.stats().applied.load(Ordering::Relaxed) > 100).await;

    ex.kill_connections();
    wait_until("a second connection", || c.stats().connections.load(Ordering::Relaxed) >= 2).await;
    wait_until("a sync on it", || c.stats().synced.load(Ordering::Relaxed)).await;
    let applied_after_first_kill = c.stats().applied.load(Ordering::Relaxed);
    wait_until("deltas on the new connection", || c.stats().applied.load(Ordering::Relaxed) > applied_after_first_kill + 50)
        .await;

    ex.kill_connections();
    wait_until("a third connection", || c.stats().connections.load(Ordering::Relaxed) >= 3).await;

    ex.set_paused(true);
    assert_converged(&c, &ex).await;
    assert!(c.stats().disconnects.load(Ordering::Relaxed) >= 2);
    assert!(ex.connections() >= 3);
    // Across the kills no delta was applied twice or out of order.
    finals_strictly_increase(&c.applied_log());
}

#[tokio::test]
async fn refused_connections_are_retried_with_a_growing_wait_and_then_it_syncs() {
    let ex = StandIn::start(ex_cfg()).await.unwrap();
    ex.set_accepting(false);
    let mut cfg = fast(ex.url(), ex.symbol());
    cfg.backoff_base = Duration::from_millis(40);
    cfg.backoff_max = Duration::from_millis(1000);
    let started = Instant::now();
    let c = spawn(cfg);
    wait_until("three failed attempts", || c.stats().connect_failures.load(Ordering::Relaxed) >= 3).await;
    // Waits of 40 ms and then 80 ms sit between the first three attempts.
    assert!(started.elapsed() >= Duration::from_millis(120), "retried too fast: {:?}", started.elapsed());
    assert!(!c.stats().synced.load(Ordering::Relaxed));
    assert_eq!(c.best_bid(), None, "no book while there is no connection");
    ex.set_accepting(true);
    wait_until("a sync", || c.stats().synced.load(Ordering::Relaxed)).await;
    for _ in 0..10 {
        ex.publish_random();
    }
    assert_converged(&c, &ex).await;
}

#[tokio::test]
async fn a_silent_server_is_noticed_by_the_idle_timeout_and_the_client_comes_back() {
    let ex = StandIn::start(StandInConfig { rate_per_sec: 200, ..ex_cfg() }).await.unwrap();
    let mut cfg = fast(ex.url(), ex.symbol());
    cfg.idle_timeout = Duration::from_millis(250);
    cfg.snapshot_timeout = Duration::from_millis(400);
    let c = spawn(cfg);
    wait_until("the first sync", || c.stats().synced.load(Ordering::Relaxed)).await;
    ex.set_silent(true);
    wait_until("a disconnect from the idle timeout", || c.stats().disconnects.load(Ordering::Relaxed) >= 1).await;
    assert_eq!(c.best_bid(), None, "a client that lost its feed shows no book");
    ex.set_silent(false);
    wait_until("a sync again", || c.stats().synced.load(Ordering::Relaxed)).await;
    ex.set_paused(true);
    assert_converged(&c, &ex).await;
    finals_strictly_increase(&c.applied_log());
}

#[tokio::test]
async fn a_slow_consumer_loses_notifications_never_state_and_is_told_how_many() {
    let ex = StandIn::start(ex_cfg()).await.unwrap();
    let mut cfg = fast(ex.url(), ex.symbol());
    cfg.channel_capacity = 4;
    let mut c = spawn(cfg);
    wait_until("the first sync", || c.stats().synced.load(Ordering::Relaxed)).await;
    // The consumer reads nothing while 500 deltas go by.
    for _ in 0..500 {
        ex.publish_random();
    }
    assert_converged(&c, &ex).await; // the book is right regardless
    let dropped = c.stats().dropped_events.load(Ordering::Relaxed);
    assert!(dropped > 400, "expected most events to be dropped, got {dropped}");

    let mut got: Vec<BookEvent> = Vec::new();
    while let Ok(ev) = c.events.try_recv() {
        got.push(ev);
    }
    assert_eq!(got.len(), 4, "the channel holds exactly its capacity");
    let attempts = c.stats().event_attempts.load(Ordering::Relaxed);
    assert_eq!(got.len() as u64 + c.stats().dropped_events.load(Ordering::Relaxed), attempts);
    let reported: u64 = got.iter().map(|e| e.missed_before).sum();

    // The next event that gets through carries the count of everything dropped since the last delivery.
    ex.publish_random();
    let next = tokio::time::timeout(Duration::from_secs(5), c.events.recv()).await.unwrap().unwrap();
    assert_eq!(next.kind, EventKind::Update);
    assert_eq!(next.missed_before, c.stats().dropped_events.load(Ordering::Relaxed) - reported);
    assert_eq!(next.last_update_id, ex.last_update_id());
}

#[tokio::test]
async fn a_crossing_delta_throws_the_book_away_and_a_snapshot_rebuilds_it() {
    let ex = StandIn::start(ex_cfg()).await.unwrap();
    let c = spawn(fast(ex.url(), ex.symbol()));
    wait_until("the first sync", || c.stats().synced.load(Ordering::Relaxed)).await;
    let id = c.last_update_id().unwrap();
    // A frame the exchange would never send: a bid far above every ask, at the next update id.
    let crossing = bookwatch::book::Delta {
        first_id: id + 1,
        final_id: id + 1,
        bids: vec![Level { price: Fixed(1_000 * SCALE), qty: Fixed(SCALE) }],
        asks: vec![],
    };
    ex.publish_raw(&proto::depth_update_json(ex.symbol(), 0, &crossing));
    wait_until("a resync", || c.stats().resyncs.load(Ordering::Relaxed) >= 1).await;
    ex.publish_random();
    assert_converged(&c, &ex).await;
    assert!(c.best_bid().unwrap().price < Fixed(1_000 * SCALE), "the crossing level must not survive");
}

#[tokio::test]
async fn a_garbage_frame_ends_the_connection_and_the_client_reconnects() {
    let ex = StandIn::start(ex_cfg()).await.unwrap();
    let c = spawn(fast(ex.url(), ex.symbol()));
    wait_until("the first sync", || c.stats().synced.load(Ordering::Relaxed)).await;
    ex.publish_raw("this is not json");
    wait_until("a second connection", || c.stats().connections.load(Ordering::Relaxed) >= 2).await;
    wait_until("a sync on it", || c.stats().synced.load(Ordering::Relaxed)).await;
    ex.publish_random();
    assert_converged(&c, &ex).await;
}

#[tokio::test]
async fn the_stand_in_refuses_to_publish_a_crossing_delta() {
    let ex = StandIn::start(ex_cfg()).await.unwrap();
    let (id, bids, asks) = ex.book();
    let bad = Level { price: Fixed(asks[0].price.0 + 1), qty: Fixed(SCALE) };
    assert_eq!(ex.publish_levels(&[bad], &[]).unwrap_err(), PublishError::Crossed);
    assert_eq!(ex.book(), (id, bids, asks), "a refused publish leaves the exchange untouched");
}
