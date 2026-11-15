# bookwatch

[![Rust CI](https://github.com/Exalt24/ledgerbell/actions/workflows/rust.yml/badge.svg)](https://github.com/Exalt24/ledgerbell/actions/workflows/rust.yml)

Two halves of a trading bot's worker, in Rust, each tested against stand ins.

The first is an async client that keeps a local order book from a websocket depth stream: a snapshot, then incremental deltas, with gap detection, a new snapshot on a gap, reconnects with backoff and a bounded channel for a slow consumer. The second is a small worker fleet: an Axum control server (a C2) that sends Protobuf commands over WebSockets, numbered with monotonic sequence ids, to bot workers. Each worker keeps its own state in SQLite in WAL mode, sizes an order with `delta_position`, guards it with a slippage ladder, a momentum filter, a fill stabilization window and a circuit breaker, signs it with EIP-712 and sends it to a stand in CLOB.

It lives in the ledgerbell repository because ledgerbell's Python bot sizes its orders against a price it fetches over REST, and this is the part that would watch a book and follow a leader instead. Nothing in the Python code imports it yet.

![The client syncing from a snapshot, recovering from a dropped delta and two dropped connections, and ending with a book equal to the exchange's](docs/demo.png)

*The real output of `cargo run --release --bin bookwatch -- demo`, drawn into this image from the captured text in [docs/demo-captured.txt](docs/demo-captured.txt). The exchange in it is the local stand in described below. The fleet has its own captured run in [docs/fleet-demo-captured.txt](docs/fleet-demo-captured.txt).*

## What it was built and tested against

Two stand in servers, both written by me from public documentation, and one public test vector.

* The depth stream is a stand in exchange in [`src/standin.rs`](src/standin.rs), written from Binance's public documentation (the WebSocket Streams page and the WebSocket API page, loaded in Chromium on 2026-11-15; the lines it was written from are in [docs/feed-contract.md](docs/feed-contract.md)). It speaks the shape of Binance spot's `depthUpdate` stream and its `depth` snapshot request.
* The CLOB the worker sends orders to is a second stand in, in [`src/clob.rs`](src/clob.rs), written from Polymarket's public documentation (the Protocol V2 API migration, Builder Fees, Place Orders and Prices and Order Books pages and the CLOB OpenAPI file, loaded in Chromium on 2026-11-15; the lines used are quoted in [docs/clob-contract.md](docs/clob-contract.md)). It serves a book, takes a signed order on `POST /order` after recovering the signer from the signature, and reports a matched size that a test scripts.
* The EIP-712 code is checked against the public Mail example in the EIP-712 text and its Example.js: the type hash, the struct hash, the domain separator, the `\x19\x01` digest, the address and the signature all match the values the example states.
* Every other key in a test, the demo and the benchmarks is a random throwaway made when the process starts.

**It was never connected to a live exchange, never signed a real order, and never ran against money.** No request was ever sent to Binance or Polymarket, and neither stand in is either of them. The Polymarket documentation names the signed order's fields, their order, the domain and the REST routes, but not the Solidity type of each field, so a signature made by this crate is not claimed to be valid on the real exchange (see [docs/clob-contract.md](docs/clob-contract.md)).

Everything below was run over real sockets on loopback, and the process tests used real operating system processes, on one desktop: an AMD Ryzen 7 9800X3D (8 cores, 16 threads), 62 GB of memory, Windows 11 Pro, rustc 1.99.1.

## What it does

**The order book client**

* **The book** (`book.rs`). Prices and sizes are integers with eight decimals, so there is no float in it. A delta is refused, and the book left exactly as it was, when it is stale (already applied), when it skips an update id, when it overlaps the book's id after the first delta, when `first` is above `final`, or when it would leave the best bid at or above the best ask. A size of zero removes a level. It gives the best bid, the best ask, a depth limited view and the spread.
* **The client** (`client.rs`). One Tokio task owns the connection and the book. It follows the procedure on Binance's page: buffer the stream, take a snapshot, drop buffered deltas the snapshot covers, apply the rest. On a gap it throws the book away and asks for a new snapshot on the same connection; after five snapshots in a row that do not line up with the stream it drops the connection. On a closed or silent connection (no frame, not even a ping, for `idle_timeout`) it reconnects, waiting `base * 2^n` up to a cap, and goes through the same procedure, so no delta is applied twice and none is skipped. While it has no trustworthy book, `best_bid()` and `depth()` return `None` instead of a stale answer.
* **A slow consumer** (`client.rs`). Events go through a bounded `tokio::sync::mpsc` channel with `try_send`. When the channel is full the event is dropped and counted, the socket is never held up, and the book is unaffected, because it is read through the handle and not through the events. The next event that gets through carries `missed_before`, the number dropped since the last delivery.
* **`delta_position`** (`follow.rs`). Given a leader's position before and after, a ratio, a slippage cap in basis points and a book, it returns the follower order: side, the size the book can fill inside the cap, a limit price rounded so it never goes past the cap, the average fill and the slippage in basis points. Pure, no clock, no state.
* **The stand in exchange** (`standin.rs`). Keeps its own book (a plain `BTreeMap` that shares no code with `book.rs`), publishes deltas with ids that rise without a hole, answers snapshot requests, sends ping frames, and can be told to drop a connection with no close frame or with a close frame, silently drop the next delta, go quiet, refuse connections, answer a snapshot late, answer with a failure or with the wrong id, claim a stale update id, or send a frame the exchange would never send.

**The worker fleet**

* **The control server** (`c2.rs`, Axum). `GET /health`, `GET /status`, `GET /workers` (the registry: connected or not, connections, last acked command, orders sent, fills seen, outcomes, replays served), `GET /workers/{id}`, `POST /workers/{id}/commands` to add a command, and `GET /ws`, the WebSocket the workers use. Each worker has a command log in the C2's memory. The C2 sends a worker the commands after the id in its Hello, keeps at most a window of them unacknowledged, and sends them again from `after_seq + 1` when asked.
* **The wire** (`proto/fleet.proto`, `wire.rs`). Protobuf, compiled by `build.rs` with protox, a protobuf compiler written in Rust, so building needs no `protoc`. One `Envelope` per binary WebSocket frame, with a sequence id and one of `Hello`, `Heartbeat`, `PositionDelta` (the command), `Ack` and `ReplayRequest`. A command's id is its place in that worker's log (1, 2, 3 ...); a worker's own frames are numbered per connection, and the C2 counts any that arrive out of order.
* **Sequence ids** (`seq.rs`). One pure function: given the highest id handled and the id that arrived, the frame is the next one (apply it), at or below the last one (a duplicate, drop it), above the next one (a gap, ask for a replay after the last handled id) or zero (invalid). The worker asks once per hole and ignores later ids until the missing one comes.
* **The worker** (`worker.rs`). Connects, says Hello with its stored last acked id, and handles each command: read the book, check the breaker and the momentum filter, size the order under the ladder's cap, take a nonce and store the signed order in one transaction, `POST /order`, poll the order until the matched size has been stable for the window, store the result and move `last_acked` in one transaction, then send the Ack. A connection that drops is retried with a wait of `base * 2^n` up to a cap; after `failover_threshold` failures in a row on one address the next address in its list is tried. It does not switch back on its own.
* **The state** (`store.rs`, rusqlite with the bundled SQLite). WAL mode, checked by reading `PRAGMA journal_mode` back. Tables for the last acked id, one nonce counter per wallet, orders, fills and results. Taking a nonce and storing the order that uses it is one `BEGIN IMMEDIATE` transaction, so two connections to the same file never get the same nonce; a command's result and the move of `last_acked` are one transaction. An order is stored before it is sent, so a worker that dies after the exchange took the order and before it wrote that down sends the same signed bytes again on restart, and the CLOB stand in keeps one order. The default is `synchronous=NORMAL`, which survives a killed process; `--full-sync` asks for `FULL`.
* **Execution guards** (`guard.rs`, pure, time is a number of milliseconds passed in). A slippage ladder: a base cap in basis points, widened by a step per recent miss and by a thin book (resting size within reach under twice the wanted size), tightened by a step per recent clean fill and by a deep book (four times the wanted size or more), always inside a minimum and a maximum. A momentum filter: a buy is refused when the best ask is more than a threshold above the price the leader traded at, a sell when the best bid is below it, rounded up; a move in the follower's favour is never refused. A fill stabilizer: a matched size counts after it has stayed unchanged for a window, and any change restarts the window. A circuit breaker: closed, open after a number of failures in a row, half open after a cooldown with one probe allowed at a time, closed again by a good probe and reopened with a fresh cooldown by a bad one.
* **EIP-712 and the order** (`eip712.rs`, `clob.rs`). Typed data hashing from the EIP text (type hash, struct hash with nested structs, domain separator, the `\x19\x01` digest) with `sha3` for Keccak-256, and secp256k1 signing and signer recovery with `k256`, RFC 6979 nonces. The Polymarket shaped order has the documented fields in the documented order and the documented domain; amounts are in 6 decimals and rounded so the price they imply is never worse than the limit. The worker uses its per wallet counter as the order's `salt`.
* **The CLOB stand in** (`clob.rs`). `GET /book`, `POST /order` (it recovers the signer, refuses a wrong signature, an EOA order whose maker is not its signer and a second order from a maker with a salt that maker already used; the same signed order twice gets the same answer and is kept once), `GET /data/order/{id}` with a scripted matched size, a configurable answer delay and a switch that makes the next posts fail.

No `unsafe` (the crate forbids it). Dependencies: tokio, tokio-tungstenite, axum, prost, rusqlite, sha3, k256, getrandom, serde and serde_json, futures-util; criterion and proptest for tests; prost-build and protox to build.

## Run it

You need a recent stable Rust toolchain and a C compiler (SQLite is built from source).

```
cd rust
cargo test                                  # unit, property, integration and real process tests
cargo run --release --bin bookwatch -- demo       # the order book client against its stand in
cargo run --release --bin bookwatch -- fleet-demo # two C2s, a worker and a CLOB stand in, with real kills
cargo run --release --bin bookwatch -- c2 --listen 127.0.0.1:9100 --id A --generate 50
cargo run --release --bin bookwatch -- clob --port 9200
cargo run --release --bin bookwatch -- worker --id bot-1 --c2 ws://127.0.0.1:9100/ws --db bot.db --clob http://127.0.0.1:9200 --key-file bot.key
cargo bench                                 # criterion
python scripts/breakages.py                 # named deliberate breakages
```

`--key-file` makes a throwaway key there on first use and prints only its address.

## Tests

@@TESTS@@

## Numbers

@@NUMBERS@@

## Limits

* Only the stand ins have been used. The real update rates, the 24 hour connection limit, `serverShutdown` and rate limits of Binance, and everything about Polymarket's real matching, fees, API authentication, balances, allowances, tick sizes and settlement, are not exercised. The Data API client in the job post's stack, rebate structures and MEV or relayer behaviour are not built.
* The order's Solidity types are this crate's reading of a page that lists field names only, and `side` is encoded 0 and 1 on a guess. A signature from this crate is not claimed to be accepted by the real exchange. There is no nonce in the documented signed order; the counter is this crate's own design and becomes the `salt`.
* The command log lives in the C2's memory. A second C2 shows the same commands only because it is started with the same `--generate` count; there is no replication, no persistence on the C2 and no leader election. A C2 that restarts empty has nothing to send until commands are added again.
* A worker is a single task that handles one command at a time. The sequence checks, the resend and the SQLite writes are tested; throughput across many workers is not measured.
* A silent network partition is noticed only when the worker's ping gets no answer for `idle_timeout`; a C2 that closes the socket is noticed at once.
* `delta_position` reads one book and applies one cap. It does not model fees, partial fills over time, an order that rests, or the leader's own fills. The momentum filter compares the best price with the leader's trade price; it does not look at a window of the book's history.
* The HTTP client the worker uses is a few lines over a TCP socket and speaks plain `http://` only, because the only server it ever talks to is a stand in on loopback. It would need TLS and a real client for anything else.
* In the first order book part: one symbol per client, plain `ws://` on loopback, and one rule stricter than the page (a live delta that overlaps the book's id is treated as a gap, see [docs/feed-contract.md](docs/feed-contract.md)). A reconnect waits `base * 2^n` with no random jitter, which a fleet of clients would want.
* The numbers are loopback timings inside one process, or between processes on one desktop. They say how fast this code is. They say nothing about a network, an exchange, trading latency, co-location or running this against money.

## License

MIT, see [LICENSE](https://github.com/Exalt24/ledgerbell/blob/main/LICENSE).
