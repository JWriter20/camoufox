"""
Without identity coordinates, geolocation uses the platform's provider, as stock.

camoufox.cfg set geo.provider.testing, which makes GeolocationService::Init
replace the platform provider (Linux: the location portal or Geoclue) with the
network one. Camoufox has no Google location key, so with no coordinates
configured a granted page's getCurrentPosition() failed within ~0.4 s with
POSITION_UNAVAILABLE (code 2, "Unknown error acquiring position"). Stock
158.0.1 on the same Linux host selects Geoclue, which has no fix yet: the
page's {timeout: 1000} runs out (code 3, "Position acquisition timed out";
cfparity perms.granted/geo, also with no identity at all). The pref now keeps
stock's default, and GeolocationService.cpp (geolocation-spoofing.patch) picks
the network provider only when geolocation:latitude/longitude are set, which it
serves at once.

Checks, Linux identity on a Linux host, the page granted geolocation:
  * no coordinates, Geoclue on the system bus: getCurrentPosition({timeout:
    1000}) does not fail with code 2 (stock: code 3 after 1 s). Skipped when
    the host has no Geoclue (stock then uses the network provider too);
  * coordinates: success with them within 1 s.

    python browser/tests/playwright/patches/geolocation-provider-stock.py
"""

import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import resolve_binary  # noqa: E402

PROBE = """async () => {
  const t0 = performance.now();
  return await new Promise((res) => navigator.geolocation.getCurrentPosition(
    (p) => res({ cb: 'success', lat: p.coords.latitude, lon: p.coords.longitude, ms: Math.round(performance.now() - t0) }),
    (e) => res({ cb: 'error', code: e.code, message: e.message, ms: Math.round(performance.now() - t0) }),
    { timeout: 1000 }));
}"""


class Page(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<!doctype html><title>geo</title><p>geo</p>")

    def log_message(self, *args):
        pass


def host_has_geoclue() -> bool:
    if not shutil.which("gdbus"):
        return False
    r = subprocess.run(["gdbus", "introspect", "--system", "--dest", "org.freedesktop.GeoClue2",
                        "--object-path", "/org/freedesktop/GeoClue2/Manager"],
                       capture_output=True, text=True, timeout=10)
    return r.returncode == 0


def run(server_url: str, config: dict) -> dict:
    from camoufox.sync_api import Camoufox

    with Camoufox(executable_path=str(resolve_binary()), headless=True, os="linux", config=config,
                  i_know_what_im_doing=True) as browser:
        context = browser.new_context()
        context.grant_permissions(["geolocation"], origin=server_url)
        page = context.new_page()
        page.goto(server_url)
        return page.evaluate(PROBE)


def main() -> int:
    if sys.platform != "linux":
        print("SKIP: the Linux platform provider")
        return 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), Page)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    errors = []
    try:
        if host_has_geoclue():
            got = run(url, {})
            print(f"  no coordinates: {got}")
            if got.get("cb") == "error" and got.get("code") == 2:
                errors.append(f"no coordinates: {got['message']!r} after {got['ms']} ms; stock's Geoclue provider "
                              "has no fix and the 1000 ms timeout runs out (code 3)")
        else:
            print("  no coordinates: SKIP (no Geoclue on the system bus; stock uses the network provider here)")
        got = run(url, {"geolocation:latitude": 39.7392, "geolocation:longitude": -104.9903})
        print(f"  coordinates: {got}")
        if got.get("cb") != "success" or abs(got["lat"] - 39.7392) > 1e-6 or abs(got["lon"] + 104.9903) > 1e-6:
            errors.append(f"coordinates: {got}, want the configured position within the 1000 ms timeout")
    finally:
        server.shutdown()
    for e in errors:
        print(f"FAIL: {e}")
    if errors:
        return 1
    print("PASS: geolocation uses stock's provider, and the identity's coordinates when set")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
