from __future__ import annotations

import json
from datetime import date

from ledgerbell.risk import Proposal

TODAY = date(2026, 11, 9)


def prop(sym="AAPL", side="buy", qty=10, price=190.0, source="strategy"):
    return Proposal(sym, side, qty, price, "test", source)


def events(desk, name):
    return [e for e in desk.ledger.read() if e["event"] == name]


def test_a_buy_goes_through_with_a_stop_and_a_ledger_line(desk):
    out = desk.execute(prop(qty=20), "open", TODAY)
    assert out.submitted and out.verdict.stop_price == round(190.0 * 0.97, 2)
    line = events(desk, "order_submitted")[0]["data"]
    assert line["symbol"] == "AAPL" and line["stop_price"] == 184.3
    assert any(o["type"] == "stop" for o in desk.client.open_orders())


def test_a_refused_order_never_reaches_the_broker_and_says_why(desk, server):
    _, book = server
    out = desk.execute(prop("TSLA"), "open", TODAY)
    assert not out.submitted and book.orders == []
    assert "watchlist" in events(desk, "order_refused")[0]["data"]["why"][0]


def test_the_size_limit_is_applied_before_the_broker_sees_the_order(desk):
    out = desk.execute(prop(qty=1000), "open", TODAY)  # 5 percent of 100k at 190 is 26 shares
    assert out.submitted and out.verdict.qty == 26
    assert desk.client.positions()[0].qty == 26


def test_running_the_same_routine_twice_does_not_double_the_order(desk):
    desk.execute(prop(qty=5), "open", TODAY)
    again = desk.execute(prop(qty=5), "open", TODAY)
    assert not again.submitted and "client_order_id" in again.error
    assert desk.client.positions()[0].qty == 5


def test_no_buying_power_is_refused_by_the_risk_rules_before_the_broker(desk, server):
    _, book = server
    book.cash = 100.0
    out = desk.execute(prop(qty=5), "open", TODAY)
    assert not out.submitted and events(desk, "order_refused")


def test_sell_closes_the_position(desk):
    desk.execute(prop(qty=10), "open", TODAY)
    out = desk.execute(prop(side="sell", qty=10), "open", TODAY)
    assert out.submitted and desk.client.positions() == []


def test_kill_switch_cancels_orders_closes_positions_blocks_trading_and_alerts(desk, capture, cfg):
    desk.execute(prop(qty=10), "open", TODAY)
    info = desk.engage_kill("test kill")
    assert info["problems"] == []
    assert desk.client.positions() == [] and desk.client.open_orders() == []
    assert cfg.kill_file.exists() and json.loads(cfg.kill_file.read_text(encoding="utf-8"))["reason"] == "test kill"
    out = desk.execute(prop(), "open", TODAY)
    assert not out.submitted and "kill switch" in out.verdict.reasons[0]
    assert capture.mails and capture.posts and "KILL SWITCH" in capture.mails[0]["Subject"]


def test_kill_flag_is_written_even_when_the_broker_is_down(desk, cfg):
    def boom():
        raise OSError("broker unreachable")

    desk.client.close_all_positions = boom
    desk.client.cancel_all_orders = boom
    info = desk.engage_kill("broker down")
    assert cfg.kill_file.exists() and len(info["problems"]) == 2
    assert events(desk, "kill_engaged")[0]["data"]["problems"]


def test_a_failing_alert_does_not_stop_the_kill(desk, capture, cfg):
    capture.fail_post = True
    desk.notifier._smtp_factory = lambda h, p: (_ for _ in ()).throw(OSError("smtp down"))
    desk.engage_kill("alerts down")
    assert cfg.kill_file.exists() and events(desk, "alert_failed")


def test_a_kill_file_touched_by_hand_blocks_trading(desk, cfg):
    cfg.state_dir.mkdir(parents=True, exist_ok=True)
    cfg.kill_file.write_text("", encoding="utf-8")
    assert desk.kill_engaged() and desk.kill_info()["reason"] == "KILL file present"
    assert not desk.execute(prop(), "open", TODAY).submitted


def test_clear_kill_needs_the_file_and_logs_it(desk):
    assert desk.clear_kill() is False
    desk.engage_kill("x")
    assert desk.clear_kill() is True and not desk.kill_engaged()
    assert events(desk, "kill_cleared")


def test_loss_limit_engages_the_kill_switch(desk, server):
    _, book = server
    desk.execute(prop(qty=26), "open", TODAY)  # about 5 percent of the account in one name
    book.set_prices({"AAPL": 190.0 * 0.5})  # the name halves: account down about 2.5 percent
    assert desk.enforce_loss_limit("midday") is True
    assert desk.kill_engaged() and desk.client.positions() == []
    assert "daily loss limit" in desk.kill_info()["reason"]


def test_loss_inside_the_limit_leaves_trading_on(desk, server):
    _, book = server
    desk.execute(prop(qty=26), "open", TODAY)
    book.set_prices({"AAPL": 190.0 * 0.9})
    assert desk.enforce_loss_limit("midday") is False and not desk.kill_engaged()


def test_a_stop_fires_on_the_tape_and_the_position_is_gone(desk, server):
    _, book = server
    desk.execute(prop(qty=10), "open", TODAY)
    book.set_prices({"AAPL": 180.0})  # through the 184.30 stop
    assert desk.client.positions() == []


def test_ensure_stops_places_a_missing_stop_at_the_configured_distance(desk, server):
    _, book = server
    book.positions["MSFT"] = {"qty": 5, "avg": 400.0}  # a position with no stop at all
    notes = desk.ensure_stops("midday", TODAY)
    assert notes == ["MSFT: stop placed at 388.00"]
    assert any(o["symbol"] == "MSFT" and o["stop_price"] == "388.00" for o in desk.client.open_orders())


def test_ensure_stops_closes_a_position_that_is_already_through_its_stop(desk, server):
    _, book = server
    book.positions["MSFT"] = {"qty": 5, "avg": 450.0}  # the tape says 410: down about 9 percent
    notes = desk.ensure_stops("midday", TODAY)
    assert "closed" in notes[0] and desk.client.positions() == []


def test_ensure_stops_leaves_a_covered_position_alone(desk):
    desk.execute(prop(qty=10), "open", TODAY)
    assert desk.ensure_stops("midday", TODAY) == []


def test_flatten_finds_stop_legs_nested_under_a_parent_order(desk):
    leg = {"symbol": "AAPL", "side": "sell", "type": "stop", "legs": []}
    parent = {"symbol": "AAPL", "side": "buy", "type": "market", "legs": [leg]}
    assert [o["type"] for o in desk._flatten([parent])] == ["market", "stop"]
