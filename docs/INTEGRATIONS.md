# More market data providers, text alerts and the Airtable run log

## Market data provider

`marketdata.py` holds a small interface (`quote(symbol)` and `daily_bars(symbols, limit)`), an Alpaca implementation that wraps the existing client, a Finnhub client and a Polygon client. `[market_data] provider = "finnhub"` or `"polygon"` switches the routines over; `FINNHUB_API_KEY` and `POLYGON_API_KEY` are read from the environment and never from the config file. Orders and the account stay on Alpaca either way.

Written from Finnhub's documentation (read in Chromium on 2026-11-10):

* "All GET request require a token parameter token=apiKey in the URL or a header X-Finnhub-Token : apiKey", so the client sends the header and the key never appears in a URL or a log line.
* `/quote` returns `c` (current price), `d`, `dp`, `h`, `l`, `o` and `pc` (previous close). An unknown symbol comes back as zeros, which the client turns into an error instead of a price of 0.
* "If your limit is exceeded, you will receive a response with status code 429", with a hard cap of 30 calls a second on every plan. The client retries a 429 or a 5xx twice, honouring `Retry-After`.
* The documentation lists "Candles (OHLCV)" as Premium. With a free key the candle call returns `403 You don't have access to this resource.`, so `FinnhubData.daily_bars` raises a `ProviderError` and the routine falls back to Alpaca's bars (`provider_fallback` in the ledger).

What the provider is used for: the open routine takes each watchlist symbol's latest price from Finnhub's `/quote` and uses it to size the order and place its stop, while the moving average still comes from Alpaca's bars. A symbol Finnhub cannot quote keeps its bar close (`quote_failed` in the ledger). `ledgerbell quote AAPL` prints the configured provider's price.

Tests run against a local stand-in server in `tests/fakes.py` that checks the header, answers zeros for an unknown symbol, sends a 429 on demand and returns the free plan's 403 for candles.

### Polygon

Written from Polygon's documentation, which moved to massive.com when Polygon.io was renamed Massive on 2025-10-30 (read in Chromium on 2026-11-10). It has not been run against the live service.

* Auth: "You can include your API key in two ways", `?apiKey=YOUR_API_KEY` or `Authorization: Bearer YOUR_API_KEY`. The client sends the header, so the key is not in a URL, a log line or the `next_url` a response returns.
* Hosts: the rename notice says `api.polygon.io` keeps working "for an extended period" with `api.massive.com` available in parallel and the old one to be phased out. `[market_data] polygon_url` defaults to `https://api.massive.com`; set it to `https://api.polygon.io` to use the old host.
* Daily bars: `GET /v2/aggs/ticker/{stocksTicker}/range/1/day/{from}/{to}` with `adjusted=true`, `sort=asc` and `limit=50000`. The docs list it in all Stocks plans (Basic is end of day with two years of history). Each `results` row has `o`, `h`, `l`, `c`, `v`, `vw`, `n` and `t`, a millisecond timestamp, and a ticker with no data answers 200 with no `results`.
* Price: `GET /v2/aggs/ticker/{stocksTicker}/prev`, the previous trading day's bar, in all Stocks plans. `GET /v2/last/trade/{stocksTicker}` is documented as only in the Developer and Advanced plans, so the free plan's price is the previous close and `prev_close` is left empty.
* Limits: Basic allows 5 requests a minute and answers `429 {"status":"ERROR","error":"You've exceeded the maximum requests per minute..."}`. The client retries a 429 or a 5xx twice, waiting `Retry-After` when it is sent and otherwise 12 then 24 seconds for a 429 (1 then 2 for a 5xx). A 403 `NOT_AUTHORIZED` is a plan that does not cover the data.
* The tests check the header, that the key is not in the query string, the date range of the bars request, the retry waits, the unknown ticker and the open routine pricing its orders from the Polygon quote.

## Text message alerts

`alerts.py` sends each alert by email, to a chat webhook and, when `sms_to` and `sms_from` are set in `[alerts]`, as a text through Twilio. Written from Twilio's Messages documentation (read in Chromium on 2026-11-10). It has not been run against the live service and no text was sent to a phone.

* `POST https://api.twilio.com/2010-04-01/Accounts/{AccountSid}/Messages.json`, encoding `application/x-www-form-urlencoded`, with `To` (E.164), `From` (a number the account owns) and `Body` (up to 1,600 characters, which the client cuts to).
* HTTP basic auth with the account SID as the user name and the auth token as the password, read from `TWILIO_ACCOUNT_SID` and `TWILIO_AUTH_TOKEN` in the environment each time a text is sent. Neither is a config field, and a problem string never holds the token.
* Success is `201 Created` with the message resource. Twilio's documented error body is JSON with `code`, `message`, `more_info` and `status`, so a 4xx is reported as, for example, `sms: HTTP 400 (code 21608: ...)`. A trial account may only text numbers it has verified.
* A text that fails, a dead host and missing credentials are all returned in the problems list and logged as `alert_failed`; none of them raises, and each of the three channels is tried even when another failed.
* `twilio_url` in `[alerts]` replaces `https://api.twilio.com`; the tests use it to reach the local stand-in in `tests/fakes.py`, which checks the auth header, the form body, the body length and the error codes.

## Airtable run log

With `[airtable] enabled = true`, every routine run appends one row to an Airtable table through the REST API: `POST /v0/{baseId}/{tableIdOrName}` with a personal access token in the Authorization header (`AIRTABLE_TOKEN`, from the environment). Columns: Routine, Run at, Status (done, skipped or crashed), Orders suggested, Orders placed, Orders refused, Mode, Summary.

* Suggested is what the model or the rules asked for (premarket: the model's suggestions; open: every proposal considered, share and option; eod: the day's total). Placed and refused are the gateway's verdicts.
* Airtable allows 5 requests a second per base and says to wait 30 seconds after a 429, so the client does that once and then gives up.
* A log failure never fails a routine: it is written to the ledger as `runlog_failed` and the routine carries on.
* `python scripts/airtable_setup.py --base-id appXXXX` creates the table "Ledgerbell run log (test)" with those columns through the Metadata API and reuses it if it exists (`--workspace wspXXXX` makes a base instead). The approach is the same as the Lead intake lab setup in the admission-intake-automation repo.
* Notion is not covered.

[docs/real-run-captured.txt](real-run-captured.txt) is a real run: Finnhub's free API, the Alpaca paper account and an Airtable table in one session, then the table read back through the API.
