r"""
A NewContext() identity's navigator.appVersion follows its own user agent.

navigator.appVersion came only from the launch config, so a macOS context in
a Windows launch reported "5.0 (Windows)" beside a Macintosh user agent, in the
page and in its workers -- a mismatch a page reads in one line. The launcher
hands each context the appVersion it derives from that context's user agent
(setNavigatorAppVersion), so the expected value is the launcher's own rule
applied to the user agent the page sees.

A context now runs on its browser's machine (LEAKS 619): it claims the launch
OS, and asking it for another is refused, since a context cannot carry
another machine's GPU limits, fonts or glyph rendering. So the guard checks:

  * NewContext(browser) and NewContext(browser, os=<the launch OS>): appVersion
    follows the context's user agent in the page and in a worker, and both
    equal the launch's own;
  * NewContext(browser, os="macos") on the Windows launch raises ValueError
    naming the machine's OS and the way to claim another (launch a browser
    with it). A launcher that accepted it handed the context a macOS user
    agent on a Windows machine.

The launch has canvas noise on, since a browser without it binds its contexts
to the host.

    python browser/tests/playwright/patches/context-app-version.py
"""

import http.server
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import launch_kwargs  # noqa: E402


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

READ = """async () => {
  const src = 'postMessage([navigator.appVersion, navigator.userAgent])';
  const w = new Worker(URL.createObjectURL(new Blob([src])));
  const worker = await new Promise(r => { w.onmessage = e => r(e.data); });
  return {window: [navigator.appVersion, navigator.userAgent], worker};
}"""


def main() -> int:
    from camoufox.fingerprints import _app_version_from_user_agent
    from camoufox.sync_api import Camoufox, NewContext

    failures = []
    url = page_server()
    with Camoufox(**launch_kwargs(os="windows", canvas_noise=True, i_know_what_im_doing=True)) as browser:
        launch_page = browser.new_page()
        launch_page.goto(url)
        launch = launch_page.evaluate(READ)
        launch_page.close()
        print(f"  launch   window={launch['window']}")

        for name, kwargs in (("NewContext()", {}), ("NewContext(os='windows')", {"os": "windows"})):
            context = NewContext(browser, **kwargs)
            page = context.new_page()
            page.goto(url)
            seen = page.evaluate(READ)
            context.close()
            print(f"  {name} window={seen['window']} worker={seen['worker']}")

            for where in ("window", "worker"):
                app_version, user_agent = seen[where]
                expected = _app_version_from_user_agent(user_agent)
                if app_version != expected:
                    failures.append(
                        f"{name} {where}: appVersion {app_version!r}, expected {expected!r} for UA {user_agent!r}"
                    )
                if [app_version, user_agent] != launch["window"]:
                    failures.append(
                        f"{name} {where}: {[app_version, user_agent]!r}, the machine's is {launch['window']!r}"
                    )

        try:
            context = NewContext(browser, os="macos")
        except ValueError as error:
            print(f"  NewContext(os='macos') refused: {error}")
            if "windows" not in str(error) or "Launch a browser" not in str(error):
                failures.append(f"the refusal does not name the machine's OS and the remedy: {error}")
        else:
            page = context.new_page()
            page.goto(url)
            seen = page.evaluate(READ)
            context.close()
            failures.append(f"NewContext(os='macos') on a Windows machine was accepted: window={seen['window']}")

    for failure in failures:
        print(f"FAIL: {failure}")
    if failures:
        return 1
    print("PASS: a context's appVersion follows its own user agent on its browser's machine; "
          "another OS is refused.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
