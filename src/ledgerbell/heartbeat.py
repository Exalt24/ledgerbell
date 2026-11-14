"""The heartbeat file the Netlify status function reads: when the bot last did anything, which routine, and whether the kill switch is engaged.

It holds no account figure, no symbol and no order, so it is safe to publish on a public site.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from .config import Config
from .ledger import Ledger


def build(rows: list[dict], kill_engaged: bool, mode: str, now: datetime | None = None) -> dict:
    now = now or datetime.now(UTC)
    last = rows[-1] if rows else None
    return {
        "generated_at": now.isoformat(timespec="seconds"),
        "last_event_at": last["ts"] if last else None,
        "last_routine": last["routine"] if last else "",
        "kill_engaged": kill_engaged,
        "mode": mode,
    }


def write(cfg: Config, out: Path, now: datetime | None = None) -> dict:
    beat = build(Ledger(cfg.ledger_file).read(limit=1), cfg.kill_file.exists(), cfg.mode, now)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(beat, indent=2) + "\n", encoding="utf-8")
    return beat
