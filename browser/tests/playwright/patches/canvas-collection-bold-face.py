"""
An accelerated canvas draws a font collection's bold face, as a CPU canvas does.

An accelerated 2D canvas is recorded in the content process and replayed in the
GPU process, which reopens each font from its file and collection index.
UnscaledFontFontconfig::CreateFromFontDescriptor checked that pair with
FcFontMatch. fontconfig does not match on the index, and FcDefaultSubstitute
asks for regular weight. So Nirmala.ttc's Bold (index 1) came back as index 0,
and YuGothB.ttc's Bold as its Semibold UI face, and bold text drew thin on the
GPU canvas only. cfparity's gfx.glyphs probe caught it against win-i9
(Firefox 159, 2026-11-09).

The guard draws each family bold on a CPU canvas (willReadFrequently) and on an
accelerated one. Both must match, and differ from the regular weight. It runs
on a virtual display, where the canvas is accelerated.

    python browser/tests/playwright/patches/canvas-collection-bold-face.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import resolve_binary  # noqa: E402

# Accelerate and remote the canvas whatever GPU the display has.
PREFS = {"gfx.canvas.accelerated.force-enabled": True, "gfx.canvas.remote.allow-in-parent": True}
# Collection faces whose replay came back as another face on win-i9's run;
# Nirmala UI (Bold is Nirmala.ttc index 1) fails without the fix here.
FAMILIES = ("Nirmala UI", "Yu Gothic")
PROBE = """([family, weight, cpu]) => {
  const c = document.createElement('canvas'); c.width = 400; c.height = 60;
  const ctx = c.getContext('2d', {willReadFrequently: cpu});
  ctx.fillStyle = '#fff'; ctx.fillRect(0, 0, 400, 60); ctx.fillStyle = '#000';
  ctx.font = `${weight} 32px "${family}"`; ctx.fillText('Hamburgefonstiv', 4, 44);
  return c.toDataURL();
}"""


def main() -> int:
    from camoufox.sync_api import Camoufox

    failures = []
    with Camoufox(executable_path=str(resolve_binary()), headless="virtual", os="windows",
                  fonts=list(FAMILIES), firefox_user_prefs=PREFS) as browser:
        page = browser.new_page()
        page.set_content("<body></body>")
        for family in FAMILIES:
            cpu_bold, gpu_bold, cpu_regular = (page.evaluate(PROBE, args) for args in (
                [family, "bold", True], [family, "bold", False], [family, "normal", True]))
            same = gpu_bold == cpu_bold
            print(f"  {family:12} accelerated bold {'matches' if same else 'DIFFERS from'} the CPU canvas")
            if cpu_bold == cpu_regular:
                failures.append(f"{family}: bold draws as regular on the CPU canvas, so nothing is tested")
            if not same:
                failures.append(f"{family}: the accelerated canvas draws another face for bold")
    for failure in failures:
        print(f"FAIL: {failure}")
    if failures:
        return 1
    print("PASS: an accelerated canvas draws a collection's bold face")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
