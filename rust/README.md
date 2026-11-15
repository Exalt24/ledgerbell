# bookwatch

[![Rust CI](https://github.com/Exalt24/ledgerbell/actions/workflows/rust.yml/badge.svg)](https://github.com/Exalt24/ledgerbell/actions/workflows/rust.yml)

An async Rust client that keeps a local order book from a websocket depth stream: a snapshot, then incremental deltas, with gap detection, a new snapshot on a gap, reconnects with backoff and a bounded channel for a slow consumer.

It lives in the ledgerbell repository because ledgerbell's Python bot sizes its orders against a price it fetches over REST, and this is the part that would watch a book instead. Nothing in ledgerbell imports it.

```
$ cargo run --release --bin bookwatch -- demo
@@DEMO@@
```

*The real output of `cargo run --release --bin bookwatch -- demo`, captured in [docs/demo-captured.txt](docs/demo-captured.txt).*

## What it was built and tested against

The exchange is a stand in: a local websocket server in [`src/standin.rs`](src/standin.rs), written from Binance's public documentation (the WebSocket Streams page and the WebSocket API page, loaded in Chromium on 2026-11-15; the lines it was written from are in [docs/feed-contract.md](docs/feed-contract.md)). It speaks the shape of Binance spot's `depthUpdate` stream and its `depth` snapshot request. It is not Binance, and this crate has not been connected to a live feed or any exchange.

Everything below was run against that stand in over real websockets on loopback, on one desktop (@@MACHINE@@).

## What it does

* **The book** (`book.rs`). Prices and sizes are integers with eight decimals, so there is no float in it. A delta is refused, and the book left exactly as it was, when it is stale (already applied), when it skips an update id, when it overlaps the book's id after the first delta, when `first` is above `final`, or when it would leave the best bid at or above the best ask. A size of zero removes a level. It gives the best bid, the best ask, a depth limited view and the spread.
* **The client** (`client.rs`). One Tokio task owns the connection and the book. It follows the procedure on Binance's page: buffer the stream, take a snapshot, drop buffered deltas the snapshot covers, apply the rest. On a gap it throws the book away and asks for a new snapshot on the same connection. On a closed or silent connection (no frame, not even a ping, for `idle_timeout`) it reconnects, waiting `base * 2^n` up to a cap, and goes through the same procedure, so no delta is applied twice and none is skipped. While it has no trustworthy book, `best_bid()` and `depth()` return `None` instead of a stale answer.
* **A slow consumer** (`client.rs`). Events go through a bounded `tokio::sync::mpsc` channel with `try_send`. When the channel is full the event is dropped and counted, the socket is never held up, and the book is unaffected, because it is read through the handle and not through the events. The next event that gets through carries `missed_before`, the number dropped since the last delivery. A slow consumer loses notifications, not state, and is told how many.
* **`delta_position`** (`follow.rs`). Given a leader's position before and after, a ratio, a slippage cap in basis points and a book, it returns the follower order: side, the size the book can fill inside the cap, a limit price rounded so it never goes past the cap, the average fill and the slippage in basis points. Pure, no clock, no state.
* **The stand in** (`standin.rs`). Keeps its own book (a plain `BTreeMap` that shares no code with `book.rs`), publishes deltas with ids that rise without a hole, answers snapshot requests, sends ping frames, and can be told to drop a connection with no close frame, silently drop the next delta, go quiet, refuse connections or send a frame the exchange would never send.

No `unsafe` (the crate forbids it). Dependencies: tokio, tokio-tungstenite, serde_json, futures-util; criterion and proptest for tests.

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

@@TESTS@@

## Numbers

@@NUMBERS@@

## Limits

* Only the stand in has been used. The real update rates, the 24 hour connection limit, `serverShutdown`, rate limits and a snapshot that is slow under load are not exercised. The stand in answers `depth` requests on the stream's own socket, which Binance does not (two endpoints there).
* One symbol per client, plain `ws://` on loopback in the tests (TLS is available through tokio-tungstenite and untried here).
* One rule is stricter than the page: a live delta that overlaps the book's id is treated as a gap (see [docs/feed-contract.md](docs/feed-contract.md)).
* A reconnect waits `base * 2^n` with no random jitter, which a fleet of clients would want.
* The numbers are loopback timings inside one process on a desktop. They say how fast this code is. They say nothing about a network, an exchange, trading latency, co-location or running this against money.
* `delta_position` reads one book and applies one cap. It does not model fees, partial fills over time, an order that rests, or the leader's own fills.

## License

MIT, see [LICENSE](https://github.com/Exalt24/ledgerbell/blob/main/LICENSE).
