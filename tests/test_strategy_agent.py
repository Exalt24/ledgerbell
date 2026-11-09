from __future__ import annotations

import json

import pytest

from ledgerbell import agent
from ledgerbell.alpaca import Account, Position
from ledgerbell.config import RiskRules, StrategyRules
from ledgerbell.strategy import rule_proposals, sma

STRAT = StrategyRules(watchlist=("AAA", "BBB", "CCC"), sma_days=5)
RULES = RiskRules()


def bars(closes):
    return [{"c": c} for c in closes]


def test_sma_is_the_mean_of_the_last_n():
    assert sma([1, 2, 3, 4, 100], 4) == (2 + 3 + 4 + 100) / 4


def test_buy_when_the_close_is_over_the_average_sized_by_entry_pct():
    out = rule_proposals({"AAA": bars([10, 10, 10, 10, 12])}, [], 100_000.0, RULES, STRAT)
    assert len(out) == 1 and out[0].side == "buy" and out[0].qty == 208  # 2.5 percent of 100k at 12


def test_no_buy_when_the_close_is_under_the_average():
    assert rule_proposals({"AAA": bars([12, 12, 12, 12, 10])}, [], 100_000.0, RULES, STRAT) == []


def test_sell_what_is_held_when_the_close_falls_under_the_average():
    held = [Position("AAA", 7, 12.0, 10.0, -14.0, -0.16)]
    out = rule_proposals({"AAA": bars([12, 12, 12, 12, 10])}, held, 100_000.0, RULES, STRAT)
    assert [(p.side, p.qty) for p in out] == [("sell", 7)]


def test_held_and_still_strong_means_no_order():
    held = [Position("AAA", 7, 10.0, 12.0, 14.0, 0.2)]
    assert rule_proposals({"AAA": bars([10, 10, 10, 10, 12])}, held, 100_000.0, RULES, STRAT) == []


def test_too_little_history_is_no_opinion_not_a_sell():
    held = [Position("AAA", 7, 12.0, 10.0, -14.0, -0.16)]
    assert rule_proposals({"AAA": bars([10, 9])}, held, 100_000.0, RULES, STRAT) == []


def test_a_one_share_minimum_means_a_tiny_account_gets_no_order():
    assert rule_proposals({"AAA": bars([10, 10, 10, 10, 12])}, [], 100.0, RULES, STRAT) == []


WATCH = ("AAPL", "MSFT")
PRICES = {"AAPL": 190.0, "MSFT": 410.0}
GOOD = json.dumps({"summary": "Quiet tape.", "proposals": [{"symbol": "aapl", "side": "buy", "qty": 5, "reason": "above average"}]})


def test_a_valid_answer_becomes_proposals_priced_from_our_bars_not_the_models():
    b = agent.parse_answer(GOOD, WATCH, PRICES)
    assert b.summary == "Quiet tape." and b.proposals[0].symbol == "AAPL" and b.proposals[0].price == 190.0
    assert b.proposals[0].source == "agent"


def test_json_inside_a_code_fence_is_still_read():
    assert len(agent.parse_answer(f"```json\n{GOOD}\n```", WATCH, PRICES).proposals) == 1


@pytest.mark.parametrize(
    "bad,needle",
    [
        ("no json here", "no JSON"),
        ("{not json}", "does not parse"),
        (json.dumps({"proposals": []}), "summary"),
        (json.dumps({"summary": "x", "proposals": [{"symbol": "TSLA", "side": "buy", "qty": 1}]}), "watchlist"),
        (json.dumps({"summary": "x", "proposals": [{"symbol": "AAPL", "side": "short", "qty": 1}]}), "side"),
        (json.dumps({"summary": "x", "proposals": [{"symbol": "AAPL", "side": "buy", "qty": 1.5}]}), "qty"),
        (json.dumps({"summary": "x", "proposals": [{"symbol": "AAPL", "side": "buy", "qty": True}]}), "qty"),
        (json.dumps({"summary": "x", "proposals": [{"symbol": "AAPL", "side": "buy", "qty": 1}] * 6}), "at most"),
    ],
)
def test_every_kind_of_bad_answer_is_refused_with_its_reason(bad, needle):
    with pytest.raises(agent.AnswerError, match=needle):
        agent.parse_answer(bad, WATCH, PRICES)


class Scripted:
    def __init__(self, *replies):
        self.replies, self.prompts = list(replies), []

    def run(self, prompt):
        self.prompts.append(prompt)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


ACCOUNT = Account(100_000.0, 100_000.0, 100_000.0, 100_000.0, False)
BARS = {"AAPL": bars([190.0] * 12), "MSFT": bars([410.0] * 12)}


def test_a_bad_first_answer_is_re_asked_once_with_the_exact_error():
    runner = Scripted("sorry, no JSON", GOOD)
    out = agent.research(runner, ACCOUNT, [], BARS, WATCH)
    assert out.problem is None and len(out.proposals) == 1
    assert "no JSON object" in runner.prompts[1]


def test_two_bad_answers_give_an_empty_briefing_not_a_crash():
    out = agent.research(Scripted("nope", "still nope"), ACCOUNT, [], BARS, WATCH)
    assert out.proposals == [] and out.problem and "No briefing" in out.summary


def test_a_model_that_is_down_gives_an_empty_briefing_without_a_retry_storm():
    runner = Scripted(RuntimeError("503"))
    out = agent.research(runner, ACCOUNT, [], BARS, WATCH)
    assert out.proposals == [] and "503" in out.problem and len(runner.prompts) == 1


def test_the_prompt_lists_only_watchlist_symbols_and_never_a_key():
    p = agent.build_prompt(ACCOUNT, [], BARS, WATCH)
    assert "AAPL, MSFT" in p and "APCA" not in p and "secret" not in p.lower()


def test_the_real_runner_turns_off_every_tool():
    assert agent.ClaudeCodeRunner().command[:4] == ("claude", "-p", "--tools", "")
