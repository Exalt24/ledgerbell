"""The heartbeat file: what it holds, and that it never holds an account figure."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from ledgerbell import cli, heartbeat
from ledgerbell.config import Config
from ledgerbell.ledger import Ledger

NOW = datetime(2026, 11, 12, 12, 0, tzinfo=UTC)


def test_the_last_ledger_row_gives_the_last_event_and_routine():
    rows = [{"ts": "2026-11-11T20:15:00+00:00", "routine": "eod", "event": "recap", "data": {"equity": 100000.0}}]
    beat = heartbeat.build(rows, False, "paper", NOW)
    assert beat == {
        "generated_at": "2026-11-12T12:00:00+00:00", "last_event_at": "2026-11-11T20:15:00+00:00",
        "last_routine": "eod", "kill_engaged": False, "mode": "paper",
    }


def test_an_empty_ledger_has_no_last_event():
    beat = heartbeat.build([], False, "paper", NOW)
    assert beat["last_event_at"] is None and beat["last_routine"] == ""


def test_the_file_never_carries_an_account_figure_or_a_symbol(tmp_path):
    cfg = Config(state_dir=tmp_path / "state")
    Ledger(cfg.ledger_file).append("eod", "recap", equity=123456.78, day_pl=-99.5, positions=3)
    out = tmp_path / "site" / "status.json"
    beat = heartbeat.write(cfg, out)
    text = out.read_text(encoding="utf-8")
    assert "123456" not in text and "99.5" not in text and "equity" not in text
    assert json.loads(text) == beat and beat["last_routine"] == "eod"


def test_an_engaged_kill_file_is_reported(tmp_path):
    cfg = Config(state_dir=tmp_path / "state")
    cfg.state_dir.mkdir()
    cfg.kill_file.write_text("loss limit", encoding="utf-8")
    assert heartbeat.write(cfg, tmp_path / "s.json")["kill_engaged"] is True


def test_the_heartbeat_command_needs_no_broker_keys(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    conf = tmp_path / "c.toml"
    conf.write_text("[paths]\nstate_dir = '" + (tmp_path / "state").as_posix() + "'\n", encoding="utf-8")
    out = tmp_path / "site" / "status.json"
    assert cli.main(["--config", str(conf), "heartbeat", "--out", str(out)]) == 0
    assert Path(out).exists() and "kill switch off" in capsys.readouterr().out
