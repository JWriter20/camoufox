r"""
A context's system fonts follow the OS of the machine it runs on.

`system-ui` and the CSS2 system-font keywords (`font: caption` and the rest)
resolve to the claimed OS's interface font (system-ui-font-spoofing.patch,
font-system-fonts-css2.patch). They asked the launch's navigator.platform, once
for the whole browser, so a context drawn for another OS reported its own
navigator.platform next to the launch OS's interface font:

    AsyncNewContext(browser, os="macos") on a browser launched os="windows":
    navigator.platform "MacIntel", getComputedStyle(caption).fontFamily "Segoe UI"

A context now runs on its browser's machine (LEAKS 619), so that context is
refused instead. For a Windows and a macOS launch in turn, the guard checks:

  * the launch's page and an AsyncNewContext() on it each report the launch
    OS's platform, the family `font: caption` computes to, and `system-ui`
    measuring as that OS's interface family;
  * AsyncNewContext(os=<the other OS>) raises ValueError naming the machine's
    OS and the remedy (launch a browser with that OS). A launcher that accepted
    it produced the mismatch above.

    python browser/tests/playwright/patches/context-os-system-font.py
"""

import asyncio
import http.server
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import resolve_binary  # noqa: E402


class _Page(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<!doctype html><body><p>x</p></body>")


def page_server() -> str:
    """A loaded page for the probe. A new page's initial about:blank never ran
    the context's init scripts (Juggler injects them into documents it sees
    created), so set_content() there showed the launch identity, not the
    context's; a page a site serves always gets them."""
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Page)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{server.server_address[1]}/"

# navigator.platform -> (the family `font: caption` computes to, the family
# system-ui resolves to), as the patches spoof them.
EXPECTED = {
    "Win32": ("Segoe UI", "Segoe UI"),
    "MacIntel": ("-apple-system", "Helvetica"),
}

PROBE = """(uiFamily) => {
  const el = document.createElement('div');
  el.style.font = 'caption';
  document.body.appendChild(el);
  const caption = getComputedStyle(el).fontFamily;
  el.remove();
  const ctx = document.createElement('canvas').getContext('2d');
  const width = font => { ctx.font = font; return ctx.measureText('mmmmmmmmmmlli10OQ@#').width; };
  return {
    platform: navigator.platform,
    caption,
    systemUiIsUiFamily: width('40px system-ui') === width(`40px "${uiFamily}", monospace`),
    uiFamilyInstalled: width(`40px "${uiFamily}", monospace`) !== width('40px monospace'),
  };
}"""


# The launches, the OS each claims, its navigator.platform, and the OS a
# context on it asks for (and is refused).
LAUNCHES = (("windows", "Win32", "macos"), ("macos", "MacIntel", "windows"))


async def run(binary, launch_os, platform, other_os):
    """[(where, probe)] for the launch's page and a context on it, and the
    error AsyncNewContext(os=other_os) raised (None if it was accepted)."""
    from camoufox.async_api import AsyncCamoufox, AsyncNewContext

    results = []
    url = page_server()
    async with AsyncCamoufox(headless=True, os=launch_os, canvas_noise=True,
                             executable_path=str(binary), i_know_what_im_doing=True) as browser:
        launch_page = await browser.new_page()
        await launch_page.goto(url)
        results.append((f"{launch_os} launch", await launch_page.evaluate(PROBE, EXPECTED[platform][1])))

        context = await AsyncNewContext(browser)
        page = await context.new_page()
        await page.goto(url)
        results.append((f"{launch_os} AsyncNewContext()", await page.evaluate(PROBE, EXPECTED[platform][1])))
        await context.close()

        try:
            other = await AsyncNewContext(browser, os=other_os)
        except ValueError as error:
            refusal = str(error)
        else:
            page = await other.new_page()
            await page.goto(url)
            got = await page.evaluate(PROBE, EXPECTED[platform][1])
            await other.close()
            refusal = None
            results.append((f"{launch_os} AsyncNewContext(os={other_os!r}) ACCEPTED", got))
    return results, refusal


def main() -> int:
    binary = resolve_binary()
    failures = []
    for launch_os, platform, other_os in LAUNCHES:
        results, refusal = asyncio.run(run(binary, launch_os, platform, other_os))
        for where, got in results:
            print(f"  {where}: {got}")
            if "ACCEPTED" in where:
                failures.append(f"AsyncNewContext(os={other_os!r}) on a {launch_os} machine was accepted, "
                                f"and reports {got['platform']!r} with font: caption {got['caption']!r}")
                continue
            if got["platform"] != platform:
                failures.append(f"{where}: navigator.platform {got['platform']!r}, the machine's is {platform!r}")
                continue
            caption, ui_family = EXPECTED[platform]
            if got["caption"] != caption:
                failures.append(f"{where}: font: caption computes to {got['caption']!r}, expected {caption!r}")
            if not got["uiFamilyInstalled"]:
                failures.append(f"{where}: {ui_family} is not available, so system-ui cannot be checked")
            elif not got["systemUiIsUiFamily"]:
                failures.append(f"{where}: system-ui does not resolve to {ui_family}")
        if refusal is not None:
            print(f"  {launch_os} AsyncNewContext(os={other_os!r}) refused: {refusal}")
            if launch_os not in refusal or "Launch a browser" not in refusal:
                failures.append(f"the refusal does not name the machine's OS and the remedy: {refusal}")
    for failure in failures:
        print(f"FAIL: {failure}")
    if failures:
        return 1
    print("PASS: a context's system fonts follow its machine's OS, and another OS is refused")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
