"""A stand-in Alpaca server written from the public documentation, with made-up prices.

It answers the endpoints the client uses (account, clock, positions, orders, close all, bars, option contracts and
option snapshots) with the same
field names and status codes the documentation shows, fills market orders at its own price tape and fires
stop orders when the tape crosses them. It exists so the whole bot can run, and be tested, with no account
and no money. It is not a model of Alpaca's matching or fees. Control endpoints sit under /__standin.
"""

from __future__ import annotations

import json
import math
import random
import threading
from datetime import UTC, date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

KEY, SECRET = "standin-key", "standin-secret"
DEFAULT_PRICES = {"SPY": 520.0, "QQQ": 450.0, "AAPL": 190.0, "MSFT": 410.0, "NVDA": 120.0}
DRIFT = {"SPY": 0.002, "QQQ": 0.003, "AAPL": 0.004, "MSFT": -0.003, "NVDA": -0.002}  # per day, made up


class Book:
    def __init__(self, cash: float = 100_000.0, page_cap: int = 10_000, prices: dict[str, float] | None = None,
                 today: date | None = None, options_level: int = 2):
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
        self.today = today or datetime.now(UTC).date()
        self.options_level = options_level
        self.premiums: dict[str, float] = {}  # a control endpoint can pin a contract's mid premium
        self.contracts = self._make_contracts()

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

    def _make_contracts(self) -> dict[str, dict]:
        """A made-up chain: four expiries and five strikes around each price, calls and puts, standard 100 share contracts."""
        out: dict[str, dict] = {}
        for sym, spot in self.prices.items():
            for days in (3, 12, 26, 70):
                exp = self.today + timedelta(days=days)
                for m in (0.95, 0.975, 1.0, 1.025, 1.05):
                    strike = round(spot * m)
                    for right in ("call", "put"):
                        occ = f"{sym}{exp:%y%m%d}{'C' if right == 'call' else 'P'}{int(strike * 1000):08d}"
                        out[occ] = {
                            "id": f"contract-{occ}", "symbol": occ, "name": f"{sym} {exp:%b %d %Y} {strike} {right.title()}",
                            "status": "active", "tradable": True, "expiration_date": exp.isoformat(), "root_symbol": sym,
                            "underlying_symbol": sym, "underlying_asset_id": f"asset-{sym}", "type": right, "style": "american",
                            "strike_price": str(strike), "multiplier": "100", "size": "100", "open_interest": "250",
                            "open_interest_date": (self.today - timedelta(days=1)).isoformat(), "ppind": True,
                        }
        return out

    def option_mid(self, occ: str) -> float:
        if occ in self.premiums:
            return self.premiums[occ]
        c = self.contracts[occ]
        spot, strike = self.prices[c["underlying_symbol"]], float(c["strike_price"])
        days = max((date.fromisoformat(c["expiration_date"]) - self.today).days, 1)
        intrinsic = max(spot - strike, 0.0) if c["type"] == "call" else max(strike - spot, 0.0)
        return round(intrinsic + spot * 0.02 * math.sqrt(days / 30), 2)

    def option_quote(self, occ: str) -> tuple[float, float]:
        mid = self.option_mid(occ)
        return round(mid * 0.97, 2), max(round(mid * 1.03, 2), round(mid + 0.01, 2))

    def option_snapshot(self, occ: str) -> dict:
        bid, ask = self.option_quote(occ)
        c = self.contracts[occ]
        spot, strike = self.prices[c["underlying_symbol"]], float(c["strike_price"])
        call = 0.5 + max(-0.45, min(0.45, (spot - strike) / (spot * 0.1) * 0.5))
        delta = call if c["type"] == "call" else call - 1
        now = datetime.now(UTC).isoformat().replace("+00:00", "Z")
        return {
            "greeks": {"delta": round(delta, 4), "gamma": 0.03, "rho": 0.01, "theta": -0.12, "vega": 0.08},
            "impliedVolatility": 0.31,
            "latestQuote": {"ap": ask, "as": 10, "ax": "B", "bp": bid, "bs": 10, "bx": "C", "c": "A", "t": now},
            "latestTrade": {"c": "I", "p": self.option_mid(occ), "s": 1, "t": now, "x": "A"},
        }

    def is_opt(self, symbol: str) -> bool:
        return symbol in self.contracts

    def px(self, symbol: str) -> float:
        return self.option_mid(symbol) if self.is_opt(symbol) else self.prices[symbol]

    def mult(self, symbol: str) -> int:
        return 100 if self.is_opt(symbol) else 1

    def next_id(self, prefix: str) -> str:
        self.seq += 1
        return f"{prefix}-{self.seq:06d}"

    # -- account views --------------------------------------------------------------------------

    def equity(self) -> float:
        return self.cash + sum(p["qty"] * self.px(s) * self.mult(s) for s, p in self.positions.items())

    def account(self) -> dict:
        eq = self.equity()
        return {"id": "standin-account", "status": "ACTIVE", "currency": "USD", "cash": f"{self.cash:.2f}",
                "portfolio_value": f"{eq:.2f}", "equity": f"{eq:.2f}", "last_equity": f"{self.last_equity:.2f}",
                "buying_power": f"{max(self.cash, 0):.2f}", "trading_blocked": False, "account_blocked": False}

    def position_json(self, symbol: str) -> dict:
        p, price, m = self.positions[symbol], self.px(symbol), self.mult(symbol)
        pl = (price - p["avg"]) * p["qty"] * m
        return {"symbol": symbol, "qty": str(p["qty"]), "side": "long", "avg_entry_price": f"{p['avg']:.4f}",
                "asset_class": "us_option" if m == 100 else "us_equity",
                "current_price": f"{price:.2f}", "market_value": f"{p['qty'] * price * m:.2f}", "cost_basis": f"{p['avg'] * p['qty'] * m:.2f}",
                "unrealized_pl": f"{pl:.2f}", "unrealized_plpc": f"{(price - p['avg']) / p['avg']:.6f}"}

    # -- trading ----------------------------------------------------------------------------------

    def _fill(self, symbol: str, side: str, qty: int) -> dict | None:
        return self._fill_at(symbol, side, qty, self.px(symbol))

    def _fill_at(self, symbol: str, side: str, qty: int, price: float) -> dict | None:
        m = self.mult(symbol)
        if side == "buy":
            pos = self.positions.setdefault(symbol, {"qty": 0, "avg": 0.0})
            pos["avg"] = (pos["avg"] * pos["qty"] + price * qty) / (pos["qty"] + qty)
            pos["qty"] += qty
            self.cash -= price * qty * m
        else:
            pos = self.positions[symbol]
            pos["qty"] -= qty
            self.cash += price * qty * m
            if pos["qty"] == 0:
                del self.positions[symbol]
        return {"filled_avg_price": f"{price:.2f}"}

    def place_option(self, body: dict) -> tuple[int, dict]:
        """Option orders as the documentation describes them: whole contracts, day or gtc, no notional, no extended hours."""
        symbol, side, otype = body["symbol"], body.get("side"), body.get("type")

        def bad(msg: str) -> tuple[int, dict]:
            return 422, {"code": 42210000, "message": msg}

        try:
            qty = int(str(body.get("qty", "")))
        except ValueError:
            return bad("qty must be a whole number")
        if qty < 1 or side not in {"buy", "sell"}:
            return bad("invalid order")
        if body.get("notional") is not None:
            return bad("notional is not supported for options")
        intent = body.get("position_intent")
        if intent is not None and not str(intent).startswith(f"{side}_to_"):
            return bad(f"position_intent {intent} does not match side {side}")
        if body.get("extended_hours"):
            return bad("extended_hours must be false or not populated for options")
        if body.get("time_in_force") not in {"day", "gtc"}:
            return bad("time_in_force must be day or gtc for options")
        if body.get("order_class") not in {None, "", "simple", "mleg"}:
            return bad("order_class must be simple or mleg for options")
        if otype not in {"market", "limit", "stop", "stop_limit"}:
            return bad("invalid order type for options")
        if otype == "limit" and not body.get("limit_price"):
            return bad("limit_price is required for limit orders")
        if otype == "stop" and not body.get("stop_price"):
            return bad("stop_price is required for stop orders")
        cid = body.get("client_order_id") or self.next_id("auto")
        if cid in self.client_ids:
            return 422, {"code": 40010001, "message": "client_order_id must be unique"}
        held = self.positions.get(symbol, {}).get("qty", 0)
        if side == "sell" and held < qty:
            return 403, {"code": 40310000, "message": "account not eligible to trade uncovered option contracts"}
        if side == "buy" and self.options_level < 2:
            return 403, {"code": 40310000, "message": "account not eligible for level2 options trading"}
        bid, ask = self.option_quote(symbol)
        if side == "buy" and ask * 100 * qty > self.cash:
            return 403, {"code": 40310000, "message": "insufficient options buying power"}
        self.client_ids.add(cid)
        order = {"id": self.next_id("ord"), "client_order_id": cid, "symbol": symbol, "side": side, "type": otype,
                 "qty": str(qty), "time_in_force": body.get("time_in_force"), "order_class": body.get("order_class") or "simple",
                 "position_intent": body.get("position_intent"), "asset_class": "us_option", "legs": [],
                 "created_at": datetime.now(UTC).isoformat()}
        if otype == "stop":
            order.update(status="new", stop_price=body["stop_price"])
            self.orders.append(order)
            return 200, order
        if otype == "limit":
            order["limit_price"] = body["limit_price"]
        marketable = otype == "market" or (side == "buy" and float(body["limit_price"]) >= ask) or (side == "sell" and float(body["limit_price"]) <= bid)
        if not marketable:
            order["status"] = "new"
            self.orders.append(order)
            return 200, order
        order.update(status="filled", filled_qty=str(qty), **self._fill_at(symbol, side, qty, ask if side == "buy" else bid))
        self.orders.append(order)
        return 200, order

    def place(self, body: dict) -> tuple[int, dict]:
        if body.get("symbol") in self.contracts:
            return self.place_option(body)
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
        self._fire_stops()

    def set_premiums(self, new: dict[str, float]) -> None:
        for occ, mid in new.items():
            if occ in self.contracts:
                self.premiums[occ] = float(mid)
        self._fire_stops()

    def _fire_stops(self) -> None:
        for o in self.open_orders():
            if o["type"] == "stop" and self.px(o["symbol"]) <= float(o["stop_price"]) and o["symbol"] in self.positions:
                self._fill(o["symbol"], "sell", int(o["qty"]))
                o["status"] = "filled"

    def page_of_contracts(self, q: dict) -> dict:
        today = self.today
        lte = q.get("expiration_date_lte") or (today + timedelta(days=(6 - today.weekday()) % 7 or 7)).isoformat()  # default: next weekend
        rows = [c for c in self.contracts.values() if c["underlying_symbol"] in q.get("underlying_symbols", "").split(",")]
        rows = [c for c in rows if c["status"] == (q.get("status") or "active")]
        rows = [c for c in rows if c["expiration_date"] <= lte]
        if q.get("expiration_date_gte"):
            rows = [c for c in rows if c["expiration_date"] >= q["expiration_date_gte"]]
        if q.get("expiration_date"):
            rows = [c for c in rows if c["expiration_date"] == q["expiration_date"]]
        if q.get("type"):
            rows = [c for c in rows if c["type"] == q["type"]]
        if q.get("strike_price_gte"):
            rows = [c for c in rows if float(c["strike_price"]) >= float(q["strike_price_gte"])]
        if q.get("strike_price_lte"):
            rows = [c for c in rows if float(c["strike_price"]) <= float(q["strike_price_lte"])]
        rows.sort(key=lambda c: (c["expiration_date"], c["symbol"]))
        offset = int(q["page_token"]) if q.get("page_token") else 0
        size = min(int(q.get("limit", 100)), self.page_cap)
        more = offset + size < len(rows)
        return {"option_contracts": rows[offset:offset + size], "next_page_token": str(offset + size) if more else None}

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
            if method == "GET" and url.path.startswith("/v2/orders/"):
                found = next((o for o in b.orders if o["id"] == url.path.rsplit("/", 1)[1]), None)
                return self._send(200, found) if found else self._send(404, {"message": "order not found"})
            if method == "GET" and url.path == "/v2/orders":
                return self._send(200, b.open_orders())
            if method == "DELETE" and url.path == "/v2/orders":
                return self._send(207, b.cancel_all())
            if method == "POST" and url.path == "/v2/orders":
                status, payload = b.place(self._body())
                return self._send(status, payload)
            if method == "GET" and url.path.startswith("/v2/stocks/") and url.path.endswith("/trades/latest"):
                sym = url.path.split("/")[3]
                if sym not in b.prices:
                    return self._send(404, {"message": f"asset {sym} not found"})
                stamp = datetime.now(UTC).isoformat().replace("+00:00", "Z")
                return self._send(200, {"symbol": sym, "trade": {"t": stamp, "x": "V", "p": b.prices[sym], "s": 100, "c": ["@"], "i": 1, "z": "C"}})
            if method == "GET" and url.path == "/v2/options/contracts":
                return self._send(200, b.page_of_contracts(q))
            if method == "GET" and url.path == "/v1beta1/options/snapshots":
                symbols = [x for x in q.get("symbols", "").split(",") if x]
                if not symbols or len(symbols) > 100:
                    return self._send(400, {"message": "symbols is required and limited to 100"})
                if q.get("feed", "indicative") not in {"opra", "indicative"}:
                    return self._send(400, {"message": "invalid feed"})
                return self._send(200, {"snapshots": {s: b.option_snapshot(s) for s in symbols if s in b.contracts}, "next_page_token": None})
            if method == "GET" and url.path == "/v2/stocks/bars":
                return self._send(200, b.page_of_bars(q.get("symbols", "").split(","), int(q.get("limit", 1000)), q.get("page_token"), q.get("start")))
        self._send(404, {"message": "not found"})

    def _control(self, method: str, path: str, b: Book) -> None:
        with b.lock:
            if method == "POST" and path == "/__standin/prices":
                b.set_prices(self._body())
            elif method == "POST" and path == "/__standin/option_premium":
                b.set_premiums(self._body())
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
