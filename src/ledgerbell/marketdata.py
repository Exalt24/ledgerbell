"""Where prices come from: one small interface, an Alpaca implementation, a Finnhub one and a Polygon one.

The routines ask a `MarketDataProvider` for daily bars (the example rule's moving average) and for a latest quote
(the briefing's price check). `[market_data] provider` in the config picks which one. Orders and the account are
always Alpaca's: this only switches the price feed.

Finnhub's REST API (finnhub.io/docs/api): "All GET request require a token parameter token=apiKey in the URL or a
header X-Finnhub-Token : apiKey", `/quote` answers c (current price), d, dp, h, l, o and pc (previous close), and
"If your limit is exceeded, you will receive a response with status code 429" with a hard 30 calls a second cap on top.
The docs list Candles (OHLCV) as Premium, so `FinnhubData.daily_bars` raises a `ProviderError` on a free key and the
routine falls back to Alpaca's bars (see `routines._bars`).

Polygon's REST API (polygon.io, renamed Massive on 2025-10-30; docs at massive.com/docs/rest): "Authorization: Bearer
YOUR_API_KEY" or "?apiKey=YOUR_API_KEY" (the header is used here so the key never lands in a URL, a log line or the
`next_url` a response hands back), daily bars are `GET /v2/aggs/ticker/{ticker}/range/1/day/{from}/{to}` answering
`results` rows of o, h, l, c, v, vw, n and t (a millisecond timestamp), and `GET /v2/aggs/ticker/{ticker}/prev` is the
previous trading day's bar, included in every plan. `/v2/last/trade/{ticker}` is not on the free Basic plan, so the price
here is the previous close. Basic is limited to 5 calls a minute and answers 429 over that. Both api.polygon.io and
api.massive.com are documented as serving the same API; `[market_data] polygon_url` picks the host.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol
from urllib.parse import quote as urlquote

import requests

from .alpaca import AlpacaClient
from .config import Config, ConfigError, secret_from_env


class ProviderError(RuntimeError):
    def __init__(self, provider: str, status: int, message: str):
        super().__init__(f"{provider} {status}: {message}")
        self.provider = provider
        self.status = status


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: float
    prev_close: float | None
    high: float | None
    low: float | None
    open: float | None
    at: datetime | None
    source: str

    @property
    def change_pct(self) -> float | None:
        if not self.prev_close:
            return None
        return round(100 * (self.price - self.prev_close) / self.prev_close, 2)


class MarketDataProvider(Protocol):
    name: str

    def quote(self, symbol: str) -> Quote: ...

    def daily_bars(self, symbols: list[str], limit: int = 60) -> dict[str, list[dict]]: ...


class AlpacaData:
    """Alpaca's own market data, through the same client the orders use."""

    name = "alpaca"

    def __init__(self, client: AlpacaClient):
        self.client = client

    def quote(self, symbol: str) -> Quote:
        trade = self.client.latest_trade(symbol)
        return Quote(symbol, float(trade["p"]), None, None, None, None, _when(trade.get("t")), self.name)

    def daily_bars(self, symbols: list[str], limit: int = 60) -> dict[str, list[dict]]:
        return self.client.daily_bars(symbols, limit=limit)


def _when(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


class FinnhubData:
    name = "finnhub"

    def __init__(
        self, base_url: str, key: str, session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep, retries: int = 2,
    ):
        self.base = base_url.rstrip("/")
        self._http = session or requests.Session()
        self._http.headers.update({"X-Finnhub-Token": key})
        self._sleep = sleep
        self._retries = retries

    def _get(self, path: str, **params):
        """GET with a retry on 429 (the free plan's 60 calls a minute) and on a 5xx, honouring Retry-After when sent."""
        for attempt in range(self._retries + 1):
            resp = self._http.get(self.base + path, params=params, timeout=15)
            if resp.status_code in {429, 500, 502, 503, 504} and attempt < self._retries:
                wait = float(resp.headers.get("Retry-After") or 2 ** attempt)
                self._sleep(min(wait, 30.0))
                continue
            if resp.status_code >= 400:
                try:
                    message = resp.json().get("error", resp.text)
                except ValueError:
                    message = resp.text
                raise ProviderError(self.name, resp.status_code, str(message)[:200])
            return resp.json()
        raise AssertionError("unreachable")  # pragma: no cover

    def quote(self, symbol: str) -> Quote:
        q = self._get("/api/v1/quote", symbol=symbol)
        if not q.get("c"):  # Finnhub answers an unknown symbol with all zeros
            raise ProviderError(self.name, 404, f"no quote for {symbol}")
        at = datetime.fromtimestamp(q["t"], UTC) if q.get("t") else None
        return Quote(symbol, float(q["c"]), q.get("pc"), q.get("h"), q.get("l"), q.get("o"), at, self.name)

    def daily_bars(self, symbols: list[str], limit: int = 60) -> dict[str, list[dict]]:
        now = int(time.time())
        out: dict[str, list[dict]] = {}
        for sym in symbols:
            c = self._get("/api/v1/stock/candle", symbol=sym, resolution="D", **{"from": now - int(limit * 1.6 + 10) * 86400, "to": now})
            if c.get("s") != "ok":
                out[sym] = []
                continue
            rows = [
                {"t": datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"), "o": o, "h": h, "l": lo, "c": cl, "v": v}
                for t, o, h, lo, cl, v in zip(c["t"], c["o"], c["h"], c["l"], c["c"], c["v"], strict=True)
            ]
            out[sym] = rows[-limit:]
        return out


class PolygonData:
    name = "polygon"

    def __init__(
        self, base_url: str, key: str, session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep, retries: int = 2,
    ):
        self.base = base_url.rstrip("/")
        self._http = session or requests.Session()
        self._http.headers.update({"Authorization": f"Bearer {key}"})
        self._sleep = sleep
        self._retries = retries

    def _get(self, path: str, **params):
        """GET with a retry on 429 and on a 5xx, honouring Retry-After when sent.

        The free plan allows 5 calls a minute, so a 429 with no Retry-After waits 12 seconds, then 24, not the 1 and 2 of a 5xx.
        """
        for attempt in range(self._retries + 1):
            resp = self._http.get(self.base + path, params=params, timeout=15)
            if resp.status_code in {429, 500, 502, 503, 504} and attempt < self._retries:
                fallback = (12.0 if resp.status_code == 429 else 1.0) * 2 ** attempt
                self._sleep(min(float(resp.headers.get("Retry-After") or fallback), 30.0))
                continue
            if resp.status_code >= 400:
                try:
                    body = resp.json()
                    message = body.get("error") or body.get("message") or resp.text
                except (ValueError, AttributeError):
                    message = resp.text
                raise ProviderError(self.name, resp.status_code, str(message)[:200])
            return resp.json()
        raise AssertionError("unreachable")  # pragma: no cover

    def quote(self, symbol: str) -> Quote:
        bar = self._get(f"/v2/aggs/ticker/{urlquote(symbol.upper(), safe='')}/prev").get("results") or []
        if not bar or not bar[0].get("c"):  # a ticker Polygon does not know is a 200 with no results
            raise ProviderError(self.name, 404, f"no quote for {symbol}")
        b = bar[0]
        at = datetime.fromtimestamp(b["t"] / 1000, UTC) if b.get("t") else None
        return Quote(symbol, float(b["c"]), None, b.get("h"), b.get("l"), b.get("o"), at, self.name)

    def daily_bars(self, symbols: list[str], limit: int = 60) -> dict[str, list[dict]]:
        today = datetime.now(UTC).date()
        start = today - timedelta(days=int(limit * 1.6 + 10))
        out: dict[str, list[dict]] = {}
        for sym in symbols:
            path = f"/v2/aggs/ticker/{urlquote(sym.upper(), safe='')}/range/1/day/{start.isoformat()}/{today.isoformat()}"
            rows = self._get(path, adjusted="true", sort="asc", limit=50000).get("results") or []
            out[sym] = [
                {"t": datetime.fromtimestamp(r["t"] / 1000, UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                 "o": r["o"], "h": r["h"], "l": r["l"], "c": r["c"], "v": r["v"]}
                for r in rows
            ][-limit:]
        return out


def build_provider(cfg: Config, client: AlpacaClient, env: dict[str, str] | None = None, session: requests.Session | None = None) -> MarketDataProvider:
    """The provider the config names. Finnhub needs FINNHUB_API_KEY and Polygon POLYGON_API_KEY in the environment, never in the config file."""
    if cfg.market_data.provider == "alpaca":
        return AlpacaData(client)
    if cfg.market_data.provider == "finnhub":
        return FinnhubData(cfg.market_data.finnhub_url, secret_from_env("FINNHUB_API_KEY", env), session)
    if cfg.market_data.provider == "polygon":
        return PolygonData(cfg.market_data.polygon_url, secret_from_env("POLYGON_API_KEY", env), session)
    raise ConfigError(f"unknown market data provider {cfg.market_data.provider!r}")
