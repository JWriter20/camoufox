"""
A granted page of a Windows identity meets the identity's OS location setting.

Stock Firefox on Windows asks the OS whether desktop apps may have the location
(GeolocationSystemWin.cpp). When they may not yet (win-i9: the OS will prompt)
or may not at all, a page whose geolocation permission is granted still reads
query() "prompt" (PermissionStatusSink.cpp ComputeGeolocationBehavior), and
getCurrentPosition() waits: no callback, whatever its timeout (cfparity
perms.granted on win-i9: query "prompt", cb "pending"). What it waits on is
the permission doorhanger, shown again although the site is allowed
(nsGeolocationRequest::GetIgnoreAllowSitePermission while the behaviour is not
NoPrompt; PermissionUI prompt()), with the "This will open system location
settings" hint; its Allow then opens the tab-modal system-settings dialog
(Geolocation::ReallowWithSystemPermissionOrCancel), and closing that ends the
request with PERMISSION_DENIED (dom.geolocation.require_system_permission.
enabled is true on Windows). Neither is a page dialog: Playwright's "dialog"
event reports content-modal prompts only (Juggler's _updateModalDialogs reads
getContentDialogManager()), so the guard does not count them. A headed run
under Xvfb (2026-10-31, pkg-it2) showed the doorhanger with that hint.
Camoufox on Linux used the generic GeolocationSystem.cpp, which always answers
NoPrompt: "granted", then error 2 from the network provider.

geolocation-spoofing.patch reads geolocation:systemPermission ("allowed",
"prompt", "denied") there; the launcher draws it per Windows identity.

Checks, on a page granted geolocation:
  * "prompt" and "denied": query() "prompt"; getCurrentPosition({timeout:1000})
    pending after 3 s, its timeout not run, and no page dialog;
  * "allowed" with coordinates, site granted (context.grant_permissions, as a
    user's Allow): query() "granted" and the coordinates;
  * "allowed" with coordinates, site never granted: query() "prompt" and the
    request waits on the doorhanger, as stock's does. The coordinates are what
    a granted site receives; they never grant it (Camoufox used to auto-allow
    in Geolocation::RegisterRequestWithPrompt and set permissions.default.geo
    1, so a first visit read "granted" with no prompt).

    python browser/tests/playwright/patches/geolocation-system-setting-claimed-os.py
"""

import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import resolve_binary  # noqa: E402

PROBE = """async () => {
  const query = (await navigator.permissions.query({ name: 'geolocation' })).state;
  const call = new Promise((res) => {
    navigator.geolocation.getCurrentPosition(
      (p) => res({ cb: 'success', lat: p.coords.latitude, lon: p.coords.longitude }),
      (e) => res({ cb: 'error', code: e.code }), { timeout: 1000 });
  });
  window.__geoCall = call;
  const first = await Promise.race([call, new Promise((res) => setTimeout(() => res({ cb: 'pending' }), 3000))]);
  return { query, first };
}"""


class Page(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<!doctype html><title>geo</title><p>geo</p>")

    def log_message(self, *args):
        pass


def run(server_url: str, config: dict, grant: bool = True) -> dict:
    from camoufox.sync_api import Camoufox

    with Camoufox(executable_path=str(resolve_binary()), headless=True, os="windows", config=config,
                  i_know_what_im_doing=True) as browser:
        context = browser.new_context()
        if grant:
            # As a user who clicked Allow (and remembered it) for this site.
            context.grant_permissions(["geolocation"], origin=server_url)
        page = context.new_page()
        dialogs = []
        # A listener keeps Playwright from dismissing a page dialog on its own.
        page.on("dialog", lambda d: dialogs.append(d))
        page.goto(server_url)
        got = page.evaluate(PROBE)
        got["dialogs"] = len(dialogs)
    return got


def main() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Page)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}"
    errors = []
    try:
        for state in ("prompt", "denied"):
            got = run(url, {"geolocation:systemPermission": state})
            print(f"  {state}: {got}")
            if got["query"] != "prompt":
                errors.append(f"{state}: a granted page reads query() {got['query']!r}, stock's reads 'prompt'")
            if got["first"] != {"cb": "pending"}:
                errors.append(f"{state}: getCurrentPosition answered {got['first']}, stock's waits on the permission doorhanger")
            if got["dialogs"]:
                errors.append(f"{state}: {got['dialogs']} page dialogs opened; stock's prompt is the doorhanger")
        got = run(url, {"geolocation:systemPermission": "allowed",
                        "geolocation:latitude": 39.7392, "geolocation:longitude": -104.9903})
        print(f"  allowed: {got}")
        if got["query"] != "granted" or got["first"].get("cb") != "success" or got["dialogs"]:
            errors.append(f"allowed: {got}, want query 'granted', the coordinates and no dialog")
        # Coordinates do not grant the site: one never granted asks, as stock.
        got = run(url, {"geolocation:systemPermission": "allowed",
                        "geolocation:latitude": 39.7392, "geolocation:longitude": -104.9903}, grant=False)
        print(f"  not granted: {got}")
        if got["query"] != "prompt":
            errors.append(f"not granted: query() {got['query']!r} on a first visit; stock reads 'prompt'")
        if got["first"] != {"cb": "pending"}:
            errors.append(f"not granted: getCurrentPosition answered {got['first']} without a prompt; "
                          "stock waits on the permission doorhanger")
    finally:
        server.shutdown()
    for e in errors:
        print(f"FAIL: {e}")
    if errors:
        return 1
    print("PASS: a granted page meets the identity's OS location setting as on stock Windows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
