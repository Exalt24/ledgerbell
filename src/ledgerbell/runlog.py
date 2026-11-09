"""A run log in Airtable: one summary row per routine run, written through Airtable's REST API.

Create records: `POST https://api.airtable.com/v0/{baseId}/{tableIdOrName}` with `{"records": [{"fields": {...}}]}`,
a personal access token in the `Authorization: Bearer` header. A record is read back with `GET .../{recordId}`.
Airtable allows 5 requests a second per base and answers 429 beyond that; the documented wait is 30 seconds, so the
client waits and tries once more. A log failure never stops a routine: the caller records it in the ledger.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote

import requests

API = "https://api.airtable.com"

FIELDS = ("Routine", "Run at", "Status", "Orders suggested", "Orders placed", "Orders refused", "Mode", "Summary")


class AirtableError(RuntimeError):
    def __init__(self, status: int, message: str):
        super().__init__(f"airtable {status}: {message}")
        self.status = status


@dataclass(frozen=True)
class RunRow:
    routine: str
    at: datetime
    status: str  # done, skipped or crashed
    suggested: int
    placed: int
    refused: int
    mode: str
    summary: str

    def fields(self) -> dict:
        return {
            "Routine": self.routine,
            "Run at": self.at.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
            "Status": self.status,
            "Orders suggested": self.suggested,
            "Orders placed": self.placed,
            "Orders refused": self.refused,
            "Mode": self.mode,
            "Summary": self.summary[:2000],
        }


class RunLog:
    def __init__(
        self, base_id: str, table: str, token: str, url: str = API, session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.base_id, self.table, self.url = base_id, table, url.rstrip("/")
        self._http = session or requests.Session()
        self._http.headers.update({"Authorization": f"Bearer {token}"})
        self._sleep = sleep

    def _endpoint(self, record_id: str | None = None) -> str:
        path = f"{self.url}/v0/{self.base_id}/{quote(self.table, safe='')}"
        return path + (f"/{record_id}" if record_id else "")

    def _call(self, method: str, url: str, **kwargs) -> dict:
        for attempt in range(2):
            resp = self._http.request(method, url, timeout=20, **kwargs)
            if resp.status_code == 429 and attempt == 0:
                self._sleep(30)  # Airtable's documented wait after a 429
                continue
            if resp.status_code >= 400:
                try:
                    err = resp.json().get("error", {})
                    message = err.get("message", resp.text) if isinstance(err, dict) else str(err)
                except ValueError:
                    message = resp.text
                raise AirtableError(resp.status_code, str(message)[:200])
            return resp.json()
        raise AssertionError("unreachable")  # pragma: no cover

    def append(self, row: RunRow) -> str:
        """Add the row and return its record id."""
        body = self._call("POST", self._endpoint(), json={"records": [{"fields": row.fields()}]})
        return body["records"][0]["id"]

    def read(self, record_id: str) -> dict:
        return self._call("GET", self._endpoint(record_id))["fields"]
