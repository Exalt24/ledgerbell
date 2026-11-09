"""A small Alpaca REST client written from the public documentation (docs.alpaca.markets).

Trading API: /v2/account, /v2/clock, /v2/positions, /v2/orders. Market data API: /v2/stocks/bars.
Auth is two headers, APCA-API-KEY-ID and APCA-API-SECRET-KEY. The client refuses a host that does not
match the configured mode, so a paper config can never reach the live endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlparse

import requests

from .config import DATA_HOST, LIVE_HOST, PAPER_HOST, Config, ConfigError


class AlpacaError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"alpaca {status}: {message}")
        self.status = status
        self.message = message


@dataclass(frozen=True)
class Account:
    equity: float
    last_equity: float
    cash: float
    buying_power: float
    trading_blocked: bool

    @property
    def day_pl(self) -> float:
        return self.equity - self.last_equity

    @property
    def day_pl_pct(self) -> float:
        return 0.0 if self.last_equity <= 0 else 100.0 * self.day_pl / self.last_equity


@dataclass(frozen=True)
class Position:
    symbol: str
    qty: int
    avg_entry_price: float
    current_price: float
    unrealized_pl: float
    unrealized_plpc: float  # a fraction, as Alpaca sends it: -0.03 is three percent down

    @property
    def market_value(self) -> float:
        return self.qty * self.current_price


def check_hosts(cfg: Config) -> None:
    """Refuse to build a client whose URLs do not match the mode."""
    trading = urlparse(cfg.base_url).hostname or ""
    data = urlparse(cfg.data_url).hostname or ""
    if cfg.mode == "paper":
        if trading != PAPER_HOST:
            raise ConfigError(f"mode paper only talks to {PAPER_HOST}, got {trading!r}")
    elif cfg.mode == "live":
        if not cfg.live_confirmed:
            raise ConfigError("mode live needs live_confirmed = true in the config, set on purpose by the account owner")
        if trading != LIVE_HOST:
            raise ConfigError(f"mode live only talks to {LIVE_HOST}, got {trading!r}")
    else:  # standin
        if trading not in {"127.0.0.1", "localhost"} or data not in {"127.0.0.1", "localhost"}:
            raise ConfigError("mode standin only talks to localhost")
    if cfg.mode != "standin" and data != DATA_HOST:
        raise ConfigError(f"market data comes from {DATA_HOST}, got {data!r}")


class AlpacaClient:
    def __init__(self, cfg: Config, key: str, secret: str, session: requests.Session | None = None):
        check_hosts(cfg)
        self.cfg = cfg
        self._http = session or requests.Session()
        self._http.headers.update({"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret})

    # -- plumbing -----------------------------------------------------------------------------

    def _call(self, method: str, url: str, **kwargs):
        resp = self._http.request(method, url, timeout=15, **kwargs)
        if resp.status_code >= 400:
            try:
                message = resp.json().get("message", resp.text)
            except ValueError:
                message = resp.text
            raise AlpacaError(resp.status_code, str(message)[:300])
        return resp.json() if resp.content else None

    def _t(self, path: str) -> str:
        return self.cfg.base_url.rstrip("/") + path

    # -- reads --------------------------------------------------------------------------------

    def account(self) -> Account:
        a = self._call("GET", self._t("/v2/account"))
        return Account(
            equity=float(a["equity"]),
            last_equity=float(a["last_equity"]),
            cash=float(a["cash"]),
            buying_power=float(a["buying_power"]),
            trading_blocked=bool(a.get("trading_blocked", False)) or bool(a.get("account_blocked", False)),
        )

    def clock(self) -> dict:
        return self._call("GET", self._t("/v2/clock"))

    def positions(self) -> list[Position]:
        return [
            Position(
                symbol=p["symbol"],
                qty=int(float(p["qty"])),
                avg_entry_price=float(p["avg_entry_price"]),
                current_price=float(p["current_price"]),
                unrealized_pl=float(p["unrealized_pl"]),
                unrealized_plpc=float(p["unrealized_plpc"]),
            )
            for p in self._call("GET", self._t("/v2/positions"))
        ]

    def open_orders(self) -> list[dict]:
        return self._call("GET", self._t("/v2/orders"), params={"status": "open", "limit": 100, "nested": "true"})

    def daily_bars(self, symbols: list[str], limit: int = 60) -> dict[str, list[dict]]:
        """Daily bars, oldest first, following next_page_token until every symbol has its bars."""
        out: dict[str, list[dict]] = {s: [] for s in symbols}
        params = {
            "symbols": ",".join(symbols),
            "timeframe": "1Day",
            "limit": limit * len(symbols),
            "adjustment": "all",
            "feed": self.cfg.feed,
            "sort": "asc",
        }
        for _ in range(20):
            page = self._call("GET", self.cfg.data_url.rstrip("/") + "/v2/stocks/bars", params=params)
            for sym, bars in (page.get("bars") or {}).items():
                out.setdefault(sym, []).extend(bars)
            token = page.get("next_page_token")
            if not token:
                break
            params = {**params, "page_token": token}
        return {s: bars[-limit:] for s, bars in out.items()}

    # -- writes -------------------------------------------------------------------------------

    def submit_entry(self, symbol: str, qty: int, stop_price: float, client_order_id: str) -> dict:
        """A market buy with a server-side stop attached (an OTO order), so the stop outlives this program."""
        body = {
            "symbol": symbol,
            "qty": str(qty),
            "side": "buy",
            "type": "market",
            "time_in_force": "day",
            "order_class": "oto",
            "stop_loss": {"stop_price": f"{stop_price:.2f}"},
            "client_order_id": client_order_id,
        }
        return self._call("POST", self._t("/v2/orders"), json=body)

    def submit_exit(self, symbol: str, qty: int, client_order_id: str) -> dict:
        body = {
            "symbol": symbol,
            "qty": str(qty),
            "side": "sell",
            "type": "market",
            "time_in_force": "day",
            "client_order_id": client_order_id,
        }
        return self._call("POST", self._t("/v2/orders"), json=body)

    def submit_stop(self, symbol: str, qty: int, stop_price: float, client_order_id: str) -> dict:
        body = {
            "symbol": symbol,
            "qty": str(qty),
            "side": "sell",
            "type": "stop",
            "time_in_force": "gtc",
            "stop_price": f"{stop_price:.2f}",
            "client_order_id": client_order_id,
        }
        return self._call("POST", self._t("/v2/orders"), json=body)

    def cancel_all_orders(self) -> list[dict]:
        return self._call("DELETE", self._t("/v2/orders")) or []

    def close_all_positions(self) -> list[dict]:
        return self._call("DELETE", self._t("/v2/positions"), params={"cancel_orders": "true"}) or []
