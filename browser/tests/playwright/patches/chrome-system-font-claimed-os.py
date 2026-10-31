"""
The browser's own chrome claiming Windows uses the Windows system font, so its bars are Windows's height.

A page cannot read chrome styles, but it reads their size: an infobar (pop-up
blocked, find bar, notifications) takes its height out of innerHeight. Stock
Firefox 158 on Windows draws them in the NONCLIENTMETRICS font, "Segoe UI"
12 px; a Linux build claiming Windows drew them in GTK's "Sans" 10pt (13.33
px), so on win-i9 at 250 % the pop-up blocked bar was 39.2 CSS px where
stock's is 39.6, and the page's innerHeight read 770 where stock's reads 769
(cfparity chrome.window, both GPUs).

The guard opens a pop-up without a gesture, which Firefox blocks with that bar,
and reads the bar's font through Marionette in the chrome context, on a hidden
display. Claiming Windows: "Segoe UI" 12px. Claiming Linux: the host's own
font, as stock Linux Firefox has it.

    python browser/tests/playwright/patches/chrome-system-font-claimed-os.py
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import Marionette, free_port, hidden_display, launch_kwargs, marionette_args  # noqa: E402

# A file, not a data: URL: Firefox opens no top-level data: document from the command line.
PAGE = "<!doctype html><p>bar</p><script>setTimeout(() => open('about:blank', 'x', 'width=300,height=200'), 300)</script>"

BAR = """
const deadline = Date.now() + 15000;
const box = gBrowser.getNotificationBox();
while (!box.currentNotification && Date.now() < deadline) await new Promise(r => setTimeout(r, 100));
const bar = box.currentNotification;
if (!bar) return null;
const cs = getComputedStyle(bar);
return {value: bar.getAttribute("value"), family: cs.fontFamily, size: cs.fontSize,
        height: bar.getBoundingClientRect().height};
"""


def bar_font(claimed):
    from camoufox.utils import launch_options

    options = launch_options(**launch_kwargs(os=claimed, headless=False, window=(1280, 900),
                                             i_know_what_im_doing=True))
    port = free_port()
    prefs = {**(options.get("firefox_user_prefs") or {}), "marionette.port": port,
             # Marionette's recommended prefs let pop-ups through; the bar needs the blocker.
             "remote.prefs.recommended": False}
    with tempfile.TemporaryDirectory() as profile:
        page = Path(profile, "bar.html")
        page.write_text(PAGE)
        Path(profile, "user.js").write_text("".join(f"user_pref({json.dumps(k)}, {json.dumps(v)});\n"
                                                    for k, v in prefs.items()))
        env = {**os.environ, **{k: str(v) for k, v in (options.get("env") or {}).items()},
               "MOZ_CRASHREPORTER_DISABLE": "1"}
        args = [a for a in options.get("args") or [] if a not in ("-headless", "--headless")]
        proc = subprocess.Popen([options["executable_path"], *args, "-no-remote", "-profile", profile,
                                 *marionette_args(), page.as_uri()],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env)
        try:
            m = Marionette(port)
            try:
                time.sleep(1)
                return m.js("return (async () => {" + BAR + "})()")
            finally:
                m.close()
        finally:
            proc.kill()
            proc.wait()


def main() -> int:
    failures = []
    with hidden_display("1920x1080"):
        for claimed in ("windows", "linux"):
            bar = bar_font(claimed)
            print(f"  {claimed:8} {bar}")
            if not bar or bar["value"] != "popup-blocked":
                failures.append(f"claiming {claimed}: no pop-up blocked bar")
                continue
            windows_font = bar["family"] == "Segoe UI" and bar["size"] == "12px"
            if claimed == "windows" and not windows_font:
                failures.append(f"claiming windows: the bar is {bar['size']} {bar['family']!r}, "
                                "stock Windows's is 12px 'Segoe UI'")
            if claimed == "linux" and bar["family"] == "Segoe UI":
                failures.append("claiming linux: the bar took the Windows font, not the host's")
    for failure in failures:
        print(f"FAIL: {failure}")
    if failures:
        return 1
    print("PASS: chrome claiming Windows draws its bars in the Windows system font, and the host's otherwise")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
