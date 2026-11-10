from __future__ import annotations

import threading
from datetime import UTC, date, datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from ledgerbell import standin
from ledgerbell.alerts import Notifier
from ledgerbell.alpaca import AlpacaClient
from ledgerbell.config import AlertSettings, Config, OptionRules, RiskRules, StrategyRules
from ledgerbell.desk import Desk
from ledgerbell.ledger import Ledger

NOW = datetime(2026, 11, 9, 14, 30, tzinfo=UTC)  # a Monday, 09:30 in New York


class Capture:
    """Stands in for requests.post and for smtplib.SMTP and records what would have been sent."""

    def __init__(self):
        self.posts, self.mails = [], []
        self.fail_post = False

    def post(self, url, json=None, timeout=None):
        if self.fail_post:
            raise OSError("connection refused")
        self.posts.append((url, json))

        class R:
            status_code = 200

        return R()

    def smtp(self, host, port):
        cap = self

        class S:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def send_message(self, msg):
                cap.mails.append(msg)

        return S()


@pytest.fixture()
def server():
    srv, book = standin.start()
    yield srv, book
    srv.shutdown()


@pytest.fixture()
def cfg(server, tmp_path):
    srv, _ = server
    url = f"http://127.0.0.1:{srv.server_port}"
    return Config(
        mode="standin", base_url=url, data_url=url, state_dir=tmp_path / "state",
        risk=RiskRules(), strategy=StrategyRules(),
        alerts=AlertSettings(email_to="owner@example.com", smtp_host="localhost", webhook_url="http://hook.invalid/x"),
    )


@pytest.fixture()
def capture():
    return Capture()


@pytest.fixture()
def desk(cfg, capture):
    client = AlpacaClient(cfg, standin.KEY, standin.SECRET)
    notifier = Notifier(cfg.alerts, smtp_factory=capture.smtp, http_post=capture.post)
    return Desk(cfg, client, Ledger(cfg.ledger_file), notifier)


class _Hook(BaseHTTPRequestHandler):
    received: list = []

    def do_POST(self):  # noqa: N802
        n = int(self.headers.get("Content-Length", 0))
        _Hook.received.append(self.rfile.read(n))
        self.send_response(200)
        self.end_headers()

    def log_message(self, *a):
        pass


@pytest.fixture()
def webhook():
    _Hook.received = []
    srv = HTTPServer(("127.0.0.1", 0), _Hook)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_port}/hook", _Hook.received
    srv.shutdown()


# -- the options leg, a second market data provider and the Airtable run log ---------------------------------------------

OPTION_TODAY = date(2026, 11, 9)  # the day the option tests act on; the stand-in builds its chain relative to it


@pytest.fixture()
def oserver():
    srv, book = standin.start(today=OPTION_TODAY)
    yield srv, book
    srv.shutdown()


@pytest.fixture()
def ocfg(oserver, tmp_path):
    srv, _ = oserver
    url = f"http://127.0.0.1:{srv.server_port}"
    return Config(
        mode="standin", base_url=url, data_url=url, state_dir=tmp_path / "state",
        risk=RiskRules(), strategy=StrategyRules(), options=OptionRules(enabled=True),
        alerts=AlertSettings(email_to="owner@example.com", smtp_host="localhost", webhook_url="http://hook.invalid/x"),
    )


@pytest.fixture()
def odesk(ocfg, capture):
    client = AlpacaClient(ocfg, standin.KEY, standin.SECRET)
    notifier = Notifier(ocfg.alerts, smtp_factory=capture.smtp, http_post=capture.post)
    return Desk(ocfg, client, Ledger(ocfg.ledger_file), notifier, sleep=lambda s: None)


def contract(book, symbol="AAPL", right="call", days=12, strike=None):
    """The stand-in's contract symbol for a symbol, right, days to expiry and strike (default: the one nearest the money)."""
    rows = [c for c in book.contracts.values() if c["underlying_symbol"] == symbol and c["type"] == right
            and (date.fromisoformat(c["expiration_date"]) - book.today).days == days]
    spot = book.prices[symbol]
    rows.sort(key=lambda c: abs(float(c["strike_price"]) - (strike or spot)))
    return rows[0]["symbol"]


@pytest.fixture()
def finnhub():
    from .fakes import Finnhub

    f = Finnhub()
    yield f
    f.stop()


@pytest.fixture()
def airtable():
    from .fakes import Airtable

    a = Airtable()
    yield a
    a.stop()


@pytest.fixture()
def polygon():
    from .fakes import Polygon

    p = Polygon()
    yield p
    p.stop()


@pytest.fixture()
def twilio():
    from .fakes import Twilio

    t = Twilio()
    yield t
    t.stop()
