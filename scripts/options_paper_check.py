# LIFESPAN: permanent
"""A read-and-roundtrip check of the options leg against a REAL Alpaca paper account (no real money, paper keys only).

    APCA_API_KEY_ID=... APCA_API_SECRET_KEY=... python scripts/options_paper_check.py [SYMBOL] [--place]

It reads the account and clock, lists the option contracts for the symbol inside the expiry window, reads their
snapshots (price, implied volatility, greeks) from the free indicative feed and shows which contract the example
signal would pick. With --place it sends one long call through the real gateway (the risk rules, a limit at the ask,
the stop on the premium once the entry fills), reads the orders back, then cancels everything and closes anything
it opened. Without --place nothing is ordered. Output is plain text to paste.
"""

from __future__ import annotations

import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path

from ledgerbell import config as cfgmod
from ledgerbell import options
from ledgerbell.alerts import Notifier
from ledgerbell.alpaca import AlpacaClient
from ledgerbell.desk import Desk
from ledgerbell.ledger import Ledger


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    place = "--place" in sys.argv
    symbol = (args[0] if args else "SPY").upper()
    key, secret = cfgmod.credentials()
    state = Path(tempfile.mkdtemp()) / "state"
    cfg = cfgmod.Config(mode="paper", state_dir=state, options=cfgmod.OptionRules(enabled=True))
    client = AlpacaClient(cfg, key, secret)
    raw = client._call("GET", client._t("/v2/account"))
    acct = client.account()
    print(f"account: equity {acct.equity:.2f}, cash {acct.cash:.2f}, options approved level {raw.get('options_approved_level')}, "
          f"trading level {raw.get('options_trading_level')}, options buying power {raw.get('options_buying_power')}")
    clock = client.clock()
    print(f"clock: is_open {clock['is_open']}, next_open {clock['next_open']}")
    today = datetime.now(UTC).date()
    spot = float(client.latest_trade(symbol)["p"])
    lo, hi = options.expiry_window(today, cfg.options)
    contracts = options.list_contracts(client, symbol, today, cfg.options)
    print(f"{symbol} last {spot:.2f}; {len(contracts)} active contracts expiring {lo} to {hi}")
    rows = options.chain(client, symbol, today, cfg.options, spot=spot, width_pct=3.0, right="call")
    with_data = [(c, s) for c, s in rows if s is not None]
    print(f"calls within 3 percent of the price: {len(rows)}, with a snapshot: {len(with_data)} (feed {cfg.options.feed})")
    for c, s in rows[:6]:
        print("  " + options.describe(c, s))
    picked = options.pick_call(rows, spot)
    if picked is None:
        print("no call with a usable ask: nothing to propose")
        return 0
    contract, snap = picked
    print(f"picked: {contract.symbol} ask {snap.ask} iv {snap.iv} delta {snap.delta}")
    desk = Desk(cfg, client, Ledger(cfg.ledger_file), Notifier(cfg.alerts), sleep=lambda s: None)
    proposal = options.call_for_signal(client, symbol, spot, acct.equity, today, cfg.options, "paper check")
    assert proposal is not None
    if not place:
        print("dry run: no order sent (pass --place to send one through the gateway)")
        return 0
    ok = True
    routine = f"paper_check_{int(time.time())}"  # the order id is built from it, so each check gets its own
    out = desk.execute(proposal, routine, today)
    verdict = out.verdict
    print(f"gateway: allowed {verdict.allowed}, qty {verdict.qty}, limit {getattr(verdict, 'limit_price', None)}, "
          f"stop {getattr(verdict, 'stop_price', None)}, notes {verdict.reasons}")
    print(f"submitted {out.submitted}, order {out.order_id}, exit stop placed {out.exit_placed}, error {out.error}")
    orders = client.open_orders()
    print("open orders:", [(o["symbol"], o["side"], o["type"], o["status"], o.get("stop_price") or o.get("limit_price")) for o in orders])
    print("positions:", [(p.symbol, p.qty, p.avg_entry_price) for p in client.positions()])
    again = desk.execute(proposal, routine + "b", today)
    print(f"a second contract right after: submitted {again.submitted}, error {again.error}, reasons {again.verdict.reasons}")
    ok = ok and not again.submitted
    print("ledger:", [e["event"] for e in desk.ledger.read()])
    print(f"cancel all: {len(client.cancel_all_orders())} orders cancelled")
    print(f"close all: {len(client.close_all_positions())} positions closed")
    for _ in range(10):  # a cancel is asynchronous at the broker: read the book until it settles
        if not client.open_orders():
            break
        time.sleep(0.3)
    print(f"after: positions {len(client.positions())}, open orders {len(client.open_orders())}")
    return 0 if ok and out.submitted else 1


if __name__ == "__main__":
    sys.exit(main())
