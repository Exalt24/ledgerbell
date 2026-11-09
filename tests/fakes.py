# LIFESPAN: permanent
"""Local stand-ins for the Finnhub and Airtable REST APIs, written from their public documentation.

Finnhub (finnhub.io/docs/api): the key goes in the X-Finnhub-Token header (or a token parameter), /api/v1/quote answers
c, d, dp, h, l, o, pc and t, an unknown symbol answers zeros, Candles (OHLCV) is a premium endpoint and a free key gets a
403 "You don't have access to this resource.", and going over the limit is a 429.

Airtable (airtable.com/developers/web/api): POST /v0/{baseId}/{tableIdOrName} with {"records": [{"fields": {...}}]} and a
Bearer token creates records and answers {"records": [{"id", "createdTime", "fields"}]}; an unknown field is a 422
UNKNOWN_FIELD_NAME, an unknown table a 404, GET .../{recordId} reads one back, and over 5 requests a second is a 429.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

FINNHUB_KEY = "fh-test-key"
AIRTABLE_TOKEN = "pat-test-token"
BASE_ID, TABLE_ID, TABLE_NAME = "appTESTBASE000001", "tblTESTTABLE0001", "Ledgerbell run log (test)"
FIELD_TYPES = {
    "Routine": str, "Run at": str, "Status": str, "Orders suggested": int, "Orders placed": int, "Orders refused": int,
    "Mode": str, "Summary": str,
}
QUOTES = {
    "AAPL": {"c": 191.25, "d": 1.25, "dp": 0.658, "h": 192.0, "l": 189.9, "o": 190.1, "pc": 190.0, "t": 1_762_700_000},
    "MSFT": {"c": 409.5, "d": -0.5, "dp": -0.122, "h": 411.0, "l": 407.0, "o": 410.0, "pc": 410.0, "t": 1_762_700_000},
}


class _Server:
    def __init__(self, handler_cls):
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_cls)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}"

    def stop(self) -> None:
        self.server.shutdown()


def _send(h: BaseHTTPRequestHandler, status: int, payload, headers: dict | None = None) -> None:
    raw = json.dumps(payload).encode()
    h.send_response(status)
    h.send_header("Content-Type", "application/json")
    h.send_header("Content-Length", str(len(raw)))
    for k, v in (headers or {}).items():
        h.send_header(k, v)
    h.end_headers()
    h.wfile.write(raw)


class Finnhub:
    def __init__(self):
        self.calls: list[str] = []
        self.rate_limit_next = 0  # answer this many requests with a 429 before behaving
        me = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):  # noqa: N802
                url = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(url.query).items()}
                me.calls.append(url.path)
                if (self.headers.get("X-Finnhub-Token") or q.get("token")) != FINNHUB_KEY:
                    return _send(self, 401, {"error": "Invalid API key"})
                if me.rate_limit_next > 0:
                    me.rate_limit_next -= 1
                    return _send(self, 429, {"error": "API limit reached. Please try again later."}, {"Retry-After": "0"})
                if url.path == "/api/v1/quote":
                    zero = {"c": 0, "d": None, "dp": None, "h": 0, "l": 0, "o": 0, "pc": 0, "t": 0}
                    return _send(self, 200, QUOTES.get(q.get("symbol", ""), zero))
                if url.path == "/api/v1/stock/candle":
                    return _send(self, 403, {"error": "You don't have access to this resource."})
                _send(self, 404, {"error": "not found"})

        self._s = _Server(H)
        self.url = self._s.url

    def stop(self) -> None:
        self._s.stop()


class Airtable:
    def __init__(self):
        self.records: dict[str, dict] = {}
        self.rate_limit_next = 0
        self.requests: list[tuple[str, str]] = []
        self.tables = {TABLE_ID, TABLE_NAME}  # a test may add a table whose name needs encoding
        me = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _auth(self) -> bool:
                return self.headers.get("Authorization") == f"Bearer {AIRTABLE_TOKEN}"

            def _table_ok(self, parts: list[str]) -> bool:
                return len(parts) >= 3 and parts[0] == "v0" and parts[1] == BASE_ID and unquote(parts[2]) in me.tables

            def do_POST(self):  # noqa: N802
                parts = urlparse(self.path).path.strip("/").split("/")
                me.requests.append(("POST", unquote(urlparse(self.path).path)))
                if not self._auth():
                    return _send(self, 401, {"error": {"type": "AUTHENTICATION_REQUIRED", "message": "Authentication required"}})
                if me.rate_limit_next > 0:
                    me.rate_limit_next -= 1
                    return _send(self, 429, {"error": {"type": "RATE_LIMIT_REACHED", "message": "Rate limit reached"}})
                if not self._table_ok(parts) or len(parts) != 3:
                    return _send(self, 404, {"error": {"type": "TABLE_NOT_FOUND", "message": "Could not find table"}})
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                records = body.get("records") or ([{"fields": body["fields"]}] if "fields" in body else [])
                if not records or len(records) > 10:
                    return _send(self, 422, {"error": {"type": "INVALID_REQUEST_BODY", "message": "records must hold 1 to 10 items"}})
                made = []
                for rec in records:
                    for name, value in rec["fields"].items():
                        if name not in FIELD_TYPES:
                            return _send(self, 422, {"error": {"type": "UNKNOWN_FIELD_NAME", "message": f'Unknown field name: "{name}"'}})
                        if not isinstance(value, FIELD_TYPES[name]) or isinstance(value, bool):
                            message = f'Field "{name}" cannot accept the provided value'
                            return _send(self, 422, {"error": {"type": "INVALID_VALUE_FOR_COLUMN", "message": message}})
                    rid = f"rec{len(me.records) + 1:014d}"
                    me.records[rid] = {"id": rid, "createdTime": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()), "fields": rec["fields"]}
                    made.append(me.records[rid])
                _send(self, 200, {"records": made})

            def do_GET(self):  # noqa: N802
                parts = urlparse(self.path).path.strip("/").split("/")
                me.requests.append(("GET", unquote(urlparse(self.path).path)))
                if not self._auth():
                    return _send(self, 401, {"error": {"type": "AUTHENTICATION_REQUIRED", "message": "Authentication required"}})
                if not self._table_ok(parts) or len(parts) != 4 or parts[3] not in me.records:
                    return _send(self, 404, {"error": {"type": "NOT_FOUND", "message": "Record not found"}})
                _send(self, 200, me.records[parts[3]])

        self._s = _Server(H)
        self.url = self._s.url

    def stop(self) -> None:
        self._s.stop()
