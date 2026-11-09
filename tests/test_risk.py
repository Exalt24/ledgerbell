from __future__ import annotations

import pytest

from ledgerbell import risk
from ledgerbell.alpaca import Account, Position
from ledgerbell.config import ConfigError, RiskRules, validate_risk
from ledgerbell.risk import Proposal

RULES = RiskRules(max_position_pct=5, max_open_positions=2, daily_loss_limit_pct=2, stop_loss_pct=3, entry_pct=2.5)
WATCH = ("AAPL", "MSFT", "SPY")


def acct(equity=100_000.0, last=100_000.0, cash=100_000.0, bp=100_000.0, blocked=False):
    return Account(equity, last, cash, bp, blocked)


def pos(sym="AAPL", qty=10, price=100.0):
    return Position(sym, qty, price, price, 0.0, 0.0)


def buy(sym="AAPL", qty=10, price=100.0):
    return Proposal(sym, "buy", qty, price, "t")


def sell(sym="AAPL", qty=10, price=100.0):
    return Proposal(sym, "sell", qty, price, "t")


def check(p, account=None, positions=(), kill=False, rules=RULES):
    return risk.check(p, account or acct(), list(positions), rules, WATCH, kill)


def test_a_plain_buy_is_allowed_and_carries_a_stop():
    v = check(buy(qty=10, price=100.0))
    assert v.allowed and v.qty == 10 and v.stop_price == 97.0


def test_kill_switch_refuses_everything_even_a_sell():
    assert not check(buy(), kill=True).allowed
    assert not check(sell(), positions=[pos()], kill=True).allowed


def test_symbol_off_the_watchlist_is_refused():
    v = check(buy("TSLA"))
    assert not v.allowed and "watchlist" in v.reasons[0]


@pytest.mark.parametrize("qty", [0, -3])
def test_zero_or_negative_quantity_is_refused(qty):
    assert not check(buy(qty=qty)).allowed


def test_position_size_limit_shrinks_the_order():
    # 5% of 100k is 5000, so at 100 a share the cap is 50 shares, not the 80 asked for
    v = check(buy(qty=80, price=100.0))
    assert v.allowed and v.qty == 50 and "reduced" in v.reasons[0]


def test_position_size_counts_what_is_already_held():
    v = check(buy(qty=80, price=100.0), positions=[pos(qty=45, price=100.0)])
    assert v.allowed and v.qty == 5


def test_no_room_left_is_a_refusal_not_a_zero_order():
    v = check(buy(qty=5, price=100.0), positions=[pos(qty=50, price=100.0)])
    assert not v.allowed and "size limit" in v.reasons[0]


def test_max_open_positions_blocks_a_third_symbol_but_not_adding_to_one_held():
    held = [pos("AAPL", 10), pos("MSFT", 10)]
    assert not check(buy("SPY"), positions=held).allowed
    assert check(buy("AAPL", qty=5), positions=held).allowed


def test_daily_loss_limit_refuses_new_buys():
    v = check(buy(), account=acct(equity=97_900.0, last=100_000.0))  # down 2.1 percent
    assert not v.allowed and "daily loss" in v.reasons[0]


def test_just_inside_the_daily_loss_limit_still_trades():
    assert check(buy(), account=acct(equity=98_100.0, last=100_000.0)).allowed  # down 1.9 percent


def test_loss_exactly_at_the_limit_counts_as_breached():
    assert risk.daily_loss_breached(acct(equity=98_000.0, last=100_000.0), RULES)


def test_a_blocked_account_does_not_buy():
    assert not check(buy(), account=acct(blocked=True)).allowed


def test_buying_power_caps_the_order():
    v = check(buy(qty=40, price=100.0), account=acct(bp=1_000.0))
    assert v.allowed and v.qty == 10


def test_no_buying_power_refuses():
    assert not check(buy(), account=acct(bp=50.0)).allowed


def test_selling_nothing_held_is_refused_no_shorting():
    v = check(sell())
    assert not v.allowed and "short" in v.reasons[0]


def test_selling_more_than_held_is_cut_to_the_held_quantity():
    v = check(sell(qty=99), positions=[pos(qty=10)])
    assert v.allowed and v.qty == 10


def test_a_sell_is_still_allowed_after_the_loss_limit_is_hit():
    v = check(sell(), positions=[pos()], account=acct(equity=90_000.0, last=100_000.0))
    assert v.allowed


@pytest.mark.parametrize(
    "bad",
    [RiskRules(max_position_pct=0), RiskRules(max_position_pct=30), RiskRules(stop_loss_pct=60),
     RiskRules(daily_loss_limit_pct=-1), RiskRules(entry_pct=9, max_position_pct=5), RiskRules(max_open_positions=0)],
)
def test_nonsense_risk_settings_are_refused_at_load(bad):
    with pytest.raises(ConfigError):
        validate_risk(bad)
