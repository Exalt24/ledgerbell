"""The desk: the one place an order can leave this program, and the kill switch beside it.

Everything that wants to trade (the example strategy, the model agent) hands a Proposal to
`Desk.execute`. That method runs the risk rules, attaches the stop, writes the ledger line and only then
calls the broker. Nothing else in the package calls an order endpoint.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime

from . import risk
from .alerts import Notifier
from .alpaca import AlpacaClient, AlpacaError, Position
from .config import Config
from .ledger import Ledger
from .risk import Proposal, Verdict


@dataclass(frozen=True)
class Outcome:
    proposal: Proposal
    verdict: Verdict
    submitted: bool
    order_id: str | None = None
    error: str | None = None


class Desk:
    def __init__(self, cfg: Config, client: AlpacaClient, ledger: Ledger, notifier: Notifier):
        self.cfg = cfg
        self.client = client
        self.ledger = ledger
        self.notifier = notifier

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

    def execute(self, proposal: Proposal, routine: str, today: date) -> Outcome:
        account = self.client.account()
        positions = self.client.positions()
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
        limit = self.cfg.risk.stop_loss_pct / 100
        cid = f"lb-{routine}-{today.isoformat()}-{pos.symbol}-stop"
        try:
            if pos.unrealized_plpc <= -limit:
                self.client.submit_exit(pos.symbol, pos.qty, cid)
                self.ledger.append(routine, "stop_breach_exit", symbol=pos.symbol, qty=pos.qty, plpc=pos.unrealized_plpc)
                return f"{pos.symbol}: no stop and already {pos.unrealized_plpc * 100:.1f}% down, closed"
            price = round(pos.avg_entry_price * (1 - limit), 2)
            self.client.submit_stop(pos.symbol, pos.qty, price, cid)
            self.ledger.append(routine, "stop_placed", symbol=pos.symbol, qty=pos.qty, stop_price=price)
            return f"{pos.symbol}: stop placed at {price:.2f}"
        except AlpacaError as exc:
            self.ledger.append(routine, "stop_failed", symbol=pos.symbol, error=str(exc))
            self.notifier.send(f"Stop missing on {pos.symbol}", str(exc))
            return f"{pos.symbol}: could not fix the stop ({exc})"
