"""Command line: run a routine, check status, pull the kill switch, build the dashboard, start the stand-in."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import config as cfgmod
from . import dashboard, standin
from .agent import ClaudeCodeRunner
from .alerts import Notifier
from .alpaca import AlpacaClient
from .desk import Desk
from .ledger import Ledger
from .routines import ROUTINES, run


def build_desk(cfg: cfgmod.Config) -> Desk:
    key, secret = cfgmod.credentials()
    return Desk(cfg, AlpacaClient(cfg, key, secret), Ledger(cfg.ledger_file), Notifier(cfg.alerts))


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="ledgerbell", description=__doc__)
    p.add_argument("--config", default="config.toml", help="path to the TOML settings file")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="run one scheduled routine")
    r.add_argument("routine", choices=ROUTINES)
    r.add_argument("--no-model", action="store_true", help="skip the Claude Code research step")
    sub.add_parser("status", help="print the account, positions and kill switch state")
    k = sub.add_parser("kill", help="engage the kill switch: cancel orders, close positions, block trading")
    k.add_argument("--reason", default="manual kill")
    u = sub.add_parser("unkill", help="clear the kill switch (a person decides this, nothing else does)")
    u.add_argument("--yes", action="store_true", help="required: confirms a person is clearing it")
    d = sub.add_parser("dashboard", help="write the HTML dashboard")
    d.add_argument("--out", default="state/dashboard.html")
    s = sub.add_parser("standin", help="start the local stand-in Alpaca server")
    s.add_argument("--port", type=int, default=8765)
    return p


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.cmd == "standin":
        standin.serve_forever(args.port)
        return 0
    try:
        cfg = cfgmod.load(args.config)
        desk = build_desk(cfg)
    except (cfgmod.ConfigError, OSError) as exc:
        print(f"config problem: {exc}", file=sys.stderr)
        return 2
    if args.cmd == "run":
        result = run(args.routine, desk, runner=None if args.no_model else ClaudeCodeRunner())
        print(f"{result.routine}: {result.status}: {result.summary}")
        for line in result.lines:
            print(f"  {line}")
        return 3 if result.status == "skipped" else 0
    if args.cmd == "status":
        a = desk.client.account()
        print(f"equity {a.equity:.2f}  day {a.day_pl:+.2f} ({a.day_pl_pct:+.2f}%)  cash {a.cash:.2f}")
        for p in desk.client.positions():
            print(f"  {p.symbol} {p.qty} @ {p.avg_entry_price:.2f}  now {p.current_price:.2f}  {p.unrealized_plpc * 100:+.2f}%")
        info = desk.kill_info()
        print(f"kill switch: {'ENGAGED, ' + info['reason'] if info else 'off'}")
        return 0
    if args.cmd == "kill":
        info = desk.engage_kill(args.reason)
        print("kill switch engaged" + (f", with problems: {info['problems']}" if info["problems"] else ""))
        return 1 if info["problems"] else 0
    if args.cmd == "unkill":
        if not args.yes:
            print("refused: pass --yes to confirm a person is clearing the kill switch", file=sys.stderr)
            return 2
        print("kill switch cleared" if desk.clear_kill() else "kill switch was not engaged")
        return 0
    if args.cmd == "dashboard":
        html = dashboard.render(desk.client.account(), desk.client.positions(), desk.ledger.read(limit=40), desk.kill_info())
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")
        print(f"wrote {out}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
