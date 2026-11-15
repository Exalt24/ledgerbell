# bookwatch

[![Rust CI](https://github.com/Exalt24/ledgerbell/actions/workflows/rust.yml/badge.svg)](https://github.com/Exalt24/ledgerbell/actions/workflows/rust.yml)

An async Rust client that keeps a local order book from a websocket depth stream: a snapshot, then incremental deltas, with gap detection, a new snapshot on a gap, reconnects with backoff and a bounded channel for a slow consumer.

It lives in the ledgerbell repository because ledgerbell's Python bot sizes its orders against a price it fetches over REST, and this is the part that would watch a book instead. Nothing in the Python code imports it yet.

![The client syncing from a snapshot, recovering from a dropped delta and two dropped connections, and ending with a book equal to the exchange's](docs/demo.png)

*The real output of `cargo run --release --bin bookwatch -- demo`, drawn into this image from the captured text in [docs/demo-captured.txt](docs/demo-captured.txt). The exchange in it is the local stand in described below.*

## What it was built and tested against

The exchange is a stand in: a local websocket server in [`src/standin.rs`](src/standin.rs), written from Binance's public documentation (the WebSocket Streams page and the WebSocket API page, loaded in Chromium on 2026-11-15; the lines it was written from are in [docs/feed-contract.md](docs/feed-contract.md)). It speaks the shape of Binance spot's `depthUpdate` stream and its `depth` snapshot request. It is not Binance, and this crate has not been connected to a live feed or to any exchange.

Everything below was run against that stand in over real websockets on loopback, on one desktop: an AMD Ryzen 7 9800X3D (8 cores, 16 threads), 62 GB of memory, Windows 11 Pro, rustc 1.99.1.

## What it does

* **The book** (`book.rs`). Prices and sizes are integers with eight decimals, so there is no float in it. A delta is refused, and the book left exactly as it was, when it is stale (already applied), when it skips an update id, when it overlaps the book's id after the first delta, when `first` is above `final`, or when it would leave the best bid at or above the best ask. A size of zero removes a level. It gives the best bid, the best ask, a depth limited view and the spread.
* **The client** (`client.rs`). One Tokio task owns the connection and the book. It follows the procedure on Binance's page: buffer the stream, take a snapshot, drop buffered deltas the snapshot covers, apply the rest. On a gap it throws the book away and asks for a new snapshot on the same connection; after five snapshots in a row that do not line up with the stream it drops the connection. On a closed or silent connection (no frame, not even a ping, for `idle_timeout`) it reconnects, waiting `base * 2^n` up to a cap, and goes through the same procedure, so no delta is applied twice and none is skipped. While it has no trustworthy book, `best_bid()` and `depth()` return `None` instead of a stale answer.
* **A slow consumer** (`client.rs`). Events go through a bounded `tokio::sync::mpsc` channel with `try_send`. When the channel is full the event is dropped and counted, the socket is never held up, and the book is unaffected, because it is read through the handle and not through the events. The next event that gets through carries `missed_before`, the number dropped since the last delivery. A slow consumer loses notifications, not state, and is told how many.
* **`delta_position`** (`follow.rs`). Given a leader's position before and after, a ratio, a slippage cap in basis points and a book, it returns the follower order: side, the size the book can fill inside the cap, a limit price rounded so it never goes past the cap, the average fill and the slippage in basis points. Pure, no clock, no state.
* **The stand in** (`standin.rs`). Keeps its own book (a plain `BTreeMap` that shares no code with `book.rs`), publishes deltas with ids that rise without a hole, answers snapshot requests, sends ping frames, and can be told to drop a connection with no close frame or with a close frame, silently drop the next delta, go quiet, refuse connections, answer a snapshot late, answer with a failure or with the wrong id, claim a stale update id, or send a frame the exchange would never send.

No `unsafe` (the crate forbids it). Dependencies: tokio, tokio-tungstenite, serde_json and futures-util; criterion and proptest for tests.

## Run it

You need a recent stable Rust toolchain.

```
cd rust
cargo test                                  # unit, property and integration tests
cargo run --release --bin bookwatch -- demo # stand in and client in one process
cargo run --release --bin bookwatch -- standin --port 9001 --rate 50
cargo run --release --bin bookwatch -- watch ws://127.0.0.1:9001/ws/btcusdt@depth BTCUSDT
cargo bench                                 # criterion
cargo run --release --example latency       # send to best bid latency
python scripts/breakages.py                 # named deliberate breakages
```

## Tests

`cargo test` prints 40 unit tests, 3 property tests and 17 integration tests, 60 tests with 0 failed.

* **Unit.** The book (a crossed delta is refused and the book is left untouched, a zero size removes a level, a stale or out of order delta is refused, a gap names the id it expected, the first delta after a snapshot may overlap and later ones may not), `delta_position`, the frame parser, the backoff and the join of a snapshot with its buffered deltas.
* **Property** (proptest, 512 cases each). Applying a random valid delta stream gives the same book as a snapshot of the final state, checked against an independent `BTreeMap` model. Any delta at all, crossing or out of order, either leaves the book untouched or leaves it uncrossed at the delta's final id. A follower order never goes past its cap and its parts add up.
* **Integration** (the client against the stand in over websockets). A connection killed mid stream twice while the stand in publishes 1,000 deltas a second: the client reconnects and its book equals the exchange's level for level, and no delta was applied twice or out of order. A dropped delta gives a gap and a new snapshot on the same connection. Refused connections are retried with a growing wait. A silent server is noticed by the idle timeout. A slow consumer loses events and is told how many while the book stays right. A crossing delta, a garbage frame, replies for another request id, a failed snapshot request, a snapshot that never lines up, a close frame, a delta that arrives before the snapshot reply, shutdown and drop.
* **Stability.** The 17 integration tests were run 30 times in a row: 0 failures.
* **Mutation.** `cargo mutants` over `book.rs`, `follow.rs`, `proto.rs` and `client.rs`: 176 mutants tested, 148 caught, 0 missed, 28 unviable (they do not compile). The earlier runs left 11, 22 and then 3 alive and each led to a test or a fix; all runs are in [docs/mutants-captured.txt](docs/mutants-captured.txt). Separately, [scripts/breakages.py](scripts/breakages.py) makes 27 named deliberate breakages in a temp copy and runs the whole suite: 27 caught, 0 survived, 0 stale anchors ([output](docs/breakages-run-captured.txt), which also names the 4 that survived the first run and what each led to).

## Numbers

All on the machine named above, in release mode, one process, loopback. Printed as is in [docs/latency-captured.txt](docs/latency-captured.txt) and [docs/bench-captured.txt](docs/bench-captured.txt).

`cargo bench`, criterion, 10,000 deltas of four updates each on a book of 1,000 levels a side:

| | time for 10,000 deltas | throughput |
|---|---|---|
| `OrderBook::apply` | [1.9759 ms 2.0190 ms 2.0637 ms] | [4.8456 Melem/s 4.9530 Melem/s 5.0611 Melem/s] |
| parse the JSON frame, then apply | [16.250 ms 16.701 ms 17.153 ms] | [582.98 Kelem/s 598.78 Kelem/s 615.39 Kelem/s] |

`cargo run --release --example latency`, three runs, the time from the stand in handing a delta to its connections until the client's event shows the new best bid. 5,000 deltas, each one tick above the last best bid, one at a time:

| run | min | p50 | p90 | p99 | max | mean |
|---|---|---|---|---|---|---|
| 1 | 12.5µs | 24.3µs | 52.8µs | 72.3µs | 1.6897ms | 30.988µs |
| 2 | 12.6µs | 26.4µs | 53.3µs | 86.5µs | 175.6µs | 31.594µs |
| 3 | 10.5µs | 32.9µs | 56.8µs | 76.2µs | 1.0873ms | 36.371µs |

And the same 5,000 deltas sent as fast as the stand in can: 117,573, 112,662 and 107,781 deltas per second from the first send until the last delta is in the client's book, with no event dropped.

The time includes serializing the frame, the loopback TCP hop, a websocket frame each way, JSON parsing, the book update and the event reaching the consumer task.

## Limits

* Only the stand in has been used. The real update rates, the 24 hour connection limit, `serverShutdown`, rate limits and a snapshot that is slow under load are not exercised. The stand in answers `depth` requests on the stream's own socket, which Binance does not (two endpoints there).
* One symbol per client, plain `ws://` on loopback in the tests (TLS is available through tokio-tungstenite and untried here).
* One rule is stricter than the page: a live delta that overlaps the book's id is treated as a gap (see [docs/feed-contract.md](docs/feed-contract.md)).
* A reconnect waits `base * 2^n` with no random jitter, which a fleet of clients would want.
* The numbers are loopback timings inside one process on a desktop. They say how fast this code is. They say nothing about a network, an exchange, trading latency, co-location or running this against money.
* `delta_position` reads one book and applies one cap. It does not model fees, partial fills over time, an order that rests, or the leader's own fills.

## License

MIT, see [LICENSE](https://github.com/Exalt24/ledgerbell/blob/main/LICENSE).
