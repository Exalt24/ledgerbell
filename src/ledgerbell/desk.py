"""The desk: the one place an order can leave this program, and the kill switch beside it.

Everything that wants to trade (the example strategy, the model agent) hands a Proposal (a share order) or an
OptionProposal (one long call or put) to `Desk.execute`. That method runs the risk rules, attaches the stop, writes
the ledger line and only then calls the broker. Nothing else in the package calls an order endpoint.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime

from . import risk
from .alerts import Notifier
from .alpaca import AlpacaClient, AlpacaError, Position
from .config import Config
from .ledger import Ledger
from .marketdata import AlpacaData, MarketDataProvider
from .occ import is_option_symbol
from .risk import OptionProposal, OptionVerdict, Proposal, Verdict
from .runlog import RunLog


@dataclass(frozen=True)
class Outcome:
    proposal: Proposal | OptionProposal
    verdict: Verdict | OptionVerdict
    submitted: bool
    order_id: str | None = None
    error: str | None = None
    exit_placed: bool | None = None  # options only: True when the stop on the premium is live, False when it is pending


class Desk:
    def __init__(
        self, cfg: Config, client: AlpacaClient, ledger: Ledger, notifier: Notifier,
        data: MarketDataProvider | None = None, sleep: Callable[[float], None] = time.sleep, runlog: RunLog | None = None,
    ):
        self.cfg = cfg
        self.client = client
        self.ledger = ledger
        self.notifier = notifier
        self.data = data or AlpacaData(client)  # where prices come from: Alpaca's own feed unless the config says otherwise
        self.sleep = sleep
        self.runlog = runlog  # the Airtable run log, when the config turns it on

    # -- kill switch --------------------------------------------------------------------------

    def kill_engaged(self) -> bool:
        return self.cfg.kill_file.exists()

    def kill_info(self) -> dict | None:
        if not self.kill_engaged():
            return None
        try:
            return json.loads(self.cfg.kill_file.read_text(encoding="utf-8"))
        except ValueError:
            return {"reason": "KILL file present", "at": ""}  # a person touched the file by hand

    def engage_kill(self, reason: str, routine: str = "kill") -> dict:
        """Stop everything. The flag is written FIRST so a broker error half way cannot leave trading enabled."""
        info = {"reason": reason, "at": datetime.now(UTC).isoformat(timespec="seconds")}
        self.cfg.state_dir.mkdir(parents=True, exist_ok=True)
        self.cfg.kill_file.write_text(json.dumps(info), encoding="utf-8")
        problems: list[str] = []
        for label, call in (("cancel_orders", self.client.cancel_all_orders), ("close_positions", self.client.close_all_positions)):
            try:
                call()
            except Exception as exc:  # noqa: BLE001 - keep going: the next step still has to run
                problems.append(f"{label}: {exc}")
        self.ledger.append(routine, "kill_engaged", reason=reason, problems=problems)
        alert_problems = self.notifier.send(
            "KILL SWITCH ENGAGED", f"{reason}\nOpen orders cancelled and positions closed."
            + (f"\nProblems: {'; '.join(problems)}" if problems else "")
        )
        if alert_problems:
            self.ledger.append(routine, "alert_failed", problems=alert_problems)
        return {**info, "problems": problems}

    def clear_kill(self) -> bool:
        if not self.cfg.kill_file.exists():
            return False
        self.cfg.kill_file.unlink()
        self.ledger.append("unkill", "kill_cleared")
        return True

    def enforce_loss_limit(self, routine: str) -> bool:
        """Engage the kill switch when the day's loss limit is hit. Returns True when it just did."""
        if self.kill_engaged():
            return False
        account = self.client.account()
        if risk.daily_loss_breached(account, self.cfg.risk):
            self.engage_kill(
                f"daily loss limit hit: equity {account.equity:.2f} vs {account.last_equity:.2f} at yesterday's close "
                f"({account.day_pl_pct:.2f}%, limit {self.cfg.risk.daily_loss_limit_pct}%)",
                routine,
            )
            return True
        return False

    # -- the gateway --------------------------------------------------------------------------

    def execute(self, proposal: Proposal | OptionProposal, routine: str, today: date) -> Outcome:
        if isinstance(proposal, OptionProposal):
            return self._execute_option(proposal, routine, today)
        account = self.client.account()
        positions = [p for p in self.client.positions() if not is_option_symbol(p.symbol)]  # option premium has its own limit
        verdict = risk.check(
            proposal, account, positions, self.cfg.risk, self.cfg.strategy.watchlist, self.kill_engaged()
        )
        base = {
            "symbol": proposal.symbol, "side": proposal.side, "asked_qty": proposal.qty, "source": proposal.source,
            "reason": proposal.reason, "price": proposal.price,
        }
        if not verdict.allowed:
            self.ledger.append(routine, "order_refused", **base, why=verdict.reasons)
            return Outcome(proposal, verdict, False)
        cid = f"lb-{routine}-{today.isoformat()}-{proposal.symbol}-{proposal.side}"
        try:
            if proposal.side == "buy":
                assert verdict.stop_price is not None  # a buy without a stop is not possible by construction
                order = self.client.submit_entry(proposal.symbol, verdict.qty, verdict.stop_price, cid)
            else:
                order = self.client.submit_exit(proposal.symbol, verdict.qty, cid)
        except AlpacaError as exc:
            self.ledger.append(routine, "order_failed", **base, qty=verdict.qty, error=str(exc))
            return Outcome(proposal, verdict, False, error=str(exc))
        self.ledger.append(
            routine, "order_submitted", **base, qty=verdict.qty, stop_price=verdict.stop_price,
            order_id=order.get("id"), notes=verdict.reasons,
        )
        return Outcome(proposal, verdict, True, order_id=order.get("id"))

    # -- the options leg ----------------------------------------------------------------------

    def _unprotected_options(self, positions: list[Position], open_orders: list[dict]) -> tuple[str, ...]:
        """Option contracts that are held with no stop, or have an entry still resting: no new entry until they are covered."""
        stops = {o["symbol"] for o in open_orders if o.get("side") == "sell" and o.get("type") in {"stop", "stop_limit"}}
        bare = [p.symbol for p in positions if is_option_symbol(p.symbol) and p.symbol not in stops]
        resting = [o["symbol"] for o in open_orders if is_option_symbol(o.get("symbol", "")) and o.get("side") == "buy"]
        return tuple(dict.fromkeys(bare + resting))

    def _execute_option(self, proposal: OptionProposal, routine: str, today: date) -> Outcome:
        account = self.client.account()
        positions = self.client.positions()
        unprotected = self._unprotected_options(positions, self._flatten(self.client.open_orders()))
        verdict = risk.check_option(
            proposal, account, positions, self.cfg.options, self.cfg.risk, self.cfg.strategy.watchlist,
            self.kill_engaged(), today, unprotected,
        )
        base = {
            "asset": "option", "symbol": proposal.contract, "side": f"{proposal.side}_to_{'open' if proposal.intent == 'open' else 'close'}",
            "asked_qty": proposal.qty, "source": proposal.source, "reason": proposal.reason, "price": proposal.price,
        }
        if not verdict.allowed:
            self.ledger.append(routine, "order_refused", **base, why=verdict.reasons)
            return Outcome(proposal, verdict, False)
        assert verdict.limit_price is not None and verdict.stop_price is not None  # an entry without an exit is not possible by construction
        cid = f"lb-{routine}-{today.isoformat()}-{proposal.contract}-{proposal.side}"
        try:
            order = self.client.submit_option_entry(proposal.contract, verdict.qty, verdict.limit_price, cid)
        except AlpacaError as exc:
            self.ledger.append(routine, "order_failed", **base, qty=verdict.qty, error=str(exc))
            return Outcome(proposal, verdict, False, error=str(exc))
        self.ledger.append(
            routine, "order_submitted", **base, qty=verdict.qty, limit_price=verdict.limit_price, stop_price=verdict.stop_price,
            order_id=order.get("id"), notes=verdict.reasons,
        )
        placed = self._attach_option_exit(order, proposal.contract, verdict, cid, routine)
        return Outcome(proposal, verdict, True, order_id=order.get("id"), exit_placed=placed)

    def _attach_option_exit(self, order: dict, contract: str, verdict: OptionVerdict, cid: str, routine: str) -> bool:
        """Alpaca has no OTO leg for options, so the stop on the premium is sent as soon as the entry has filled.

        A buy limit at the ask normally fills within a second in market hours: wait a few short polls for it. If it has not
        filled (the market is closed, say), the entry rests with no exit; the next entry is refused until the stop exists,
        and the open and midday audits place it as soon as the position shows up.
        """
        status, filled = order.get("status"), order.get("filled_qty")
        for _ in range(6):
            if status == "filled":
                break
            self.sleep(0.25)
            order = self.client.get_order(order["id"])
            status, filled = order.get("status"), order.get("filled_qty")
        if status != "filled":
            self.ledger.append(routine, "option_exit_pending", symbol=contract, entry_status=status, stop_price=verdict.stop_price)
            return False
        qty = int(float(filled or verdict.qty))
        try:
            stop = self.client.submit_option_stop(contract, qty, verdict.stop_price, f"{cid}-stop")
        except AlpacaError as exc:
            self.ledger.append(routine, "option_exit_failed", symbol=contract, qty=qty, error=str(exc))
            self.notifier.send(f"Option entry on {contract} has no stop", str(exc))
            return False
        self.ledger.append(routine, "option_exit_placed", symbol=contract, qty=qty, stop_price=verdict.stop_price, order_id=stop.get("id"))
        return True

    # -- stops --------------------------------------------------------------------------------

    @staticmethod
    def _flatten(orders: list[dict]) -> list[dict]:
        flat: list[dict] = []
        for o in orders:
            flat.append(o)
            flat.extend(Desk._flatten(o.get("legs") or []))
        return flat

    def ensure_stops(self, routine: str, today: date) -> list[str]:
        """Every held position must have a live stop order. Missing: place it, or exit if it is already through it."""
        notes: list[str] = []
        stops = {
            o["symbol"]
            for o in self._flatten(self.client.open_orders())
            if o.get("side") == "sell" and o.get("type") in {"stop", "stop_limit"}
        }
        for pos in self.client.positions():
            if pos.symbol in stops:
                continue
            notes.append(self._fix_missing_stop(pos, routine, today))
        return notes

    def _fix_missing_stop(self, pos: Position, routine: str, today: date) -> str:
        option = is_option_symbol(pos.symbol)
        limit = (self.cfg.options.stop_premium_pct if option else self.cfg.risk.stop_loss_pct) / 100
        cid = f"lb-{routine}-{today.isoformat()}-{pos.symbol}-stop"
        try:
            if pos.unrealized_plpc <= -limit:
                (self.client.submit_option_close if option else self.client.submit_exit)(pos.symbol, pos.qty, cid)
                self.ledger.append(routine, "stop_breach_exit", symbol=pos.symbol, qty=pos.qty, plpc=pos.unrealized_plpc)
                return f"{pos.symbol}: no stop and already {pos.unrealized_plpc * 100:.1f}% down, closed"
            price = round(pos.avg_entry_price * (1 - limit), 2)
            (self.client.submit_option_stop if option else self.client.submit_stop)(pos.symbol, pos.qty, price, cid)
            self.ledger.append(routine, "stop_placed", symbol=pos.symbol, qty=pos.qty, stop_price=price)
            return f"{pos.symbol}: stop placed at {price:.2f}"
        except AlpacaError as exc:
            self.ledger.append(routine, "stop_failed", symbol=pos.symbol, error=str(exc))
            self.notifier.send(f"Stop missing on {pos.symbol}", str(exc))
            return f"{pos.symbol}: could not fix the stop ({exc})"
