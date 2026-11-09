"""The activity log: one JSON line per event, append only, so a bad day can be replayed line by line."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path


class Ledger:
    def __init__(self, path: Path):
        self.path = Path(path)

    def append(self, routine: str, event: str, **data) -> dict:
        record = {"ts": datetime.now(UTC).isoformat(timespec="seconds"), "routine": routine, "event": event, "data": data}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, sort_keys=True, default=str) + "\n")
        return record

    def read(self, since: datetime | None = None, limit: int | None = None) -> list[dict]:
        if not self.path.exists():
            return []
        rows = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue  # a torn last line after a crash must not hide the rest
            if since is not None and datetime.fromisoformat(row["ts"]) < since:
                continue
            rows.append(row)
        return rows[-limit:] if limit else rows
