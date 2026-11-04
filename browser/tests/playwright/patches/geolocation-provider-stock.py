"""
Geolocation never reaches this machine's real position.

Camoufox always uses its network provider (GeolocationService.cpp,
geolocation-spoofing.patch), never the platform's (Linux: the location portal
or Geoclue; CoreLocation; Windows Location), and the network provider serves
the identity's geolocation:latitude/longitude. Without them it never answers
and never asks a location service: a granted page's request stays unanswered,
as with a platform service that has no fix, and the page's own timeout runs out
(code 3, "Position acquisition timed out"), as stock 158.0.1 on Linux with
Geoclue and no fix (cfparity perms.granted/geo). Before, the platform provider
stayed when no coordinates were set, so a page granted geolocation on a desktop
read the machine's real position, and the forced network provider before that
sent the machine's IP to Google and failed at once (code 2).

Checks, Linux identity on a Linux host, the page granted geolocation, the
location service URL pointed at a local server:
  * no coordinates: getCurrentPosition({timeout: 1000}) fails with code 3 after
    the timeout, and the location service gets no request;
  * coordinates: success with them within 1 s, and no request either.

    python browser/tests/playwright/patches/geolocation-provider-stock.py
"""

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


LOCATION_REQUESTS = []


class Page(BaseHTTPRequestHandler):
    def do_POST(self):
        LOCATION_REQUESTS.append(self.path)
        self.send_response(500)
        self.end_headers()

    def do_GET(self):
        if self.path.startswith("/geolocate"):
            LOCATION_REQUESTS.append(self.path)
            self.send_response(500)
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<!doctype html><title>geo</title><p>geo</p>")

    def log_message(self, *args):
        pass


def run(server_url: str, config: dict) -> dict:
    from camoufox.sync_api import Camoufox

    prefs = {"geo.provider.network.url": f"{server_url}/geolocate"}
    with Camoufox(executable_path=str(resolve_binary()), headless=True, os="linux", config=config,
                  firefox_user_prefs=prefs, i_know_what_im_doing=True) as browser:
        context = browser.new_context()
        context.grant_permissions(["geolocation"], origin=server_url)
        page = context.new_page()
        page.goto(server_url)
        return page.evaluate(PROBE)


def main() -> int:
    if sys.platform != "linux":
        print("SKIP: Linux host only")
        return 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), Page)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    errors = []
    try:
        got = run(url, {})
        print(f"  no coordinates: {got}")
        if got.get("cb") != "error" or got.get("code") != 3 or got.get("ms", 0) < 900:
            errors.append(f"no coordinates: {got}; want no answer, so the 1000 ms timeout runs out (code 3)")
        got = run(url, {"geolocation:latitude": 39.7392, "geolocation:longitude": -104.9903})
        print(f"  coordinates: {got}")
        if got.get("cb") != "success" or abs(got["lat"] - 39.7392) > 1e-6 or abs(got["lon"] + 104.9903) > 1e-6:
            errors.append(f"coordinates: {got}, want the configured position within the 1000 ms timeout")
        if LOCATION_REQUESTS:
            errors.append(f"the location service was asked: {LOCATION_REQUESTS}")
    finally:
        server.shutdown()
    for e in errors:
        print(f"FAIL: {e}")
    if errors:
        return 1
    print("PASS: no real position and no location service; the identity's coordinates when set")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
