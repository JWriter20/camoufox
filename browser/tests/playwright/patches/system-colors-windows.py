"""
A Windows identity reports stock Firefox 158's CSS system colours on Windows 11,
not the Linux host's GTK theme.

assets/system-colors-windows.json is stock Firefox 158.0 on Windows 11 in the
default light theme (provenance in the file): all 92 CSS system colours the
cfparity css.system probe resolves, the same in each of its three launches.
The page resolves the same names the same way in a Windows identity and must
get every one. The launcher's windowsUiColors prefs (launcher-constants.json)
supply the ones the host theme answers otherwise: Highlight and SelectedItem
rgb(0, 120, 215), -moz-dialog white, -moz-cellhighlight rgb(206, 206, 206),
-moz-menuhover rgba(0, 0, 0, 0.118), -moz-menuhovertext and
-moz-menubarhovertext rgb(21, 20, 26). A Linux identity on the same host must
differ on Highlight and -moz-dialog, so the check is not vacuous.

    python browser/tests/playwright/patches/system-colors-windows.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import resolve_binary  # noqa: E402

ASSETS = Path(__file__).resolve().parent / "assets"
# The probe's own resolution (cfparity/web/probes/css.js, css.system).
RESOLVE = """(names) => {
  const el = document.createElement('div');
  document.body.appendChild(el);
  const out = {};
  for (const c of names) {
    el.style.color = '';
    el.style.color = c;
    out[c] = el.style.color ? getComputedStyle(el).color : '<unsupported>';
  }
  return out;
}"""


def resolve(binary, os_name, names):
    from camoufox.sync_api import Camoufox

    with Camoufox(os=os_name, headless=True, executable_path=str(binary),
                  i_know_what_im_doing=True) as browser:
        page = browser.new_page()
        page.set_content("<body></body>")
        return page.evaluate(RESOLVE, names)


def main() -> int:
    recorded = json.loads((ASSETS / "system-colors-windows.json").read_text(encoding="utf-8"))["colors"]
    binary = resolve_binary()
    windows = resolve(binary, "windows", list(recorded))
    linux = resolve(binary, "linux", ["Highlight", "-moz-dialog"])

    mismatches = [f"{name}: {windows[name]} vs Windows 11 {want}"
                  for name, want in recorded.items() if windows[name] != want]
    print(f"{len(recorded) - len(mismatches)}/{len(recorded)} system colours match stock Firefox 158 on Windows 11")
    for m in mismatches:
        print("   ", m)
    if all(linux[n] == recorded[n] for n in linux):
        print("FAIL: a Linux identity reports the Windows colours too -- the check is vacuous")
        return 1
    if mismatches:
        print("FAIL: a Windows identity reports the host theme's system colours")
        return 1
    print("PASS: a Windows identity reports stock Windows 11's system colours.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
