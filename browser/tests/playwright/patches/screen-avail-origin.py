r"""
Every context reads the available screen stock Firefox reports, and a popup
is placed in the drawn work area.

A real screen's work area often does not start at 0,0: a dock on the left, a
top bar, a second monitor. fpgen's recorded Firefox screens put
availLeft/availTop off 0,0 for about 14% of Linux and Windows draws (66,32
for a GNOME dock and bar), and the browser places windows in the drawn work
area (MaskConfig screen.availLeft/availTop: ScreenHelperGTK's claimed
screen, window-frame-claimed-os.patch), so window.open(..., 'left=37,top=41')
comes up at 66,41, as on such a machine.

What a page reads depends on stock's baseline fingerprinting protection:

  - on (the default, RFPTarget ScreenAvailToResolution): the available rect
    is derived from the screen size -- origin 0,0 (0,25 on macOS), the whole
    width, the height less the claimed OS's taskbar -- in every context, as
    in stock Firefox on any machine;
  - off (ScreenAvailToResolution overridden): the work area itself, origin
    included. A NewContext() context set its screen through the per-context
    setter, which carries a size only, so it read availLeft/availTop 0 next
    to the default context's 66,32 (LEAKS 635). A context now runs on its
    browser's machine and reads the machine's screen from the launch config.

For a Linux and a Windows identity with a work area at 66,32, the default
context and a NewContext() context, in each mode, must read the same
screen.avail* (the value above), their popups the same, and a popup opened
at left=37,top=41 must land inside the work area.

    python browser/tests/playwright/patches/screen-avail-origin.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import resolve_binary  # noqa: E402

AVAIL = [66, 32, 1854, 1048]  # left dock 66 px, top bar 32 px
PLACED = [37, 41]  # left of and above the work area
CONFIG = {
    "screen.width": 1920, "screen.height": 1080,
    "screen.availLeft": AVAIL[0], "screen.availTop": AVAIL[1],
    "screen.availWidth": AVAIL[2], "screen.availHeight": AVAIL[3],
    "window.screenX": 123, "window.screenY": 45,
    "window.outerWidth": 1200, "window.outerHeight": 800,
}

READ = "() => [screen.availLeft, screen.availTop, screen.availWidth, screen.availHeight]"


async def probe(page) -> dict:
    await page.goto("about:blank")
    avail = await page.evaluate(READ)
    async with page.context.expect_page() as popup_info:
        await page.evaluate(
            f"() => {{ window.open('about:blank', '', "
            f"'width=400,height=300,left={PLACED[0]},top={PLACED[1]}'); }}"
        )
    popup = await popup_info.value
    await popup.wait_for_load_state()
    positions = []
    for _ in range(4):
        positions.append(await popup.evaluate("() => [screenX, screenY]"))
        await asyncio.sleep(0.5)
    popup_avail = await popup.evaluate(READ)
    await popup.close()
    return {"avail": avail, "popup at": positions, "popup avail": popup_avail}


TASKBAR = {"linux": 0, "windows": 48}
FPP_OFF = {"privacy.baselineFingerprintingProtection.overrides": "-ScreenAvailToResolution"}


async def run(binary, os_name: str, prefs: dict) -> dict:
    from camoufox.async_api import AsyncCamoufox, AsyncNewContext

    seen = {}
    async with AsyncCamoufox(headless=True, os=os_name, executable_path=str(binary),
                             config=dict(CONFIG), firefox_user_prefs=dict(prefs),
                             i_know_what_im_doing=True) as browser:
        seen["default context"] = await probe(await browser.new_page())
        context = await AsyncNewContext(browser)
        seen["NewContext()"] = await probe(await context.new_page())
        await context.close()
    return seen


def main() -> int:
    binary = resolve_binary()
    failures = []
    for os_name in ("linux", "windows"):
        legs = (
            ("baseline protection on", {},
             [0, 0, CONFIG["screen.width"], CONFIG["screen.height"] - TASKBAR[os_name]]),
            ("ScreenAvailToResolution off", FPP_OFF, AVAIL),
        )
        for leg, prefs, expected in legs:
            seen = asyncio.run(run(binary, os_name, prefs))
            for name, got in seen.items():
                where = f"{os_name}, {leg}, {name}"
                print(f"  {where}: {got}")
                if got["avail"] != expected:
                    failures.append(f"{where}: screen.avail* reads {got['avail']}, expected {expected}")
                if got["popup avail"] != got["avail"]:
                    failures.append(f"{where}: the popup reads screen.avail* {got['popup avail']}, "
                                    f"its opener {got['avail']}")
                for x, y in got["popup at"]:
                    if x < AVAIL[0] or y < AVAIL[1]:
                        failures.append(f"{where}: the popup sits at {x},{y}, outside the work area "
                                        f"starting at {AVAIL[0]},{AVAIL[1]}")
                        break
    for failure in failures:
        print(f"FAIL: {failure}")
    if failures:
        return 1
    print("PASS: every context reads the available screen stock reports, and popups are placed in the work area")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
