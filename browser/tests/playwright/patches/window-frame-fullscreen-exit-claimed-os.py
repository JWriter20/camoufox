r"""
A page leaving fullscreen under a claimed Windows frame sees its window come back in one step.

Stock Firefox, on Windows and on Linux, answers the end of fullscreen with one
`fullscreenchange` and one `resize`, and every size the page can read after the
first of them is the restored window's. A Linux build claiming Windows gave
the page a `resize` at the screen's width in between: innerWidth 1920 with the
toolbars back, then the restored 1000.

The claimed frame is none in fullscreen and Windows' resize border in a normal
window, and the size mode changes before the client area does: for a moment
the window was the screen-sized client area inside the new border, 1936 wide.
Gecko waits for the widget's resize only while the window is still the
screen's size, so it finished the change at once and laid the page out
screen-wide. window-frame-claimed-os.patch now keeps the frame of the size
mode the client area belongs to until the client area fits the restored
window.

The claimed screen is the display's, as it is wherever the launcher sizes the
display: Gecko compares the window with the screen it is told about.

Needs a window manager that restores the window (openbox): a bare X server has
none, and fullscreen there is only a resize.

    python browser/tests/playwright/patches/window-frame-fullscreen-exit-claimed-os.py
"""

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import hidden_display, resolve_binary  # noqa: E402

SCREEN = (1920, 1080)
# The page's own script and the harness's evaluate run in different worlds: the events are kept on the document.
PAGE = """<!doctype html><body><button onclick="document.documentElement.requestFullscreen()">go</button><script>
const root = document.documentElement;
for (const type of ['fullscreenchange', 'resize']) {
  (type === 'resize' ? window : document).addEventListener(type, () => {
    root.dataset.seen = JSON.stringify(JSON.parse(root.dataset.seen || '[]').concat(
      [[type, innerWidth, innerHeight, !!document.fullscreenElement]]));
  });
}
</script></body>"""

def probe(binary, claimed):
    from camoufox.fingerprints import Screen
    from camoufox.sync_api import Camoufox

    screen = Screen(min_width=SCREEN[0], max_width=SCREEN[0], min_height=SCREEN[1], max_height=SCREEN[1])
    with Camoufox(executable_path=str(binary), headless=False, os=claimed, window=(1000, 700), screen=screen,
                  i_know_what_im_doing=True) as browser:
        page = browser.new_page()
        page.set_content(PAGE)
        page.wait_for_timeout(6000)  # past the launcher's startup size hold
        before = page.evaluate("[innerWidth, innerHeight]")
        page.click("button")
        page.wait_for_function(f"document.fullscreenElement && innerWidth === {SCREEN[0]}", timeout=10000)
        page.wait_for_timeout(1000)
        page.evaluate("document.documentElement.dataset.seen = '[]'; document.exitFullscreen()")
        page.wait_for_function("!document.fullscreenElement", timeout=10000)
        page.wait_for_timeout(2000)
        return before, json.loads(page.evaluate("document.documentElement.dataset.seen"))


def main() -> int:
    binary = resolve_binary()
    if not shutil.which("openbox"):
        raise SystemExit("openbox is required for this guard")
    failures = []
    with hidden_display(f"{SCREEN[0]}x{SCREEN[1]}"):
        wm = subprocess.Popen(["openbox", "--sm-disable"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            time.sleep(1)
            if wm.poll() is not None:
                raise SystemExit(f"openbox did not start on {os.environ['DISPLAY']}")
            for claimed in ("windows", "linux"):
                before, seen = probe(binary, claimed)
                print(f"  {claimed:8} window {before}  leaving fullscreen {seen}")
                sizes = [event[1:3] for event in seen]
                if not sizes or sizes[-1] != before:
                    failures.append(f"claiming {claimed}: the window did not come back at {before}")
                between = [size for size in sizes if size != before]
                if between:
                    failures.append(f"claiming {claimed}: the page read {between} on the way back to {before}")
        finally:
            wm.terminate()
    for failure in failures:
        print(f"FAIL: {failure}")
    if failures:
        return 1
    print("PASS: leaving fullscreen restores the window in one step under either frame")
    return 0


if __name__ == "__main__":
    sys.exit(main())
