r"""
Gecko's C++ runs as fast, relative to JIT-compiled script, as it does in stock Firefox.

A page can time a call into the engine against plain script on the same page,
and the ratio does not depend on the CPU. Firefox 158 built without PGO and LTO,
Mozilla's own linux64-opt build of the stock source included, sorts with a
comparator about twice as slowly per regexp replace as stock does. Measured over
8-11 fresh launches each of stock 158.0 and Camoufox 158.0-beta.40 on a Ryzen
9850X3D (Linux), an i9-11900H (Windows 11), an Apple M4 (macOS) and a 4-vCPU
GitHub runner, every ratio below separated the two on every machine with one
cut-off:

  ratio         stock, highest   beta.40, lowest   cut-off
  sort / re     1.26             1.51              1.38
  sort / js     16.6             23.2              19.7
  td / js       4.26             4.76              4.50
  json / js     10.2             11.0              10.6
  te / js       3.29             3.53              3.41

Each launch is a fresh profile with no automation attached; the guard fails
when the median of a ratio over the launches reaches its cut-off. Release
builds carry the PGO profile this measures, so the guard runs against them
before they publish (ci/run_patch_guards.py, group `release`); a pull request
build has no profile and fails it by design.

    python browser/tests/playwright/patches/speed-parity.py --binary <camoufox-bin>
"""

import http.server
import json
import statistics
import subprocess
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import free_port, resolve_binary  # noqa: E402

PAGE = (Path(__file__).resolve().parent / "assets" / "speed-parity.html").read_bytes()
LAUNCHES = 6
CUTOFFS = {
    ("sort", "re"): 1.38,
    ("sort", "js"): 19.7,
    ("td", "js"): 4.50,
    ("json", "js"): 10.6,
    ("te", "js"): 3.41,
}
PREFS = ('user_pref("browser.shell.checkDefaultBrowser", false);\n'
         'user_pref("browser.aboutwelcome.enabled", false);\n'
         'user_pref("datareporting.policy.dataSubmissionEnabled", false);\n')


def serve(results):
    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(PAGE)

        def do_POST(self):
            results.put(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", free_port()), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def launch(binary, url, results):
    with tempfile.TemporaryDirectory() as profile:
        Path(profile, "user.js").write_text(PREFS)
        proc = subprocess.Popen([str(binary), "-headless", "-no-remote", "-profile", profile, url],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            return results.get(timeout=180)["r"]
        finally:
            proc.kill()
            proc.wait()


def main():
    import queue

    binary = resolve_binary()
    results = queue.Queue()
    server = serve(results)
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    runs = [launch(binary, url, results) for _ in range(LAUNCHES)]
    server.shutdown()

    failed = []
    for (a, b), cut in CUTOFFS.items():
        values = sorted(r[a] / r[b] for r in runs)
        median = statistics.median(values)
        ok = median < cut
        print(f"{'ok  ' if ok else 'FAIL'} {a}/{b}: median {median:.3f} (launches {values[0]:.3f}-{values[-1]:.3f}), "
              f"cut-off {cut}")
        if not ok:
            failed.append(f"{a}/{b}")
    if failed:
        print(f"{binary} times like a build without stock's PGO+LTO: {', '.join(failed)}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
