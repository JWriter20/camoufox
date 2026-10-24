"""
A Windows identity picks the fonts stock Firefox picks on Windows, per script,
and lays them out with DirectWrite's line boxes.

The page draws a sample of 28 scripts (Hangul, Thai, Devanagari, Arabic,
Georgian, emoji, ...) in serif, sans-serif and monospace, on a canvas and in
lang-tagged spans, and records the widths. assets/font-fallback-windows.json is
the same page in stock Firefox 158.0 on Windows 11 25H2 at devicePixelRatio 1
(its "source" says where and how).

This build is a Linux build, so without the fixes it resolved fonts the Linux
way: 75 of 168 widths matched Windows (emoji 192 vs 263.6, Thai 323 vs 361.6,
Mongolian 115.8 vs 74.1). The fixes are Windows' font.name-list defaults from
the launcher, async fallback off on a Linux host, slight hinting in the bundled
Windows fonts.conf, and fallback-fonts-claimed-os.patch for the per-script
table and the default font. The default font is what a lang-tagged generic
gets when none of its font.name-list fonts is installed: Arial on Windows
(lang="he" serif names Narkisim and David, lang="hy" monospace Arial AMU,
none of them on Windows 11), where fontconfig matched other faces.

The 6 Mongolian widths still differ (74.07 vs 74.83): the bundled
monbaiti.ttf is an older version than Windows 11 25H2's. That is the font
bundle, not this code; when the bundle carries the current file, raise
MIN_MATCHING to 168.

Line boxes: for 13 bundled Windows fonts at 11, 16 and 37.5px, canvas
measureText()'s fontBoundingBox and emHeight ascent and descent (Gecko's
maxAscent, maxDescent, emAscent, emDescent) and a block's line-height: normal
must be stock Windows': assets/font-metrics-windows.json, recorded with the
widths. They are what gfxDWriteFont::ComputeMetrics derives from the font
file, which font-metrics-windows.patch does too: OS/2 usWinAscent/usWinDescent
(sTypo* when fsSelection bit 7 is set), the line gap max(0, hhea ascender -
descender + lineGap - (winAscent + winDescent)), scale = float(size /
unitsPerEm), ascent and descent rounded to pixels, emAscent = size * ascent /
(ascent + descent), external leading ceil(gap * scale), and the line height
emHeight + internal + external leading in app units, as GetNormalLineHeight
adds them. Arial Black has its own (16px: 18/5, line 23), not Arial Bold's.
FreeType's own hhea values (font-metrics-windows.patch or fonts:metrics
missing) give other numbers on every font.

The font list is pinned too: a launch draws its fonts at random, and which
fonts a draw adds decides some fallbacks, so the guard takes one realistic
draw every time -- the Windows base (font-bases.json) plus the LibreOffice
bundle (font-groups.json, 6.6% of Windows draws). That bundle carries DejaVu
Sans, which this build's global fallback (fontconfig) picked for Lao and
Inuktitut, where DirectWrite's system fallback on Windows picks Leelawadee UI
and Gadugi.

The scale is pinned to 1: at devicePixelRatio 1.25 every canvas width moves by
~0.26% (FreeType's fixed-point advances at another pixel size), as Windows'
would at 125% too, and Windows' emHeight sums to the size less 1/24 px at 2.5
-- a different reference, not a mismatch.

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
LAUNCHER_DATA = Path(__file__).resolve().parents[4] / "python" / "src" / "camoufox"
BASE = "win11"
BUNDLE = "libreoffice"
TOLERANCE_PX = 0.5
METRICS_TOLERANCE_PX = 0.01
LINE_BOXES = """(spec) => {
  const c = document.createElement('canvas').getContext('2d');
  const out = {};
  for (const [size, fonts] of Object.entries(spec.sizes)) {
    out[size] = {};
    for (const family of Object.keys(fonts)) {
      c.font = `${size}px "${family}"`;
      const m = c.measureText(spec.text);
      const div = document.createElement('div');
      div.style.cssText = `font: ${size}px "${family}"; line-height: normal; white-space: nowrap; position: absolute`;
      div.textContent = spec.text;
      document.body.appendChild(div);
      out[size][family] = {
        fontBoundingBoxAscent: m.fontBoundingBoxAscent, fontBoundingBoxDescent: m.fontBoundingBoxDescent,
        emHeightAscent: m.emHeightAscent, emHeightDescent: m.emHeightDescent,
        lineHeight: div.getBoundingClientRect().height,
      };
      div.remove();
    }
  }
  return out;
}"""
# Matching widths out of 168; raise it as parity improves, never lower it.
MIN_MATCHING = 162


class Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def log_error(self, *a):
        pass

    def do_POST(self):
        self.send_response(204)
        self.end_headers()


def pinned_fonts() -> list:
    """The Windows base plus the LibreOffice bundle, from the launcher's data."""
    bases = json.loads((LAUNCHER_DATA / "font-bases.json").read_text(encoding="utf-8"))["win"]
    groups = json.loads((LAUNCHER_DATA / "font-groups.json").read_text(encoding="utf-8"))["win"]
    base = next((b["fonts"] for b in bases if b["id"] == BASE), None)
    bundle = next((g["fonts"] for g in groups if g["id"] == BUNDLE), None)
    if base is None or bundle is None or "DejaVu Sans" not in bundle:
        raise SystemExit(f"FAIL: no {BASE} base or {BUNDLE} bundle with DejaVu Sans -- re-pin the draw")
    return sorted(set(base) | set(bundle))


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
                      fonts=pinned_fonts(), i_know_what_im_doing=True) as browser:
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
        f"{family} {size}px {key}: {boxes[size][family][key]:.4f} vs Windows {want:.4f}"
        for size, fonts in line_boxes["sizes"].items()
        for family, metrics in fonts.items()
        for key, want in metrics.items()
        if abs(boxes[size][family][key] - want) > METRICS_TOLERANCE_PX
    ]
    n_boxes = sum(len(fonts) for fonts in line_boxes["sizes"].values())
    print(f"{n_boxes} line boxes (fonts x sizes) against Windows': {len(box_mismatches)} values differ")
    for m in box_mismatches:
        print("   ", m)
    print()
    failed = False
    if matching < MIN_MATCHING:
        print(f"FAIL: {matching} match, fewer than the {MIN_MATCHING} this build reached")
        failed = True
    if box_mismatches:
        print("FAIL: line boxes are not Windows'")
        failed = True
    if failed:
        return 1
    print(f"PASS: at least {MIN_MATCHING} of {total} widths match, and every line box is Windows'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
