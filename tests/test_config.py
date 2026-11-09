from __future__ import annotations

from pathlib import Path

import pytest

from ledgerbell import config
from ledgerbell.routines import ROUTINES

ROOT = Path(__file__).resolve().parents[1]


def test_the_example_config_loads_and_is_paper_mode():
    cfg = config.load(ROOT / "config.example.toml")
    assert cfg.mode == "paper" and cfg.base_url == "https://paper-api.alpaca.markets"
    assert cfg.risk.max_position_pct == 5.0 and cfg.strategy.watchlist[0] == "SPY"


def test_a_nonsense_mode_is_refused(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[alpaca]\nmode = "yolo"\n', encoding="utf-8")
    with pytest.raises(config.ConfigError):
        config.load(p)


def test_a_bad_risk_value_in_the_file_is_refused_at_load(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text("[risk]\nmax_position_pct = 90\n", encoding="utf-8")
    with pytest.raises(config.ConfigError):
        config.load(p)


def test_the_crontab_schedules_every_routine_on_weekdays_in_new_york_time():
    text = (ROOT / "deploy" / "crontab").read_text(encoding="utf-8")
    assert "CRON_TZ=America/New_York" in text
    for name in ROUTINES:
        assert f"ledgerbell run {name}" in text


def test_the_example_config_has_the_new_sections_switched_off_and_on_alpaca_data():
    cfg = config.load(ROOT / "config.example.toml")
    assert cfg.options.enabled is False and cfg.options.stop_premium_pct == 50.0 and cfg.options.feed == "indicative"
    assert cfg.market_data.provider == "alpaca" and cfg.airtable.enabled is False


def test_a_config_can_turn_the_options_leg_finnhub_and_the_log_on(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text(
        '[options]\nenabled = true\nmax_premium_pct = 2.5\n[market_data]\nprovider = "finnhub"\n[airtable]\nenabled = true\nbase_id = "appX"\n',
        encoding="utf-8",
    )
    cfg = config.load(p)
    assert cfg.options.enabled and cfg.options.max_premium_pct == 2.5 and cfg.market_data.provider == "finnhub" and cfg.airtable.base_id == "appX"
