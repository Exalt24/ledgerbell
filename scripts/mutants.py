# LIFESPAN: permanent
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
    ("risk.py", "kill switch refuses all", "    if kill_engaged:\n        return Verdict", "    if False:\n        return Verdict"),
    ("risk.py", "watchlist check", "    if proposal.symbol not in watchlist:", "    if False:"),
    ("risk.py", "loss limit at exactly the limit", "account.day_pl_pct <= -rules.daily_loss_limit_pct", "account.day_pl_pct < -rules.daily_loss_limit_pct"),
    ("risk.py", "buys refused after the loss limit", "    if daily_loss_breached(account, rules):", "    if False:"),
    ("risk.py", "size limit counts what is held", "room = cap_value - (held.market_value if held else 0.0)", "room = cap_value"),
    ("risk.py", "max open positions", "len(positions) >= rules.max_open_positions", "len(positions) > rules.max_open_positions"),
    ("risk.py", "no short selling", "if held is None or held.qty < 1:", "if False:"),
    ("risk.py", "stop sits under the price", "price * (1 - rules.stop_loss_pct / 100)", "price * (1 + rules.stop_loss_pct / 100)"),
    ("risk.py", "buying power cap", "proposal.price)\n    qty = min(proposal.qty, max_qty, affordable)", "proposal.price)\n    qty = min(proposal.qty, max_qty)"),
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
    # -- the options leg: one entry per rule of the gateway, plus the exit rule, the reads and the new services
    ("risk.py", "options off in the config refuses", "    if not rules.enabled:\n        return OptionVerdict", "    if False:\n        return OptionVerdict"),
    ("risk.py", "kill switch refuses an option entry", "    if kill_engaged:  # the option leg obeys the kill switch too", "    if False:  # the option leg obeys the kill switch too"),
    ("risk.py", "single leg only", "    if proposal.legs != 1:", "    if False:"),
    ("risk.py", "no short or naked option leg", '    if proposal.side != "buy":', "    if False:"),
    ("risk.py", "buy to open only", '    if proposal.intent != "open":', "    if False:"),
    ("risk.py", "contract symbol must parse", "    if occ is None:", "    if False:"),
    ("risk.py", "option underlying on the watchlist", "    if occ.underlying not in watchlist:", "    if False:"),
    ("risk.py", "expiry minimum", "    if days < rules.min_days_to_expiry:", "    if False:"),
    ("risk.py", "expiry maximum", "    if days > rules.max_days_to_expiry:", "    if False:"),
    ("risk.py", "standard multiplier only", "    if proposal.multiplier != 100:", "    if False:"),
    ("risk.py", "loss limit blocks option entries", "    if daily_loss_breached(account, risk):", "    if False:"),
    ("risk.py", "no entry while an option has no stop", "    if unprotected:", "    if False:"),
    ("risk.py", "premium cap counts what is held", " - held_option_premium(positions)", ""),
    ("risk.py", "premium cap sized on the limit", "per_contract = limit * proposal.multiplier", "per_contract = proposal.price * proposal.multiplier"),
    ("risk.py", "option cash cap", "affordable = math.floor(account.cash / per_contract)", "affordable = max_qty"),
    ("risk.py", "stop sits under the premium", "price * (1 - rules.stop_premium_pct / 100)", "price * (1 + rules.stop_premium_pct / 100)"),
    ("risk.py", "a premium too small for a stop is refused", "    if stop < 0.01:", "    if False:"),
    ("risk.py", "entry limit sits over the ask", "limit = round(proposal.price * (1 + rules.limit_buffer_pct / 100), 2)", "limit = round(proposal.price, 2)"),
    ("desk.py", "exit stop placed after the entry fills", 'stop = self.client.submit_option_stop(contract, qty, verdict.stop_price, f"{cid}-stop")', "stop = {}"),
    ("desk.py", "unfilled entry is pending, not stopped", '        if status != "filled":', "        if False:"),
    ("desk.py", "bare option positions block entries", "bare = [p.symbol for p in positions if is_option_symbol(p.symbol) and p.symbol not in stops]", "bare = []"),
    ("desk.py", "resting option entries block entries", 'resting = [o["symbol"] for o in open_orders if is_option_symbol(o.get("symbol", "")) and o.get("side") == "buy"]', "resting = []"),
    ("desk.py", "audit knows option stops", "option = is_option_symbol(pos.symbol)", "option = False"),
    ("desk.py", "share limits ignore option positions", "positions = [p for p in self.client.positions() if not is_option_symbol(p.symbol)]", "positions = self.client.positions()"),
    ("alpaca.py", "entry is buy to open", '"position_intent": "buy_to_open",', '"position_intent": "sell_to_open",'),
    ("alpaca.py", "contracts follow the page token", 'token = page.get("next_page_token") or page.get("page_token")', "token = None"),
    ("alpaca.py", "snapshots go 100 symbols at a time", "for i in range(0, len(symbols), 100):", "for i in range(0, len(symbols), 1000):"),
    ("alpaca.py", "contracts are asked for inside the window", '"expiration_date_lte": expiry_lte.isoformat(),', '"expiration_date_lte": expiry_gte.isoformat(),'),
    ("config.py", "stop of 100 percent is not a stop", "    if opt.stop_premium_pct >= 100:", "    if False:"),
    ("config.py", "premium cap above 5 percent refused", "    if opt.max_premium_pct > 5:", "    if False:"),
    ("config.py", "unknown market data provider refused", 'if market_data.provider not in {"alpaca", "finnhub"}:', "if False:"),
    ("config.py", "airtable log needs a base id", "if airtable.enabled and not airtable.base_id:", "if False:"),
    ("marketdata.py", "finnhub key goes in its documented header", '{"X-Finnhub-Token": key}', '{"X-Wrong-Token": key}'),
    ("marketdata.py", "finnhub zero quote is an error", 'if not q.get("c"):', "if False:"),
    ("marketdata.py", "finnhub 429 is retried", "if resp.status_code in {429, 500, 502, 503, 504} and attempt < self._retries:", "if False:"),
    ("routines.py", "no candles falls back to alpaca bars", "    except ProviderError as exc:\n        desk.ledger.append(\"data\", \"provider_fallback\"", "    except KeyError as exc:\n        desk.ledger.append(\"data\", \"provider_fallback\""),
    ("routines.py", "provider quotes price the orders", "prices.update(_live_prices(desk, watch))", "pass"),
    ("routines.py", "option legs only when options are on", "    if not desk.cfg.options.enabled:\n        return []", "    if False:\n        return []"),
    ("routines.py", "a dead run log never stops a routine", "except Exception as exc:  # noqa: BLE001 - the log is a convenience, the trading day is not", "except KeyboardInterrupt as exc:  # noqa: BLE001 - the log is a convenience, the trading day is not"),
    ("runlog.py", "airtable 429 waits and retries", "if resp.status_code == 429 and attempt == 0:", "if False:"),
    ("runlog.py", "table name is url encoded", "quote(self.table, safe='')", "self.table"),
    ("cli.py", "provider errors are an exit code", "except (AlpacaError, ProviderError) as exc:", "except KeyError as exc:"),
    ("routines.py", "midday scan uses the option stop share", "return desk.cfg.options.stop_premium_pct if is_option_symbol(symbol) else desk.cfg.risk.stop_loss_pct", "return desk.cfg.risk.stop_loss_pct"),
]


def run_suite(src: Path, tests: Path) -> bool:
    env = {**os.environ, "PYTHONPATH": str(src), "PYTHONDONTWRITEBYTECODE": "1"}
    done = subprocess.run([sys.executable, "-m", "pytest", "-x", "-q", "-p", "no:cacheprovider", str(tests)],
                          capture_output=True, text=True, env=env, cwd=tests.parent, check=False, timeout=240)
    return done.returncode == 0


def main() -> int:
    only = sys.argv[1] if len(sys.argv) > 1 else ""  # an optional substring of "file: label" to run a slice of the list
    caught = survived = stale = 0
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "copy"
        shutil.copytree(ROOT, work, ignore=shutil.ignore_patterns(".venv", ".git", "__pycache__", ".pytest_cache", "state", ".ruff_cache", "*.egg-info"))
        src, tests = work / "src", work / "tests"
        if not run_suite(src, tests):
            print("the suite is red before any change: fix that first")
            return 2
        for fname, label, old, new in MUTANTS:
            if only and only not in f"{fname}: {label}":
                continue
            path = src / "ledgerbell" / fname
            original = path.read_text(encoding="utf-8")
            if original.count(old) != 1:  # absent, or more than once so the first hit may not be the guard meant
                print(f"STALE   {fname}: {label} (anchor found {original.count(old)} times, it must be exactly once)")
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
    print(f"{caught + survived + stale} mutants: {caught} caught, {survived} survived, {stale} stale")
    return 0 if survived == 0 and stale == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
