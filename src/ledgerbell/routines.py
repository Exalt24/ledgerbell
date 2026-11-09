"""The five scheduled routines: premarket, open, midday, eod and weekly.

Each one logs its start and its end, skips cleanly on a closed market or an engaged kill switch, and
returns a Result the command line turns into an exit code (0 done, 3 skipped).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from . import agent, dashboard
from .alpaca import Account, Position
from .desk import Desk
from .strategy import rule_proposals

ROUTINES = ("premarket", "open", "midday", "eod", "weekly")


@dataclass
class Result:
    routine: str
    status: str  # done or skipped
    summary: str
    lines: list[str] = field(default_factory=list)


def _alert(desk: Desk, routine: str, subject: str, body: str) -> None:
    problems = desk.notifier.send(subject, body)
    if problems:
        desk.ledger.append(routine, "alert_failed", problems=problems)


def _skip(desk: Desk, routine: str, why: str) -> Result:
    desk.ledger.append(routine, "skipped", why=why)
    return Result(routine, "skipped", why)


def _trading_day(desk: Desk, now: datetime, clock: dict) -> bool:
    if clock.get("is_open"):
        return True
    tz = ZoneInfo(desk.cfg.timezone)
    nxt = datetime.fromisoformat(str(clock["next_open"]).replace("Z", "+00:00")).astimezone(tz)
    return nxt.date() == now.astimezone(tz).date()


def _positions_text(positions: list[Position]) -> str:
    if not positions:
        return "no open positions"
    return "; ".join(f"{p.symbol} {p.qty} at {p.avg_entry_price:.2f} ({p.unrealized_plpc * 100:+.1f}%)" for p in positions)


def _account_text(a: Account) -> str:
    return f"equity {a.equity:.2f}, day change {a.day_pl:+.2f} ({a.day_pl_pct:+.2f}%), cash {a.cash:.2f}"


def premarket(desk: Desk, now: datetime, runner: agent.ModelRunner | None) -> Result:
    clock = desk.client.clock()
    if not _trading_day(desk, now, clock):
        return _skip(desk, "premarket", "not a trading day")
    account, positions = desk.client.account(), desk.client.positions()
    watch = desk.cfg.strategy.watchlist
    bars = desk.client.daily_bars(list(watch), limit=max(desk.cfg.strategy.sma_days + 5, 30))
    if runner is None:
        briefing = agent.Briefing("Model research is off: no runner configured.", [])
    else:
        briefing = agent.research(runner, account, positions, bars, watch)
        if briefing.problem:
            desk.ledger.append("premarket", "agent_failed", problem=briefing.problem)
    plan = {
        "date": now.astimezone(ZoneInfo(desk.cfg.timezone)).date().isoformat(),
        "summary": briefing.summary,
        "proposals": [p.__dict__ for p in briefing.proposals],
    }
    desk.cfg.state_dir.mkdir(parents=True, exist_ok=True)
    desk.cfg.plan_file.write_text(json.dumps(plan, indent=2), encoding="utf-8")
    desk.ledger.append("premarket", "plan_saved", proposals=len(briefing.proposals), summary=briefing.summary)
    body = f"{briefing.summary}\n{_account_text(account)}\nHeld: {_positions_text(positions)}\n" + (
        "Suggested orders (they still go through the risk rules at the open):\n"
        + "\n".join(f"  {p.side} {p.qty} {p.symbol}: {p.reason}" for p in briefing.proposals)
        if briefing.proposals else "No orders suggested."
    )
    _alert(desk, "premarket", "Pre-market briefing", body)
    return Result("premarket", "done", briefing.summary, body.splitlines())


def _load_plan(desk: Desk, now: datetime, prices: dict[str, float]) -> list:
    from .risk import Proposal

    try:
        plan = json.loads(desk.cfg.plan_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    today = now.astimezone(ZoneInfo(desk.cfg.timezone)).date().isoformat()
    if plan.get("date") != today:
        return []  # yesterday's suggestions are stale and are never replayed
    out = []
    for p in plan.get("proposals", []):
        if p["symbol"] in prices:
            out.append(Proposal(p["symbol"], p["side"], int(p["qty"]), prices[p["symbol"]], p.get("reason", ""), "agent"))
    return out


def market_open(desk: Desk, now: datetime, runner: agent.ModelRunner | None) -> Result:
    if not desk.client.clock().get("is_open"):
        return _skip(desk, "open", "market is closed")
    if desk.enforce_loss_limit("open"):
        return Result("open", "done", "daily loss limit hit before the open orders, kill switch engaged")
    account, positions = desk.client.account(), desk.client.positions()
    watch = desk.cfg.strategy.watchlist
    bars = desk.client.daily_bars(list(watch), limit=max(desk.cfg.strategy.sma_days + 5, 30))
    prices = {s: float(b[-1]["c"]) for s, b in bars.items() if b}
    proposals = _load_plan(desk, now, prices) + rule_proposals(bars, positions, account.equity, desk.cfg.risk, desk.cfg.strategy)
    lines, seen = [], set()
    for prop in proposals:
        if (prop.symbol, prop.side) in seen:
            continue  # the model and the rule agree: one order, not two
        seen.add((prop.symbol, prop.side))
        out = desk.execute(prop, "open", now.astimezone(ZoneInfo(desk.cfg.timezone)).date())
        status = "sent" if out.submitted else ("failed: " + out.error if out.error else "refused: " + "; ".join(out.verdict.reasons))
        lines.append(f"{prop.side} {prop.qty} {prop.symbol} ({prop.source}) {status}")
    lines += desk.ensure_stops("open", now.astimezone(ZoneInfo(desk.cfg.timezone)).date())
    summary = f"{len(proposals)} proposals considered at the open"
    desk.ledger.append("open", "routine_done", considered=len(proposals))
    _alert(desk, "open", "Market open", summary + "\n" + ("\n".join(lines) or "Nothing to do."))
    return Result("open", "done", summary, lines)


def midday(desk: Desk, now: datetime, runner: agent.ModelRunner | None) -> Result:
    if not desk.client.clock().get("is_open"):
        return _skip(desk, "midday", "market is closed")
    if desk.enforce_loss_limit("midday"):
        return Result("midday", "done", "daily loss limit hit, kill switch engaged")
    today = now.astimezone(ZoneInfo(desk.cfg.timezone)).date()
    lines = desk.ensure_stops("midday", today)
    near = [p for p in desk.client.positions() if p.unrealized_plpc <= -desk.cfg.risk.stop_loss_pct / 200]
    for p in near:
        lines.append(f"{p.symbol} is {p.unrealized_plpc * 100:.1f}% down, halfway to its stop")
    desk.ledger.append("midday", "scan_done", near_stop=[p.symbol for p in near], fixes=lines)
    if lines:
        _alert(desk, "midday", "Midday scan", "\n".join(lines))
    return Result("midday", "done", f"{len(near)} positions near their stop", lines)


def eod(desk: Desk, now: datetime, runner: agent.ModelRunner | None) -> Result:
    account, positions = desk.client.account(), desk.client.positions()
    tz = ZoneInfo(desk.cfg.timezone)
    start = now.astimezone(tz).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
    events = desk.ledger.read(since=start)
    count = lambda name: sum(1 for e in events if e["event"] == name)  # noqa: E731
    body = (
        f"{_account_text(account)}\nHeld: {_positions_text(positions)}\n"
        f"Today: {count('order_submitted')} orders sent, {count('order_refused')} refused by the risk rules, "
        f"{count('order_failed')} rejected by the broker, kill switch {'ENGAGED' if desk.kill_engaged() else 'off'}."
    )
    html = dashboard.render(account, positions, desk.ledger.read(limit=40), desk.kill_info())
    (desk.cfg.state_dir / "dashboard.html").write_text(html, encoding="utf-8")
    desk.ledger.append("eod", "recap", equity=account.equity, day_pl=round(account.day_pl, 2), positions=len(positions))
    _alert(desk, "eod", "End of day recap", body)
    return Result("eod", "done", "recap sent", body.splitlines())


def weekly(desk: Desk, now: datetime, runner: agent.ModelRunner | None) -> Result:
    account = desk.client.account()
    events = desk.ledger.read(since=now.astimezone(UTC) - timedelta(days=7))
    tally: dict[str, int] = {}
    for e in events:
        tally[e["event"]] = tally.get(e["event"], 0) + 1
    recaps = [e for e in events if e["event"] == "recap"]
    lines = [f"Weekly review, {len(recaps)} trading day recaps logged.", _account_text(account)]
    lines += [f"{name}: {n}" for name, n in sorted(tally.items())]
    refused = [e for e in events if e["event"] == "order_refused"]
    for e in refused[-5:]:
        lines.append(f"refused {e['data']['side']} {e['data']['symbol']}: {'; '.join(e['data']['why'])}")
    report = "\n".join(lines)
    desk.cfg.state_dir.mkdir(parents=True, exist_ok=True)
    (desk.cfg.state_dir / f"weekly-{now.astimezone(ZoneInfo(desk.cfg.timezone)).date().isoformat()}.txt").write_text(report, encoding="utf-8")
    desk.ledger.append("weekly", "review_written", events=len(events))
    _alert(desk, "weekly", "Weekly review", report)
    return Result("weekly", "done", "weekly review written", lines)


def run(name: str, desk: Desk, now: datetime | None = None, runner: agent.ModelRunner | None = None) -> Result:
    now = now or datetime.now(UTC)
    if name not in ROUTINES:
        raise ValueError(f"unknown routine {name!r}, choose from {', '.join(ROUTINES)}")
    desk.ledger.append(name, "routine_start")
    if desk.kill_engaged() and name in {"premarket", "open", "midday"}:
        return _skip(desk, name, "kill switch is engaged")
    fn = {"premarket": premarket, "open": market_open, "midday": midday, "eod": eod, "weekly": weekly}[name]
    try:
        return fn(desk, now, runner)
    except Exception as exc:  # noqa: BLE001 - log it, tell the owners, then let the exit code say it failed
        desk.ledger.append(name, "routine_crashed", error=f"{type(exc).__name__}: {exc}")
        _alert(desk, name, f"Routine {name} crashed", f"{type(exc).__name__}: {exc}")
        raise
