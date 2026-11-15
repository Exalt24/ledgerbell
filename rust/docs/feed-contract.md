# Feed contract notes

*The parts of Binance's public documentation that the stand in exchange and the client were written from, with the page they came from. Both pages were loaded in Chromium on 2026-11-15 (`webread.cjs`, HTTP 200). Nothing here was observed on a live feed.*

## Sources

* WebSocket Streams for Binance: https://github.com/binance/binance-spot-api-docs/blob/master/web-socket-streams.md
* WebSocket API: https://github.com/binance/binance-spot-api-docs/blob/master/web-socket-api.md

## What the pages say, and what the code does with it

The stream event, from the Diff. Depth Stream section of the streams page:

```
{"e":"depthUpdate","E":1672515782136,"s":"BNBBTC","U":157,"u":160,"b":[["0.0024","10"]],"a":[["0.0026","100"]]}
```

`U` is the first update id in the event and `u` the final one. `b` and `a` are the bids and asks to update, each `[price, quantity]`. `proto.rs` parses exactly this, and a test parses the example above.

The procedure for a local book, from "How to manage a local order book correctly" on the streams page:

1. Open a stream and buffer the events, noting the `U` of the first one.
2. Get a depth snapshot. If its `lastUpdateId` is strictly less than that `U`, take another.
3. Drop buffered events where `u` is less than or equal to the snapshot's `lastUpdateId`.
4. Set the book to the snapshot, then apply the buffered events and every later one.
5. For each event: if `u` is below the book's update id, ignore it. If `U` is above the book's update id plus one, events were missed, so discard the book and start again. Normally `U` of the next event is `u + 1` of the previous one.
6. For each level, set the new quantity, and remove the level if the quantity is zero. Set the book's update id to `u`.

`book.rs` implements 5 and 6 (`OrderBook::apply`) and `client.rs` implements 1 to 4 and the restart.

One rule is stricter than the page. The page only says `U` is normally `u + 1` of the previous event. After the first event, `apply` refuses a delta whose `U` is at or below the book's id while its `u` is above it (`ApplyError::Overlap`), and the client treats that like a gap and takes a new snapshot. A feed that legitimately overlaps would cost extra snapshots here.

The snapshot request, from the Order book request on the WebSocket API page:

```
{"id": "5494febb-d167-46a2-996d-70533eb4d976", "method": "depth", "params": {"symbol": "BNBUSDT"}}
```

with `limit` optional (default 100, maximum 5000). The reply is `{"id": ..., "status": 200, "result": {"lastUpdateId": ..., "bids": [[price, quantity]], "asks": [[price, quantity]]}}`. The page says: "You can use depth request together with `<symbol>@depth` streams to maintain a local order book."

Connection behaviour, from the general information on both pages: the server sends a ping frame every 20 seconds and expects a pong with a copy of the ping's payload, a connection is dropped if no pong comes within a minute, and a single connection to the stream is only valid for 24 hours.

## What the stand in does differently

* On Binance the stream and the WebSocket API are two endpoints. The stand in answers `depth` requests on the same socket as the stream, which keeps the ordering of a snapshot and the stream deterministic.
* Its ping interval is a configuration value (30 to 200 ms in the tests), not 20 seconds, so a test of an idle connection does not take a minute.
* Its price and size grid, its update rate and the random walk of its book are made up. It does not model matching, trades, other streams, rate limits, the 24 hour limit or `serverShutdown`.
