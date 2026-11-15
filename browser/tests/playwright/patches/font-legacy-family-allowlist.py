"""
A claimed canonical family also exposes the names of its own faces.

Windows enumerates DirectWrite's families (Arial, Calibri, Rockwell, Sitka Text),
and once its deferred font-info loader has landed, a page can also ask for the
nameID 1/16 names of their faces. A list in that namespace (a captured Windows
enumeration, which cfparity mirrors) does not hold those names, so the launcher
adds them (launcher-constants.json), or the allowlist blocks them for good.

Stock 159 on win-i9 (Windows 11 25H2 with Office), fonts.detect in frames, which
run after the loader landed: Arial Narrow, Rockwell Extra Bold and Sitka are
found and Calibri Light is its Light face (944.917, where Calibri is 951.000).
Camoufox found none of the three and drew Calibri Light as Calibri Regular:
the launcher's table had no entry for them, and fonts.conf rewrote the Light
names to their regular family.

Checked with a list of canonical families only, at top level and in a fresh
frame, after the aliases have landed (a legacy lookup starts the loader;
fonts:windowsAliasLoadMs is pinned):
  1. every alias renders, and not as the fallback font;
  2. Calibri Light is not Calibri's regular face;
and before they land:
  3. the bare Sitka is missing (it has no space, so Gecko's Windows fallback
     never strips it to a base family and starts no loader).

    python browser/tests/playwright/patches/font-legacy-family-allowlist.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import resolve_binary  # noqa: E402

# Agency FB is an Office family: Arial Narrow is an Office face of Arial.
CANONICAL = ["Arial", "Calibri", "Franklin Gothic", "Rockwell", "Segoe UI Variable Text", "Sitka Text",
             "Agency FB"]
ALIASES = ["Arial Black", "Calibri Light", "Franklin Gothic Medium",
           "Arial Narrow", "Rockwell Extra Bold", "Segoe UI Variable", "Sitka"]
# What each alias must NOT measure as: the family the page would get if the
# name were answered with the canonical family's regular face.
REGULAR = {"Arial Black": "Arial", "Calibri Light": "Calibri", "Arial Narrow": "Arial",
           "Rockwell Extra Bold": "Rockwell"}
LOAD_MS = 1780
TEXT = "mmMwWLliI0O&1@ẞ∑ж日本語"
PROBE = """async (arg) => {
  const measure = (doc) => {
    const c = doc.createElement('canvas').getContext('2d');
    return Object.fromEntries(['serif', ...arg.families].map((family) => {
      c.font = family === 'serif' ? '72px serif' : `72px "${family}", serif`;
      return [family, c.measureText(arg.text).width];
    }));
  };
  // Asked first: the bare Sitka starts no loader and has no answer yet.
  const early = measure(document);
  // A legacy lookup starts Windows' deferred loader; wait until its aliases land.
  const c = document.createElement('canvas').getContext('2d');
  c.font = '72px "Arial Black", serif';
  c.measureText(arg.text);
  await new Promise((r) => setTimeout(r, arg.wait));
  const top = measure(document);
  const f = document.createElement('iframe');
  f.srcdoc = '<!doctype html><meta charset="utf-8"><p>frame';
  await new Promise((r) => { f.onload = r; document.body.appendChild(f); });
  return { early, top, frame: measure(f.contentDocument) };
}"""


def measure(fonts):
    from camoufox.sync_api import Camoufox

    with Camoufox(os="windows", headless=True, executable_path=str(resolve_binary()),
                  fonts=fonts, config={"fonts:windowsAliasLoadMs": LOAD_MS},
                  i_know_what_im_doing=True) as browser:
        page = browser.new_page()
        page.set_content("<!doctype html><body>")
        return page.evaluate(PROBE, {"families": ["Sitka"], "text": TEXT, "wait": 0}), page.evaluate(
            PROBE, {"families": sorted({*ALIASES, *REGULAR.values()}), "text": TEXT, "wait": LOAD_MS + 1500})


def main() -> int:
    first, got = measure(CANONICAL)
    failed = False
    for realm in ("top", "frame"):
        widths = got[realm]
        for name in ALIASES:
            if widths[name] == widths["serif"]:
                print(f"FAIL {realm} {name}: blocked, measures as the fallback font ({widths[name]})")
                failed = True
            elif name in REGULAR and widths[name] == widths[REGULAR[name]]:
                print(f"FAIL {realm} {name}: drawn as {REGULAR[name]}'s regular face after the aliases "
                      f"landed ({widths[name]})")
                failed = True
    if first["early"]["Sitka"] != first["early"]["serif"]:
        print(f"FAIL early Sitka: answered before the aliases landed ({first['early']['Sitka']}, "
              f"fallback {first['early']['serif']})")
        failed = True
    if failed:
        return 1
    print("PASS: the names of a canonical family's faces resolve through it (top and frame)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
