"""A read-and-roundtrip check against a REAL Alpaca paper account (no real money, paper keys only).

    APCA_API_KEY_ID=... APCA_API_SECRET_KEY=... python scripts/paper_check.py

It reads the account, clock, positions and daily bars, places one 1 share entry with an attached stop,
reads the open orders back, then cancels everything and closes anything it opened. Outside market hours
the entry is accepted and queued, so cancelling it leaves nothing behind. Output is plain text to paste.
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

from ledgerbell import config as cfgmod
from ledgerbell.alpaca import AlpacaClient, AlpacaError


def main() -> int:
    key, secret = cfgmod.credentials()
    cfg = cfgmod.Config(mode="paper", state_dir=Path(tempfile.mkdtemp()) / "state")
    client = AlpacaClient(cfg, key, secret)
    acct = client.account()
    print(f"account: equity {acct.equity:.2f}, cash {acct.cash:.2f}, buying power {acct.buying_power:.2f}, blocked {acct.trading_blocked}")
    clock = client.clock()
    print(f"clock: is_open {clock['is_open']}, next_open {clock['next_open']}")
    print(f"positions: {len(client.positions())}, open orders: {len(client.open_orders())}")
    bars = client.daily_bars(["SPY", "AAPL"], limit=25)
    for sym, rows in bars.items():
        print(f"bars {sym}: {len(rows)} daily bars, last close {rows[-1]['c']} on {rows[-1]['t'][:10]}")
    ok = True
    try:
        cid = f"ledgerbell-paper-check-{int(time.time())}"
        stop = round(float(bars["SPY"][-1]["c"]) * 0.97, 2)
        order = client.submit_entry("SPY", 1, stop, cid)
        print(f"entry order: status {order['status']}, class {order.get('order_class')}, legs {len(order.get('legs') or [])}")
        print("open orders after the entry:", [(o["symbol"], o["side"], o["type"], o["status"]) for o in client.open_orders()])
        try:
            client.submit_entry("SPY", 1, stop, cid)
            print("duplicate client_order_id was ACCEPTED: reruns are not idempotent")
            ok = False
        except AlpacaError as exc:
            print(f"same client_order_id again: refused as expected ({exc})")
    except AlpacaError as exc:
        ok = False
        print(f"entry order refused: {exc}")
    cancelled = client.cancel_all_orders()
    print(f"cancel all: {len(cancelled)} orders cancelled")
    closed = client.close_all_positions()
    print(f"close all: {len(closed)} positions closed")
    print(f"after: positions {len(client.positions())}, open orders {len(client.open_orders())}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
