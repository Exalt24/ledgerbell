"""The SES alert goes through boto3 over real HTTP to a local stand-in written from the SES v2 documentation, not through an injected sender."""

from __future__ import annotations

import json
import socket
from pathlib import Path

import pytest

from ledgerbell import config, ses
from ledgerbell.alerts import Notifier
from ledgerbell.config import AlertSettings

from .fakes import AWS_KEY_ID, AWS_SECRET, SES_REGION


def settings(stand_in, to="owner@example.com", sender="alerts@example.com") -> AlertSettings:
    return AlertSettings(ses_to=to, ses_from=sender, ses_region=SES_REGION, ses_endpoint_url=stand_in.url)


def test_an_alert_reaches_sesv2_signed_for_the_ses_service_in_the_region(aws_env, ses):
    problems = Notifier(settings(ses)).send("Kill switch engaged", "Daily loss limit tripped.")
    assert problems == []
    req = ses.requests[0]
    assert req["path"] == "/v2/email/outbound-emails"
    assert f"Credential={AWS_KEY_ID}/" in req["authorization"] and f"/{SES_REGION}/ses/aws4_request" in req["authorization"]
    assert ses.sent == [{"from": "alerts@example.com", "to": ["owner@example.com"], "subject": "Kill switch engaged", "text": "Daily loss limit tripped."}]


def test_the_secret_key_is_never_in_the_request_or_a_problem_string(aws_env, ses):
    Notifier(settings(ses)).send("a", "b")
    req = ses.requests[0]
    assert AWS_SECRET not in req["raw"] and AWS_SECRET not in req["authorization"] and AWS_SECRET not in req["path"]
    bad = Notifier(settings(ses, to="stranger@example.com")).send("a", "b")
    assert len(bad) == 1 and AWS_SECRET not in bad[0] and AWS_KEY_ID not in bad[0]
    assert not [f for f in AlertSettings.__dataclass_fields__ if "secret" in f or "key" in f or "token" in f]  # no config field can hold one


def test_a_subject_with_a_line_break_is_sent_as_one_line(aws_env, ses):
    assert Notifier(settings(ses)).send("first line\nBcc: someone@else.example", "body") == []
    assert ses.sent[0]["subject"] == "first line Bcc: someone@else.example"


def test_a_subject_is_cut_to_200_characters():
    assert len(ses.one_line("x" * 500)) == 200 and ses.one_line("  a   b  ") == "a b"


def test_an_unverified_recipient_in_the_sandbox_is_reported_with_the_ses_code_and_never_raised(aws_env, ses):
    problems = Notifier(settings(ses, to="stranger@example.com")).send("x", "y")
    assert len(problems) == 1 and problems[0].startswith("ses: MessageRejected: Email address is not verified")
    assert ses.sent == []


def test_an_unverified_sender_is_refused_too(aws_env, ses):
    problems = Notifier(settings(ses, sender="nobody@example.com")).send("x", "y")
    assert len(problems) == 1 and "MessageRejected" in problems[0] and "nobody@example.com" in problems[0]


def test_throttling_is_reported_by_name(aws_env, ses):
    ses.throttle = True
    problems = Notifier(settings(ses)).send("x", "y")
    assert len(problems) == 1 and "TooManyRequestsException" in problems[0]


def test_bad_credentials_are_a_reported_403_and_the_secret_is_not_in_it(monkeypatch, aws_env, ses):
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "AKIA" + "X" * 16)
    problems = Notifier(settings(ses)).send("x", "y")
    assert len(problems) == 1 and "UnrecognizedClientException" in problems[0] and AWS_SECRET not in problems[0]


def test_a_dead_port_is_reported_not_raised(aws_env):
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    st = AlertSettings(ses_to="owner@example.com", ses_from="alerts@example.com", ses_region=SES_REGION, ses_endpoint_url=f"http://127.0.0.1:{port}")
    problems = Notifier(st).send("x", "y")
    assert len(problems) == 1 and problems[0].startswith("ses:") and AWS_SECRET not in problems[0]


def test_no_alert_is_sent_unless_both_addresses_are_set(aws_env, ses):
    url = ses.url
    cases = (AlertSettings(ses_endpoint_url=url), AlertSettings(ses_to="owner@example.com", ses_endpoint_url=url))
    for s in (*cases, AlertSettings(ses_from="alerts@example.com", ses_endpoint_url=url)):
        assert Notifier(s).send("x", "y") == []
    assert ses.requests == []


def test_a_failed_ses_alert_does_not_stop_the_webhook_and_the_webhook_does_not_hide_it(aws_env, ses, webhook):
    url, received = webhook
    st = AlertSettings(webhook_url=url, ses_to="stranger@example.com", ses_from="alerts@example.com", ses_region=SES_REGION, ses_endpoint_url=ses.url)
    problems = Notifier(st).send("x", "y")
    assert len(received) == 1 and len(problems) == 1 and problems[0].startswith("ses:")


def test_a_missing_boto3_is_a_named_problem(monkeypatch):
    import builtins

    real = builtins.__import__

    def no_boto(name, *a, **k):
        if name == "boto3":
            raise ImportError("no boto3")
        return real(name, *a, **k)

    monkeypatch.setattr(builtins, "__import__", no_boto)
    problems = Notifier(AlertSettings(ses_to="owner@example.com", ses_from="alerts@example.com")).send("x", "y")
    assert problems == ['ses: the SES alert needs boto3: pip install -e ".[aws]"']


def test_send_needs_both_addresses():
    with pytest.raises(ses.SesError, match="both needed"):
        ses.send(AlertSettings(ses_to="owner@example.com"), "x", "y", client_factory=lambda s: None)


def test_the_ses_settings_load_from_the_toml_and_the_example_has_them_off(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[alerts]\nses_to = "owner@example.com"\nses_from = "alerts@example.com"\nses_region = "ap-southeast-2"\n', encoding="utf-8")
    a = config.load(p).alerts
    assert (a.ses_to, a.ses_from, a.ses_region) == ("owner@example.com", "alerts@example.com", "ap-southeast-2")
    example = config.load(Path(__file__).resolve().parents[1] / "config.example.toml").alerts
    assert example.ses_to == "" and example.ses_from == "" and example.ses_endpoint_url == ""


def test_the_request_body_is_the_documented_shape(aws_env, ses):
    Notifier(settings(ses)).send("S", "B")
    body = json.loads(ses.requests[0]["raw"])
    assert body == {
        "FromEmailAddress": "alerts@example.com",
        "Destination": {"ToAddresses": ["owner@example.com"]},
        "Content": {"Simple": {"Subject": {"Data": "S", "Charset": "UTF-8"}, "Body": {"Text": {"Data": "B", "Charset": "UTF-8"}}}},
    }
