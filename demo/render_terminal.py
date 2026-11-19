"""Turn a real captured log into a terminal-style still (PNG) for the demo video.

Every pixel here comes from output that actually ran; nothing is typed by hand.
Usage:
    python3 render_terminal.py --title "bridge.py demo" --cmd "python3 bridge.py demo" \
        --log /tmp/demo.txt --out shots/01_demo.png
"""

from __future__ import annotations

import argparse
import os
import re
from typing import List

from PIL import Image, ImageDraw, ImageFont

# Noto Sans Mono CJK SC keeps Latin monospaced and gives real glyphs for Chinese.
FONT_MONO = ("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", 7)
FONT_MONO_BOLD = ("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc", 7)
INDENT_PX = 72

BG = (13, 17, 23)
BAR = (22, 27, 34)
BORDER = (48, 54, 61)
FG = (201, 209, 217)
DIM = (110, 118, 129)
GREEN = (46, 160, 67)
BRIGHT_GREEN = (63, 185, 80)
CYAN = (86, 182, 194)
YELLOW = (210, 153, 34)
RED = (248, 81, 73)
WHITE = (240, 246, 252)

ANSI = re.compile(r"\x1b\[[0-9;]*m")


def strip_ansi(text: str) -> str:
    return ANSI.sub("", text)


def colour_for(line: str) -> tuple:
    s = line.strip()
    if s.startswith("$") or s.startswith("python3") or s.startswith("cargo") or s.startswith("git "):
        return BRIGHT_GREEN
    if s.startswith("[") and ("/3]" in s or "]" in s[:5]):
        return CYAN
    if s.startswith("task "):
        return CYAN
    if s.startswith("status:") or " status:" in line:
        return GREEN if "success" in line else YELLOW
    if s.startswith("saved") or s.startswith("wrote") or "wrote " in s:
        return BRIGHT_GREEN
    if s.startswith("link ") or s.startswith("replaced:"):
        return FG
    if s.startswith("asset") or "asset " in s or " tris," in s:
        return WHITE
    if "error" in s.lower() or "failed" in s.lower() or "traceback" in s.lower():
        return RED
    if s.startswith("#") or s.startswith("---"):
        return DIM
    return FG


def wrap(line: str, limit_px: float, font: ImageFont.FreeTypeFont) -> List[str]:
    """Wrap on measured width so CJK (full-width) and Latin (half-width) both fit."""
    if font.getlength(line) <= limit_px:
        return [line]
    out, cur, cur_px, first = [], "", 0.0, True
    for ch in line:
        w = font.getlength(ch)
        if cur_px + w > limit_px:
            out.append(cur)
            cur, cur_px = "", 0.0
            if not first:
                cur = " " * 4
                cur_px = font.getlength(cur)
            first = False
        cur += ch
        cur_px += w
    if cur.strip():
        out.append(cur)
    return out


def render(title: str, cmd: str, log_path: str, out_path: str, width: int = 1600,
           size: int = 22, max_lines: int = 30) -> None:
    with open(log_path, "r", encoding="utf-8", errors="replace") as handle:
        raw = strip_ansi(handle.read()).rstrip("\n").splitlines()

    font = ImageFont.truetype(FONT_MONO[0], size, index=FONT_MONO[1])
    bold = ImageFont.truetype(FONT_MONO_BOLD[0], size, index=FONT_MONO_BOLD[1])
    pad = 34
    limit_px = width - 2 * pad
    lines: List[str] = []
    for line in raw:
        lines.extend(wrap(line, limit_px, font))
    clipped = len(lines) > max_lines
    lines = lines[:max_lines]

    line_h = int(size * 1.55)
    pad = 34
    bar_h = 54
    height = bar_h + pad + line_h * (len(lines) + 2) + pad

    img = Image.new("RGB", (width, height), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, width - 1, height - 1], outline=BORDER, width=2)
    d.rectangle([0, 0, width - 1, bar_h], fill=BAR, outline=BORDER)
    for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        d.ellipse([22 + i * 26, 18, 34 + i * 26, 30], fill=c)
    d.text((width // 2, bar_h // 2), title, font=bold, fill=FG, anchor="mm")

    y = bar_h + pad
    prompt_line = f"$ {cmd}"
    d.text((pad, y), prompt_line, font=bold, fill=BRIGHT_GREEN)
    y += line_h

    for line in lines:
        d.text((pad, y), line, font=font, fill=colour_for(line))
        y += line_h

    if clipped:
        d.text((pad, y), f"… {len(raw) - max_lines} more lines", font=font, fill=DIM)

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    img.save(out_path)
    print(f"wrote {out_path} ({img.width}x{img.height}, {len(lines)} lines)")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", required=True)
    ap.add_argument("--cmd", required=True)
    ap.add_argument("--log", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--max-lines", type=int, default=30)
    args = ap.parse_args()
    render(args.title, args.cmd, args.log, args.out, max_lines=args.max_lines)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
