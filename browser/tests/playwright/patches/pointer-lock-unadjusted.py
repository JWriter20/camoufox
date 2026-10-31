r"""
requestPointerLock({unadjustedMovement: true}) answers as the claimed OS does.

Firefox checks whether the widget supports unadjusted (raw) movement before it
checks for a user gesture. Stock Windows Firefox supports it, so a request made
outside an input handler rejects with NotAllowedError; GTK does not, so stock
Linux rejects with NotSupportedError. A Windows identity on Linux answered with
Linux's error, which a page reads with one call and no prompt.

Measured on stock Firefox 158 on Windows 11 and on Linux. Needs a Linux host.

    python browser/tests/playwright/patches/pointer-lock-unadjusted.py
"""

import platform
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import launch_kwargs  # noqa: E402

READ = """async () => {
  const el = document.body.appendChild(document.createElement('div'));
  try {
    await el.requestPointerLock({unadjustedMovement: true});
    return 'locked';
  } catch (e) {
    return e.name;
  }
}"""

EXPECTED = {"windows": "NotAllowedError", "linux": "NotSupportedError"}


def main() -> int:
    from camoufox.sync_api import Camoufox

    if platform.system() != "Linux":
        raise SystemExit("this guard needs a Linux host: GTK is the widget without unadjusted movement")
    failures = []
    for os_name, expected in EXPECTED.items():
        with Camoufox(**launch_kwargs(os=os_name, i_know_what_im_doing=True)) as browser:
            page = browser.new_page()
            page.set_content("<p>x</p>")
            seen = page.evaluate(READ)
        print(f"  {os_name:8s} {seen}")
        if seen != expected:
            failures.append(f"{os_name}: {seen}, stock {os_name} says {expected}")

    for failure in failures:
        print(f"FAIL: {failure}")
    if failures:
        return 1
    print("PASS: an unadjusted pointer lock without a gesture fails as on the claimed OS.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
