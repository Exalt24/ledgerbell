"""The hard risk rules. Pure functions: no network, no clock, so every rule has a test that breaks it."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from .alpaca import Account, Position
from .config import RiskRules


@dataclass(frozen=True)
class Proposal:
    symbol: str
    side: str  # "buy" or "sell"
    qty: int
    price: float  # the last price the proposer saw, used for sizing and for the stop
    reason: str
    source: str = "strategy"  # strategy or agent


@dataclass(frozen=True)
class Verdict:
    allowed: bool
    qty: int = 0
    stop_price: float | None = None
    reasons: list[str] = field(default_factory=list)


def daily_loss_breached(account: Account, rules: RiskRules) -> bool:
    """Equity is down at least the limit against yesterday's close."""
    return account.last_equity > 0 and account.day_pl_pct <= -rules.daily_loss_limit_pct


def stop_price(price: float, rules: RiskRules) -> float:
    return round(price * (1 - rules.stop_loss_pct / 100), 2)


def check(
    proposal: Proposal,
    account: Account,
    positions: list[Position],
    rules: RiskRules,
    watchlist: tuple[str, ...],
    kill_engaged: bool,
) -> Verdict:
    """Decide one proposed order. A refusal always carries its reason, and nothing here talks to a broker."""
    if kill_engaged:
        return Verdict(False, reasons=["kill switch is engaged"])
    if proposal.symbol not in watchlist:
        return Verdict(False, reasons=[f"{proposal.symbol} is not on the watchlist"])
    if proposal.side not in {"buy", "sell"}:
        return Verdict(False, reasons=[f"side {proposal.side!r} is not buy or sell"])
    if not isinstance(proposal.qty, int) or proposal.qty < 1:
        return Verdict(False, reasons=["quantity must be a whole number of shares, at least 1"])
    if proposal.price <= 0:
        return Verdict(False, reasons=["no usable price"])

    held = next((p for p in positions if p.symbol == proposal.symbol), None)

    if proposal.side == "sell":
        if held is None or held.qty < 1:
            return Verdict(False, reasons=["nothing held to sell, and short selling is not allowed"])
        qty = min(proposal.qty, held.qty)
        notes = [] if qty == proposal.qty else [f"reduced from {proposal.qty} to the {held.qty} held"]
        return Verdict(True, qty=qty, reasons=notes)

    # buys, every rule below can only refuse or shrink
    if account.trading_blocked:
        return Verdict(False, reasons=["the account is blocked from trading"])
    if daily_loss_breached(account, rules):
        return Verdict(False, reasons=[f"daily loss limit of {rules.daily_loss_limit_pct}% is hit, no new buys today"])
    if held is None and len(positions) >= rules.max_open_positions:
        return Verdict(False, reasons=[f"already holding {len(positions)} positions, the limit is {rules.max_open_positions}"])

    cap_value = account.equity * rules.max_position_pct / 100
    room = cap_value - (held.market_value if held else 0.0)
    max_qty = math.floor(room / proposal.price)
    affordable = math.floor(account.buying_power / proposal.price)
    qty = min(proposal.qty, max_qty, affordable)
    if qty < 1:
        why = "position size limit leaves no room" if max_qty < 1 else "not enough buying power"
        return Verdict(False, reasons=[f"{why} for {proposal.symbol} at {proposal.price:.2f}"])
    notes = []
    if qty < proposal.qty:
        notes.append(f"reduced from {proposal.qty} to {qty} to fit the {rules.max_position_pct}% size limit or buying power")
    return Verdict(True, qty=qty, stop_price=stop_price(proposal.price, rules), reasons=notes)
