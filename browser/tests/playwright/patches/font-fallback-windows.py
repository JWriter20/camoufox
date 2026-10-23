"""
A Windows identity picks the fonts stock Firefox picks on Windows, per script,
and lays them out with DirectWrite's line boxes.

The page draws a sample of 28 scripts (Hangul, Thai, Devanagari, Arabic,
Georgian, emoji, ...) in serif, sans-serif and monospace, on a canvas and in
lang-tagged spans, and records the widths. assets/font-fallback-windows.json is
the same page in stock Firefox 152.0.4 on Windows 10 (devicePixelRatio 1).
158.0 ships the same Windows font prefs (all.js, which font-prefs.json is
generated from) and per-script table (gfxWindowsPlatform.cpp), so it is the
reference until re-measured.

This build is a Linux build, so without the fixes it resolved fonts the Linux
way: 75 of 168 widths matched Windows (emoji 192 vs 263.6, Thai 323 vs 361.6,
Mongolian 115.8 vs 74.1). The fixes are Windows' font.name-list defaults from
the launcher, async fallback off on a Linux host, slight hinting in the bundled
Windows fonts.conf, and fallback-fonts-claimed-os.patch for the per-script
table.

Line boxes: for 13 bundled Windows fonts at 16px, canvas measureText()'s
fontBoundingBox and emHeight ascent and descent (Gecko's maxAscent, maxDescent,
emAscent, emDescent) and a block's line-height: normal must be what
gfxDWriteFont::ComputeMetrics derives from the same font file:
assets/font-metrics-windows.json. Per font: OS/2 usWinAscent/usWinDescent
(sTypo* when fsSelection bit 7 is set), the line gap max(0, hhea ascender -
descender + lineGap - (winAscent + winDescent)), scale = float(16 / unitsPerEm),
ascent and descent rounded to pixels, emAscent = 16 * ascent / (ascent +
descent), external leading ceil(gap * scale), and the line height
emHeight + internal + external leading in app units, as GetNormalLineHeight
adds them; Arial Black takes Arial Bold's tables. Arial is 17/18 px, as
measured on Windows. FreeType's own hhea values (font-metrics-windows.patch or
fonts:metrics missing) give other numbers on every font.

The scale is pinned to 1: at devicePixelRatio 1.25 every canvas width moves by
~0.26% (FreeType's fixed-point advances at another pixel size), as Windows'
would at 125% too -- a different reference, not a mismatch.

    python browser/tests/playwright/patches/font-fallback-windows.py
"""

import functools
import http.server
import json
import shutil
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import resolve_binary  # noqa: E402

ASSETS = Path(__file__).resolve().parent / "assets"
TOLERANCE_PX = 0.5
METRICS_TOLERANCE_PX = 0.01
LINE_BOXES = """(spec) => {
  const c = document.createElement('canvas').getContext('2d');
  const out = {};
  for (const family of Object.keys(spec.fonts)) {
    c.font = `${spec.size}px "${family}"`;
    const m = c.measureText('Hxg');
    const div = document.createElement('div');
    div.style.cssText = `font: ${spec.size}px "${family}"; line-height: normal; white-space: nowrap; position: absolute`;
    div.textContent = 'Hxg';
    document.body.appendChild(div);
    out[family] = {
      fontBoundingBoxAscent: m.fontBoundingBoxAscent, fontBoundingBoxDescent: m.fontBoundingBoxDescent,
      emHeightAscent: m.emHeightAscent, emHeightDescent: m.emHeightDescent,
      lineHeight: div.getBoundingClientRect().height,
    };
    div.remove();
  }
  return out;
}"""
# Matching widths out of 168; raise it as parity improves, never lower it.
MIN_MATCHING = 160


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def log_error(self, *a):
        pass

    def do_POST(self):
        self.send_response(204)
        self.end_headers()


def main() -> int:
    from camoufox.sync_api import Camoufox

    binary = resolve_binary()
    reference = json.loads((ASSETS / "font-fallback-windows.json").read_text(encoding="utf-8"))
    line_boxes = json.loads((ASSETS / "font-metrics-windows.json").read_text(encoding="utf-8"))
    site = tempfile.mkdtemp()
    shutil.copy(ASSETS / "font-fallback.html", Path(site) / "fb.html")
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(Quiet, directory=site))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_address[1]}/fb.html"
    try:
        with Camoufox(os="windows", headless=True, executable_path=str(binary),
                      firefox_user_prefs={"layout.css.devPixelsPerPx": "1"},
                      i_know_what_im_doing=True) as browser:
            page = browser.new_page()
            page.goto(url)
            page.wait_for_function("document.body.dataset.r", timeout=60000)
            got = json.loads(page.evaluate("document.body.dataset.r"))
            boxes = page.evaluate(LINE_BOXES, line_boxes)
    finally:
        srv.shutdown()

    if got.get("dpr") != 1:
        print(f"FAIL: devicePixelRatio {got.get('dpr')} -- the scale pin did not hold, check is vacuous")
        return 1

    total = 0
    mismatches = []
    for where in ("canvas", "dom"):
        for script, generics in reference[where].items():
            for generic, want in generics.items():
                total += 1
                have = got[where][script][generic]
                if abs(have - want) > TOLERANCE_PX:
                    mismatches.append(f"{where}/{script}/{generic}: {have:.2f} vs Windows {want:.2f}")
    matching = total - len(mismatches)
    print(f"{matching}/{total} per-script widths match stock Firefox on Windows")
    for m in mismatches:
        print("   ", m)
    print()
    box_mismatches = [
        f"{family} {key}: {boxes[family][key]:.4f} vs DirectWrite {want:.4f}"
        for family, metrics in line_boxes["fonts"].items()
        for key, want in metrics.items()
        if abs(boxes[family][key] - want) > METRICS_TOLERANCE_PX
    ]
    print(f"{len(line_boxes['fonts'])} fonts' line boxes against DirectWrite's: {len(box_mismatches)} values differ")
    for m in box_mismatches:
        print("   ", m)
    print()
    failed = False
    if matching < MIN_MATCHING:
        print(f"FAIL: {matching} match, fewer than the {MIN_MATCHING} this build reached")
        failed = True
    if box_mismatches:
        print("FAIL: line boxes are not DirectWrite's")
        failed = True
    if failed:
        return 1
    print(f"PASS: at least {MIN_MATCHING} of {total} widths match, and every line box is DirectWrite's.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
