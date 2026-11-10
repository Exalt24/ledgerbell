"""The text message alert goes over real HTTP to a local stand-in written from Twilio's documentation, not through an injected sender."""

from __future__ import annotations

import base64
import socket
from pathlib import Path
from urllib.parse import parse_qs

from ledgerbell import config, standin
from ledgerbell.alerts import Notifier
from ledgerbell.alpaca import AlpacaClient
from ledgerbell.config import AlertSettings, Config
from ledgerbell.desk import Desk
from ledgerbell.ledger import Ledger

from .fakes import TWILIO_SID, TWILIO_TOKEN

ENV = {"TWILIO_ACCOUNT_SID": TWILIO_SID, "TWILIO_AUTH_TOKEN": TWILIO_TOKEN}


def settings(twilio, to="+15558675310", sender="+15557122661") -> AlertSettings:
    return AlertSettings(sms_to=to, sms_from=sender, twilio_url=twilio.url)


def test_a_text_reaches_twilios_messages_endpoint_with_basic_auth_and_a_form_body(twilio):
    problems = Notifier(settings(twilio), env=ENV).send("Kill switch engaged", "Daily loss limit tripped.")
    assert problems == []
    req = twilio.requests[0]
    assert req["path"] == f"/2010-04-01/Accounts/{TWILIO_SID}/Messages.json"
    assert req["authorization"] == "Basic " + base64.b64encode(f"{TWILIO_SID}:{TWILIO_TOKEN}".encode()).decode()
    assert req["content_type"].startswith("application/x-www-form-urlencoded")
    assert parse_qs(req["raw"]) == {"To": ["+15558675310"], "From": ["+15557122661"], "Body": ["Kill switch engaged\nDaily loss limit tripped."]}
    assert twilio.messages == [{"To": "+15558675310", "From": "+15557122661", "Body": "Kill switch engaged\nDaily loss limit tripped."}]


def test_the_token_is_only_ever_in_the_authorization_header(twilio):
    Notifier(settings(twilio), env=ENV).send("a", "b")
    req = twilio.requests[0]
    assert TWILIO_TOKEN not in req["path"] and TWILIO_TOKEN not in req["raw"]
    assert not [f for f in AlertSettings.__dataclass_fields__ if "token" in f or "secret" in f or "sid" in f]  # no config field can hold it


def test_a_long_alert_is_cut_to_twilios_1600_character_body_limit(twilio):
    assert Notifier(settings(twilio), env=ENV).send("Weekly", "x" * 5000) == []
    body = twilio.messages[0]["Body"]
    assert len(body) == 1600 and body.startswith("Weekly\nxxx")


def test_a_4xx_from_twilio_is_reported_with_its_code_and_never_raised(twilio):
    problems = Notifier(settings(twilio, to="+15550001111"), env=ENV).send("x", "y")
    assert len(problems) == 1 and problems[0].startswith("sms: HTTP 400 (code 21608: ")
    assert twilio.messages == []


def test_a_wrong_auth_token_is_a_reported_401_and_the_token_is_not_in_the_problem(twilio):
    problems = Notifier(settings(twilio), env={**ENV, "TWILIO_AUTH_TOKEN": "not-the-token"}).send("x", "y")
    assert problems == ["sms: HTTP 401 (code 20003: Authenticate)"] and "not-the-token" not in problems[0]


def test_a_text_to_a_dead_port_is_reported_not_raised():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    problems = Notifier(AlertSettings(sms_to="+15558675310", sms_from="+15557122661", twilio_url=f"http://127.0.0.1:{port}"), env=ENV).send("x", "y")
    assert len(problems) == 1 and problems[0].startswith("sms:") and TWILIO_TOKEN not in problems[0]


def test_missing_credentials_are_reported_not_raised_and_nothing_is_sent(twilio):
    for env in ({}, {"TWILIO_ACCOUNT_SID": TWILIO_SID}, {"TWILIO_AUTH_TOKEN": TWILIO_TOKEN}):
        problems = Notifier(settings(twilio), env=env).send("x", "y")
        assert len(problems) == 1 and problems[0].startswith("sms: set TWILIO_")
    assert twilio.requests == []


def test_no_text_is_sent_unless_both_numbers_are_set(twilio):
    url = twilio.url
    for s in (AlertSettings(twilio_url=url), AlertSettings(sms_to="+15558675310", twilio_url=url), AlertSettings(sms_from="+15557122661", twilio_url=url)):
        assert Notifier(s, env=ENV).send("x", "y") == []
    assert twilio.requests == []


def test_a_failed_text_does_not_stop_the_webhook_and_the_webhook_does_not_hide_a_failed_text(twilio, webhook):
    url, received = webhook
    s = AlertSettings(webhook_url=url, sms_to="+15550001111", sms_from="+15557122661", twilio_url=twilio.url)
    problems = Notifier(s, env=ENV).send("x", "y")
    assert len(received) == 1 and len(problems) == 1 and problems[0].startswith("sms:")


def test_the_kill_switch_texts_the_owner(tmp_path, twilio, server):
    srv, _ = server
    url = f"http://127.0.0.1:{srv.server_port}"
    cfg = Config(mode="standin", base_url=url, data_url=url, state_dir=tmp_path / "state", alerts=settings(twilio))
    desk = Desk(cfg, AlpacaClient(cfg, standin.KEY, standin.SECRET), Ledger(cfg.ledger_file), Notifier(cfg.alerts, env=ENV))
    desk.engage_kill("loss limit")
    assert len(twilio.messages) == 1 and "kill" in twilio.messages[0]["Body"].lower()


def test_the_sms_settings_load_from_the_toml_and_the_example_has_them_off(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[alerts]\nsms_to = "+15558675310"\nsms_from = "+15557122661"\ntwilio_url = "http://127.0.0.1:9"\n', encoding="utf-8")
    a = config.load(p).alerts
    assert (a.sms_to, a.sms_from, a.twilio_url) == ("+15558675310", "+15557122661", "http://127.0.0.1:9")
    example = config.load(Path(__file__).resolve().parents[1] / "config.example.toml").alerts
    assert example.sms_to == "" and example.sms_from == "" and example.twilio_url == "https://api.twilio.com"
