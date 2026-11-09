from __future__ import annotations

from datetime import date

from ledgerbell import options, routines, standin
from ledgerbell.alpaca import AlpacaClient
from ledgerbell.config import Config, OptionRules
from ledgerbell.occ import is_option_symbol
from ledgerbell.risk import OptionProposal, Proposal

from .conftest import NOW, OPTION_TODAY, contract

TODAY = OPTION_TODAY


def events(desk, name):
    return [e for e in desk.ledger.read() if e["event"] == name]


def entry(book, price=None, qty=1, **kw):
    sym = contract(book)
    return OptionProposal(sym, qty, price or book.option_quote(sym)[1], "test", **kw)


# -- reading contracts and snapshots ------------------------------------------------------------------------------------


def test_contracts_are_listed_for_the_expiry_window_only(odesk, oserver):
    rows = options.list_contracts(odesk.client, "AAPL", TODAY, odesk.cfg.options)
    days = {(c.expiry - TODAY).days for c in rows}
    assert days == {12, 26}  # the 3 day and the 70 day expiries sit outside 7 to 45 days
    assert rows == sorted(rows, key=lambda c: (c.expiry, c.right, c.strike)) and all(c.underlying == "AAPL" and c.tradable for c in rows)


def test_without_a_window_alpaca_only_returns_up_to_the_next_weekend(odesk, oserver):
    raw = odesk.client._call("GET", odesk.client._t("/v2/options/contracts"), params={"underlying_symbols": "AAPL"})
    assert {(date.fromisoformat(c["expiration_date"]) - TODAY).days for c in raw["option_contracts"]} == {3}


def test_contracts_follow_next_page_token_to_the_end():
    srv, book = standin.start(today=TODAY, page_cap=7)
    try:
        url = f"http://127.0.0.1:{srv.server_port}"
        client = AlpacaClient(Config(mode="standin", base_url=url, data_url=url), standin.KEY, standin.SECRET)
        rows = options.list_contracts(client, "AAPL", TODAY, OptionRules(enabled=True))
        expected = [c for c in book.contracts.values()
                    if c["underlying_symbol"] == "AAPL" and 7 <= (date.fromisoformat(c["expiration_date"]) - TODAY).days <= 45]
        assert len(rows) == len(expected) > 7
    finally:
        srv.shutdown()


def test_snapshots_carry_price_implied_volatility_and_greeks(odesk, oserver):
    _, book = oserver
    sym = contract(book)
    snap = options.snapshots(odesk.client, [sym], odesk.cfg.options)[sym]
    assert snap.bid < snap.ask and snap.iv == 0.31 and 0 < snap.delta < 1 and snap.theta < 0 and snap.gamma and snap.vega and snap.rho
    assert snap.last == book.option_mid(sym) and snap.mid > 0


def test_more_than_a_hundred_symbols_are_fetched_in_chunks_of_a_hundred(odesk, oserver):
    _, book = oserver
    symbols = list(book.contracts) + [f"ZZZ261121C{n:08d}" for n in range(30)]  # 230 symbols, 30 of them unknown
    sizes = []
    real = odesk.client._call

    def spy(method, url, **kw):
        if "snapshots" in url:
            sizes.append(len(kw["params"]["symbols"].split(",")))
        return real(method, url, **kw)

    odesk.client._call = spy
    assert len(options.snapshots(odesk.client, symbols, odesk.cfg.options)) == 200
    assert sizes == [100, 100, 30]  # the documented limit is 100 symbols a request


def test_a_symbol_with_no_data_is_absent_not_an_error(odesk):
    assert options.snapshots(odesk.client, ["AAPL261121C00999000"], odesk.cfg.options) == {}


def test_the_chain_only_fetches_strikes_near_the_price(odesk, oserver):
    rows = options.chain(odesk.client, "AAPL", TODAY, odesk.cfg.options, spot=190.0, width_pct=3.0, right="call")
    assert rows and all(c.right == "call" and 184 <= c.strike <= 196 and s is not None for c, s in rows)


def test_the_pick_is_the_soonest_expiry_and_the_strike_nearest_the_money(odesk):
    rows = options.chain(odesk.client, "AAPL", TODAY, odesk.cfg.options, spot=190.0, right="call")
    contract_, snap = options.pick_call(rows, 190.0)
    assert (contract_.expiry - TODAY).days == 12 and contract_.strike == 190.0 and snap.ask > 0


def test_a_contract_with_no_ask_is_never_picked(odesk):
    rows = options.chain(odesk.client, "AAPL", TODAY, odesk.cfg.options, spot=190.0, right="call")
    bare = [(c, None) for c, _ in rows]
    assert options.pick_call(bare, 190.0) is None


# -- the gateway -------------------------------------------------------------------------------------------------------


def test_a_long_call_fills_and_gets_its_stop_on_the_premium(odesk, oserver):
    _, book = oserver
    p = entry(book)
    out = odesk.execute(p, "open", TODAY)
    assert out.submitted and out.exit_placed is True and out.verdict.qty == 1
    pos = odesk.client.positions()[0]
    assert pos.symbol == p.contract and pos.qty == 1
    stops = [o for o in odesk.client.open_orders() if o["type"] == "stop"]
    assert len(stops) == 1 and float(stops[0]["stop_price"]) == out.verdict.stop_price == round(p.price * 0.5, 2)
    assert stops[0]["side"] == "sell" and stops[0]["position_intent"] == "sell_to_close" and stops[0]["time_in_force"] == "gtc"
    assert [e["event"] for e in odesk.ledger.read()] == ["order_submitted", "option_exit_placed"]
    assert events(odesk, "order_submitted")[0]["data"]["asset"] == "option"


def test_the_entry_is_a_buy_to_open_limit_with_a_whole_number_of_contracts(odesk, oserver):
    _, book = oserver
    odesk.execute(entry(book), "open", TODAY)
    sent = next(o for o in book.orders if o["side"] == "buy")
    assert sent["type"] == "limit" and sent["position_intent"] == "buy_to_open" and sent["time_in_force"] == "day" and "." not in sent["qty"]


def test_a_refused_option_order_never_reaches_the_broker_and_says_why(odesk, oserver):
    _, book = oserver
    out = odesk.execute(entry(book, side="sell"), "open", TODAY)
    assert not out.submitted and book.orders == []
    assert "short or naked" in events(odesk, "order_refused")[0]["data"]["why"][0]


def test_a_multi_leg_proposal_is_refused_at_the_gateway(odesk, oserver):
    _, book = oserver
    out = odesk.execute(entry(book, legs=2), "open", TODAY)
    assert not out.submitted and book.orders == [] and "single leg" in out.verdict.reasons[0]


def test_options_off_in_the_config_refuses_at_the_gateway(odesk, oserver):
    _, book = oserver
    odesk.cfg = Config(**{**odesk.cfg.__dict__, "options": OptionRules(enabled=False)})
    out = odesk.execute(entry(book), "open", TODAY)
    assert not out.submitted and book.orders == [] and "switched off" in out.verdict.reasons[0]


def test_running_the_same_entry_twice_does_not_double_it(odesk, oserver):
    _, book = oserver
    odesk.cfg = Config(**{**odesk.cfg.__dict__, "options": OptionRules(enabled=True, max_premium_pct=5.0)})  # room for the rerun to reach the broker
    p = entry(book)
    first = odesk.execute(p, "open", TODAY)
    again = odesk.execute(p, "open", TODAY)
    assert first.submitted and not again.submitted and "client_order_id" in again.error
    assert odesk.client.positions()[0].qty == 1


def test_the_broker_refusing_a_level_is_a_failed_order_not_a_crash(odesk, oserver):
    _, book = oserver
    book.options_level = 1
    out = odesk.execute(entry(book), "open", TODAY)
    assert not out.submitted and "level2" in out.error and events(odesk, "order_failed")


def test_an_entry_that_has_not_filled_rests_and_blocks_the_next_one_until_its_stop_exists(odesk, oserver):
    _, book = oserver
    resting = entry(book, price=0.5)  # a limit far under the ask never fills on the stand-in
    out = odesk.execute(resting, "open", TODAY)
    assert out.submitted and out.exit_placed is False and events(odesk, "option_exit_pending")
    other = OptionProposal(contract(book, "MSFT"), 1, book.option_quote(contract(book, "MSFT"))[1], "t")
    blocked = odesk.execute(other, "open", TODAY)
    assert not blocked.submitted and "no exit stop" in blocked.verdict.reasons[0]
    # the entry fills later: the audit finds a bare option position and places its stop, then entries are allowed again
    for o in book.orders:
        o["status"] = "canceled"
    book.positions[resting.contract] = {"qty": 1, "avg": 4.0}
    assert odesk.ensure_stops("midday", TODAY) == [f"{resting.contract}: stop placed at 2.00"]
    assert odesk.execute(other, "open", TODAY).submitted


def test_a_held_option_with_no_stop_blocks_a_new_entry(odesk, oserver):
    _, book = oserver
    book.positions[contract(book)] = {"qty": 1, "avg": 5.0}
    other = OptionProposal(contract(book, "MSFT"), 1, 1.0, "t")
    out = odesk.execute(other, "open", TODAY)
    assert not out.submitted and "no exit stop" in out.verdict.reasons[0]


def test_a_failed_stop_is_logged_and_alerted_and_the_audit_retries_it(odesk, oserver, capture):
    _, book = oserver
    real = odesk.client.submit_option_stop

    def boom(*a, **k):
        raise standin_error()

    def standin_error():
        from ledgerbell.alpaca import AlpacaError

        return AlpacaError(500, "down")

    odesk.client.submit_option_stop = boom
    out = odesk.execute(entry(book), "open", TODAY)
    assert out.submitted and out.exit_placed is False and events(odesk, "option_exit_failed") and capture.posts
    odesk.client.submit_option_stop = real
    assert "stop placed" in odesk.ensure_stops("midday", TODAY)[0]


def test_the_premium_stop_fires_when_the_premium_falls_through_it(odesk, oserver):
    _, book = oserver
    p = entry(book)
    out = odesk.execute(p, "open", TODAY)
    book.set_premiums({p.contract: out.verdict.stop_price - 0.01})
    assert odesk.client.positions() == []


def test_the_audit_closes_an_option_that_is_already_through_its_stop(odesk, oserver):
    _, book = oserver
    sym = contract(book)
    book.positions[sym] = {"qty": 1, "avg": book.option_mid(sym) * 3}  # down about 67 percent, no stop
    notes = odesk.ensure_stops("midday", TODAY)
    assert "closed" in notes[0] and odesk.client.positions() == []


def test_the_audit_uses_the_option_stop_share_not_the_share_stop_share(odesk, oserver):
    _, book = oserver
    sym = contract(book)
    avg = round(book.option_mid(sym) * 1.25, 2)  # down 20 percent: inside a 50 percent stop, but outside a 3 percent one
    book.positions[sym] = {"qty": 1, "avg": avg}
    assert odesk.ensure_stops("midday", TODAY)[0].endswith(f"stop placed at {avg * 0.5:.2f}")


def test_the_kill_switch_cancels_option_orders_closes_option_positions_and_blocks_entries(odesk, oserver):
    _, book = oserver
    odesk.execute(entry(book), "open", TODAY)
    info = odesk.engage_kill("test")
    assert info["problems"] == [] and odesk.client.positions() == [] and odesk.client.open_orders() == []
    out = odesk.execute(entry(book), "open", TODAY)
    assert not out.submitted and "kill switch" in out.verdict.reasons[0]


def test_option_positions_do_not_use_up_the_share_position_count(odesk, oserver):
    _, book = oserver
    for i, sym in enumerate(c for c in book.contracts if c.startswith("QQQ")):
        if i >= 5:
            break
        book.positions[sym] = {"qty": 1, "avg": 5.0}
    out = odesk.execute(Proposal("AAPL", "buy", 10, 190.0, "t"), "open", TODAY)
    assert out.submitted  # five option positions are held and the share limit is five names


# -- the routine -------------------------------------------------------------------------------------------------------


def test_the_open_routine_adds_a_call_for_a_share_signal_when_options_are_on(odesk, oserver):
    res = routines.run("open", odesk, NOW, None)
    placed = [e["data"] for e in events(odesk, "order_submitted") if e["data"].get("asset") == "option"]
    refused = [e["data"] for e in events(odesk, "order_refused") if e["data"].get("asset") == "option"]
    assert placed and all(is_option_symbol(p["symbol"]) for p in placed)
    assert sum(p["limit_price"] * 100 * p["qty"] for p in placed) <= 100_000 * 0.01  # the 1 percent premium cap holds across the whole run
    assert refused and all("premium limit" in w for r in refused for w in r["why"])  # and it turned the rest away
    assert res.placed >= 2 and res.refused >= 1 and res.suggested >= res.placed + res.refused


def test_with_options_off_the_open_routine_makes_no_option_proposal(desk):
    routines.run("open", desk, NOW, None)
    assert not [e for e in desk.ledger.read() if e["data"].get("asset") == "option"]


def test_the_midday_scan_measures_an_option_against_its_own_stop_not_the_share_stop(odesk, oserver):
    _, book = oserver
    sym = contract(book)
    mid = book.option_mid(sym)
    book.positions[sym] = {"qty": 1, "avg": round(mid * 1.1, 2)}  # down about 9 percent: nowhere near halfway to a 50 percent stop
    book.orders.append({"id": "s1", "symbol": sym, "side": "sell", "type": "stop", "status": "new", "stop_price": "0.01", "qty": "1", "legs": []})
    assert "positions near their stop" in routines.run("midday", odesk, NOW, None).summary and routines.run("midday", odesk, NOW, None).summary.startswith("0 ")
    book.positions[sym] = {"qty": 1, "avg": round(mid * 1.5, 2)}  # down about 33 percent: past halfway to the 50 percent stop
    assert routines.run("midday", odesk, NOW, None).summary.startswith("1 ")
