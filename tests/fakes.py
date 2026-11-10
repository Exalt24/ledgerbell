# LIFESPAN: permanent
"""Local stand-ins for the Finnhub, Airtable, Polygon and Twilio REST APIs, written from their public documentation.

Finnhub (finnhub.io/docs/api): the key goes in the X-Finnhub-Token header (or a token parameter), /api/v1/quote answers
c, d, dp, h, l, o, pc and t, an unknown symbol answers zeros, Candles (OHLCV) is a premium endpoint and a free key gets a
403 "You don't have access to this resource.", and going over the limit is a 429.

Airtable (airtable.com/developers/web/api): POST /v0/{baseId}/{tableIdOrName} with {"records": [{"fields": {...}}]} and a
Bearer token creates records and answers {"records": [{"id", "createdTime", "fields"}]}; an unknown field is a 422
UNKNOWN_FIELD_NAME, an unknown table a 404, GET .../{recordId} reads one back, and over 5 requests a second is a 429.

Polygon and Twilio are described on their classes below. None of the four stand-ins has been checked against the live service.
"""

from __future__ import annotations

import base64
import json
import threading
import time
from datetime import UTC, date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

FINNHUB_KEY = "fh-test-key"
POLYGON_KEY = "pg-test-key"
POLYGON_PRICES = {"AAPL": 191.25, "MSFT": 409.5}
TWILIO_SID, TWILIO_TOKEN = "AC" + "1" * 32, "tw-test-token"
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


class Polygon:
    """GET /v2/aggs/ticker/{ticker}/prev and .../range/{multiplier}/{timespan}/{from}/{to}, written from Polygon's (Massive's) docs.

    The key is accepted as "Authorization: Bearer KEY" or as ?apiKey=KEY, like the real API; the stand-in records which one came
    so a test can check the key was not put in the URL. A 200 with no results is how an unknown ticker answers, and over the
    free plan's 5 calls a minute the answer is a 429 with {"status": "ERROR", "error": ...}.
    """

    def __init__(self):
        self.requests: list[dict] = []
        self.rate_limit_next = 0
        self.retry_after: str | None = None
        me = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):  # noqa: N802
                url = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(url.query).items()}
                header = self.headers.get("Authorization")
                me.requests.append({"path": unquote(url.path), "query": q, "authorization": header})
                key = header[len("Bearer "):] if header and header.startswith("Bearer ") else q.get("apiKey")
                if key != POLYGON_KEY:
                    return _send(self, 401, {"status": "ERROR", "request_id": "r1", "message": "Unknown API Key"})
                if me.rate_limit_next > 0:
                    me.rate_limit_next -= 1
                    message = "You've exceeded the maximum requests per minute, please wait or upgrade your subscription to continue."
                    return _send(self, 429, {"status": "ERROR", "error": message}, {"Retry-After": me.retry_after} if me.retry_after else None)
                parts = unquote(url.path).strip("/").split("/")
                if parts[:3] != ["v2", "aggs", "ticker"] or len(parts) < 5:
                    return _send(self, 404, {"status": "NOT_FOUND", "message": "not found"})
                ticker = parts[3]
                known = ticker in POLYGON_PRICES
                empty = {"ticker": ticker, "queryCount": 0, "resultsCount": 0, "adjusted": True, "status": "OK", "request_id": "r2"}
                if parts[4] == "prev" and len(parts) == 5:
                    if not known:
                        return _send(self, 200, empty)
                    bar = {"T": ticker, **_bar(POLYGON_PRICES[ticker], date(2026, 11, 6))}
                    return _send(self, 200, {**empty, "queryCount": 1, "resultsCount": 1, "results": [bar]})
                if parts[4] == "range" and len(parts) == 9 and parts[5:7] == ["1", "day"]:
                    if not known:
                        return _send(self, 200, empty)
                    first, last = date.fromisoformat(parts[7]), date.fromisoformat(parts[8])
                    days = [first + timedelta(days=i) for i in range((last - first).days + 1)]
                    days = [d for d in days if d.weekday() < 5]  # a bar for each weekday, closing a cent higher each day
                    rows = [_bar(POLYGON_PRICES[ticker] + 0.01 * i, d) for i, d in enumerate(days)]
                    if q.get("sort") == "desc":
                        rows.reverse()
                    rows = rows[: int(q.get("limit", 5000))]
                    if not rows:
                        return _send(self, 200, empty)
                    return _send(self, 200, {**empty, "queryCount": len(rows), "resultsCount": len(rows), "results": rows})
                _send(self, 404, {"status": "NOT_FOUND", "message": "not found"})

        self._s = _Server(H)
        self.url = self._s.url

    def stop(self) -> None:
        self._s.stop()


def _bar(close: float, day: date) -> dict:
    ms = int(datetime(day.year, day.month, day.day, 5, tzinfo=UTC).timestamp() * 1000)  # the start of the New York day
    return {"o": close - 1, "h": close + 1, "l": close - 2, "c": round(close, 2), "v": 1_000_000, "vw": close, "n": 500, "t": ms}


class Twilio:
    """POST /2010-04-01/Accounts/{AccountSid}/Messages.json, written from Twilio's docs.

    HTTP basic auth with the account SID and auth token, a form body with To, From and Body (up to 1,600 characters), 201 Created
    with the message resource, and an error as JSON {code, message, more_info, status}. A trial account may only text numbers it
    has verified (error 21608).
    """

    def __init__(self):
        self.messages: list[dict] = []
        self.requests: list[dict] = []
        self.verified = {"+15558675310"}
        me = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _error(self, status: int, code: int, message: str) -> None:
                _send(self, status, {"code": code, "message": message, "more_info": f"https://www.twilio.com/docs/errors/{code}", "status": status})

            def do_POST(self):  # noqa: N802
                path = urlparse(self.path).path
                raw = self.rfile.read(int(self.headers.get("Content-Length") or 0)).decode()
                me.requests.append({
                    "path": path, "authorization": self.headers.get("Authorization"), "content_type": self.headers.get("Content-Type"), "raw": raw,
                })
                expected = "Basic " + base64.b64encode(f"{TWILIO_SID}:{TWILIO_TOKEN}".encode()).decode()
                if self.headers.get("Authorization") != expected:
                    return self._error(401, 20003, "Authenticate")
                if path != f"/2010-04-01/Accounts/{TWILIO_SID}/Messages.json":
                    return self._error(404, 20404, "The requested resource " + path + " was not found")
                if not (self.headers.get("Content-Type") or "").startswith("application/x-www-form-urlencoded"):
                    return self._error(400, 21201, "No 'To' number is specified")  # a JSON body has no form fields
                form = {k: v[0] for k, v in parse_qs(raw).items()}
                if not form.get("To"):
                    return self._error(400, 21201, "No 'To' number is specified")
                if not form.get("From") and not form.get("MessagingServiceSid"):
                    return self._error(400, 21603, "A 'From' or 'MessagingServiceSid' parameter is required")
                if not form.get("Body"):
                    return self._error(400, 21602, "Message body is required")
                if len(form["Body"]) > 1600:
                    return self._error(400, 21617, "The concatenated message body exceeds the 1600 character limit")
                if form["To"] not in me.verified:
                    return self._error(400, 21608, f"The number {form['To']} is unverified. Trial accounts cannot send messages to unverified numbers")
                me.messages.append(form)
                sid = f"SM{len(me.messages):032d}"
                message = {"sid": sid, "account_sid": TWILIO_SID, "status": "queued", "error_code": None}
                _send(self, 201, {**message, "to": form["To"], "from": form["From"], "body": form["Body"]})

        self._s = _Server(H)
        self.url = self._s.url

    def stop(self) -> None:
        self._s.stop()
