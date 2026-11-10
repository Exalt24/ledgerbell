"""Alerts by email, by a chat webhook and by a text message through Twilio. A failed alert is logged and never stops a routine.

Twilio's REST API (twilio.com/docs/messaging/api/message-resource): `POST /2010-04-01/Accounts/{AccountSid}/Messages.json`
with `application/x-www-form-urlencoded` fields To, From and Body (a body of up to 1,600 characters), authenticated with HTTP
basic auth. The account SID and auth token come from TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN in the environment, read when a
text is sent, never from the config file and never put in a problem string.
"""

from __future__ import annotations

import smtplib
from collections.abc import Callable
from email.message import EmailMessage
from urllib.parse import quote

import requests

from .config import AlertSettings, secret_from_env

SMS_MAX_CHARS = 1600  # Twilio's limit for one Body


class Notifier:
    def __init__(
        self,
        settings: AlertSettings,
        smtp_factory: Callable[[str, int], smtplib.SMTP] = smtplib.SMTP,
        http_post: Callable[..., requests.Response] = requests.post,
        smtp_login: tuple[str, str] | None = None,
        env: dict[str, str] | None = None,
    ):
        self.s = settings
        self._smtp_factory = smtp_factory
        self._post = http_post
        self._login = smtp_login
        self._env = env

    def send(self, subject: str, body: str) -> list[str]:
        """Returns one problem string per channel that failed (empty list means all went out)."""
        problems: list[str] = []
        if self.s.email_to and self.s.smtp_host:
            try:
                msg = EmailMessage()
                msg["Subject"] = subject
                msg["From"] = self.s.email_from or self.s.email_to
                msg["To"] = self.s.email_to
                msg.set_content(body)
                with self._smtp_factory(self.s.smtp_host, self.s.smtp_port) as smtp:
                    if self._login:
                        smtp.starttls()
                        smtp.login(*self._login)
                    smtp.send_message(msg)
            except Exception as exc:  # noqa: BLE001 - an alert must never take a routine down
                problems.append(f"email: {exc}")
        if self.s.webhook_url:
            try:
                resp = self._post(self.s.webhook_url, json={"text": f"{subject}\n{body}"}, timeout=10)
                if resp.status_code >= 400:
                    problems.append(f"webhook: HTTP {resp.status_code}")
            except Exception as exc:  # noqa: BLE001
                problems.append(f"webhook: {exc}")
        if self.s.sms_to and self.s.sms_from:
            try:
                sid = secret_from_env("TWILIO_ACCOUNT_SID", self._env)
                token = secret_from_env("TWILIO_AUTH_TOKEN", self._env)
                url = f"{self.s.twilio_url.rstrip('/')}/2010-04-01/Accounts/{quote(sid, safe='')}/Messages.json"
                form = {"To": self.s.sms_to, "From": self.s.sms_from, "Body": f"{subject}\n{body}"[:SMS_MAX_CHARS]}
                resp = self._post(url, data=form, auth=(sid, token), timeout=10)
                if resp.status_code >= 400:
                    problems.append(f"sms: HTTP {resp.status_code}{_twilio_reason(resp)}")
            except Exception as exc:  # noqa: BLE001
                problems.append(f"sms: {exc}")
        return problems


def _twilio_reason(resp: requests.Response) -> str:
    """Twilio answers an error with JSON {"code", "message", "more_info", "status"}; name the code and message and nothing else."""
    try:
        data = resp.json()
        return f" (code {data['code']}: {str(data['message'])[:120]})"
    except (ValueError, KeyError, TypeError):
        return ""
