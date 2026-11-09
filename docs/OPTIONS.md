# The options leg

One shape of option order can leave this program: a single leg long call or put, bought to open, with a premium cap, an expiry window and a stop on the premium. Everything else is refused by `risk.check_option` with a named reason, before the broker is called. The leg is off until `[options] enabled = true` is set in the config.

## What Alpaca's documentation says (read in Chromium on 2026-11-10)

* Contracts: `GET /v2/options/contracts?underlying_symbols=` on the trading API. "By default only active contracts that expire before the upcoming weekend are returned", so the client always sends `expiration_date_gte` and `expiration_date_lte`. The response pages with `next_page_token`.
* Snapshots: `GET /v1beta1/options/snapshots?symbols=` on the market data API, "a comma-separated list of contract symbols with a limit of 100". It returns the latest trade, latest quote, `impliedVolatility` and the greeks. `feed` is `opra` (a subscription) or `indicative` ("a free indicative feed where trades are delayed and quotes are modified"), so the config default is `indicative`.
* Orders go through the same `POST /v2/orders`. For options, "time_in_force must be day or gtc", "qty is a whole number", no notional, no extended hours, and "stop and stop_limit are only available for single-leg orders".
* Order classes: "Equity trading: simple (or ""), oco, oto, bracket. Options trading: simple (or "") and mleg". There is no OTO or bracket for an option, so the stop cannot ride on the entry the way it does for shares.
* Trading levels: level 2 may buy a call or a put, level 3 adds spreads. The paper account used for the real run reported approved level 3.

## The gateway rules

| Rule | Refusal |
|---|---|
| `[options] enabled` is true | options trading is switched off in the config |
| Kill switch off | kill switch is engaged |
| One leg | N leg option orders (spreads and combos) are not allowed |
| Buy side only | selling an option is a short or naked leg and is not allowed |
| Open intent only | only buy to open is allowed: positions are closed by the exit rule or the kill switch |
| Symbol is a real OCC contract | not an option contract symbol |
| Underlying on the watchlist | not on the watchlist |
| Expiry at least `min_days_to_expiry` away | expires in N days, under the minimum |
| Expiry at most `max_days_to_expiry` away | expires in N days, over the maximum |
| Standard 100 share contract | non-standard contract is not allowed |
| Whole contracts, a usable quote, account not blocked, daily loss limit not hit | one reason each |
| A stop on the premium can be priced (at least 0.01) | premium too small to carry a stop |
| No held option without a stop and no entry still resting | no exit stop yet, no new option entry until it does |
| Premium cap | cut to fit `max_premium_pct` of equity (premium already held counts), or refused |
| Cash | cut to what cash buys, or refused |

The entry is a buy limit at the ask seen plus `limit_buffer_pct`, because the free feed's quotes are modified and a limit at the quoted ask rested unfilled on the first real try. Sizing and the cap use that limit, the worst the entry can cost.

## The exit rule

Alpaca has no OTO leg for options, so the exit is sent as soon as the entry has filled: `Desk._attach_option_exit` polls the order a few times at 250 ms, then sends a sell to close stop (`stop_premium_pct` under the entry, `gtc`). Two things keep a missed exit from being a hole:

* An entry that has not filled, or a stop that failed to send, is logged (`option_exit_pending`, `option_exit_failed`) and the next option entry is refused until every held contract has a stop.
* The open and midday audits (`ensure_stops`) place the stop on any option position that has none, at the option stop share, or close the position if it is already through it.

Whether the stop fires is the broker's job once it is placed, the same as for shares. On the stand-in server a premium pushed through the stop closes the position, and a test pins that.

## Reading the chain

```
ledgerbell options SPY --right call --rows 5
python scripts/options_paper_check.py SPY            # read only, real paper account
python scripts/options_paper_check.py SPY --place    # one contract through the gateway, then cancel all and close all
```

[docs/options-paper-run-captured.txt](options-paper-run-captured.txt) is the output of the last command on a Monday during market hours: 3804 active contracts in the window, 516 calls within 3 percent of the price, every one with a snapshot, one contract bought at 7.16 through the gateway with its stop at 3.54, a second contract refused by the premium cap, then everything closed. The first filled round trip on that account moved equity from 100000.00 to 99997.95, which is the spread between the ask paid and the bid received.

## What the stand-in server models and does not

It serves the same endpoints and field names as the documentation, builds a made-up chain around each price, rejects what Alpaca documents as invalid for options (fractional quantity, notional, extended hours, a bad time in force, an order class other than simple, a position intent that does not match the side, an uncovered sell, a level too low) and fires a stop when a premium falls through it. It does not model implied volatility surfaces, assignment, exercise, early expiry handling or fees.
