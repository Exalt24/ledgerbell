from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from ledgerbell import cli, dashboard, routines
from ledgerbell.alerts import Notifier
from ledgerbell.config import AlertSettings
from ledgerbell.alpaca import Account, Position

from .conftest import NOW
from .test_strategy_agent import GOOD, Scripted


def names(desk):
    return [e["event"] for e in desk.ledger.read()]


def test_premarket_saves_the_models_plan_and_alerts_by_email_and_text(desk, capture, cfg):
    res = routines.run("premarket", desk, NOW, Scripted(GOOD))
    assert res.status == "done"
    plan = json.loads(cfg.plan_file.read_text(encoding="utf-8"))
    assert plan["date"] == "2026-11-09" and plan["proposals"][0]["symbol"] == "AAPL"
    assert "Pre-market briefing" in capture.mails[0]["Subject"] and capture.posts


def test_premarket_without_a_model_still_runs_and_says_so(desk, capture):
    res = routines.run("premarket", desk, NOW, None)
    assert res.status == "done" and "off" in res.summary


def test_premarket_survives_a_dead_model(desk):
    res = routines.run("premarket", desk, NOW, Scripted(RuntimeError("down"), ))
    assert res.status == "done" and "agent_failed" in names(desk)


def test_open_trades_the_plan_and_the_rule_through_the_risk_rules(desk, server):
    routines.run("premarket", desk, NOW, Scripted(GOOD))
    res = routines.run("open", desk, NOW, None)
    assert res.status == "done"
    held = {p.symbol: p for p in desk.client.positions()}
    assert "AAPL" in held and held["AAPL"].qty <= 26  # the model asked for 5, the rule for more: one order, capped
    assert any(o["type"] == "stop" and o["symbol"] == "AAPL" for o in desk.client.open_orders())


def test_open_skips_on_a_closed_market(desk, server):
    _, book = server
    book.is_open = False
    res = routines.run("open", desk, NOW, None)
    assert res.status == "skipped" and desk.client.positions() == []


def test_open_refuses_to_replay_yesterdays_plan(desk, cfg, server):
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.plan_file.write_text(json.dumps({"date": "2026-11-06", "summary": "old", "proposals": [
        {"symbol": "MSFT", "side": "buy", "qty": 3, "price": 410.0, "reason": "old", "source": "agent", "side_": ""}]}), encoding="utf-8")
    routines.run("open", desk, NOW, None)
    assert "MSFT" not in {p.symbol for p in desk.client.positions()}  # MSFT trends down on the tape, so the rule never buys it


def test_every_routine_skips_while_the_kill_switch_is_engaged(desk):
    desk.engage_kill("test")
    for name in ("premarket", "open", "midday"):
        assert routines.run(name, desk, NOW, None).status == "skipped"
    assert desk.client.positions() == []


def test_eod_and_weekly_still_report_while_killed(desk, capture):
    desk.engage_kill("test")
    assert routines.run("eod", desk, NOW, None).status == "done"
    assert routines.run("weekly", desk, NOW, None).status == "done"
    assert any("End of day" in m["Subject"] for m in capture.mails)


def test_midday_engages_the_kill_switch_when_the_day_is_down_past_the_limit(desk, server):
    _, book = server
    routines.run("open", desk, NOW, None)
    assert desk.client.positions()
    book.set_prices({s: p * 0.3 for s, p in book.prices.items()})
    res = routines.run("midday", desk, NOW, None)
    assert "kill switch engaged" in res.summary and desk.kill_engaged() and desk.client.positions() == []


def test_midday_warns_when_a_position_is_halfway_to_its_stop(desk, server, capture):
    _, book = server
    book.positions["MSFT"] = {"qty": 5, "avg": 417.0}  # 410 now: down 1.7 percent, the stop is 3
    book.orders.append({"id": "o1", "symbol": "MSFT", "side": "sell", "type": "stop", "status": "new", "stop_price": "404.49",
                        "qty": "5", "legs": []})
    res = routines.run("midday", desk, NOW, None)
    assert any("halfway" in line for line in res.lines) and capture.mails


def test_eod_counts_the_days_orders_and_writes_the_dashboard(desk, cfg):
    routines.run("open", desk, NOW, None)
    res = routines.run("eod", desk, NOW, None)
    assert "orders sent" in "\n".join(res.lines)
    assert "<title>ledgerbell dashboard</title>" in (cfg.state_dir / "dashboard.html").read_text(encoding="utf-8")


def test_weekly_writes_a_report_file_from_the_ledger(desk, cfg):
    routines.run("open", desk, NOW, None)
    routines.run("eod", desk, NOW, None)
    res = routines.run("weekly", desk, NOW, None)
    report = (cfg.state_dir / "weekly-2026-11-09.txt").read_text(encoding="utf-8")
    assert "order_submitted" in report and res.status == "done"


def test_a_crashing_routine_is_logged_alerted_and_re_raised(desk, capture, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("tape unreachable")

    monkeypatch.setattr(desk.client, "daily_bars", boom)
    with pytest.raises(RuntimeError):
        routines.run("open", desk, NOW, None)
    assert "routine_crashed" in names(desk) and any("crashed" in m["Subject"] for m in capture.mails)


def test_an_unknown_routine_is_an_error(desk):
    with pytest.raises(ValueError):
        routines.run("lunch", desk, NOW)


def test_a_weekend_premarket_is_not_a_trading_day(desk, server):
    _, book = server
    book.is_open = False  # the stand-in's next_open is tomorrow at 14:30 UTC
    sunday_late = datetime(2026, 11, 8, 23, 0, tzinfo=UTC)  # 18:00 Sunday in New York: next open is Monday, not today
    assert routines.run("premarket", desk, sunday_late, None).status == "skipped"


def test_the_ledger_is_json_lines_with_a_timestamp_and_survives_a_torn_last_line(desk, cfg):
    routines.run("eod", desk, NOW, None)
    with cfg.ledger_file.open("a", encoding="utf-8") as fh:
        fh.write('{"ts": "2026-11-09T')  # a crash mid-write
    rows = desk.ledger.read()
    assert rows and all("ts" in r and "event" in r for r in rows)


def test_alerts_reach_a_real_webhook_over_http(cfg, webhook):
    url, received = webhook
    s = AlertSettings(webhook_url=url)
    assert Notifier(s).send("Subject line", "body text") == []
    assert json.loads(received[0])["text"] == "Subject line\nbody text"


def test_a_dead_webhook_is_reported_not_raised():
    problems = Notifier(AlertSettings(webhook_url="http://127.0.0.1:9/none")).send("s", "b")
    assert problems and problems[0].startswith("webhook")


def test_dashboard_shows_positions_pnl_and_the_kill_banner_and_escapes_everything():
    a = Account(98_000.0, 100_000.0, 50_000.0, 50_000.0, False)
    p = [Position("AAA", 3, 10.0, 9.0, -3.0, -0.1)]
    ev = [{"ts": "2026-11-09T14:30:00+00:00", "routine": "open", "event": "order_refused", "data": {"why": ["<script>alert(1)</script>"]}}]
    page = dashboard.render(a, p, ev, {"reason": "daily loss <b>limit</b>", "at": "x"})
    assert "KILL SWITCH ENGAGED" in page and "-2,000.00 (-2.00%)" in page and "AAA" in page
    assert "<script>alert" not in page and "&lt;script&gt;" in page and "<b>limit</b>" not in page


def test_dashboard_without_a_kill_has_no_banner():
    page = dashboard.render(Account(1.0, 1.0, 1.0, 1.0, False), [], [], None)
    assert "KILL SWITCH ENGAGED" not in page and "No open positions" in page


def test_cli_run_status_kill_unkill_and_dashboard_against_the_stand_in(cfg, tmp_path, monkeypatch, capsys):
    toml = tmp_path / "config.toml"
    toml.write_text(
        f'[alpaca]\nmode = "standin"\nbase_url = "{cfg.base_url}"\ndata_url = "{cfg.data_url}"\n'
        f'[paths]\nstate_dir = "{(tmp_path / "st").as_posix()}"\n', encoding="utf-8")
    monkeypatch.setenv("APCA_API_KEY_ID", "standin-key")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "standin-secret")
    base = ["--config", str(toml)]
    assert cli.main([*base, "run", "open", "--no-model"]) == 0
    assert cli.main([*base, "status"]) == 0 and "kill switch: off" in capsys.readouterr().out
    assert cli.main([*base, "kill", "--reason", "cli test"]) == 0
    assert cli.main([*base, "run", "open", "--no-model"]) == 3  # skipped while killed
    assert cli.main([*base, "unkill"]) == 2  # refuses without --yes
    assert cli.main([*base, "unkill", "--yes"]) == 0
    out = tmp_path / "d.html"
    assert cli.main([*base, "dashboard", "--out", str(out)]) == 0 and out.exists()


def test_cli_reports_a_missing_key_as_a_config_problem(cfg, tmp_path, monkeypatch, capsys):
    toml = tmp_path / "c.toml"
    toml.write_text(f'[alpaca]\nmode = "standin"\nbase_url = "{cfg.base_url}"\ndata_url = "{cfg.data_url}"\n', encoding="utf-8")
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    assert cli.main(["--config", str(toml), "status"]) == 2
    assert "config problem" in capsys.readouterr().err
