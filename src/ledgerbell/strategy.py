"""The example rule. The owner sets the real strategy; this one only shows where it plugs in.

Rule: buy a watchlist symbol that is not held when its last daily close is above its simple moving average;
sell what is held when the last close falls below it. It makes no claim about returns.
"""

from __future__ import annotations

import math

from .alpaca import Position
from .config import RiskRules, StrategyRules
from .risk import Proposal


def sma(values: list[float], days: int) -> float:
    return sum(values[-days:]) / days


def rule_proposals(
    bars: dict[str, list[dict]], positions: list[Position], equity: float, risk: RiskRules, strategy: StrategyRules
) -> list[Proposal]:
    held = {p.symbol: p for p in positions}
    out: list[Proposal] = []
    for symbol in strategy.watchlist:
        closes = [float(b["c"]) for b in bars.get(symbol, [])]
        if len(closes) < strategy.sma_days:
            continue  # not enough history: no opinion, which is different from a sell
        last, mean = closes[-1], sma(closes, strategy.sma_days)
        if symbol in held:
            if last < mean:
                out.append(Proposal(symbol, "sell", held[symbol].qty, last,
                                    f"close {last:.2f} is under its {strategy.sma_days} day average {mean:.2f}"))
        elif last > mean:
            qty = math.floor(equity * risk.entry_pct / 100 / last)
            if qty >= 1:
                out.append(Proposal(symbol, "buy", qty, last,
                                    f"close {last:.2f} is over its {strategy.sma_days} day average {mean:.2f}"))
    return out
