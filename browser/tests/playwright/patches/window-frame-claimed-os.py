r"""
A Linux build claiming Windows reports a Windows window frame.

outerWidth/outerHeight, screenX/screenY and mozInnerScreenX/Y are the window
rect and where the content starts in it, so their differences are the frame
around the content. Windows Firefox draws its tabs in the titlebar: a normal
window's rect carries only the invisible resize border, on the left, right and
bottom, SM_CXFRAME + SM_CXPADDEDBORDER device pixels (8 at 96 DPI, 16 at 240
DPI). On Linux the GTK widget answered with this host's window manager
decorations instead: none on a bare X server (outer == inner), a 1 px border
and an 8 CSS px titlebar under openbox. Stock Firefox 158 on win-i9 (240 DPI,
dpr 2.5): outer 1280x900, inner 1267x809, mozInnerScreenX 6.4.

widget/gtk (window-frame-claimed-os.patch) now reports the claimed OS's frame
and keeps the real one for placing the window.

    python browser/tests/playwright/patches/window-frame-claimed-os.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import resolve_binary  # noqa: E402

GEOMETRY = ("() => [outerWidth, outerHeight, innerWidth, innerHeight, "
            "screenX, screenY, mozInnerScreenX, mozInnerScreenY]")
# (os, devicePixelRatio, the frame stock reports there in CSS px)
CASES = [("windows", 2.5, 6.4), ("windows", 1.0, 8.0), ("linux", 2.5, None)]


async def probe(binary, os_name, dpr):
    from camoufox.async_api import AsyncCamoufox

    config = {"window.devicePixelRatio": dpr, "window.innerWidth": 1267, "window.innerHeight": 700,
              "window.screenX": 0, "window.screenY": 0,
              "screen.width": 1382, "screen.height": 864, "screen.availWidth": 1382, "screen.availHeight": 816}
    async with AsyncCamoufox(headless="virtual", os=os_name, executable_path=str(binary),
                             config=config, i_know_what_im_doing=True) as browser:
        page = await browser.new_page()
        await page.goto("about:blank")
        await page.wait_for_timeout(6000)  # past the launcher's startup size hold
        return await page.evaluate(GEOMETRY)


def main():
    binary = resolve_binary()
    failures = []
    for os_name, dpr, frame in CASES:
        ow, oh, iw, ih, sx, sy, mx, my = asyncio.run(probe(binary, os_name, dpr))
        left, bottom = mx - sx, (oh - ih) - (my - sy)
        print(f"{os_name} dpr {dpr}: outer {ow}x{oh} inner {iw}x{ih} screen {sx},{sy} "
              f"mozInnerScreen {mx:.2f},{my:.2f} -> left {left:.2f} bottom {bottom:.2f}")
        if frame is None:
            if left >= 1:
                failures.append(f"{os_name} dpr {dpr}: a Linux identity got a Windows frame (left {left:.2f})")
            continue
        if abs(left - frame) > 0.01:
            failures.append(f"{os_name} dpr {dpr}: left frame {left:.2f}, stock {frame}")
        if ow != round(iw + 2 * frame):
            failures.append(f"{os_name} dpr {dpr}: outerWidth {ow}, stock innerWidth + 2 x {frame}")
        if abs(bottom - frame) > 1:  # outerHeight and innerHeight are rounded to whole px
            failures.append(f"{os_name} dpr {dpr}: bottom frame {bottom:.2f}, stock {frame}")
    for f in failures:
        print("FAIL", f)
    print("FAIL" if failures else "PASS")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
