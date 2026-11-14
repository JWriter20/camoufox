"""
A claimed canonical family also exposes its legacy styled-family aliases.

Windows records Arial Black, Calibri Light and Franklin Gothic Medium as aliases
of installed families, not necessarily as separate entries in the captured
system font list. The mask must judge the family an alias resolves to, not reject
the spelling before Gecko gets that far.

    python browser/tests/playwright/patches/font-legacy-family-allowlist.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import resolve_binary  # noqa: E402

CANONICAL = ["Arial", "Calibri", "Franklin Gothic"]
ALIASES = ["Arial Black", "Calibri Light", "Franklin Gothic Medium"]
TEXT = "mmMwWLliI0O&1@ẞ∑ж日本語"
PROBE = """(arg) => {
  const c = document.createElement('canvas').getContext('2d');
  return Object.fromEntries(arg.families.map((family) => {
    c.font = `72px "${family}", serif`;
    const m = c.measureText(arg.text);
    return [family, [m.width, m.actualBoundingBoxLeft, m.actualBoundingBoxRight,
      m.actualBoundingBoxAscent, m.actualBoundingBoxDescent]];
  }));
}"""


def measure(fonts):
    from camoufox.sync_api import Camoufox

    with Camoufox(os="windows", headless=True, executable_path=str(resolve_binary()),
                  fonts=fonts, i_know_what_im_doing=True) as browser:
        page = browser.new_page()
        return page.evaluate(PROBE, {"families": ALIASES, "text": TEXT})


def main() -> int:
    control = measure(CANONICAL + ALIASES)
    masked = measure(CANONICAL)
    different = [name for name in ALIASES if control[name] != masked[name]]
    if different:
        for name in different:
            print(f"FAIL {name}: canonical-only {masked[name]} != alias-allowed {control[name]}")
        return 1
    print("PASS: legacy styled-family aliases resolve through their allowed canonical families")
    return 0


if __name__ == "__main__":
    sys.exit(main())
