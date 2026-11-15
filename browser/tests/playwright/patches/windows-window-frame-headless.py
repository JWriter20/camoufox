r"""
A headless window reports the frame a window on a desktop reports.

outerWidth/outerHeight, screenX/screenY and mozInnerScreenX/Y are the window
rect and where the content starts in it. A window on a desktop claiming
Windows carries Windows' invisible resize border on the left, right and bottom
(window-frame-claimed-os.patch, in widget/gtk). The headless widget had no
frame at all: outerWidth equalled innerWidth and mozInnerScreenX equalled
screenX, so a page told headless from a window by subtracting two numbers. Its
toolbars also stood a pixel taller, because the headless look-and-feel named
no GTK theme and no client-side decorations, so innerHeight differed too.

widget/headless (windows-window-frame-headless.patch) reports the same frame,
from the code the GTK widget uses, and the same toolbar metrics.

A Linux identity's frame is whatever this host's window manager draws, which
headless has none of: it reports no frame, as a window with client-side
decorations does, and must not be given the Windows one.

    python browser/tests/playwright/patches/windows-window-frame-headless.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import resolve_binary  # noqa: E402

GEOMETRY = ("() => [outerWidth, outerHeight, innerWidth, innerHeight, "
            "screenX, screenY, mozInnerScreenX, mozInnerScreenY]")
# (os, devicePixelRatio, the left frame stock Firefox reports there in CSS px)
CASES = [("windows", 1.0, 8.0), ("windows", 2.5, 6.4), ("linux", 1.0, None)]


def probe(binary, os_name, dpr, headless):
    from camoufox.sync_api import Camoufox

    config = {"window.devicePixelRatio": dpr, "window.outerWidth": 1280, "window.outerHeight": 800,
              "window.screenX": 100, "window.screenY": 60,
              "screen.width": 1920, "screen.height": 1080, "screen.availWidth": 1920, "screen.availHeight": 1032,
              "screen.availLeft": 0, "screen.availTop": 0}
    with Camoufox(headless=headless, os=os_name, executable_path=str(binary),
                  config=config, i_know_what_im_doing=True) as browser:
        page = browser.new_page()
        page.goto("about:blank")
        page.wait_for_timeout(6000)  # past the launcher's startup size hold
        return page.evaluate(GEOMETRY)


def main() -> int:
    binary = resolve_binary()
    failures = []
    for os_name, dpr, frame in CASES:
        window = probe(binary, os_name, dpr, "virtual")
        headless = probe(binary, os_name, dpr, True)
        print(f"  {os_name} dpr {dpr}: window {window}")
        print(f"  {os_name} dpr {dpr}: headless {headless}")
        left = headless[6] - headless[4]
        if frame is None:
            if left != 0 or headless[0] != headless[2]:
                failures.append(f"{os_name} dpr {dpr}: a headless Linux identity got a frame: {headless}")
            continue
        if headless != window:
            failures.append(f"{os_name} dpr {dpr}: headless reports {headless}, a window {window}")
        if abs(left - frame) > 0.01:
            failures.append(f"{os_name} dpr {dpr}: headless left frame {left:.2f}, stock {frame}")
    for failure in failures:
        print(f"FAIL: {failure}")
    if failures:
        return 1
    print("PASS: a headless window reports the geometry of a window on a desktop")
    return 0


if __name__ == "__main__":
    sys.exit(main())
