"""The options leg, written from Alpaca's options documentation.

Contracts come from the trading API (`GET /v2/options/contracts`, filtered by underlying and an expiry window).
Prices, implied volatility and greeks come from the market data API (`GET /v1beta1/options/snapshots`, the free
`indicative` feed by default). An option symbol is the OCC form: root, six digit expiry (YYMMDD), C or P, then the
strike times 1000 in eight digits, for example AAPL240119C00190000.

Nothing here places an order. An order goes through `Desk.execute`, which refuses everything but a single leg long
call or put bought to open.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import date, timedelta

from .alpaca import AlpacaClient
from .config import OptionRules
from .risk import OptionProposal


@dataclass(frozen=True)
class Contract:
    symbol: str
    underlying: str
    expiry: date
    right: str
    strike: float
    tradable: bool
    multiplier: int
    open_interest: int | None
    close_price: float | None


@dataclass(frozen=True)
class Snapshot:
    symbol: str
    bid: float | None
    ask: float | None
    last: float | None
    iv: float | None
    delta: float | None
    gamma: float | None
    theta: float | None
    vega: float | None
    rho: float | None

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None or self.ask <= 0:
            return None
        return round((self.bid + self.ask) / 2, 4)


def _f(value) -> float | None:
    try:
        return None if value is None else float(value)
    except (TypeError, ValueError):
        return None


def parse_contract(raw: dict) -> Contract:
    return Contract(
        symbol=raw["symbol"],
        underlying=raw["underlying_symbol"],
        expiry=date.fromisoformat(raw["expiration_date"]),
        right=raw["type"],
        strike=float(raw["strike_price"]),
        tradable=bool(raw.get("tradable", False)),
        multiplier=int(float(raw.get("multiplier") or raw.get("size") or 100)),
        open_interest=None if raw.get("open_interest") is None else int(float(raw["open_interest"])),
        close_price=_f(raw.get("close_price")),
    )


def parse_snapshot(symbol: str, raw: dict) -> Snapshot:
    quote, trade, greeks = raw.get("latestQuote") or {}, raw.get("latestTrade") or {}, raw.get("greeks") or {}
    return Snapshot(
        symbol=symbol,
        bid=_f(quote.get("bp")),
        ask=_f(quote.get("ap")),
        last=_f(trade.get("p")),
        iv=_f(raw.get("impliedVolatility")),
        delta=_f(greeks.get("delta")),
        gamma=_f(greeks.get("gamma")),
        theta=_f(greeks.get("theta")),
        vega=_f(greeks.get("vega")),
        rho=_f(greeks.get("rho")),
    )


def expiry_window(today: date, rules: OptionRules) -> tuple[date, date]:
    return today + timedelta(days=rules.min_days_to_expiry), today + timedelta(days=rules.max_days_to_expiry)


def list_contracts(
    client: AlpacaClient, underlying: str, today: date, rules: OptionRules, right: str | None = None,
    strike_gte: float | None = None, strike_lte: float | None = None,
) -> list[Contract]:
    """Active contracts for one underlying that expire inside the configured window, soonest and lowest strike first."""
    lo, hi = expiry_window(today, rules)
    raw = client.option_contracts(underlying, lo, hi, right, strike_gte, strike_lte)
    contracts = [parse_contract(c) for c in raw]
    return sorted(contracts, key=lambda c: (c.expiry, c.right, c.strike))


def snapshots(client: AlpacaClient, symbols: list[str], rules: OptionRules) -> dict[str, Snapshot]:
    """Latest quote, trade, implied volatility and greeks per contract symbol. A symbol with no data is absent."""
    raw = client.option_snapshots(symbols, rules.feed)
    return {sym: parse_snapshot(sym, body) for sym, body in raw.items()}


def chain(
    client: AlpacaClient, underlying: str, today: date, rules: OptionRules, spot: float | None = None,
    width_pct: float = 10.0, right: str | None = None,
) -> list[tuple[Contract, Snapshot | None]]:
    """Contracts with their snapshots. With a spot price, only strikes within width_pct of it are fetched."""
    lo = hi = None
    if spot:
        lo, hi = spot * (1 - width_pct / 100), spot * (1 + width_pct / 100)
    contracts = [c for c in list_contracts(client, underlying, today, rules, right, lo, hi) if c.tradable]
    snaps = snapshots(client, [c.symbol for c in contracts], rules) if contracts else {}
    return [(c, snaps.get(c.symbol)) for c in contracts]


def entry_price(snap: Snapshot | None) -> float | None:
    """What a buy to open is priced at: the ask, because a buy limit at the ask fills at once or not at all."""
    if snap is None or snap.ask is None or snap.ask <= 0:
        return None
    return snap.ask


def pick_call(rows: list[tuple[Contract, Snapshot | None]], spot: float) -> tuple[Contract, Snapshot] | None:
    """The soonest expiry in the window, then the strike closest to the money, among calls that have a usable ask."""
    usable = [(c, s) for c, s in rows if c.right == "call" and entry_price(s) is not None]
    if not usable:
        return None
    soonest = min(c.expiry for c, _ in usable)
    same = [(c, s) for c, s in usable if c.expiry == soonest]
    return min(same, key=lambda cs: abs(cs[0].strike - spot))


def call_for_signal(
    client: AlpacaClient, symbol: str, spot: float, equity: float, today: date, rules: OptionRules, reason: str,
) -> OptionProposal | None:
    """A one contract long call proposal for a stock signal, or None when the chain has nothing usable."""
    rows = chain(client, symbol, today, rules, spot=spot, right="call")
    picked = pick_call(rows, spot)
    if picked is None:
        return None
    contract, snap = picked
    price = entry_price(snap)
    assert price is not None
    cap_value = equity * rules.max_premium_pct / 100
    qty = max(1, math.floor(cap_value / (price * contract.multiplier)))
    return OptionProposal(
        contract=contract.symbol, qty=qty, price=price, reason=f"{reason}; {contract.symbol} delta {snap.delta}", multiplier=contract.multiplier,
        source="strategy",
    )


def describe(contract: Contract, snap: Snapshot | None) -> str:
    def fmt(v, spec):
        return "n/a" if v is None else format(v, spec)

    s = snap
    return (
        f"{contract.symbol}  {contract.right:<4} strike {contract.strike:>8.2f}  exp {contract.expiry.isoformat()}  "
        f"bid {fmt(s and s.bid, '.2f')} ask {fmt(s and s.ask, '.2f')} last {fmt(s and s.last, '.2f')}  "
        f"iv {fmt(s and s.iv, '.3f')}  delta {fmt(s and s.delta, '+.3f')} gamma {fmt(s and s.gamma, '.4f')} "
        f"theta {fmt(s and s.theta, '+.3f')} vega {fmt(s and s.vega, '.3f')}"
    )

