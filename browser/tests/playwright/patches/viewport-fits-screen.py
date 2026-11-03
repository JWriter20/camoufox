r"""
A viewport the caller asks for must fit on the screen the page sees.

Playwright's viewport= sizes the page, not the window, and nothing checked it
against the screen the identity claims. humanize-seam.py asked for a 1000x700
viewport on a launch that drew a 1366x768 screen: the page read innerHeight
700 with screen.availHeight 768, while a real Firefox window draws its toolbars
(86 px here) above the page. innerHeight + chrome > availHeight is a machine
that cannot exist, and fpgen's recorded 1366x768 Firefox windows never show a
page taller than 683 px.

A browser is one machine with one screen (LEAKS 619): every context keeps the
launch's screen, and the launcher clamps a viewport whose window, the recorded
chrome larger, cannot fit the available area at the browser's window position
to the largest that does, with a LeakWarning naming both sizes
(python/tests/test_viewport_fit.py). A persistent launch is drawn around the
viewport it is given. The guard launches identities on a small drawn screen:

  - the control, a page with no viewport: its outerHeight - innerHeight is the
    chrome this build draws, which must be non-zero; the guard predicts from
    its screen and window which viewports fit, and FITS must, VIEWPORT must
    not, or the check is vacuous;
  - new_page(viewport=), new_context(viewport=) and NewContext(viewport=), for
    each: the launch's screen; the window inside the available rect;
    innerHeight + chrome <= availHeight and innerWidth <= availWidth;
    FITS: exactly the viewport asked for, and no warning;
    VIEWPORT: a LeakWarning that it does not fit, and a page no larger than
    asked for;
  - launch_persistent_context(viewport=): the viewport exactly, and the window
    around the page is the viewport in the recorded chrome, on the screen.

Pages are loaded from a data: URL: the per-context setters run from init
scripts, which a context's first about:blank never gets (as in stock
Playwright).

    python browser/tests/playwright/patches/viewport-fits-screen.py
"""

import sys
import tempfile
import warnings
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import resolve_binary  # noqa: E402

VIEWPORT = {"width": 1000, "height": 700}  # too tall for the small screen
FITS = {"width": 800, "height": 500}
SMALL = {"max_width": 1366, "max_height": 768}
PAGE = "data:text/html,<p>viewport"

MEASURE = """() => ({
  screen: [screen.width, screen.height], avail: [screen.availWidth, screen.availHeight],
  availAt: [screen.availLeft, screen.availTop],
  outer: [outerWidth, outerHeight], inner: [innerWidth, innerHeight], at: [screenX, screenY],
})"""


def measure(page) -> dict:
    page.goto(PAGE)
    return page.evaluate(MEASURE)


def _size(viewport: dict) -> str:
    return f"{viewport['width']}x{viewport['height']}"


def room(control: dict) -> list:
    """The largest window the launch's screen holds at the browser's window
    position: the available rect right and below it."""
    (left, top), (aw, ah), (x, y) = control["availAt"], control["avail"], control["at"]
    return [left + aw - max(x, left), top + ah - max(y, top)]


def judge(name: str, seen: dict, viewport: dict, caught: list, control: dict, chrome: int, fits: bool) -> list:
    failures = []
    (iw, ih), (aw, ah) = seen["inner"], seen["avail"]
    (left, top), (x, y), (ow, oh) = seen["availAt"], seen["at"], seen["outer"]
    if seen["screen"] != control["screen"] or seen["avail"] != control["avail"]:
        failures.append(f"{name}: the screen {seen['screen']} (avail {seen['avail']}) is not the machine's "
                        f"{control['screen']} (avail {control['avail']})")
    if ih + chrome > ah:
        failures.append(f"{name}: innerHeight {ih} + {chrome} px of chrome > screen.availHeight {ah} ({seen})")
    if iw > aw:
        failures.append(f"{name}: innerWidth {iw} > screen.availWidth {aw} ({seen})")
    if x < left or y < top or x + ow > left + aw or y + oh > top + ah:
        failures.append(f"{name}: the window {seen['at']}+{seen['outer']} is not within the available "
                        f"screen {seen['availAt']}+{seen['avail']}")
    clamped = [m for m in caught if "viewport does not fit" in m]
    if fits:
        if [iw, ih] != [viewport["width"], viewport["height"]]:
            failures.append(f"{name}: the page is {iw}x{ih}, not the {_size(viewport)} the caller asked for")
        if clamped:
            failures.append(f"{name}: warned about a viewport that fits: {clamped[0][:160]}")
    else:
        if iw > viewport["width"] or ih > viewport["height"]:
            failures.append(f"{name}: the page is {iw}x{ih}, larger than the {_size(viewport)} asked for")
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


def check(os_name: str, binary) -> list:
    from camoufox import fingerprints
    from camoufox.fingerprints import Screen
    from camoufox.sync_api import Camoufox, NewContext

    # The recorded chrome a window has around its page (launcher-constants.json
    # recordedWindowChrome); a launcher from before it has none, and fails below.
    recorded_chrome = getattr(fingerprints, "RECORDED_WINDOW_CHROME", {"win": [18, 130], "mac": [0, 86], "lin": [2, 86]})
    key = {"linux": "lin", "windows": "win", "macos": "mac"}[os_name]

    seen = {}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with Camoufox(os=os_name, headless=True, executable_path=str(binary), screen=Screen(**SMALL),
                      i_know_what_im_doing=True) as browser:
            control = measure(browser.new_page())
            for viewport in (FITS, VIEWPORT):
                makers = (
                    ("new_page", lambda: browser.new_page(viewport=viewport)),
                    ("new_context", lambda: browser.new_context(viewport=viewport).new_page()),
                    ("NewContext", lambda: NewContext(browser, os=os_name, viewport=viewport).new_page()),
                )
                for name, make in makers:
                    page, caught = caught_by(make)
                    seen[f"{name}(viewport={_size(viewport)})"] = (measure(page), viewport, caught)
        with tempfile.TemporaryDirectory() as profile:
            with Camoufox(os=os_name, headless=True, executable_path=str(binary), persistent_context=True,
                          user_data_dir=profile, viewport=VIEWPORT, i_know_what_im_doing=True) as context:
                persistent = measure(context.new_page())

    print(f"{os_name} identity:")
    print(f"  {'launch page':30} {control}")
    for name, (values, _, caught) in seen.items():
        print(f"  {name:30} {values}")
        for message in caught:
            print(f"  {'':30} warning: {message.splitlines()[0][:200]}")
    print(f"  {'persistent(viewport=)':30} {persistent}")

    chrome = control["outer"][1] - control["inner"][1]
    space = room(control)
    cw, ch = recorded_chrome[key]

    def predicted(viewport):
        return viewport["width"] + cw <= space[0] and viewport["height"] + ch <= space[1]

    failures = []
    if chrome <= 0:
        failures.append(f"the launch page shows no chrome ({control}): the check is vacuous")
    if not predicted(FITS):
        failures.append(f"{_size(FITS)} does not fit the launch's screen at its window ({control}): the check is vacuous")
    if predicted(VIEWPORT):
        failures.append(f"the drawn screen holds {_size(VIEWPORT)} already ({control}): the check is vacuous")
    for name, (values, viewport, caught) in seen.items():
        failures += judge(name, values, viewport, caught, control, chrome, predicted(viewport))

    if persistent["inner"] != [VIEWPORT["width"], VIEWPORT["height"]]:
        failures.append(f"persistent(viewport=): the page is {persistent['inner']}, not {_size(VIEWPORT)}")
    for axis, name in enumerate(("Width", "Height")):
        outer, inner, avail = persistent["outer"][axis], persistent["inner"][axis], persistent["avail"][axis]
        if not inner + recorded_chrome[key][axis] >= outer >= inner + (chrome if axis else 0):
            failures.append(f"persistent(viewport=): outer{name} {outer} is not the {inner} px page in its "
                            f"chrome ({persistent})")
        if outer > avail:
            failures.append(f"persistent(viewport=): outer{name} {outer} > screen.avail{name} {avail}")
    return [f"{os_name} identity: {f}" for f in failures]


def main() -> int:
    binary = resolve_binary()
    failures = []
    for os_name in ("linux", "windows"):
        failures += check(os_name, binary)

    for f in failures:
        print(f"FAIL: {f}")
    if failures:
        return 1
    print("PASS: every page with a viewport fits on its machine's screen with the browser's chrome around it; "
          "one that cannot is clamped, with a warning.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
