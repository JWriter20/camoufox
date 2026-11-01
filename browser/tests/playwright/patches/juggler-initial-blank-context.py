r"""
A context's identity holds on a page's initial about:blank, as on a served page.

Juggler ran a context's init scripts on documents it saw created; a new page's
initial about:blank was created before Juggler's FrameTree, so it never ran
them. A driver that called set_content() on a fresh page, without navigating,
read the launch identity -- navigator.platform and appVersion of the launch OS
-- next to the context's own user agent (LEAKS 216). Now Juggler runs the
context's init scripts on that document too.

The launch identity is Windows; contexts claim macOS and Linux. For each, the
navigator a set_content() page reads on the initial about:blank must equal the
one a served page in the same context reads.

    python browser/tests/playwright/patches/juggler-initial-blank-context.py
"""

import http.server
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import launch_kwargs  # noqa: E402

READ = """() => ({
  platform: navigator.platform, appVersion: navigator.appVersion, userAgent: navigator.userAgent,
  oscpu: navigator.oscpu, hardwareConcurrency: navigator.hardwareConcurrency,
})"""


class _Page(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<!doctype html><body><p>x</p></body>")


def main() -> int:
    from camoufox.sync_api import Camoufox, NewContext

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Page)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_address[1]}/"
    failures = []
    with Camoufox(**launch_kwargs(os="windows", canvas_noise=True, i_know_what_im_doing=True)) as browser:
        for os_name in ("macos", "linux"):
            context = NewContext(browser, os=os_name)
            blank_page = context.new_page()
            blank_page.set_content("<p>x</p>")
            blank = blank_page.evaluate(READ)
            blank_url = blank_page.url
            served_page = context.new_page()
            served_page.goto(url)
            served = served_page.evaluate(READ)
            context.close()
            print(f"  {os_name:8s} set_content on {blank_url}: {blank}")
            print(f"  {os_name:8s} served page:            {served}")
            for key, value in served.items():
                if blank[key] != value:
                    failures.append(f"{os_name}: initial about:blank {key} {blank[key]!r}, the context's is {value!r}")
    server.shutdown()
    for failure in failures:
        print(f"FAIL: {failure}")
    if failures:
        return 1
    print("PASS: set_content() on a fresh page reads the context's identity, as a served page does.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
