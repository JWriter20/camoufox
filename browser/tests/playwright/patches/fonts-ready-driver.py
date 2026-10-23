"""
Verify a screenshot leaves the page's document.fonts.ready alone.

Playwright reads document.fonts.ready from its utility world before every
screenshot. Gecko creates the ready promise lazily and resolves it with the
FontFaceSet, looking up "then" on it in the page's realm, so a page with a
getter on Object.prototype.then saw it fire when nothing of its own asked.
fontfaceset-privileged-ready.patch hands the driver a promise in its own global.

What PASS means:
    * a screenshot fires no Object.prototype.then getter in the page;
    * the page's own `await document.fonts.ready` still resolves to the set,
      after the trap has fired for it (the control: the trap works).

    python browser/tests/playwright/patches/fonts-ready-driver.py
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import resolve_binary  # noqa: E402

TRAP = """() => {
  window.__thenHits = [];
  Object.defineProperty(Object.prototype, 'then', {
    configurable: true,
    get() {
      window.__thenHits.push(Object.prototype.toString.call(this));
      return undefined;
    },
  });
}"""


async def main() -> int:
    from camoufox.async_api import AsyncCamoufox

    async with AsyncCamoufox(headless=True, os="linux", i_know_what_im_doing=True,
                             executable_path=str(resolve_binary())) as browser:
        page = await browser.new_page()
        await page.set_content("<p style='font-family: serif'>text</p>")
        await page.evaluate(TRAP)
        await page.screenshot()
        after_screenshot = await page.evaluate("window.__thenHits.slice()")
        own = await page.evaluate("document.fonts.ready.then(set => set === document.fonts)")
        after_own = await page.evaluate("window.__thenHits.slice()")

    checks = [
        ("a screenshot fires no then getter", after_screenshot == [], after_screenshot),
        ("the page's own ready resolves to the set", own is True, own),
        ("the trap fires for the page's own ready", "[object FontFaceSet]" in after_own, after_own),
    ]
    passed = True
    for label, ok, got in checks:
        print(f"  {'PASS' if ok else 'FAIL'} {label}: {got}")
        passed &= ok
    return 0 if passed else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
