"""
A Windows identity reports stock Windows 10's system colours, not the Linux
host's GTK theme.

The launcher sets ui.highlight, ui.selecteditem, ui.accentcolor and the
-moz-dialog and -moz-cellhighlight colours from windowsUiColors in
launcher-constants.json (Highlight #0078d7 is Windows 10's default accent).
The page resolves each as a CSS colour in a Windows identity and must get
exactly those values. A Linux identity on the same host must get other values
for Highlight and -moz-dialog (rgb(51, 153, 255) and rgb(240, 240, 240) from
the headless theme), so the check is not vacuous.

    python browser/tests/playwright/patches/system-colors-windows.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import resolve_binary  # noqa: E402

CONSTANTS = Path(__file__).resolve().parents[4] / "python" / "src" / "camoufox" / "launcher-constants.json"

RESOLVE = """(names) => {
  const d = document.createElement('div');
  document.body.appendChild(d);
  const out = {};
  for (const n of names) { d.style.color = n; out[n] = getComputedStyle(d).color; }
  return out;
}"""


def css_rgb(hex_colour: str) -> str:
    h = hex_colour.lstrip("#")
    return "rgb({}, {}, {})".format(*(int(h[i:i + 2], 16) for i in (0, 2, 4)))


def resolve(binary, os_name, names):
    from camoufox.sync_api import Camoufox

    with Camoufox(os=os_name, headless=True, executable_path=str(binary),
                  i_know_what_im_doing=True) as browser:
        page = browser.new_page()
        page.set_content("<body></body>")
        return page.evaluate(RESOLVE, names)


def main() -> int:
    colours = json.loads(CONSTANTS.read_text(encoding="utf-8"))["windowsUiColors"]
    binary = resolve_binary()
    want = {pref.removeprefix("ui."): css_rgb(v) for pref, v in colours.items()}
    windows = resolve(binary, "windows", list(want))
    linux = resolve(binary, "linux", list(want))

    failed = False
    for name, colour in want.items():
        ok = windows[name] == colour
        print(f"  {'ok  ' if ok else 'FAIL'} {name}: {windows[name]} (Windows {colour}; Linux identity {linux[name]})")
        failed |= not ok
    if all(linux[n] == want[n] for n in ("highlight", "-moz-dialog")):
        print("FAIL: a Linux identity reports the Windows colours too -- the check is vacuous")
        return 1
    if failed:
        print("FAIL: a Windows identity reports the host theme's system colours")
        return 1
    print("PASS: a Windows identity reports Windows 10's system colours.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
