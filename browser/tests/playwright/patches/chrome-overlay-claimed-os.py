r"""
A page sees the chrome:// files and about: pages of the OS its identity claims.

A page can load any file of a contentaccessible chrome package and can tell a
registered about: module, chrome package or resource host from an absent one:

    try { new Worker('about:third-party').terminate(); /* registered */ }
    catch (e) { /* SecurityError: not registered */ }

Stock Firefox 158.0.1 on Windows registers about:third-party,
about:windows-messages, chrome://gfxsanity and resource://gfxsanity and ships
aboutThirdParty.*, aboutWindowsMessages.*, printPageSetup.*, four migration
brand PNGs and a default-agent image; on Linux it registers about:webauthn and
ships aboutWebauthn.*; both register about:crashes and about:studies
(cfparity cross-os page/namespaces, run 6b, stock win-i9 3/3). A Linux
Camoufox claiming Windows answered Linux's set, and had neither
about:crashes nor about:studies.

chrome-overlay-claimed-os.patch serves pages the claimed OS's stock bytes
(toolkit/camoufox-overlay, from browser/additions) and registers the about:
modules of that OS. For each claim this guard, from a web page:
- constructs Workers for the five about: pages and the gfxsanity names, and
  asserts stock's answer;
- loads every script, stylesheet and image the overlay keys by OS
  (overlay.json) and the patched scripts it serves stock bytes of, asserting
  it loads exactly when stock ships it, and that a script defines the same
  functions with the same source, and an image has the same size, as the stock
  copy served over HTTP. Markup (.html/.xhtml) is not loadable from a page in
  any form -- a document load of chrome:// is refused and Opaque Response
  Blocking fails it as a subresource whether or not it exists (measured on
  pkg-gate3: browser.xhtml errors as a <script> under the Linux claim, where
  the build ships it) -- so its bytes are left to contentaccessible-parity.py.
It also asserts, statically, that the browser's own loads of those URLs still
resolve to the build's files, never the overlay's.

    python browser/tests/playwright/patches/chrome-overlay-claimed-os.py [--binary <camoufox-bin>]
"""

import asyncio
import functools
import http.server
import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import resolve_binary  # noqa: E402

BROWSER_ROOT = Path(__file__).resolve().parents[3]
OVERLAY = BROWSER_ROOT / "additions" / "toolkit" / "camoufox-overlay"
sys.path.insert(0, str(BROWSER_ROOT / "scripts"))
import _chrome_view as chrome_view  # noqa: E402

# new Worker(url) returns (True) or throws (False), as stock Firefox 158.0.1 answers
# on each OS (run 6b page/namespaces: stock win-i9; stock Linux from the same probe).
REGISTERED = {
    "windows": {
        "about:third-party": True, "about:windows-messages": True, "about:webauthn": False,
        "about:crashes": True, "about:studies": True,
        "chrome://gfxsanity/content/cfparity-missing.js": True,
        "resource://gfxsanity/cfparity-missing.js": True,
    },
    "linux": {
        "about:third-party": False, "about:windows-messages": False, "about:webauthn": True,
        "about:crashes": True, "about:studies": True,
        "chrome://gfxsanity/content/cfparity-missing.js": False,
        "resource://gfxsanity/cfparity-missing.js": False,
    },
}
IMAGES = (".png", ".svg", ".jpg", ".gif", ".ico", ".webp")
MARKUP = (".html", ".xhtml")

PROBE_JS = r"""
async ({workers, loads}) => {
  const fnv = (s) => { let h = 0x811c9dc5; for (let i = 0; i < s.length; i++) { h ^= s.charCodeAt(i); h = Math.imul(h, 0x01000193) >>> 0; } return h.toString(16); };
  const registered = {};
  for (const u of workers) {
    try { new Worker(u).terminate(); registered[u] = true; } catch (e) { registered[u] = false; }
  }
  const timeout = (p) => Promise.race([p, new Promise(r => setTimeout(() => r({ok: null}), 8000))]);
  const image = (url) => timeout(new Promise(res => {
    const el = new Image();
    el.onload = () => res({ok: true, sig: `${el.naturalWidth}x${el.naturalHeight}`});
    el.onerror = () => res({ok: false});
    el.src = url;
  }));
  const sheet = (url) => timeout(new Promise(res => {
    const el = document.createElement('link');
    el.rel = 'stylesheet';
    el.onload = () => { el.remove(); res({ok: true}); };
    el.onerror = () => { el.remove(); res({ok: false}); };
    el.href = url;
    document.head.appendChild(el);
  }));
  // A classic script in a fresh same-origin frame: the functions it declares
  // are bound before it runs, so their source reads back even when its first
  // statement throws (ChromeUtils is not a page global).
  const script = (url) => timeout(new Promise(res => {
    const f = document.createElement('iframe');
    document.body.appendChild(f);
    const w = f.contentWindow;
    const before = new Set(Object.getOwnPropertyNames(w));
    const s = w.document.createElement('script');
    s.onload = () => {
      const fns = Object.getOwnPropertyNames(w).filter(n => !before.has(n)).sort().map(n => {
        try { const v = w[n]; return n + '=' + (typeof v === 'function' ? Function.prototype.toString.call(v) : typeof v); }
        catch (e) { return n + '!'; }
      });
      f.remove();
      res({ok: true, sig: fnv(fns.join('\n')) + ':' + fns.length});
    };
    s.onerror = () => { f.remove(); res({ok: false}); };
    s.src = url;
    w.document.head.appendChild(s);
  }));
  const results = {};
  for (const [url, kind] of loads) {
    results[url] = await (kind === 'image' ? image(url) : kind === 'sheet' ? sheet(url) : script(url));
  }
  return {registered, results};
}
"""


def kind_of(url: str) -> str:
    if url.endswith(IMAGES):
        return "image"
    if url.endswith(".css"):
        return "sheet"
    return "script"


def expectations(overlay: dict, os_name: str) -> dict:
    """{chrome url: overlay-relative stock copy, or None when stock does not ship it}."""
    out = {url: per_os[os_name] for url, per_os in overlay["entries"].items()}
    out.update(overlay["stock_for_pages"])
    return {url: rel for url, rel in out.items() if not url.endswith(MARKUP)}


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


def serve(directory: Path):
    handler = functools.partial(_Quiet, directory=str(directory))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


async def probe(binary: Path, os_name: str, origin: str, loads: list) -> dict:
    from camoufox.async_api import AsyncCamoufox

    async with AsyncCamoufox(headless=True, os=os_name, executable_path=str(binary),
                             i_know_what_im_doing=True) as browser:
        page = await browser.new_page()
        # A web page: the overlay applies to loads a web principal triggers.
        await page.goto(f"{origin}/")
        # The probe runs as the page's own script: page.evaluate() runs in an
        # isolated world, whose Xray view of the frame hides the functions a
        # loaded script declares. It hands its result over in the DOM.
        arg = json.dumps({"workers": list(REGISTERED[os_name]), "loads": loads})
        await page.add_script_tag(content=(
            f"({PROBE_JS})({arg}).then(r => document.documentElement.setAttribute('data-probe', JSON.stringify(r)),"
            f" e => document.documentElement.setAttribute('data-probe', JSON.stringify({{error: String(e)}})));"))
        await page.wait_for_function("document.documentElement.hasAttribute('data-probe')", timeout=180_000)
        got = json.loads(await page.evaluate("document.documentElement.getAttribute('data-probe')"))
        if "error" in got:
            raise RuntimeError(f"probe threw: {got['error']}")
        return got


def static_ui_check(binary: Path, overlay: dict, failures: list) -> None:
    """The browser's own loads of an overlaid URL keep the build's file."""
    urls = set(overlay["entries"]) | set(overlay["stock_for_pages"])
    root = binary.parent
    with chrome_view.chrome_roots(root) as (gre, app):
        for os_name in ("windows", "linux"):
            reg = chrome_view.registry(gre, app, chrome_view.install_os(root), os_name)
            leaked = sorted(u for u in urls if "camoufox-overlay" in str(reg.resolve(u, content=False) or ""))
            failures += [f"{os_name}: the browser's own load of {u} resolves into the overlay" for u in leaked]
            served = [u for u in urls if u in reg.content_overrides]
            if not served:
                failures.append(f"{os_name}: no camoucontent override is registered (overlay not packaged?)")
            else:
                print(f"  ok  {os_name}: {len(served)} content-only overrides; the UI keeps its own files")


def main() -> int:
    binary = resolve_binary()
    overlay = json.loads((OVERLAY / "overlay.json").read_text())
    failures: list = []

    print("Static: the UI's own loads")
    static_ui_check(binary, overlay, failures)

    server = serve(OVERLAY)
    origin = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        for os_name in ("windows", "linux"):
            expect = expectations(overlay, os_name)
            loads = []
            for url, rel in sorted(expect.items()):
                loads.append([url, kind_of(url)])
                if rel:
                    loads.append([f"{origin}/{rel}", kind_of(url)])
            got = asyncio.run(probe(binary, os_name, origin, loads))
            print(f"\n{os_name} claim:")
            for url, want in REGISTERED[os_name].items():
                have = got["registered"].get(url)
                if have != want:
                    failures.append(f"{os_name}: new Worker({url!r}) {'returns' if have else 'throws'}, "
                                    f"stock {'returns' if want else 'throws'}")
                else:
                    print(f"  ok  {'returns' if want else 'throws':7} new Worker({url})")
            for url, rel in sorted(expect.items()):
                res = got["results"].get(url, {})
                if res.get("ok") is None:
                    failures.append(f"{os_name}: {url} timed out")
                    continue
                if res["ok"] != bool(rel):
                    failures.append(f"{os_name}: {url} {'loads' if res['ok'] else 'does not load'}; stock "
                                    f"{'ships' if rel else 'does not ship'} it")
                    continue
                if rel:
                    ref = got["results"].get(f"{origin}/{rel}", {})
                    if res.get("sig") != ref.get("sig"):
                        failures.append(f"{os_name}: {url} reads back {res.get('sig')}, the stock copy "
                                        f"{ref.get('sig')}")
                        continue
                print(f"  ok  {'loads' if rel else 'absent':7} {url}")
    finally:
        server.shutdown()

    print()
    for f in failures:
        print(f"FAIL: {f}")
    print("FAIL" if failures else "PASS")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
