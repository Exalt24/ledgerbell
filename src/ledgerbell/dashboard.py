"""A one-file HTML dashboard: positions, P&L and the bot's recent activity. Every value is escaped."""

from __future__ import annotations

import html
import json

from .alpaca import Account, Position

CSS = """
:root{--bg:#f6f7f9;--fg:#15202b;--muted:#5b6b7a;--card:#fff;--line:#d9e0e7;--up:#0b7a3b;--down:#b3261e;--warn:#7a4b00}
@media (prefers-color-scheme:dark){:root{--bg:#10151a;--fg:#e7edf3;--muted:#9fb0bf;--card:#18202a;--line:#2a3643;--up:#4cc27d;--down:#ff8a80;--warn:#ffcc80}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;padding:16px}
main{max-width:980px;margin:0 auto}h1{font-size:1.3rem;margin:0 0 4px}h2{font-size:1rem;margin:24px 0 8px}
.sub{color:var(--muted);margin:0 0 16px}.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:12px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px}.kpi b{display:block;font-size:1.2rem}
.up{color:var(--up)}.down{color:var(--down)}.banner{background:var(--card);border:2px solid var(--down);border-radius:8px;padding:12px;margin:0 0 16px;color:var(--down);font-weight:600}
table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:8px;overflow:hidden}
th,td{padding:8px 10px;text-align:left;border-bottom:1px solid var(--line);font-size:.9rem}th{color:var(--muted);font-weight:600}
td.n,th.n{text-align:right;font-variant-numeric:tabular-nums}.wrap{overflow-x:auto}
"""


def _cls(x: float) -> str:
    return "up" if x > 0 else "down" if x < 0 else ""


def _e(x) -> str:
    return html.escape(str(x), quote=True)


def render(account: Account, positions: list[Position], events: list[dict], kill: dict | None) -> str:
    banner = (
        f'<div class="banner" role="alert">KILL SWITCH ENGAGED: {_e(kill.get("reason", ""))} '
        f'({_e(kill.get("at", ""))}). Nothing trades until it is cleared by a person.</div>' if kill else ""
    )
    rows = "".join(
        f"<tr><td>{_e(p.symbol)}</td><td class='n'>{p.qty}</td><td class='n'>{p.avg_entry_price:.2f}</td>"
        f"<td class='n'>{p.current_price:.2f}</td><td class='n {_cls(p.unrealized_pl)}'>{p.unrealized_pl:+.2f}</td>"
        f"<td class='n {_cls(p.unrealized_plpc)}'>{p.unrealized_plpc * 100:+.2f}%</td></tr>"
        for p in positions
    ) or "<tr><td colspan='6'>No open positions.</td></tr>"
    acts = "".join(
        f"<tr><td>{_e(e['ts'])}</td><td>{_e(e['routine'])}</td><td>{_e(e['event'])}</td>"
        f"<td>{_e(json.dumps(e['data'], sort_keys=True, default=str)[:160])}</td></tr>"
        for e in reversed(events)
    ) or "<tr><td colspan='4'>Nothing logged yet.</td></tr>"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ledgerbell dashboard</title><style>{CSS}</style></head>
<body><main>
<h1>ledgerbell</h1><p class="sub">Paper account. Positions, P&amp;L and what the bot did.</p>
{banner}
<div class="kpis">
<div class="kpi">Equity<b>{account.equity:,.2f}</b></div>
<div class="kpi">Change today<b class="{_cls(account.day_pl)}">{account.day_pl:+,.2f} ({account.day_pl_pct:+.2f}%)</b></div>
<div class="kpi">Cash<b>{account.cash:,.2f}</b></div>
<div class="kpi">Open positions<b>{len(positions)}</b></div>
</div>
<h2>Positions</h2>
<div class="wrap"><table><thead><tr><th>Symbol</th><th class="n">Qty</th><th class="n">Entry</th><th class="n">Now</th><th class="n">P&amp;L</th><th class="n">P&amp;L %</th></tr></thead><tbody>{rows}</tbody></table></div>
<h2>Bot activity</h2>
<div class="wrap"><table><thead><tr><th>Time (UTC)</th><th>Routine</th><th>Event</th><th>Detail</th></tr></thead><tbody>{acts}</tbody></table></div>
</main></body></html>"""
