"""
A service worker exposes WebGPU exactly where the claimed OS's Firefox does.

Firefox gates WebGPU twice (Instance::PrefEnabled in dom/webgpu/Instance.cpp):
dom.webgpu.enabled for every global, and dom.webgpu.service-workers.enabled
on top of it for service workers. Both default to true on Windows and Apple
Silicon and to false in a Linux release build (StaticPrefList.yaml). A Linux
build claiming Windows that turns on only the first has navigator.gpu and
every GPU* interface in its windows and dedicated and shared workers, but
none in a service worker; stock Windows Firefox 158 has all 41 in all four.

The guard registers a service worker on a localhost page (a secure context)
under a Windows and a Linux identity and compares the GPU/WGSL globals and
navigator.gpu of the window and the service worker. They must match, and be
present for Windows and absent for Linux.

    python browser/tests/playwright/patches/webgpu-service-worker-claimed-os.py
"""

import http.server
import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import launch_kwargs  # noqa: E402

PAGE_TIMEOUT_S = 60
CHECK = ("({gpu: typeof navigator.gpu,"
         " names: Object.getOwnPropertyNames(self).filter(k => /^(GPU|WGSL)/.test(k)).sort()})")
SW = ("self.addEventListener('install', () => self.skipWaiting());"
      "self.addEventListener('activate', e => e.waitUntil(self.clients.claim()));"
      f"self.addEventListener('message', e => e.source.postMessage({CHECK}));")
PAGE = f"""<!doctype html><script>
const t = (p, ms) => Promise.race([p, new Promise(r => setTimeout(() => r('timeout'), ms))]);
(async () => {{
  const out = {{secure: isSecureContext, window: {CHECK}}};
  try {{
    await navigator.serviceWorker.register('/sw.js');
    const sw = await t(navigator.serviceWorker.ready.then(r => r.active), 15000);
    out.service = sw === 'timeout' ? 'service worker never activated' : await t(new Promise(r => {{
      navigator.serviceWorker.onmessage = e => r(e.data); sw.postMessage(0); }}), 15000);
  }} catch (e) {{ out.service = String(e); }}
  navigator.sendBeacon('/result', JSON.stringify(out));
}})();
</script>"""


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        body, kind = (SW, "text/javascript") if self.path == "/sw.js" else (PAGE, "text/html")
        self.send_response(200)
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body.encode())

    def do_POST(self):
        self.server.result = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        self.send_response(204)
        self.end_headers()
        self.server.arrived.set()


def measure(server, url, claimed):
    from camoufox.sync_api import Camoufox

    server.arrived = threading.Event()
    with Camoufox(**launch_kwargs(os=claimed, i_know_what_im_doing=True)) as browser:
        page = browser.new_page()
        page.goto(url)
        if not server.arrived.wait(PAGE_TIMEOUT_S):
            return None
        return server.result


def main() -> int:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    # A localhost origin is a secure context, which service workers,
    # navigator.gpu and the GPU interfaces ([SecureContext]) all need.
    url = f"http://localhost:{server.server_address[1]}/"
    failed = False
    try:
        for claimed, exposed in (("windows", True), ("linux", False)):
            result = measure(server, url, claimed)
            if result is None:
                print(f"FAIL [{claimed}]: the page never reported")
                failed = True
                continue
            win, sw = result["window"], result["service"]
            if not result["secure"] or not isinstance(sw, dict):
                print(f"FAIL [{claimed}]: no service worker to compare (secure={result['secure']}, {sw!r})")
                failed = True
                continue
            print(f"[{claimed}] window: navigator.gpu {win['gpu']}, {len(win['names'])} GPU/WGSL globals;"
                  f" service worker: navigator.gpu {sw['gpu']}, {len(sw['names'])}")
            if bool(win["names"]) != exposed or (win["gpu"] == "object") != exposed:
                print(f"FAIL [{claimed}]: the window should {'' if exposed else 'not '}expose WebGPU")
                failed = True
            if sw != win:
                missing = sorted(set(win["names"]) - set(sw["names"]))
                extra = sorted(set(sw["names"]) - set(win["names"]))
                print(f"FAIL [{claimed}]: the service worker differs from the window:"
                      f" missing {missing[:6]}{'...' if len(missing) > 6 else ''} ({len(missing)}),"
                      f" extra {extra} ; navigator.gpu {win['gpu']} vs {sw['gpu']}")
                failed = True
    finally:
        server.shutdown()
    if failed:
        return 1
    print("PASS: a service worker exposes WebGPU where the claimed OS's Firefox does")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
