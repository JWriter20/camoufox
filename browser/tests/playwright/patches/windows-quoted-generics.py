"""
A Windows identity treats quoted generics and fontconfig's generic names as unknown families, as Windows does.

CSS generics are keywords only when unquoted: `font-family: "sans-serif"` names
a family called sans-serif, and stock Firefox on Windows has none, so the text
falls back to the default font. `sans`, `sans serif` and `mono` are
fontconfig's names and mean nothing to Windows either. On Linux,
gfxFcPlatformFontList hands every one of them to FcConfigSubstitute, which
answers its alias (DejaVu Sans, or whatever the fonts.conf maps it to). So a
Windows identity drew a font Windows would not. cfparity's gfx.glyphs probe
quotes its generics and caught this on win-i9 (Firefox 159, 2026-11-09).

The guard measures each name in a fresh Windows identity. Each must draw
exactly what a family nobody has draws, and unquoted sans-serif must still be
a generic.

    python browser/tests/playwright/patches/windows-quoted-generics.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import resolve_binary  # noqa: E402

UNKNOWN = '"cfp-no-such-family"'
AS_UNKNOWN = ('"sans-serif"', '"serif"', '"monospace"', '"cursive"', '"fantasy"', "sans", '"sans serif"', "mono")
PROBE = """(names) => {
  const c = document.createElement('canvas').getContext('2d');
  const out = {};
  for (const n of names) { c.font = `72px ${n}`; out[n] = c.measureText('mmMwWLliI0O&1@').width.toFixed(3); }
  return out;
}"""


def main() -> int:
    from camoufox.sync_api import Camoufox

    with Camoufox(executable_path=str(resolve_binary()), headless=True, os="windows") as browser:
        page = browser.new_page()
        page.set_content("<body></body>")
        widths = page.evaluate(PROBE, [UNKNOWN, "sans-serif", *AS_UNKNOWN])
    for name, width in widths.items():
        print(f"  {name:22} {width}")

    failures = [f"{n} draws {widths[n]}, an unknown family {widths[UNKNOWN]}" for n in AS_UNKNOWN
                if widths[n] != widths[UNKNOWN]]
    if widths["sans-serif"] == widths[UNKNOWN]:
        failures.append(f"unquoted sans-serif draws the default font ({widths['sans-serif']}); it is a generic")
    for failure in failures:
        print(f"FAIL: {failure}")
    if failures:
        return 1
    print("PASS: quoted generics and fontconfig's generic names are unknown families")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
