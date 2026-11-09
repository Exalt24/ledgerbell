"""The research agent: a model reads the account and recent prices and answers in strict JSON.

The model gets no tools and no keys. Its answer is parsed against a schema, re-asked once with the exact
validation error, and then dropped if it is still wrong. Whatever survives is a list of Proposals that goes
through the same risk rules as the example strategy: the model can suggest an order, it cannot place one.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from typing import Protocol

from .alpaca import Account, Position
from .risk import Proposal


class ModelRunner(Protocol):
    def run(self, prompt: str) -> str: ...


class ClaudeCodeRunner:
    """Claude Code in headless print mode with every tool switched off: text in, text out."""

    def __init__(self, command: tuple[str, ...] = ("claude", "-p", "--tools", "", "--output-format", "text"), timeout: int = 240):
        self.command = command
        self.timeout = timeout

    def run(self, prompt: str) -> str:
        done = subprocess.run(self.command, input=prompt, capture_output=True, text=True, timeout=self.timeout, check=False)
        if done.returncode != 0:
            raise RuntimeError(f"model command exited {done.returncode}: {done.stderr.strip()[:200]}")
        return done.stdout


@dataclass(frozen=True)
class Briefing:
    summary: str
    proposals: list[Proposal]
    problem: str | None = None  # set when the model never gave a valid answer


class AnswerError(ValueError):
    pass


def build_prompt(account: Account, positions: list[Position], bars: dict[str, list[dict]], watchlist: tuple[str, ...]) -> str:
    lines = []
    for sym in watchlist:
        closes = [round(float(b["c"]), 2) for b in bars.get(sym, [])][-10:]
        lines.append(f"{sym}: last 10 daily closes {closes}")
    held = [f"{p.symbol} {p.qty} shares at {p.avg_entry_price:.2f}, now {p.current_price:.2f}" for p in positions] or ["none"]
    return (
        "You are the pre-market research step of a paper-trading bot. The owners set the strategy and decide; "
        "you summarise the picture and may suggest orders.\n"
        f"Equity {account.equity:.2f}, cash {account.cash:.2f}, change since yesterday's close {account.day_pl_pct:.2f}%.\n"
        f"Held: {'; '.join(held)}.\n" + "\n".join(lines) + "\n"
        f"Only these symbols are allowed: {', '.join(watchlist)}.\n"
        'Reply with JSON only, no prose around it: {"summary": "<three sentences at most>", "proposals": '
        '[{"symbol": "...", "side": "buy" or "sell", "qty": <whole number>, "reason": "<one sentence>"}]}. '
        "An empty proposals list is a fine answer."
    )


def parse_answer(text: str, watchlist: tuple[str, ...], prices: dict[str, float], max_proposals: int = 5) -> Briefing:
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        raise AnswerError("no JSON object in the reply")
    try:
        data = json.loads(match.group(0))
    except ValueError as exc:
        raise AnswerError(f"the JSON does not parse: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("summary"), str) or not data["summary"].strip():
        raise AnswerError('"summary" must be a non-empty string')
    raw = data.get("proposals", [])
    if not isinstance(raw, list):
        raise AnswerError('"proposals" must be a list')
    if len(raw) > max_proposals:
        raise AnswerError(f"at most {max_proposals} proposals, got {len(raw)}")
    proposals = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise AnswerError(f"proposal {i} is not an object")
        symbol = str(item.get("symbol", "")).upper()
        if symbol not in watchlist:
            raise AnswerError(f"proposal {i}: {symbol or 'a missing symbol'} is not on the watchlist")
        if item.get("side") not in {"buy", "sell"}:
            raise AnswerError(f'proposal {i}: "side" must be "buy" or "sell"')
        qty = item.get("qty")
        if isinstance(qty, bool) or not isinstance(qty, int) or qty < 1:
            raise AnswerError(f'proposal {i}: "qty" must be a whole number of at least 1')
        if symbol not in prices:
            raise AnswerError(f"proposal {i}: no recent price for {symbol}")
        proposals.append(Proposal(symbol, item["side"], qty, prices[symbol], str(item.get("reason", "")).strip()[:200], "agent"))
    return Briefing(data["summary"].strip(), proposals)


def research(
    runner: ModelRunner,
    account: Account,
    positions: list[Position],
    bars: dict[str, list[dict]],
    watchlist: tuple[str, ...],
) -> Briefing:
    prices = {s: float(b[-1]["c"]) for s, b in bars.items() if b}
    prompt = build_prompt(account, positions, bars, watchlist)
    reply, last_error = "", ""
    for attempt in range(2):
        ask = prompt if attempt == 0 else f"{prompt}\n\nYour last reply was refused: {last_error}. Reply again, JSON only."
        try:
            reply = runner.run(ask)
            return parse_answer(reply, watchlist, prices)
        except AnswerError as exc:
            last_error = str(exc)
        except Exception as exc:  # noqa: BLE001 - a model that is down must not stop the day
            last_error = f"the model call failed: {exc}"
            break
    return Briefing("No briefing today: the model gave no usable answer.", [], problem=last_error)
