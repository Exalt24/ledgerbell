# A second market data provider and the Airtable run log

## Market data provider

`marketdata.py` holds a small interface (`quote(symbol)` and `daily_bars(symbols, limit)`), an Alpaca implementation that wraps the existing client, and a Finnhub client. `[market_data] provider = "finnhub"` switches the routines over; `FINNHUB_API_KEY` is read from the environment and never from the config file. Orders and the account stay on Alpaca either way.

Written from Finnhub's documentation (read in Chromium on 2026-11-10):

* "All GET request require a token parameter token=apiKey in the URL or a header X-Finnhub-Token : apiKey", so the client sends the header and the key never appears in a URL or a log line.
* `/quote` returns `c` (current price), `d`, `dp`, `h`, `l`, `o` and `pc` (previous close). An unknown symbol comes back as zeros, which the client turns into an error instead of a price of 0.
* "If your limit is exceeded, you will receive a response with status code 429", with a hard cap of 30 calls a second on every plan. The client retries a 429 or a 5xx twice, honouring `Retry-After`.
* The documentation lists "Candles (OHLCV)" as Premium. With a free key the candle call returns `403 You don't have access to this resource.`, so `FinnhubData.daily_bars` raises a `ProviderError` and the routine falls back to Alpaca's bars (`provider_fallback` in the ledger).

What the provider is used for: the open routine takes each watchlist symbol's latest price from Finnhub's `/quote` and uses it to size the order and place its stop, while the moving average still comes from Alpaca's bars. A symbol Finnhub cannot quote keeps its bar close (`quote_failed` in the ledger). `ledgerbell quote AAPL` prints the configured provider's price.

Tests run against a local stand-in server in `tests/fakes.py` that checks the header, answers zeros for an unknown symbol, sends a 429 on demand and returns the free plan's 403 for candles.

## Airtable run log

With `[airtable] enabled = true`, every routine run appends one row to an Airtable table through the REST API: `POST /v0/{baseId}/{tableIdOrName}` with a personal access token in the Authorization header (`AIRTABLE_TOKEN`, from the environment). Columns: Routine, Run at, Status (done, skipped or crashed), Orders suggested, Orders placed, Orders refused, Mode, Summary.

* Suggested is what the model or the rules asked for (premarket: the model's suggestions; open: every proposal considered, share and option; eod: the day's total). Placed and refused are the gateway's verdicts.
* Airtable allows 5 requests a second per base and says to wait 30 seconds after a 429, so the client does that once and then gives up.
* A log failure never fails a routine: it is written to the ledger as `runlog_failed` and the routine carries on.
* `python scripts/airtable_setup.py --base-id appXXXX` creates the table "Ledgerbell run log (test)" with those columns through the Metadata API and reuses it if it exists (`--workspace wspXXXX` makes a base instead). The approach is the same as the Lead intake lab setup in the admission-intake-automation repo.
* Notion is not covered.

[docs/real-run-captured.txt](real-run-captured.txt) is a real run: Finnhub's free API, the Alpaca paper account and an Airtable table in one session, then the table read back through the API.
