"""The hard risk rules. Pure functions: no network, no clock, so every rule has a test that breaks it."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date

from .alpaca import Account, Position
from .config import OptionRules, RiskRules
from .occ import is_option_symbol, parse_occ


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


# -- the options leg ---------------------------------------------------------------------------------------------------
# One shape is allowed: a single leg long call or put, bought to open, with a premium cap, an expiry window and an exit
# rule (a stop on the premium) attached to the entry. Everything else is refused with a named reason.


@dataclass(frozen=True)
class OptionProposal:
    contract: str  # OCC symbol, for example AAPL240119C00190000
    qty: int  # contracts, not shares
    price: float  # per share premium the proposer saw: the limit price of the entry
    reason: str
    multiplier: int = 100  # shares per contract; the standard contract is 100
    side: str = "buy"
    intent: str = "open"  # open or close
    legs: int = 1
    source: str = "strategy"

    @property
    def symbol(self) -> str:
        return self.contract


@dataclass(frozen=True)
class OptionVerdict:
    allowed: bool
    qty: int = 0
    limit_price: float | None = None
    stop_price: float | None = None
    reasons: list[str] = field(default_factory=list)


def option_stop_price(price: float, rules: OptionRules) -> float:
    return round(price * (1 - rules.stop_premium_pct / 100), 2)


def held_option_premium(positions: list[Position]) -> float:
    """Money tied up in option positions already held: contracts x current premium x 100 shares."""
    return sum(abs(p.qty) * p.current_price * 100 for p in positions if is_option_symbol(p.symbol))


def check_option(
    proposal: OptionProposal,
    account: Account,
    positions: list[Position],
    rules: OptionRules,
    risk: RiskRules,
    watchlist: tuple[str, ...],
    kill_engaged: bool,
    today: date,
    unprotected: tuple[str, ...] = (),
) -> OptionVerdict:
    """Decide one proposed option order. A refusal always carries its reason, and nothing here talks to a broker."""
    if not rules.enabled:
        return OptionVerdict(False, reasons=["options trading is switched off in the config"])
    if kill_engaged:  # the option leg obeys the kill switch too
        return OptionVerdict(False, reasons=["kill switch is engaged"])
    if proposal.legs != 1:
        return OptionVerdict(False, reasons=[f"{proposal.legs} leg option orders (spreads and combos) are not allowed, single leg only"])
    if proposal.side != "buy":
        return OptionVerdict(False, reasons=["selling an option is a short or naked leg and is not allowed, only buying to open is"])
    if proposal.intent != "open":
        return OptionVerdict(False, reasons=["only buy to open is allowed: positions are closed by the exit rule or the kill switch"])
    occ = parse_occ(proposal.contract)
    if occ is None:
        return OptionVerdict(False, reasons=[f"{proposal.contract!r} is not an option contract symbol"])
    if occ.underlying not in watchlist:
        return OptionVerdict(False, reasons=[f"{occ.underlying} is not on the watchlist"])
    days = (occ.expiry - today).days
    if days < rules.min_days_to_expiry:
        return OptionVerdict(False, reasons=[f"expires in {days} days, under the {rules.min_days_to_expiry} day minimum"])
    if days > rules.max_days_to_expiry:
        return OptionVerdict(False, reasons=[f"expires in {days} days, over the {rules.max_days_to_expiry} day maximum"])
    if proposal.multiplier != 100:
        return OptionVerdict(False, reasons=[f"non-standard contract (multiplier {proposal.multiplier}) is not allowed"])
    if not isinstance(proposal.qty, int) or proposal.qty < 1:
        return OptionVerdict(False, reasons=["quantity must be a whole number of contracts, at least 1"])
    if proposal.price <= 0:
        return OptionVerdict(False, reasons=["no usable quote for the contract"])
    if account.trading_blocked:
        return OptionVerdict(False, reasons=["the account is blocked from trading"])
    if daily_loss_breached(account, risk):
        return OptionVerdict(False, reasons=["daily loss limit is hit, no new buys today"])
    if unprotected:
        return OptionVerdict(False, reasons=[f"{', '.join(unprotected)} has no exit stop yet, no new option entry until it does"])
    stop = option_stop_price(proposal.price, rules)
    if stop < 0.01:
        return OptionVerdict(False, reasons=[f"premium {proposal.price:.2f} is too small to carry a stop at {rules.stop_premium_pct}% under it"])
    limit = round(proposal.price * (1 + rules.limit_buffer_pct / 100), 2)  # the most the entry can cost a share: sizing uses it
    per_contract = limit * proposal.multiplier
    room = account.equity * rules.max_premium_pct / 100 - held_option_premium(positions)
    max_qty = math.floor(room / per_contract)
    affordable = math.floor(account.cash / per_contract)
    qty = min(proposal.qty, max_qty, affordable)
    if qty < 1:
        why = "option premium limit leaves no room" if max_qty < 1 else "not enough cash"
        return OptionVerdict(False, reasons=[f"{why} for {proposal.contract} at up to {limit:.2f} a share"])
    notes = []
    if qty < proposal.qty:
        notes.append(f"reduced from {proposal.qty} to {qty} contracts to fit the {rules.max_premium_pct}% premium limit or cash")
    return OptionVerdict(True, qty=qty, limit_price=limit, stop_price=stop, reasons=notes)
