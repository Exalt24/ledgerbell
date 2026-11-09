"""Replay docs/demo-captured.txt as docs/demo.gif, a terminal scrolling the real captured output.

    python scripts/make_demo_gif.py        # needs Pillow, not a dependency of the bot

Nothing is typed into the picture that is not in the captured file.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
FONT = ImageFont.truetype("C:/Windows/Fonts/consola.ttf", 16)
W, H, COLS, ROWS, LINE = 900, 520, 90, 24, 20
BG, FG, DIM, ALERT, TITLE = (16, 21, 26), (231, 237, 243), (140, 160, 178), (255, 138, 128), (76, 194, 125)


def wrapped(lines: list[str]) -> list[str]:
    out: list[str] = []
    for line in lines:
        if not line.strip():
            out.append("")
            continue
        indent = len(line) - len(line.lstrip())
        out += textwrap.wrap(line, COLS, subsequent_indent=" " * (indent + 2)) or [""]
    return out


def colour(line: str):
    if line.startswith("=="):
        return TITLE
    if "KILL SWITCH" in line or "kill switch engaged" in line or ": skipped:" in line:
        return ALERT
    if "[text alert]" in line:
        return DIM
    return FG


def frame(visible: list[str]) -> Image.Image:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    d.text((14, 8), "$ python scripts/demo.py", font=FONT, fill=DIM)
    for i, line in enumerate(visible[-ROWS:]):
        d.text((14, 36 + i * LINE), line, font=FONT, fill=colour(line))
    return img


def main() -> None:
    lines = wrapped((ROOT / "docs" / "demo-captured.txt").read_text(encoding="utf-8").splitlines())
    frames, durations = [], []
    step = 2
    for n in range(step, len(lines) + step, step):
        frames.append(frame(lines[:n]))
        durations.append(900 if lines[min(n, len(lines)) - 1].startswith("==") else 450)
    durations[-1] = 4000
    out = ROOT / "docs" / "demo.gif"
    frames[0].save(out, save_all=True, append_images=frames[1:], duration=durations, loop=0, optimize=True)
    print(f"wrote {out} with {len(frames)} frames, {out.stat().st_size // 1024} KB")


if __name__ == "__main__":
    main()
