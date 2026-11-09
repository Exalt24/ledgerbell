"""OCC option symbols: root, YYMMDD expiry, C or P, then the strike times 1000 in eight digits (AAPL240119C00190000)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date

OCC = re.compile(r"^([A-Z]{1,6})(\d{2})(\d{2})(\d{2})([CP])(\d{8})$")


@dataclass(frozen=True)
class OccSymbol:
    underlying: str
    expiry: date
    right: str  # "call" or "put"
    strike: float


def parse_occ(symbol: str) -> OccSymbol | None:
    """Split an OCC option symbol, or None when it is not one (a stock ticker, or a malformed string)."""
    m = OCC.match(symbol or "")
    if not m:
        return None
    root, yy, mm, dd, cp, strike = m.groups()
    try:
        expiry = date(2000 + int(yy), int(mm), int(dd))
    except ValueError:
        return None
    return OccSymbol(root, expiry, "call" if cp == "C" else "put", int(strike) / 1000)


def is_option_symbol(symbol: str) -> bool:
    return parse_occ(symbol) is not None
