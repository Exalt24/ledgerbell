"""A full made-up trading day against the stand-in server, printed as it happens.

    python scripts/demo.py             # the research step uses a scripted model answer
    python scripts/demo.py --claude    # the research step calls Claude Code headless (needs the claude command)

It starts the stand-in Alpaca on a free port, runs premarket, open, midday and eod, then pushes the price
tape down hard so the daily loss limit trips the kill switch, then shows that the next open is refused.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from ledgerbell import routines, standin
from ledgerbell.agent import ClaudeCodeRunner
from ledgerbell.alerts import Notifier
from ledgerbell.alpaca import AlpacaClient
from ledgerbell.config import AlertSettings, Config
from ledgerbell.desk import Desk
from ledgerbell.ledger import Ledger

NOW = datetime(2026, 11, 9, 14, 30, tzinfo=UTC)  # 09:30 in New York on a Monday

SCRIPTED = json.dumps({
    "summary": "Index funds sit above their 20 day average and the two tech names are drifting down. Nothing unusual before the open.",
    "proposals": [{"symbol": "QQQ", "side": "buy", "qty": 4, "reason": "closing above its 20 day average"}],
})


class Scripted:
    def run(self, prompt: str) -> str:
        return SCRIPTED


class Mailbox:
    """Alerts are printed instead of sent."""

    def post(self, url, json=None, timeout=None):
        print(f"    [text alert] {json['text'].splitlines()[0]}")

        class R:
            status_code = 200

        return R()


def say(title: str) -> None:
    print(f"\n== {title}")


def show(res: routines.Result) -> None:
    print(f"{res.routine}: {res.status}: {res.summary}")
    for line in res.lines:
        print(f"    {line}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--claude", action="store_true")
    args = ap.parse_args()
    server, book = standin.start()
    url = f"http://127.0.0.1:{server.server_port}"
    with tempfile.TemporaryDirectory() as tmp:
        cfg = Config(mode="standin", base_url=url, data_url=url, state_dir=Path(tmp) / "state",
                     alerts=AlertSettings(webhook_url="http://alerts.invalid/hook"))
        desk = Desk(cfg, AlpacaClient(cfg, standin.KEY, standin.SECRET), Ledger(cfg.ledger_file),
                    Notifier(cfg.alerts, http_post=Mailbox().post))
        runner = ClaudeCodeRunner() if args.claude else Scripted()
        say("08:00 premarket: the model reads the account and the tape")
        show(routines.run("premarket", desk, NOW, runner))
        say("09:35 open: the plan and the rule go through the risk rules, every entry gets a stop")
        show(routines.run("open", desk, NOW, None))
        say("12:00 midday: loss limit check, stop audit, scan")
        show(routines.run("midday", desk, NOW, None))
        say("16:15 eod: recap and dashboard")
        show(routines.run("eod", desk, NOW, None))
        say("next day: the market drops 40 percent in every name held")
        book.set_prices({s: p * 0.6 for s, p in book.prices.items()})
        show(routines.run("midday", desk, NOW, None))
        print(f"    kill switch engaged: {desk.kill_engaged()}  reason: {desk.kill_info()['reason']}")
        say("the next open is refused while the kill switch is engaged")
        show(routines.run("open", desk, NOW, None))
        say("what is left in the account and in the ledger")
        print(f"    positions: {desk.client.positions()}  open orders: {len(desk.client.open_orders())}")
        events = [e["event"] for e in desk.ledger.read()]
        print("    ledger events:", ", ".join(sorted(set(events))))
    server.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
