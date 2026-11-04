"""
A claimed canonical family also exposes its legacy styled-family aliases.

Windows records Arial Black, Calibri Light and Franklin Gothic Medium as aliases
of installed families, not necessarily as separate entries in the captured
system font list. The mask must judge the family an alias resolves to, not reject
the spelling before Gecko gets that far.

A list in DirectWrite's namespace (cfparity mirrors stock win-i9's enumeration)
names Arial, Rockwell, Segoe UI Variable Text and Sitka Text; once Windows'
deferred font-info loader lands, stock also resolves the nameID 1/16 names of
their faces. cfparity it7 (both GPUs) caught the gap in frames, which run after
the loader landed: stock frame-same/-cross/-sandbox fonts.detect found Arial
Narrow, Rockwell Extra Bold, Segoe UI Variable and Sitka (95 families), Camoufox
found none of them (91), because the launcher's windowsLegacyFontFamilies had no
entries for them and the allowlist blocked each name. So this measures after the
aliases land (a legacy lookup starts the loader; fonts:windowsAliasLoadMs is
pinned), at top level and in a fresh frame.

    python browser/tests/playwright/patches/font-legacy-family-allowlist.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import resolve_binary  # noqa: E402

CANONICAL = ["Arial", "Calibri", "Franklin Gothic", "Rockwell", "Segoe UI Variable Text", "Sitka Text"]
ALIASES = ["Arial Black", "Calibri Light", "Franklin Gothic Medium",
           "Arial Narrow", "Rockwell Extra Bold", "Segoe UI Variable", "Sitka"]
LOAD_MS = 1780
TEXT = "mmMwWLliI0O&1@ẞ∑ж日本語"
PROBE = """async (arg) => {
  const measure = (doc) => {
    const c = doc.createElement('canvas').getContext('2d');
    return Object.fromEntries(['serif', ...arg.families].map((family) => {
      c.font = family === 'serif' ? '72px serif' : `72px "${family}", serif`;
      const m = c.measureText(arg.text);
      return [family, [m.width, m.actualBoundingBoxLeft, m.actualBoundingBoxRight,
        m.actualBoundingBoxAscent, m.actualBoundingBoxDescent]];
    }));
  };
  // A legacy lookup starts Windows' deferred loader; wait until its aliases land.
  const c = document.createElement('canvas').getContext('2d');
  c.font = '72px "Arial Black", serif';
  c.measureText(arg.text);
  await new Promise((r) => setTimeout(r, arg.wait));
  const top = measure(document);
  const f = document.createElement('iframe');
  f.srcdoc = '<!doctype html><meta charset="utf-8"><p>frame';
  await new Promise((r) => { f.onload = r; document.body.appendChild(f); });
  return { top, frame: measure(f.contentDocument) };
}"""


def measure(fonts):
    from camoufox.sync_api import Camoufox

    with Camoufox(os="windows", headless=True, executable_path=str(resolve_binary()),
                  fonts=fonts, config={"fonts:windowsAliasLoadMs": LOAD_MS},
                  i_know_what_im_doing=True) as browser:
        page = browser.new_page()
        page.set_content("<!doctype html><body>")
        return page.evaluate(PROBE, {"families": ALIASES, "text": TEXT, "wait": LOAD_MS + 1500})


def main() -> int:
    control = measure(CANONICAL + ALIASES)
    masked = measure(CANONICAL)
    failed = False
    for realm in ("top", "frame"):
        for name in ALIASES:
            if control[realm][name] == control[realm]["serif"]:
                print(f"FAIL {realm} {name}: does not render even when listed (bundle?) {control[realm][name]}")
                failed = True
            elif control[realm][name] != masked[realm][name]:
                print(f"FAIL {realm} {name}: canonical-only {masked[realm][name]} != alias-allowed {control[realm][name]}")
                failed = True
    if failed:
        return 1
    print("PASS: legacy styled-family aliases resolve through their allowed canonical families (top and frame)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
