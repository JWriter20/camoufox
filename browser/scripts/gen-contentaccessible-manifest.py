#!/usr/bin/env python3
"""
Record which files a stock Firefox release exposes to web content.

Packages flagged `contentaccessible=yes` in chrome.manifest can be loaded as
subresources by any page, from any origin (caps/nsScriptSecurityManager.cpp
checks the target's flag and not the source), so any file Camoufox adds, drops,
renames or changes there is page-detectable. Each URL is recorded with the
SHA-256 of the bytes a page gets for it, after the manifests' overrides
(scripts/_chrome_view.py).

It also records the name of every chrome package and resource host the
manifests register, contentaccessible or not. Content processes receive all of
them, and a page can tell a registered name from an absent one:
new Worker("chrome://<name>/...") returns for the first and throws for the
second. hCaptcha probes chrome://juggler this way.

The set differs by OS, and a Camoufox identity may claim any of them, so there
is one record per OS:

    python3 scripts/gen-contentaccessible-manifest.py /path/to/stock/firefox

writes scripts/data/contentaccessible-manifest-<os>.json for the install's OS,
which tests/playwright/patches/contentaccessible-parity.py diffs a build
against under that claimed OS. Re-run it for the Windows and the Linux release
on a Firefox uplift; the guard refuses to compare across versions.
"""

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _chrome_view as cv  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA = REPO_ROOT / "scripts" / "data"
OSES = ("windows", "linux")


def record_path(os_name: str) -> Path:
    return DATA / f"contentaccessible-manifest-{os_name}.json"


def firefox_version(root: Path) -> str:
    ini = (root / "application.ini").read_text(errors="replace")
    match = re.search(r"^Version=(.+)$", ini, re.M)
    return match.group(1).strip() if match else "unknown"


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    root = Path(sys.argv[1]).resolve()
    if root.is_file():
        root = root.parent
    if not (root / "application.ini").exists():
        print(f"FAIL: {root} is not a Firefox install (no application.ini)")
        return 1
    os_name = cv.install_os(root)
    if os_name not in OSES:
        print(f"FAIL: {root} is a {os_name} build; records are kept for {', '.join(OSES)}")
        return 1

    files, registered = cv.page_view(root)
    record = {"firefox_version": firefox_version(root), "os": os_name, "files": files, "registered": registered}
    out = record_path(os_name)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=1, sort_keys=True) + "\n")
    print(f"Firefox {record['firefox_version']} ({os_name}): {len(files)} page-loadable files")
    print(f"wrote {out.relative_to(REPO_ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
