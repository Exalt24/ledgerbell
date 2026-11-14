"""Calls the deployed Modal endpoint and writes a transcript that holds no secret.

    LEDGERBELL_API_KEY=<the key from the Modal Secret> python scripts/modal_check.py https://<workspace>--ledgerbell-api-web.modal.run [--out docs/modal-run-captured.txt]

Checks: the first call after a deploy (a cold start), five warm calls, the docs pages are off, a missing and a wrong key are 401, three risk verdicts through the real
risk rules, and one alert that goes out through SES. The key is read from the environment and the transcript is refused if it contains it.
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import requests

ACCOUNT = {"equity": 100000, "last_equity": 100000, "cash": 100000, "buying_power": 100000}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("url")
    ap.add_argument("--out")
    a = ap.parse_args()
    key = os.environ.get("LEDGERBELL_API_KEY", "")
    if not key:
        print("set LEDGERBELL_API_KEY in the environment", file=sys.stderr)
        return 2
    base = a.url.rstrip("/")
    H = {"X-Api-Key": key}
    lines = [f"ledgerbell Modal check, {datetime.now(UTC).isoformat(timespec='seconds')}", f"endpoint: {base}"]
    ok = True

    def timed(method, path, **kw):
        t = time.perf_counter()
        r = requests.request(method, base + path, timeout=60, **kw)
        return r, (time.perf_counter() - t) * 1000

    def note(good: bool, text: str) -> None:
        nonlocal ok
        ok = ok and good
        lines.append(("PASS  " if good else "FAIL  ") + text)

    r, ms = timed("GET", "/health")
    note(r.status_code == 200 and r.json().get("status") == "ok", f"GET /health first call {r.status_code} in {ms:.0f} ms (a cold start when the app was idle)")
    warm = [timed("GET", "/health")[1] for _ in range(5)]
    lines.append(f"GET /health five warm calls: median {statistics.median(warm):.0f} ms, range {min(warm):.0f} to {max(warm):.0f} ms")
    note(requests.get(base + "/docs", timeout=30).status_code == 404, "GET /docs is 404 (the interactive docs are off)")
    body = {"proposal": {"symbol": "SPY", "side": "buy", "qty": 10, "price": 500.0, "reason": "check"}, "account": ACCOUNT}
    note(requests.post(base + "/risk/check", json=body, timeout=30).status_code == 401, "POST /risk/check with no key is 401")
    note(requests.post(base + "/risk/check", json=body, headers={"X-Api-Key": "wrong"}, timeout=30).status_code == 401, "POST /risk/check with a wrong key is 401")
    r, ms = timed("POST", "/risk/check", json=body, headers=H)
    v = r.json()
    note(r.status_code == 200 and v["allowed"] and v["qty"] == 10 and v["stop_price"] == 485.0, f"risk: 10 SPY at 500 allowed with a stop at {v.get('stop_price')} ({ms:.0f} ms)")
    big = {**body, "proposal": {**body["proposal"], "qty": 100}}
    v = requests.post(base + "/risk/check", json=big, headers=H, timeout=30).json()
    note(v["allowed"] and v["qty"] == 10 and "reduced from 100 to 10" in v["reasons"][0], f"risk: 100 SPY cut to {v['qty']} by the 5 percent size limit")
    off = {**body, "proposal": {**body["proposal"], "symbol": "GME"}}
    v = requests.post(base + "/risk/check", json=off, headers=H, timeout=30).json()
    note(not v["allowed"] and v["reasons"] == ["GME is not on the watchlist"], "risk: GME refused, not on the watchlist")
    r, ms = timed("POST", "/alerts", json={"subject": "ledgerbell Modal check", "body": "Sent from the Modal endpoint through Amazon SES."}, headers=H)
    note(r.status_code == 200 and r.json() == {"sent": True, "problems": []}, f"alert: POST /alerts answered {r.json()} in {ms:.0f} ms, one mail through SES")
    text = "\n".join(lines) + "\n"
    if key in text:
        print("refusing to write: the transcript contains the key", file=sys.stderr)
        return 3
    print(text, end="")
    if a.out:
        Path(a.out).write_text(text, encoding="utf-8")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
