"""
A macOS identity draws text in the fonts whose only cmap is a Unicode-platform
UCS4 one: Geneva, Apple Symbols, Heiti TC and Heiti SC.

Each of those bundled faces has a single Unicode subtable, (0, 3) or (0, 4)
format 12 (Geneva (0, 3); the others (0, 4), Heiti beside Mac Roman ones).
Gecko's non-macOS cmap reader takes format 12 only at (3, 10), so without
cmap-unicode-ucs4-fallback.patch the face gets an empty character map and
every glyph falls back to the next family: on real macOS these families draw.

The page sets 16px "Hamburgefonstiv" in `"<family>", "Menlo"` and measures it
on a canvas. The width must be the family's own, the sum of its hmtx advances
at 16px from the same font file (each family covers all 15 letters):
Geneva 131.73, Apple Symbols 93.54, Heiti TC and SC 132.45 (Light and Medium
alike). A glyph's advance may round to a whole pixel under hinting, so the
tolerance is half a pixel per glyph. Menlo alone (what the empty map gives)
is the vacuity check: it must be another width.

    python browser/tests/playwright/patches/cmap-ucs4-macos.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import resolve_binary  # noqa: E402

TEXT = "Hamburgefonstiv"
FALLBACK = "Menlo"
# Sum of hmtx advances for TEXT at 16px, from the bundled font files.
EXPECTED = {
    "Geneva": 131.7344,
    "Apple Symbols": 93.5391,
    "Heiti TC": 132.448,
    "Heiti SC": 132.448,
}
TOLERANCE_PX = 0.5 * len(TEXT)
MEASURE = """([families, fallback, text]) => {
  const c = document.createElement('canvas').getContext('2d');
  const width = (font) => { c.font = font; return c.measureText(text).width; };
  const out = {[fallback]: width(`16px "${fallback}"`)};
  for (const f of families) out[f] = width(`16px "${f}", "${fallback}"`);
  return out;
}"""


def main() -> int:
    from camoufox.sync_api import Camoufox

    binary = resolve_binary()
    with Camoufox(os="macos", headless=True, executable_path=str(binary),
                  firefox_user_prefs={"layout.css.devPixelsPerPx": "1"},
                  i_know_what_im_doing=True) as browser:
        page = browser.new_page()
        page.set_content("<body></body>")
        got = page.evaluate(MEASURE, [list(EXPECTED), FALLBACK, TEXT])

    fallback = got[FALLBACK]
    print(f"{FALLBACK} alone: {fallback:.4f}")
    failed = False
    for family, want in EXPECTED.items():
        have = got[family]
        ok = abs(have - want) <= TOLERANCE_PX and abs(have - fallback) > TOLERANCE_PX
        print(f"  {'ok  ' if ok else 'FAIL'} {family}: {have:.4f} (own advances {want:.4f})")
        failed |= not ok
    if abs(fallback - min(EXPECTED.values())) <= TOLERANCE_PX:
        print(f"FAIL: {FALLBACK} is as wide as a family under test -- the check is vacuous")
        return 1
    if failed:
        print("FAIL: a UCS4-only family fell back, its cmap was not read")
        return 1
    print("PASS: every UCS4-only family draws with its own glyphs.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
