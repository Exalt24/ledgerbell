"""The HTTP API, driven through FastAPI's test client: verdicts match `risk.check`, the key is required, and the alert route cannot be aimed at anyone."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from ledgerbell import api
from ledgerbell.alerts import Notifier
from ledgerbell.config import AlertSettings, Config, ConfigError

KEY = "test-api-key-0123456789"
H = {"X-Api-Key": KEY}
ACCOUNT = {"equity": 100000, "last_equity": 100000, "cash": 100000, "buying_power": 100000}


def check_body(**over):
    proposal = {"symbol": "SPY", "side": "buy", "qty": 10, "price": 500.0, "reason": "test"}
    body = {"proposal": proposal, "account": ACCOUNT, "positions": [], "kill_engaged": False}
    body.update(over)
    return body


@pytest.fixture()
def sent():
    return []


@pytest.fixture()
def client(sent):
    class Spy(Notifier):
        def send(self, subject, body):
            sent.append((subject, body))
            return []

    return TestClient(api.create_app(Config(), KEY, Spy(AlertSettings())))


def test_health_needs_no_key_and_reveals_no_secret(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json() == {"status": "ok", "mode": "paper"}
    assert KEY not in r.text


def test_a_good_order_is_allowed_with_a_stop(client):
    r = client.post("/risk/check", json=check_body(), headers=H)
    assert r.status_code == 200
    v = r.json()
    assert v["allowed"] is True and v["qty"] == 10 and v["stop_price"] == 485.0 and v["reasons"] == []


def test_an_oversized_order_is_cut_to_the_position_limit(client):
    r = client.post("/risk/check", json=check_body(proposal={"symbol": "SPY", "side": "buy", "qty": 100, "price": 500.0}), headers=H)
    v = r.json()
    assert v["allowed"] is True and v["qty"] == 10 and "reduced from 100 to 10" in v["reasons"][0]


def test_a_symbol_off_the_watchlist_is_refused_with_its_reason(client):
    v = client.post("/risk/check", json=check_body(proposal={"symbol": "GME", "side": "buy", "qty": 1, "price": 20.0}), headers=H).json()
    assert v["allowed"] is False and v["reasons"] == ["GME is not on the watchlist"]


def test_a_lower_case_symbol_is_normalised(client):
    assert client.post("/risk/check", json=check_body(proposal={"symbol": "spy", "side": "buy", "qty": 1, "price": 500.0}), headers=H).json()["allowed"] is True


def test_the_kill_switch_flag_refuses_everything(client):
    v = client.post("/risk/check", json=check_body(kill_engaged=True), headers=H).json()
    assert v["allowed"] is False and v["reasons"] == ["kill switch is engaged"]


def test_the_daily_loss_limit_refuses_a_buy(client):
    acct = {**ACCOUNT, "equity": 97000}
    v = client.post("/risk/check", json=check_body(account=acct), headers=H).json()
    assert v["allowed"] is False and "daily loss limit" in v["reasons"][0]


def test_a_short_sale_is_refused(client):
    v = client.post("/risk/check", json=check_body(proposal={"symbol": "SPY", "side": "sell", "qty": 5, "price": 500.0}), headers=H).json()
    assert v["allowed"] is False and "short selling is not allowed" in v["reasons"][0]


def test_a_held_position_counts_against_the_size_limit(client):
    held = {"symbol": "SPY", "qty": 10, "avg_entry_price": 500.0, "current_price": 500.0}
    v = client.post("/risk/check", json=check_body(positions=[held]), headers=H).json()
    assert v["allowed"] is False and "no room" in v["reasons"][0]


@pytest.mark.parametrize("route", ["/risk/check", "/alerts"])
def test_a_missing_or_wrong_key_is_a_401_on_every_protected_route(client, route):
    body = check_body() if route == "/risk/check" else {"subject": "s", "body": "b"}
    assert client.post(route, json=body).status_code == 401
    assert client.post(route, json=body, headers={"X-Api-Key": "wrong"}).status_code == 401
    assert client.post(route, json=body, headers={"X-Api-Key": KEY[:-1]}).status_code == 401


def test_an_app_with_no_key_cannot_be_built():
    with pytest.raises(ConfigError):
        api.create_app(Config(), "")


def test_bad_input_is_a_422_and_never_a_500(client):
    for body in (
        check_body(proposal={"symbol": "SPY", "side": "buy", "qty": "ten", "price": 500.0}),
        check_body(account={**ACCOUNT, "equity": -1}),
        check_body(extra_field=1),
        {"account": ACCOUNT},
    ):
        assert client.post("/risk/check", json=body, headers=H).status_code == 422


def test_a_zero_price_is_a_refusal_not_an_error(client):
    v = client.post("/risk/check", json=check_body(proposal={"symbol": "SPY", "side": "buy", "qty": 1, "price": 0}), headers=H).json()
    assert v["allowed"] is False and v["reasons"] == ["no usable price"]


def test_an_alert_is_handed_to_the_notifier(client, sent):
    r = client.post("/alerts", json={"subject": "Kill switch engaged", "body": "Daily loss limit tripped."}, headers=H)
    assert r.status_code == 200 and r.json() == {"sent": True, "problems": []}
    assert sent == [("Kill switch engaged", "Daily loss limit tripped.")]


def test_an_alert_cannot_name_a_recipient(client, sent):
    r = client.post("/alerts", json={"subject": "s", "body": "b", "to": "victim@example.com"}, headers=H)
    assert r.status_code == 422 and sent == []


def test_channel_problems_come_back_in_the_answer():
    class Failing(Notifier):
        def send(self, subject, body):
            return ["ses: MessageRejected: Email address is not verified"]

    c = TestClient(api.create_app(Config(), KEY, Failing(AlertSettings())))
    r = c.post("/alerts", json={"subject": "s", "body": "b"}, headers=H)
    assert r.json() == {"sent": False, "problems": ["ses: MessageRejected: Email address is not verified"]}


def test_the_docs_pages_are_off(client):
    assert client.get("/docs").status_code == 404 and client.get("/openapi.json").status_code == 404


def test_the_app_is_built_from_the_environment_the_way_modal_starts_it():
    env = {"LEDGERBELL_API_KEY": KEY, "LEDGERBELL_SES_TO": "owner@example.com", "LEDGERBELL_SES_FROM": "alerts@example.com"}
    env["LEDGERBELL_SES_REGION"] = "ap-southeast-2"
    c = TestClient(api.app_from_env(env))
    assert c.get("/health").status_code == 200
    assert c.post("/risk/check", json=check_body(), headers=H).json()["allowed"] is True
    with pytest.raises(ConfigError):
        api.app_from_env({})
