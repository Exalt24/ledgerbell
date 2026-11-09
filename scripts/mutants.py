"""Remove or flip one guard at a time in a temp copy and run the suite: it must go red every time.

    python scripts/mutants.py

A mutant that survives means a rule has no test that notices it is gone. An anchor that no longer matches
the source is reported as stale and fails the run, so the list cannot rot quietly.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

MUTANTS = [
    ("risk.py", "kill switch refuses all", "    if kill_engaged:", "    if False:"),
    ("risk.py", "watchlist check", "    if proposal.symbol not in watchlist:", "    if False:"),
    ("risk.py", "loss limit at exactly the limit", "account.day_pl_pct <= -rules.daily_loss_limit_pct", "account.day_pl_pct < -rules.daily_loss_limit_pct"),
    ("risk.py", "buys refused after the loss limit", "    if daily_loss_breached(account, rules):", "    if False:"),
    ("risk.py", "size limit counts what is held", "room = cap_value - (held.market_value if held else 0.0)", "room = cap_value"),
    ("risk.py", "max open positions", "len(positions) >= rules.max_open_positions", "len(positions) > rules.max_open_positions"),
    ("risk.py", "no short selling", "if held is None or held.qty < 1:", "if False:"),
    ("risk.py", "stop sits under the price", "price * (1 - rules.stop_loss_pct / 100)", "price * (1 + rules.stop_loss_pct / 100)"),
    ("risk.py", "buying power cap", "qty = min(proposal.qty, max_qty, affordable)", "qty = min(proposal.qty, max_qty)"),
    ("desk.py", "kill flag written first", "        self.cfg.kill_file.write_text(json.dumps(info), encoding=\"utf-8\")\n", "        pass\n"),
    ("desk.py", "kill cancels orders", '("cancel_orders", self.client.cancel_all_orders), ', ""),
    ("desk.py", "kill closes positions", ', ("close_positions", self.client.close_all_positions)', ""),
    ("desk.py", "loss limit engages the kill", "        if risk.daily_loss_breached(account, self.cfg.risk):\n            self.engage_kill(", "        if False:\n            self.engage_kill("),
    ("desk.py", "missing stops are fixed", "            if pos.symbol in stops:", "            if True:"),
    ("desk.py", "stop already breached means exit", "if pos.unrealized_plpc <= -limit:", "if False:"),
    ("desk.py", "idempotent client order id", 'cid = f"lb-{routine}-{today.isoformat()}-{proposal.symbol}-{proposal.side}"', 'cid = f"lb-{routine}-{id(proposal)}"'),
    ("alpaca.py", "paper mode host guard", "        if trading != PAPER_HOST:", "        if False:"),
    ("alpaca.py", "live needs confirmation", "        if not cfg.live_confirmed:", "        if False:"),
    ("routines.py", "kill blocks trading routines", 'if desk.kill_engaged() and name in {"premarket", "open", "midday"}:', "if False:"),
    ("routines.py", "yesterday's plan is never replayed", 'if plan.get("date") != today:', "if False:"),
    ("routines.py", "closed market skips the open", 'if not desk.client.clock().get("is_open"):\n        return _skip(desk, "open"', 'if False:\n        return _skip(desk, "open"'),
    ("agent.py", "one re-ask with the error", "for attempt in range(2):", "for attempt in range(1):"),
    ("agent.py", "symbols must be on the watchlist", "if symbol not in watchlist:", "if False:"),
    ("dashboard.py", "html escaping", "return html.escape(str(x), quote=True)", "return str(x)"),
    ("strategy.py", "sell below the average", "if last < mean:", "if last > mean:"),
    ("cli.py", "unkill needs --yes", "        if not args.yes:", "        if False:"),
]


def run_suite(src: Path, tests: Path) -> bool:
    env = {**os.environ, "PYTHONPATH": str(src), "PYTHONDONTWRITEBYTECODE": "1"}
    done = subprocess.run([sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider", str(tests)],
                          capture_output=True, text=True, env=env, cwd=tests.parent, check=False, timeout=240)
    return done.returncode == 0


def main() -> int:
    caught = survived = stale = 0
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "copy"
        shutil.copytree(ROOT, work, ignore=shutil.ignore_patterns(".venv", ".git", "__pycache__", ".pytest_cache", "state", ".ruff_cache", "*.egg-info"))
        src, tests = work / "src", work / "tests"
        if not run_suite(src, tests):
            print("the suite is red before any change: fix that first")
            return 2
        for fname, label, old, new in MUTANTS:
            path = src / "ledgerbell" / fname
            original = path.read_text(encoding="utf-8")
            if old not in original:
                print(f"STALE   {fname}: {label} (anchor not found)")
                stale += 1
                continue
            path.write_text(original.replace(old, new, 1), encoding="utf-8")
            try:
                ok = run_suite(src, tests)
            finally:
                path.write_text(original, encoding="utf-8")
            if ok:
                survived += 1
                print(f"SURVIVED {fname}: {label}")
            else:
                caught += 1
                print(f"caught  {fname}: {label}")
    print(f"{len(MUTANTS)} mutants: {caught} caught, {survived} survived, {stale} stale")
    return 0 if survived == 0 and stale == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
