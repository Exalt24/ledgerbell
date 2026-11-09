from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from ledgerbell import risk
from ledgerbell.alpaca import Account, Position
from ledgerbell.config import ConfigError, OptionRules, RiskRules, validate_options
from ledgerbell.occ import is_option_symbol, parse_occ
from ledgerbell.risk import OptionProposal

TODAY = date(2026, 11, 9)
RULES = OptionRules(enabled=True, max_premium_pct=1.0, min_days_to_expiry=7, max_days_to_expiry=45, stop_premium_pct=50.0, limit_buffer_pct=5.0)
WATCH = ("AAPL", "MSFT", "SPY")
CALL = "AAPL261121C00190000"  # 12 days out
PUT = "AAPL261121P00190000"


def acct(equity=100_000.0, last=100_000.0, cash=100_000.0, blocked=False):
    return Account(equity, last, cash, cash, blocked)


def prop(contract=CALL, qty=1, price=5.0, **kw):
    return OptionProposal(contract, qty, price, "t", **kw)


def check(p, account=None, positions=(), kill=False, rules=RULES, unprotected=()):
    return risk.check_option(p, account or acct(), list(positions), rules, RiskRules(), WATCH, kill, TODAY, tuple(unprotected))


def held(symbol=CALL, qty=1, price=5.0):
    return Position(symbol, qty, price, price, 0.0, 0.0)


def refused(v, *words):
    assert not v.allowed and all(w in " ".join(v.reasons) for w in words), v.reasons


def test_a_plain_long_call_is_allowed_with_a_limit_and_a_stop_on_the_premium():
    v = check(prop(price=5.0))
    assert v.allowed and v.qty == 1 and v.limit_price == 5.25 and v.stop_price == 2.5


def test_a_long_put_is_allowed_too():
    assert check(prop(PUT)).allowed


def test_options_switched_off_in_the_config_refuses_everything():
    refused(check(prop(), rules=replace(RULES, enabled=False)), "switched off")


def test_the_kill_switch_refuses_an_option_entry():
    refused(check(prop(), kill=True), "kill switch")


def test_a_multi_leg_order_is_refused_single_leg_only():
    refused(check(prop(legs=2)), "single leg")


@pytest.mark.parametrize("legs", [0, 2, 4])
def test_any_leg_count_but_one_is_refused(legs):
    assert not check(prop(legs=legs)).allowed


def test_selling_an_option_is_refused_as_a_short_or_naked_leg():
    refused(check(prop(side="sell", intent="open")), "short or naked")


def test_selling_to_close_is_refused_as_a_proposal_too():
    assert not check(prop(side="sell", intent="close"), positions=[held()]).allowed


def test_buying_to_close_is_refused_only_buy_to_open_is_allowed():
    refused(check(prop(side="buy", intent="close")), "buy to open")


def test_a_string_that_is_not_an_option_symbol_is_refused():
    refused(check(prop("AAPL")), "not an option")
    refused(check(prop("AAPL261121X00190000")), "not an option")


def test_an_option_on_a_symbol_off_the_watchlist_is_refused():
    refused(check(prop("TSLA261121C00190000")), "TSLA", "watchlist")


def test_expiry_under_the_minimum_is_refused():
    refused(check(prop("AAPL261113C00190000")), "4 days", "minimum")  # 4 days out


def test_expiry_exactly_at_the_minimum_is_allowed():
    assert check(prop("AAPL261116C00190000")).allowed  # 7 days out


def test_expiry_over_the_maximum_is_refused():
    refused(check(prop("AAPL270101C00190000")), "maximum")  # 53 days out


def test_expiry_exactly_at_the_maximum_is_allowed():
    assert check(prop("AAPL261224C00190000")).allowed  # 45 days out


def test_a_non_standard_multiplier_is_refused():
    refused(check(prop(multiplier=10)), "non-standard")


@pytest.mark.parametrize("qty", [0, -1])
def test_zero_or_negative_contracts_are_refused(qty):
    assert not check(prop(qty=qty)).allowed


def test_no_usable_quote_is_refused():
    refused(check(prop(price=0.0)), "quote")


def test_a_blocked_account_does_not_buy_options():
    assert not check(prop(), account=acct(blocked=True)).allowed


def test_the_daily_loss_limit_refuses_an_option_entry():
    refused(check(prop(), account=acct(equity=97_000.0, last=100_000.0)), "daily loss")


def test_the_premium_cap_shrinks_the_order():
    # 1 percent of 100k is 1000; at a 5.25 limit one contract costs 525, so 1 of the 3 asked fits
    v = check(prop(qty=3, price=5.0))
    assert v.allowed and v.qty == 1 and "reduced" in v.reasons[0]


def test_the_cap_is_sized_on_the_limit_not_on_the_quote():
    # at a quote of 9.60 the limit is 10.08: 99 contracts cost less than 1000 only if sized on the quote (960), not the limit (1008)
    refused(check(prop(qty=1, price=9.60)), "premium limit")


def test_premium_already_held_counts_against_the_cap():
    refused(check(prop(price=5.0), positions=[held(CALL, qty=1, price=6.0)]), "premium limit")  # 600 held + 525 > 1000


def test_a_stock_position_does_not_count_as_option_premium():
    stock = Position("AAPL", 400, 190.0, 190.0, 0.0, 0.0)
    assert check(prop(price=5.0), positions=[stock]).allowed


def test_not_enough_cash_refuses():
    refused(check(prop(price=5.0), account=acct(cash=100.0)), "cash")


def test_a_premium_too_small_to_carry_a_stop_is_refused():
    refused(check(prop(price=0.01), rules=replace(RULES, stop_premium_pct=90.0)), "too small")


def test_the_stop_sits_under_the_premium_by_the_configured_share():
    assert check(prop(price=4.0), rules=replace(RULES, stop_premium_pct=25.0)).stop_price == 3.0


def test_an_unprotected_option_blocks_the_next_entry():
    refused(check(prop(), unprotected=[PUT]), PUT, "no exit stop")


def test_option_symbols_parse_and_stock_tickers_do_not():
    occ = parse_occ(CALL)
    assert (occ.underlying, occ.expiry, occ.right, occ.strike) == ("AAPL", date(2026, 11, 21), "call", 190.0)
    assert parse_occ(PUT).right == "put" and parse_occ("SPY261116C00752500").strike == 752.5
    assert not is_option_symbol("AAPL") and not is_option_symbol("") and not is_option_symbol("AAPL261321C00190000")


@pytest.mark.parametrize(
    "bad",
    [OptionRules(max_premium_pct=0), OptionRules(max_premium_pct=6), OptionRules(stop_premium_pct=100), OptionRules(stop_premium_pct=0),
     OptionRules(limit_buffer_pct=-1), OptionRules(limit_buffer_pct=25), OptionRules(min_days_to_expiry=0),
     OptionRules(min_days_to_expiry=30, max_days_to_expiry=10), OptionRules(feed="free")],
)
def test_nonsense_option_settings_are_refused_at_load(bad):
    with pytest.raises(ConfigError):
        validate_options(bad)
