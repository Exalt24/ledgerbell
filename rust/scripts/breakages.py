# LIFESPAN: permanent
"""Break one rule at a time in a temp copy of the crate and run `cargo test`: it must go red every time.

    python rust/scripts/breakages.py

Each entry names the rule, the exact source text that holds it and the text that breaks it. A breakage
that leaves the tests green means a rule has no test that notices it is gone. An anchor that no longer
matches the source (or matches twice) is reported as stale, and a breakage that stops the crate compiling
is reported as broken, because a compile error is not a test going red. Both fail the run, so the list
cannot rot quietly.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

CRATE = Path(__file__).resolve().parents[1]
KEEP = ["src", "tests", "benches", "examples", "Cargo.toml", "Cargo.lock"]

BREAKAGES = [
    # -- the book
    ("book.rs", "a crossing delta is refused", "if let Some((bid, ask)) = self.crossed() {\n            for (side, price, old)", "if let Some((bid, ask)) = None::<(Fixed, Fixed)> {\n            for (side, price, old)"),
    ("book.rs", "a refused crossing delta is rolled back", "                    Some(q) => {\n                        map.insert(price, q);\n                    }", "                    Some(q) => {\n                        let _ = q;\n                    }"),
    ("book.rs", "a zero size removes the level", "if l.qty.is_zero() {\n        map.remove(&l.price)", "if false {\n        map.remove(&l.price)"),
    ("book.rs", "a delta at or below the book id is stale", "if d.final_id <= self.last_id {", "if d.final_id < self.last_id {"),
    ("book.rs", "a gap after a snapshot is refused", "Phase::AfterSnapshot => {\n                if d.first_id > next {", "Phase::AfterSnapshot => {\n                if false {"),
    ("book.rs", "a gap on the live stream is refused", "Phase::Live => {\n                if d.first_id > next {", "Phase::Live => {\n                if false {"),
    ("book.rs", "a live delta that overlaps the book id is refused", "if d.first_id < next {", "if false {"),
    ("book.rs", "a crossed snapshot is refused", "if let Some((bid, ask)) = book.crossed() {\n            return Err(SnapshotError::Crossed { bid, ask });", "if let Some((bid, ask)) = None::<(Fixed, Fixed)> {\n            return Err(SnapshotError::Crossed { bid, ask });"),
    ("book.rs", "the book id moves to the delta's final id", "        self.last_id = d.final_id;\n        self.phase = Phase::Live;", "        self.last_id = d.first_id;\n        self.phase = Phase::Live;"),
    ("book.rs", "the best bid is the highest bid", "self.bids.iter().next_back().map(|(p, q)| Level", "self.bids.iter().next().map(|(p, q)| Level"),
    ("book.rs", "the depth view lists bids from the top", "            .iter()\n            .rev()\n            .take(n)", "            .iter()\n            .take(n)"),
    ("book.rs", "a decimal with more than eight places is refused", "if extra.bytes().any(|b| b != b'0') {", "if false {"),
    # -- the follower order
    ("follow.rs", "a buy limit sits above the reference by the cap", "(reference.0 as u128) * (BPS + bps) / BPS;", "(reference.0 as u128) * (BPS - bps) / BPS;"),
    ("follow.rs", "a sell limit is rounded up, inside the cap", "num.div_ceil(BPS)", "(num / BPS)"),
    ("follow.rs", "a buy never takes a level above the limit", "OrderSide::Buy => l.price <= limit,", "OrderSide::Buy => true,"),
    ("follow.rs", "an order never takes more than a level holds", "let take = remaining.min(l.qty.0 as u128);", "let take = remaining;"),
    ("follow.rs", "a size below the minimum is refused", "if wanted < cfg.min_qty || wanted.is_zero() {", "if wanted.is_zero() {"),
    # -- the client
    ("client.rs", "a gap makes the client throw the book away and resync", "emitter.send(shared, EventKind::Resyncing(e.to_string()), None, 0);\n                    continue 'resync;", "let _ = e;"),
    ("client.rs", "buffered deltas the snapshot covers are dropped, not fatal", "Err(ApplyError::Stale { .. }) => {\n                    shared.stats.stale.fetch_add(1, Ordering::Relaxed);\n                }\n                Err(_) => {", "Err(ApplyError::Stale { .. }) => {\n                    failed_syncs += 1;\n                    continue 'resync;\n                }\n                Err(_) => {"),
    ("client.rs", "the reconnect wait grows", "attempt = attempt.saturating_add(1);", "attempt = 0;"),
    ("client.rs", "a silent connection is declared dead", 'Err(_) => Step::Ended(format!("no frame for {} ms", idle.as_millis())),', "Err(_) => Step::Frame(Message::Pong(Vec::new().into())),"),
    ("client.rs", "the book is cleared when the connection ends", "                *shared.book.write().unwrap() = None;\n                shared.stats.synced.store(false, Ordering::Relaxed);\n                if end.was_synced {", "                if end.was_synced {"),
    ("client.rs", "a delivered event resets the missed count", "Ok(()) => self.pending_missed = 0,", "Ok(()) => {}"),
    ("client.rs", "a full channel counts the dropped event", "                self.pending_missed += 1;\n                shared.stats.dropped_events.fetch_add(1, Ordering::Relaxed);", "                shared.stats.dropped_events.fetch_add(1, Ordering::Relaxed);"),
    ("client.rs", "the first delta after a snapshot is checked", "match book.apply(d) {\n                Ok(()) => {\n                    record(cfg, shared, d);", "match Ok::<(), ApplyError>(()) {\n                Ok(()) => {\n                    record(cfg, shared, d);"),
    ("client.rs", "a protocol error ends the connection", "                Ok(_) => continue,\n                Err(e) => {\n                    return SessionEnd {\n                        reason: e.to_string(),\n                        was_synced,\n                    }\n                }", "                Ok(_) => continue,\n                Err(_) => continue,"),
    # -- the wire
    ("proto.rs", "a level price must be a string", '.ok_or_else(|| ProtoError(format!("{what}: price is not a string")))?;', '.unwrap_or("0");'),
]


def main() -> int:
    work = Path(tempfile.mkdtemp(prefix="bookwatch-breakages-"))
    target = Path(tempfile.gettempdir()) / "bookwatch-breakages-target"
    copy = work / "crate"
    copy.mkdir()
    for name in KEEP:
        src = CRATE / name
        if src.is_dir():
            shutil.copytree(src, copy / name)
        elif src.exists():
            shutil.copy2(src, copy / name)
    env = dict(os.environ, CARGO_TARGET_DIR=str(target), CARGO_TERM_COLOR="never")

    def run_tests() -> tuple[int, str]:
        try:
            p = subprocess.run(["cargo", "test", "--quiet"], cwd=copy, env=env, capture_output=True, text=True, timeout=300)
            return p.returncode, p.stdout + p.stderr
        except subprocess.TimeoutExpired:
            return 124, "timed out"

    code, out = run_tests()
    if code != 0:
        print("the unbroken copy does not pass its own tests:\n" + out[-2000:])
        return 1

    caught = survived = stale = broken = 0
    for fname, name, anchor, replacement in BREAKAGES:
        path = copy / "src" / fname
        original = path.read_text(encoding="utf-8").replace("\r\n", "\n")
        if original.count(anchor) != 1:
            print(f"stale   {fname}: {name} (anchor found {original.count(anchor)} times)")
            stale += 1
            continue
        path.write_text(original.replace(anchor, replacement), encoding="utf-8", newline="\n")
        code, out = run_tests()
        path.write_text(original, encoding="utf-8", newline="\n")
        if code == 0:
            print(f"SURVIVED {fname}: {name}")
            survived += 1
        elif "could not compile" in out and "test result" not in out:
            print(f"BROKEN  {fname}: {name} (does not compile)\n{out[-600:]}")
            broken += 1
        else:
            print(f"caught  {fname}: {name}")
            caught += 1
    total = len(BREAKAGES)
    print(f"\n{total} breakages: {caught} caught, {survived} survived, {stale} stale anchors, {broken} do not compile")
    shutil.rmtree(work, ignore_errors=True)
    return 0 if survived == stale == broken == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
