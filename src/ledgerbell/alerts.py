"""Alerts by email and by a text-message webhook. A failed alert is logged and never stops a routine."""

from __future__ import annotations

import smtplib
from collections.abc import Callable
from email.message import EmailMessage

import requests

from .config import AlertSettings


class Notifier:
    def __init__(
        self,
        settings: AlertSettings,
        smtp_factory: Callable[[str, int], smtplib.SMTP] = smtplib.SMTP,
        http_post: Callable[..., requests.Response] = requests.post,
        smtp_login: tuple[str, str] | None = None,
    ):
        self.s = settings
        self._smtp_factory = smtp_factory
        self._post = http_post
        self._login = smtp_login

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
        return problems
