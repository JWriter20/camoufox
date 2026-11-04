r"""
A page with a viewport sits inside its window, as in a real browser.

Playwright's viewport= sizes the page; Juggler (PageTarget.updateViewportSize)
then resizes the browser's window around it. CamoufoxWindow.sys.mjs held every
new window at the identity's outer size for its first five seconds and undid
that resize, so a context asking for a viewport larger than the launch window
got a page that overflowed its own window: innerHeight 700 in an outerHeight
699 window (measured on pkg-it3, a Windows identity on a 1366x768 draw). No
real window is smaller than the page it shows.

Now the hold gives way to the viewport, and Juggler sizes the window to the
viewport plus the chrome the claimed OS records (launcher-constants.json
recordedWindowChrome). A browser is one machine with one screen (LEAKS 619),
so a viewport whose window cannot fit the screen's available rect is clamped
by the launcher to the largest that does, with a LeakWarning.

For a Windows and a Linux identity on a 1366x768 screen, launched with a
1000x500 window at 0,0, viewports set through new_context(viewport=),
new_page(viewport=), NewContext(viewport=) and set_viewport_size() must read:

  - outerWidth/outerHeight = the page plus the recorded chrome, so
    inner <= outer on both axes, and the window within the available rect;
  - the machine's screen;
  - a viewport that fits (FITS, larger than the launch window, so the window
    has to grow): exactly the viewport asked for, and no warning;
  - one that does not (VIEWPORT): a LeakWarning naming it, and a page no
    larger than asked for.

The control is the launch page: its window must be too small for FITS, and
the screen must hold FITS and not VIEWPORT, or the check proves nothing.
Measured in the first seconds of each window, inside the startup hold.

    python browser/tests/playwright/patches/juggler-viewport-window.py
"""

import json
import sys
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import resolve_binary  # noqa: E402

FITS = {"width": 1000, "height": 560}
VIEWPORT = {"width": 1000, "height": 700}
RESIZED = {"width": 900, "height": 650}
LAUNCH_WINDOW = (1000, 500)
PAGE = "data:text/html,<p>viewport"
CONSTANTS = Path(__file__).resolve().parents[4] / "python" / "src" / "camoufox" / "launcher-constants.json"

MEASURE = """() => ({
  inner: [innerWidth, innerHeight], outer: [outerWidth, outerHeight], at: [screenX, screenY],
  avail: [screen.availLeft, screen.availTop, screen.availWidth, screen.availHeight],
  screen: [screen.width, screen.height],
})"""


def measure(page) -> dict:
    page.goto(PAGE)
    return page.evaluate(MEASURE)


def _size(viewport: dict) -> str:
    return f"{viewport['width']}x{viewport['height']}"


def fits(control: dict, viewport: dict, chrome: list) -> bool:
    """Whether the viewport's window fits the available rect at the window's position."""
    left, top, width, height = control["avail"]
    x, y = control["at"]
    return (max(x, left) + viewport["width"] + chrome[0] <= left + width
            and max(y, top) + viewport["height"] + chrome[1] <= top + height)


def judge(name: str, seen: dict, viewport: dict, caught: list, chrome: list, control: dict) -> list:
    failures = []
    inner = seen["inner"]
    if seen["screen"] != control["screen"] or seen["avail"] != control["avail"]:
        failures.append(f"{name}: the screen {seen['screen']} {seen['avail']} is not the machine's "
                        f"{control['screen']} {control['avail']}")
    for axis, label in enumerate(("Width", "Height")):
        if inner[axis] > seen["outer"][axis]:
            failures.append(f"{name}: inner{label} {inner[axis]} > outer{label} {seen['outer'][axis]} ({seen})")
    want_outer = [inner[0] + chrome[0], inner[1] + chrome[1]]
    if seen["outer"] != want_outer:
        failures.append(f"{name}: the window is {seen['outer']}, not the page in the recorded chrome "
                        f"{chrome}: {want_outer} ({seen})")
    left, top, width, height = seen["avail"]
    x, y = seen["at"]
    if x < left or y < top or x + seen["outer"][0] > left + width or y + seen["outer"][1] > top + height:
        failures.append(f"{name}: the window {seen['at']}+{seen['outer']} is not within the available "
                        f"screen {seen['avail']}")
    clamped = [m for m in caught if "viewport does not fit" in m]
    if fits(control, viewport, chrome):
        if inner != [viewport["width"], viewport["height"]]:
            failures.append(f"{name}: the page is {inner}, not the {_size(viewport)} asked for ({seen})")
        if clamped:
            failures.append(f"{name}: warned about a viewport that fits: {clamped[0][:160]}")
    else:
        if inner[0] > viewport["width"] or inner[1] > viewport["height"]:
            failures.append(f"{name}: the page is {inner}, larger than the {_size(viewport)} asked for")
        if not any(_size(viewport) in m and "Launch a browser with a larger screen" in m for m in clamped):
            failures.append(f"{name}: no LeakWarning that the {_size(viewport)} viewport does not fit "
                            f"(warnings: {[m[:100] for m in caught]})")
    return failures


def caught_by(fn):
    """fn() and the LeakWarning messages it raised."""
    from camoufox._warnings import LeakWarning

    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        result = fn()
    return result, [str(w.message) for w in caught if issubclass(w.category, LeakWarning)]


def check(os_name: str, binary, headless) -> list:
    from camoufox.fingerprints import Screen
    from camoufox.sync_api import Camoufox, NewContext

    key = {"linux": "lin", "windows": "win", "macos": "mac"}[os_name]
    chrome = json.loads(CONSTANTS.read_text())["recordedWindowChrome"][key]
    seen = {}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with Camoufox(os=os_name, headless=headless, executable_path=str(binary),
                      screen=Screen(min_width=1366, max_width=1366, min_height=768, max_height=768),
                      window=LAUNCH_WINDOW, config={"window.screenX": 0, "window.screenY": 0},
                      i_know_what_im_doing=True) as browser:
            control = measure(browser.new_page())
            for viewport in (FITS, VIEWPORT):
                page, caught = caught_by(lambda: browser.new_context(viewport=viewport).new_page())
                seen[f"new_context(viewport={_size(viewport)})"] = (measure(page), viewport, caught)
                if viewport is FITS:
                    _, caught = caught_by(lambda: page.set_viewport_size(RESIZED))
                    seen[f"set_viewport_size({_size(RESIZED)})"] = (page.evaluate(MEASURE), RESIZED, caught)
                page, caught = caught_by(lambda: browser.new_page(viewport=viewport))
                seen[f"new_page(viewport={_size(viewport)})"] = (measure(page), viewport, caught)
                page, caught = caught_by(lambda: NewContext(browser, os=os_name, viewport=viewport).new_page())
                seen[f"NewContext(viewport={_size(viewport)})"] = (measure(page), viewport, caught)

    print(f"{os_name} identity (recorded chrome {chrome}):")
    print(f"  {'launch page':32} {control}")
    for name, (values, _, caught) in seen.items():
        print(f"  {name:32} {values}")
        for message in caught:
            print(f"  {'':32} warning: {message.splitlines()[0][:200]}")
    failures = []
    if control["outer"][1] >= FITS["height"] + chrome[1]:
        failures.append(f"the launch window {control['outer']} holds {_size(FITS)} already: the check is vacuous")
    if not fits(control, FITS, chrome) or fits(control, VIEWPORT, chrome):
        failures.append(f"the screen must hold {_size(FITS)} and not {_size(VIEWPORT)} ({control}): "
                        "the check is vacuous")
    for name, (values, viewport, caught) in seen.items():
        failures += judge(name, values, viewport, caught, chrome, control)
    return [f"{os_name} identity: {f}" for f in failures]


def main() -> int:
    binary = resolve_binary()
    headless = "--headed" not in sys.argv
    failures = []
    for os_name in ("windows", "linux"):
        failures += check(os_name, binary, headless)
    for f in failures:
        print(f"FAIL: {f}")
    if failures:
        return 1
    print("PASS: every page with a viewport sits inside a window the page plus the recorded chrome, on its "
          "machine's screen; one that cannot fit is clamped, with a warning.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
