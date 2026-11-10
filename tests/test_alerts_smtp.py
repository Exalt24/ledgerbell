"""The email alert goes over a real SMTP conversation on a local socket, not through an injected sender."""

from __future__ import annotations

import socketserver
import threading

from ledgerbell.alerts import Notifier
from ledgerbell.config import AlertSettings


class _Handler(socketserver.StreamRequestHandler):
    def _say(self, line: str) -> None:
        self.wfile.write((line + "\r\n").encode())

    def handle(self) -> None:
        box = self.server.mailbox  # type: ignore[attr-defined]
        self._say("220 test.local ESMTP")
        data_mode = False
        lines: list[str] = []
        for raw in self.rfile:
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if data_mode:
                if line == ".":
                    data_mode = False
                    box.append("\n".join(lines))
                    lines = []
                    self._say("250 queued")
                else:
                    lines.append(line)
                continue
            cmd = line.split(" ", 1)[0].upper()
            if cmd in ("EHLO", "HELO"):
                self._say("250 test.local")
            elif cmd in ("MAIL", "RCPT"):
                self._say("250 ok")
            elif cmd == "DATA":
                data_mode = True
                self._say("354 go ahead")
            elif cmd == "QUIT":
                self._say("221 bye")
                return
            else:
                self._say("250 ok")


class _Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), _Handler)
        self.mailbox: list[str] = []


def _serve():
    server = _Server()
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def test_email_alert_reaches_a_real_smtp_server():
    server = _serve()
    try:
        settings = AlertSettings(
            email_to="owner@example.com",
            email_from="bot@example.com",
            smtp_host="127.0.0.1",
            smtp_port=server.server_address[1],
        )
        problems = Notifier(settings).send("Kill switch engaged", "Daily loss limit tripped.")
        assert problems == []
        assert len(server.mailbox) == 1
        mail = server.mailbox[0]
        assert "Subject: Kill switch engaged" in mail
        assert "To: owner@example.com" in mail
        assert "From: bot@example.com" in mail
        assert "Daily loss limit tripped." in mail
    finally:
        server.shutdown()
        server.server_close()


def test_email_alert_to_a_closed_port_is_reported_not_raised():
    server = _serve()
    port = server.server_address[1]
    server.shutdown()
    server.server_close()
    settings = AlertSettings(email_to="owner@example.com", smtp_host="127.0.0.1", smtp_port=port)
    problems = Notifier(settings).send("x", "y")
    assert len(problems) == 1 and problems[0].startswith("email:")
