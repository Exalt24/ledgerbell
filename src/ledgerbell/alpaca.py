"""A small Alpaca REST client written from the public documentation (docs.alpaca.markets).

Trading API: /v2/account, /v2/clock, /v2/positions, /v2/orders. Market data API: /v2/stocks/bars.
Auth is two headers, APCA-API-KEY-ID and APCA-API-SECRET-KEY. The client refuses a host that does not
match the configured mode, so a paper config can never reach the live endpoint.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
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

    def latest_trade(self, symbol: str) -> dict:
        """The latest trade for one stock from the market data API: {"p": price, "t": time, ...}."""
        page = self._call("GET", self.cfg.data_url.rstrip("/") + f"/v2/stocks/{symbol}/trades/latest", params={"feed": self.cfg.feed})
        return page["trade"]

    def open_orders(self) -> list[dict]:
        return self._call("GET", self._t("/v2/orders"), params={"status": "open", "limit": 100, "nested": "true"})

    def daily_bars(self, symbols: list[str], limit: int = 60, now: datetime | None = None) -> dict[str, list[dict]]:
        """Daily bars, oldest first, following next_page_token until every symbol has its bars.

        Alpaca's `start` defaults to the beginning of the current day, which for daily bars means no history,
        so a start far enough back to hold `limit` trading days (weekends and holidays included) is always sent.
        """
        out: dict[str, list[dict]] = {s: [] for s in symbols}
        start = ((now or datetime.now(UTC)) - timedelta(days=int(limit * 1.6) + 10)).strftime("%Y-%m-%d")
        params = {
            "start": start,
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

    # -- options ------------------------------------------------------------------------------

    def option_contracts(
        self, underlying: str, expiry_gte: date, expiry_lte: date, right: str | None = None,
        strike_gte: float | None = None, strike_lte: float | None = None,
    ) -> list[dict]:
        """Active contracts for one underlying inside an expiry window, following next_page_token to the end."""
        params: dict = {
            "underlying_symbols": underlying, "status": "active", "limit": 1000,
            "expiration_date_gte": expiry_gte.isoformat(), "expiration_date_lte": expiry_lte.isoformat(),
        }
        if right:
            params["type"] = right
        if strike_gte is not None:
            params["strike_price_gte"] = f"{strike_gte:.2f}"
        if strike_lte is not None:
            params["strike_price_lte"] = f"{strike_lte:.2f}"
        out: list[dict] = []
        for _ in range(20):
            page = self._call("GET", self._t("/v2/options/contracts"), params=params)
            out.extend(page.get("option_contracts") or [])
            token = page.get("next_page_token") or page.get("page_token")
            if not token:
                break
            params = {**params, "page_token": token}
        return out

    def option_snapshots(self, symbols: list[str], feed: str = "indicative") -> dict[str, dict]:
        """Latest quote, trade, implied volatility and greeks, 100 contract symbols per request (the documented limit)."""
        out: dict[str, dict] = {}
        for i in range(0, len(symbols), 100):
            params: dict = {"symbols": ",".join(symbols[i:i + 100]), "feed": feed, "limit": 1000}
            for _ in range(20):
                page = self._call("GET", self.cfg.data_url.rstrip("/") + "/v1beta1/options/snapshots", params=params)
                out.update(page.get("snapshots") or {})
                token = page.get("next_page_token")
                if not token:
                    break
                params = {**params, "page_token": token}
        return out

    def submit_option_entry(self, contract: str, qty: int, limit_price: float, client_order_id: str) -> dict:
        """Buy to open one long call or put at a limit. Alpaca lists only simple and mleg order classes for options, so
        there is no OTO stop leg: the exit stop is placed once this order has filled (see Desk._attach_option_exit)."""
        body = {
            "symbol": contract,
            "qty": str(qty),
            "side": "buy",
            "type": "limit",
            "limit_price": f"{limit_price:.2f}",
            "time_in_force": "day",
            "position_intent": "buy_to_open",
            "client_order_id": client_order_id,
        }
        return self._call("POST", self._t("/v2/orders"), json=body)

    def get_order(self, order_id: str) -> dict:
        return self._call("GET", self._t(f"/v2/orders/{order_id}"))

    def submit_option_close(self, contract: str, qty: int, client_order_id: str) -> dict:
        """Sell to close at the market (used when a held option is already through its stop)."""
        body = {
            "symbol": contract,
            "qty": str(qty),
            "side": "sell",
            "type": "market",
            "time_in_force": "day",
            "position_intent": "sell_to_close",
            "client_order_id": client_order_id,
        }
        return self._call("POST", self._t("/v2/orders"), json=body)

    def submit_option_stop(self, contract: str, qty: int, stop_price: float, client_order_id: str) -> dict:
        """Sell to close at a stop on the premium (single leg stop orders are allowed for options)."""
        body = {
            "symbol": contract,
            "qty": str(qty),
            "side": "sell",
            "type": "stop",
            "time_in_force": "gtc",
            "stop_price": f"{stop_price:.2f}",
            "position_intent": "sell_to_close",
            "client_order_id": client_order_id,
        }
        return self._call("POST", self._t("/v2/orders"), json=body)

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
