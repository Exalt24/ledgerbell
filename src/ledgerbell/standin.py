"""A stand-in Alpaca server written from the public documentation, with made-up prices.

It answers the endpoints the client uses (account, clock, positions, orders, close all, bars) with the same
field names and status codes the documentation shows, fills market orders at its own price tape and fires
stop orders when the tape crosses them. It exists so the whole bot can run, and be tested, with no account
and no money. It is not a model of Alpaca's matching or fees. Control endpoints sit under /__standin.
"""

from __future__ import annotations

import json
import random
import threading
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

KEY, SECRET = "standin-key", "standin-secret"
DEFAULT_PRICES = {"SPY": 520.0, "QQQ": 450.0, "AAPL": 190.0, "MSFT": 410.0, "NVDA": 120.0}
DRIFT = {"SPY": 0.002, "QQQ": 0.003, "AAPL": 0.004, "MSFT": -0.003, "NVDA": -0.002}  # per day, made up


class Book:
    def __init__(self, cash: float = 100_000.0, page_cap: int = 10_000, prices: dict[str, float] | None = None):
        self.lock = threading.Lock()
        self.cash = cash
        self.last_equity = cash
        self.is_open = True
        self.page_cap = page_cap
        self.prices = dict(prices or DEFAULT_PRICES)
        self.positions: dict[str, dict] = {}
        self.orders: list[dict] = []
        self.client_ids: set[str] = set()
        self.seq = 0
        self.bars = {s: self._make_bars(s, p) for s, p in self.prices.items()}

    @staticmethod
    def _make_bars(symbol: str, last: float, days: int = 60) -> list[dict]:
        rng = random.Random(symbol)
        closes = [last]
        for _ in range(days - 1):  # walk backwards from today's price so the tape ends on it
            closes.append(closes[-1] / (1 + DRIFT.get(symbol, 0) + rng.uniform(-0.006, 0.006)))
        closes.reverse()
        today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        return [
            {"t": (today - timedelta(days=days - i)).strftime("%Y-%m-%dT05:00:00Z"), "o": round(c, 2), "h": round(c * 1.004, 2),
             "l": round(c * 0.996, 2), "c": round(c, 2), "v": 1_000_000 + i, "n": 5000, "vw": round(c, 2)}
            for i, c in enumerate(closes)
        ]

    def next_id(self, prefix: str) -> str:
        self.seq += 1
        return f"{prefix}-{self.seq:06d}"

    # -- account views --------------------------------------------------------------------------

    def equity(self) -> float:
        return self.cash + sum(p["qty"] * self.prices[s] for s, p in self.positions.items())

    def account(self) -> dict:
        eq = self.equity()
        return {"id": "standin-account", "status": "ACTIVE", "currency": "USD", "cash": f"{self.cash:.2f}",
                "portfolio_value": f"{eq:.2f}", "equity": f"{eq:.2f}", "last_equity": f"{self.last_equity:.2f}",
                "buying_power": f"{max(self.cash, 0):.2f}", "trading_blocked": False, "account_blocked": False}

    def position_json(self, symbol: str) -> dict:
        p, price = self.positions[symbol], self.prices[symbol]
        pl = (price - p["avg"]) * p["qty"]
        return {"symbol": symbol, "qty": str(p["qty"]), "side": "long", "avg_entry_price": f"{p['avg']:.4f}",
                "current_price": f"{price:.2f}", "market_value": f"{p['qty'] * price:.2f}", "cost_basis": f"{p['avg'] * p['qty']:.2f}",
                "unrealized_pl": f"{pl:.2f}", "unrealized_plpc": f"{(price - p['avg']) / p['avg']:.6f}"}

    # -- trading ----------------------------------------------------------------------------------

    def _fill(self, symbol: str, side: str, qty: int) -> dict | None:
        price = self.prices[symbol]
        if side == "buy":
            pos = self.positions.setdefault(symbol, {"qty": 0, "avg": 0.0})
            pos["avg"] = (pos["avg"] * pos["qty"] + price * qty) / (pos["qty"] + qty)
            pos["qty"] += qty
            self.cash -= price * qty
        else:
            pos = self.positions[symbol]
            pos["qty"] -= qty
            self.cash += price * qty
            if pos["qty"] == 0:
                del self.positions[symbol]
        return {"filled_avg_price": f"{price:.2f}"}

    def place(self, body: dict) -> tuple[int, dict]:
        symbol, side, otype = body.get("symbol"), body.get("side"), body.get("type")
        if symbol not in self.prices:
            return 404, {"code": 40410000, "message": f"asset {symbol} not found"}
        try:
            qty = int(body.get("qty", 0))
        except (TypeError, ValueError):
            return 422, {"code": 42210000, "message": "qty must be a number"}
        cid = body.get("client_order_id") or self.next_id("auto")
        if cid in self.client_ids:
            return 422, {"code": 40010001, "message": "client_order_id must be unique"}
        if qty < 1 or side not in {"buy", "sell"} or otype not in {"market", "stop"}:
            return 422, {"code": 42210000, "message": "invalid order"}
        if side == "sell" and self.positions.get(symbol, {}).get("qty", 0) < qty:
            have = self.positions.get(symbol, {}).get("qty", 0)
            return 403, {"code": 40310000, "message": f"insufficient qty available for order (requested: {qty}, available: {have})"}
        if side == "buy" and self.prices[symbol] * qty > self.cash:
            return 403, {"code": 40310000, "message": "insufficient buying power"}
        self.client_ids.add(cid)
        order = {"id": self.next_id("ord"), "client_order_id": cid, "symbol": symbol, "side": side, "type": otype,
                 "qty": str(qty), "time_in_force": body.get("time_in_force"), "order_class": body.get("order_class", "simple"),
                 "legs": [], "created_at": datetime.now(UTC).isoformat()}
        if otype == "stop":
            order.update(status="new", stop_price=body["stop_price"])
            self.orders.append(order)
            return 200, order
        order.update(status="filled", **self._fill(symbol, side, qty))
        if body.get("order_class") == "oto" and side == "buy":
            stop = {"id": self.next_id("ord"), "client_order_id": f"{cid}-leg", "symbol": symbol, "side": "sell", "type": "stop",
                    "qty": str(qty), "time_in_force": body.get("time_in_force"), "order_class": "oto", "legs": [],
                    "status": "new", "stop_price": body["stop_loss"]["stop_price"], "created_at": order["created_at"]}
            self.orders.append(stop)
            order["legs"] = [stop]
        return 200, order

    def open_orders(self) -> list[dict]:
        return [o for o in self.orders if o["status"] in {"new", "held"}]

    def cancel_all(self) -> list[dict]:
        out = []
        for o in self.open_orders():
            o["status"] = "canceled"
            out.append({"id": o["id"], "status": 200})
        return out

    def close_all(self, cancel_orders: bool) -> list[dict]:
        if cancel_orders:
            self.cancel_all()
        out = []
        for symbol in list(self.positions):
            qty = self.positions[symbol]["qty"]
            fill = self._fill(symbol, "sell", qty)
            out.append({"symbol": symbol, "status": 200, "body": {"id": self.next_id("ord"), "symbol": symbol, "qty": str(qty), **fill}})
        return out

    def set_prices(self, new: dict[str, float]) -> None:
        for symbol, price in new.items():
            if symbol in self.prices:
                self.prices[symbol] = float(price)
                self.bars[symbol][-1]["c"] = float(price)
        for o in self.open_orders():
            if o["type"] == "stop" and self.prices[o["symbol"]] <= float(o["stop_price"]) and o["symbol"] in self.positions:
                self._fill(o["symbol"], "sell", int(o["qty"]))
                o["status"] = "filled"

    def page_of_bars(self, symbols: list[str], limit: int, token: str | None, start: str | None = None) -> dict:
        # like the real API, a request with no start only sees the current day, which holds no daily bar yet
        flat = [(s, b) for s in sorted(symbols) for b in self.bars.get(s, []) if start and b["t"][:10] >= start]
        offset = int(token) if token else 0
        size = min(limit, self.page_cap)
        chunk = flat[offset:offset + size]
        bars: dict[str, list[dict]] = {}
        for s, b in chunk:
            bars.setdefault(s, []).append(b)
        more = offset + size < len(flat)
        return {"bars": bars, "next_page_token": str(offset + size) if more else None}


class Handler(BaseHTTPRequestHandler):
    book: Book

    def log_message(self, *args):  # silence the default stderr line per request
        pass

    def _send(self, status: int, payload) -> None:
        raw = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def _body(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}") if n else {}

    def _authed(self) -> bool:
        return self.headers.get("APCA-API-KEY-ID") == KEY and self.headers.get("APCA-API-SECRET-KEY") == SECRET

    def _route(self, method: str) -> None:
        url = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(url.query).items()}
        b = self.book
        if url.path.startswith("/__standin"):
            return self._control(method, url.path, b)
        if not self._authed():
            return self._send(401, {"message": "forbidden."})
        with b.lock:
            if method == "GET" and url.path == "/v2/account":
                return self._send(200, b.account())
            if method == "GET" and url.path == "/v2/clock":
                now = datetime.now(UTC)
                nxt = now + timedelta(days=1)
                return self._send(200, {"timestamp": now.isoformat(), "is_open": b.is_open,
                                        "next_open": nxt.replace(hour=14, minute=30, second=0, microsecond=0).isoformat(),
                                        "next_close": nxt.replace(hour=21, minute=0, second=0, microsecond=0).isoformat()})
            if method == "GET" and url.path == "/v2/positions":
                return self._send(200, [b.position_json(s) for s in b.positions])
            if method == "DELETE" and url.path == "/v2/positions":
                return self._send(207, b.close_all(q.get("cancel_orders") == "true"))
            if method == "GET" and url.path == "/v2/orders":
                return self._send(200, b.open_orders())
            if method == "DELETE" and url.path == "/v2/orders":
                return self._send(207, b.cancel_all())
            if method == "POST" and url.path == "/v2/orders":
                status, payload = b.place(self._body())
                return self._send(status, payload)
            if method == "GET" and url.path == "/v2/stocks/bars":
                return self._send(200, b.page_of_bars(q.get("symbols", "").split(","), int(q.get("limit", 1000)), q.get("page_token"), q.get("start")))
        self._send(404, {"message": "not found"})

    def _control(self, method: str, path: str, b: Book) -> None:
        with b.lock:
            if method == "POST" and path == "/__standin/prices":
                b.set_prices(self._body())
            elif method == "POST" and path == "/__standin/session":
                b.is_open = bool(self._body().get("is_open"))
            elif method == "POST" and path == "/__standin/new_day":
                b.last_equity = b.equity()
            elif method == "GET" and path == "/__standin/state":
                return self._send(200, {"cash": b.cash, "positions": b.positions, "orders": b.orders, "prices": b.prices})
            else:
                return self._send(404, {"message": "no such control"})
        self._send(200, {"ok": True})

    def do_GET(self):  # noqa: N802
        self._route("GET")

    def do_POST(self):  # noqa: N802
        self._route("POST")

    def do_DELETE(self):  # noqa: N802
        self._route("DELETE")


def start(port: int = 0, **book_args) -> tuple[ThreadingHTTPServer, Book]:
    """Start the server on a background thread. Port 0 picks a free one: read it from server.server_port."""
    book = Book(**book_args)
    handler = type("BoundHandler", (Handler,), {"book": book})
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, book


def serve_forever(port: int) -> None:  # pragma: no cover - used by the `standin` command
    server, _ = start(port)
    print(f"stand-in Alpaca on http://127.0.0.1:{server.server_port} (key {KEY}, secret {SECRET}); Ctrl+C stops it")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        server.shutdown()
