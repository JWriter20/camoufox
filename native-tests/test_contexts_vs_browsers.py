"""What a context is, versus what a browser launch is.

Camoufox offers two ways to get an isolated identity, and they are not
interchangeable:

  a new **browser** carries one fingerprint for its whole process, configured
  before launch through CAMOU_CONFIG;

  a new **context** inside one browser carries its own fingerprint, applied
  per-user-context inside the C++ layer.

The second is the reason the per-context patches exist, and it is the one that
is easy to get subtly wrong: a value that is really process-global will look
correct in a single-context test and leak between contexts the moment a second
one opens. That has happened here before -- commit d17c887, "fix screen size
leak in contexts". These tests are the standing check that it has not come back.

They assert *isolation and consistency*, not specific values: the fingerprint is
randomly generated per context, so pinning a number would make the suite a
liability. What must hold is that two contexts differ, that one context stays
internally coherent, and that closing one does not disturb another.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytestmark = pytest.mark.asyncio

# Read-only probes. `page.evaluate` in Camoufox reads values; it does not run
# script in the page's world, which is the whole point of the fork.
PROBES = {
    "userAgent": "navigator.userAgent",
    "platform": "navigator.platform",
    "screenWidth": "screen.width",
    "screenHeight": "screen.height",
    "hardwareConcurrency": "navigator.hardwareConcurrency",
    "timezone": "Intl.DateTimeFormat().resolvedOptions().timeZone",
    "language": "navigator.language",
}


async def probe(page) -> dict:
    out = {}
    for name, expression in PROBES.items():
        try:
            out[name] = await page.evaluate(expression)
        except Exception as exc:  # noqa: BLE001
            out[name] = f"<error: {exc}>"
    return out


async def open_page(browser):
    context = await browser.new_context()
    page = await context.new_page()
    await page.goto("about:blank")
    return context, page


# ---------------------------------------------------------------------------


async def test_two_contexts_get_different_fingerprints(binary):
    """The core promise of per-context spoofing.

    If these come back identical, per-context injection has silently degraded to
    process-global -- which passes every single-context test there is.
    """
    from camoufox.async_api import AsyncCamoufox

    async with AsyncCamoufox(executable_path=str(binary), headless=True,
                             i_know_what_im_doing=True) as browser:
        (ctx_a, page_a), (ctx_b, page_b) = await open_page(browser), await open_page(browser)
        a, b = await probe(page_a), await probe(page_b)
        await ctx_a.close()
        await ctx_b.close()

    differing = [k for k in PROBES if a.get(k) != b.get(k)]
    assert differing, (
        "two contexts in one browser reported an identical fingerprint on every probe.\n"
        f"  {a}\n"
        "Per-context injection has degraded to process-global (cf. commit d17c887)."
    )


async def test_a_context_is_internally_coherent(binary):
    """A fingerprint has to agree with itself.

    Cross-signal inconsistency -- a macOS platform with a Linux user agent -- is
    more detectable than any single wrong value, because it cannot happen on a
    real machine.
    """
    from camoufox.async_api import AsyncCamoufox

    async with AsyncCamoufox(executable_path=str(binary), headless=True, os="macos",
                             i_know_what_im_doing=True) as browser:
        context, page = await open_page(browser)
        values = await probe(page)
        await context.close()

    ua = str(values.get("userAgent", ""))
    platform = str(values.get("platform", ""))
    assert "Firefox" in ua, f"user agent does not claim Firefox: {ua!r}"
    if platform.startswith("Mac"):
        assert "Macintosh" in ua, f"platform {platform!r} disagrees with user agent {ua!r}"
    assert int(values.get("screenWidth") or 0) > 1, values
    assert int(values.get("screenHeight") or 0) > 1, (
        f"screen height is {values.get('screenHeight')!r}. A 1x1 virtual display root "
        "must never clamp the generated screen -- see the `not virtual_display` guards "
        "in pythonlib/camoufox/utils.py."
    )


async def test_closing_one_context_does_not_disturb_another(binary):
    from camoufox.async_api import AsyncCamoufox

    async with AsyncCamoufox(executable_path=str(binary), headless=True,
                             i_know_what_im_doing=True) as browser:
        ctx_a, page_a = await open_page(browser)
        ctx_b, page_b = await open_page(browser)
        before = await probe(page_b)
        await ctx_a.close()
        after = await probe(page_b)
        await ctx_b.close()

    assert before == after, (
        "closing one context changed another context's fingerprint:\n"
        f"  before {before}\n  after  {after}"
    )


async def test_two_browsers_get_different_fingerprints(binary):
    from camoufox.async_api import AsyncCamoufox

    async def one() -> dict:
        async with AsyncCamoufox(executable_path=str(binary), headless=True,
                                 i_know_what_im_doing=True) as browser:
            context, page = await open_page(browser)
            values = await probe(page)
            await context.close()
            return values

    a, b = await asyncio.gather(one(), one())
    differing = [k for k in PROBES if a.get(k) != b.get(k)]
    assert differing, f"two separate browser launches produced an identical fingerprint: {a}"


async def test_a_context_survives_its_sibling_browser(binary):
    """Two browsers are two processes; one closing must not affect the other."""
    from camoufox.async_api import AsyncCamoufox

    async with AsyncCamoufox(executable_path=str(binary), headless=True,
                             i_know_what_im_doing=True) as keeper:
        ctx_keep, page_keep = await open_page(keeper)
        before = await probe(page_keep)

        async with AsyncCamoufox(executable_path=str(binary), headless=True,
                                 i_know_what_im_doing=True) as transient:
            ctx_t, page_t = await open_page(transient)
            await probe(page_t)
            await ctx_t.close()

        after = await probe(page_keep)
        await ctx_keep.close()

    assert before == after, "closing a second browser perturbed the first one's fingerprint"


async def test_pages_in_one_context_share_its_fingerprint(binary):
    """A context is the isolation boundary; a page is not.

    Two pages in one context must agree, or the boundary has been drawn in the
    wrong place and a site could tell two of its own tabs apart.
    """
    from camoufox.async_api import AsyncCamoufox

    async with AsyncCamoufox(executable_path=str(binary), headless=True,
                             i_know_what_im_doing=True) as browser:
        context = await browser.new_context()
        page_one = await context.new_page()
        await page_one.goto("about:blank")
        page_two = await context.new_page()
        await page_two.goto("about:blank")
        one, two = await probe(page_one), await probe(page_two)
        await context.close()

    assert one == two, (
        "two pages in the SAME context reported different fingerprints:\n"
        f"  page 1 {one}\n  page 2 {two}"
    )
