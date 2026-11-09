"""Command line: run a routine, check status, pull the kill switch, build the dashboard, start the stand-in."""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime
from pathlib import Path

from . import config as cfgmod
from . import dashboard, options, standin
from .agent import ClaudeCodeRunner
from .alerts import Notifier
from .alpaca import AlpacaClient, AlpacaError
from .desk import Desk
from .ledger import Ledger
from .marketdata import ProviderError, build_provider
from .routines import ROUTINES, run
from .runlog import RunLog


def build_desk(cfg: cfgmod.Config) -> Desk:
    key, secret = cfgmod.credentials()
    client = AlpacaClient(cfg, key, secret)
    runlog = None
    if cfg.airtable.enabled:
        runlog = RunLog(cfg.airtable.base_id, cfg.airtable.table, cfgmod.secret_from_env("AIRTABLE_TOKEN"), cfg.airtable.api_url)
    return Desk(cfg, client, Ledger(cfg.ledger_file), Notifier(cfg.alerts), data=build_provider(cfg, client), runlog=runlog)


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
    o = sub.add_parser("options", help="list option contracts for a symbol with price, implied volatility and greeks (read only)")
    o.add_argument("symbol")
    o.add_argument("--right", choices=("call", "put"))
    o.add_argument("--rows", type=int, default=12, help="how many contracts to print")
    q = sub.add_parser("quote", help="latest price from the configured market data provider")
    q.add_argument("symbol")
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
    try:
        return _command(args, cfg, desk)
    except (AlpacaError, ProviderError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


def _command(args: argparse.Namespace, cfg: cfgmod.Config, desk: Desk) -> int:
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
    if args.cmd == "quote":
        quote = desk.data.quote(args.symbol.upper())
        change = "" if quote.change_pct is None else f"  ({quote.change_pct:+.2f}% on the previous close {quote.prev_close})"
        print(f"{quote.symbol} {quote.price} from {quote.source}{change}")
        return 0
    if args.cmd == "options":
        symbol = args.symbol.upper()
        today = datetime.now(UTC).date()
        spot = float(desk.client.latest_trade(symbol)["p"])
        rows = options.chain(desk.client, symbol, today, cfg.options, spot=spot, right=args.right)
        lo, hi = options.expiry_window(today, cfg.options)
        print(f"{symbol} last {spot:.2f}; {len(rows)} tradable contracts expiring {lo} to {hi}, strikes within 10 percent; feed {cfg.options.feed}")
        for contract, snap in rows[: args.rows]:
            print("  " + options.describe(contract, snap))
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
