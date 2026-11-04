r"""
Camoufox must expose web content the same files a stock Firefox release does.

Twelve chrome/resource packages are flagged `contentaccessible=yes`. Any page,
from any origin, can load their files as subresources -- ALLOW_CHROME in
caps/nsScriptSecurityManager.cpp checks the target package's flag and never the
source -- so a file Camoufox adds, drops or changes there is a build tell:

    const s = document.createElement('script');
    s.onload = () => BUILD_IS_NOT_STOCK;
    s.src = 'chrome://browser/content/browser-development-helpers.js';

Every other registered chrome package and resource host is visible too: content
processes receive all of them, and the Worker constructor returns for a
registered name and throws for an absent one. hCaptcha's hsw.js runs exactly
this against chrome://juggler (daijro/camoufox#847):

    try { new Worker('chrome://juggler/content/content/WorkerMain.js'); BUILD_IS_NOT_STOCK; }
    catch (e) { /* SecurityError, as in stock Firefox */ }

The set differs by OS, and an identity may claim an OS its build was not made
for: Camoufox then serves pages the claimed OS's stock bytes
(chrome-overlay-claimed-os.patch, toolkit/camoufox-overlay). So the checks run
once per claimed OS, against scripts/data/contentaccessible-manifest-<os>.json
(recorded from that OS's stock release by scripts/gen-contentaccessible-manifest.py):
the bytes a page gets for every page-loadable URL, after the manifests' flags
and overrides as the chrome registry applies them for that claim
(scripts/_chrome_view.py), and the names every manifest registers. Then a page
actually loads a sample and constructs the Workers, so the static diff cannot
pass while the surface is really different (chrome-overlay-claimed-os.py
loads the OS-keyed files under each claim).

    python browser/tests/playwright/patches/contentaccessible-parity.py
"""

import importlib.util
import json
import os
import re
import subprocess
import sys
from pathlib import Path

# The live probe drives a real browser, and a wedged launch would otherwise hang
# the whole patch-guard run. Fail loudly instead.
LIVE_TIMEOUT_S = 180

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import resolve_binary  # noqa: E402

BROWSER_ROOT = Path(__file__).resolve().parents[3]

# The view is the recorder's, so the guard reads a build exactly the way the
# records were made.
sys.path.insert(0, str(BROWSER_ROOT / "scripts"))
import _chrome_view as chrome_view  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "gen_contentaccessible_manifest", BROWSER_ROOT / "scripts" / "gen-contentaccessible-manifest.py"
)
_generator = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_generator)
record_path = _generator.record_path
CLAIMED_OSES = _generator.OSES

# Differences a page can reach but that are not fixed here, with the reason.
# The guard fails when one matches stock again, so the list cannot outlive
# the difference.
KNOWN_DIFFERENCES = {
    "chrome://browser/content/aboutDialog.xhtml":
        "MOZ_UPDATER is off in Camoufox (the update deck); a build flag with a wide blast "
        "radius, left as is. The same on every OS, so the claimed-OS overlay does not cover it",
}

# Loaded from a page and asserted against stock Firefox 152.0.4. Each entry is
# (url, must_load). Regenerate by running the same probe against a stock build.
LIVE_PROBES = [
    ("chrome://global/content/aboutSupport.js", True),
    ("chrome://global/content/crashes.js", True),
    ("chrome://browser/content/browser-init.js", True),
    ("chrome://browser/content/browser-development-helpers.js", False),
    ("chrome://browser/content/does-not-exist-control.js", False),
]

# Constructed as Workers from a page and asserted against stock Firefox 156.0.1.
# Each entry is (url, constructor_returns): it returns for a registered package
# and throws a SecurityError for an absent one.
WORKER_PROBES = [
    ("chrome://juggler/content/content/WorkerMain.js", False),
    ("chrome://global/content/aboutSupport.js", True),
]

WORKER_PROBE_JS = """
([urls]) => {
  const out = {};
  for (const u of urls) {
    try { new Worker(u).terminate(); out[u] = true; } catch (e) { out[u] = false; }
  }
  return out;
}
"""

PROBE_JS = """
async ([urls]) => {
  const out = {};
  for (const u of urls) {
    out[u] = await new Promise(res => {
      const s = document.createElement('script');
      s.onload = () => res(true);
      s.onerror = () => res(false);
      s.src = u;
      document.head.appendChild(s);
      setTimeout(() => res(null), 5000);
    });
  }
  return out;
}
"""


def built_version(root: Path) -> str:
    ini = root / "application.ini"
    if not ini.exists():
        return "unknown"
    match = re.search(r"^Version=(.+)$", ini.read_text(errors="replace"), re.M)
    # Camoufox appends its own release to the Firefox version ("152.0.4-beta.30");
    # only the Firefox part decides whether the recorded file set still applies.
    return match.group(1).strip().split("-")[0] if match else "unknown"


def static_check(binary: Path, os_name: str, record: dict, failures: list) -> None:
    """The build's page-loadable files and registered names under a claim of
    `os_name`, against that OS's stock record. A packaged build is read from
    its omni.ja pair; `mach build` leaves dist/bin without them, with the same
    files loose at their jar paths (CI tests that unpackaged tree)."""
    root = binary.parent
    built = built_version(root)
    if built != record["firefox_version"]:
        failures.append(
            f"{os_name}: the record is of Firefox {record['firefox_version']} but this build is "
            f"{built} -- re-run scripts/gen-contentaccessible-manifest.py against the stock "
            f"{os_name} {built} release"
        )
        return
    if not (root / "omni.ja").exists() and not (root / "chrome").is_dir():
        failures.append(f"no omni.ja next to {binary}, and no loose chrome/ tree to read instead")
        return

    files, names = chrome_view.page_view(root, claimed_os=os_name)
    stock = record["files"]
    problems = [f"MISSING vs stock -- {u}" for u in sorted(stock.keys() - files.keys())]
    problems += [f"EXTRA vs stock -- {u}" for u in sorted(files.keys() - stock.keys())]
    for url in sorted(stock.keys() & files.keys()):
        differs = files[url] != stock[url]
        if differs and url not in KNOWN_DIFFERENCES:
            problems.append(f"CHANGED vs stock -- {url}")
        elif not differs and url in KNOWN_DIFFERENCES:
            problems.append(f"matches stock again, drop it from KNOWN_DIFFERENCES -- {url}")
    failures += [f"{os_name}: {p}" for p in problems]
    if not problems:
        print(f"  ok  {os_name:8} {len(files):>5} page-loadable files")

    for kind, stock_names in sorted(record["registered"].items()):
        extra = sorted(set(names.get(kind, [])) - set(stock_names))
        missing = sorted(set(stock_names) - set(names.get(kind, [])))
        failures += [f"{os_name}: registered {kind}: EXTRA vs stock -- {n} (pages can detect it)" for n in extra]
        failures += [f"{os_name}: registered {kind}: MISSING vs stock -- {n}" for n in missing]
        if not extra and not missing:
            print(f"  ok  {os_name:8} registered {kind:14} {len(names.get(kind, [])):>5} names")


PROBE_MARK = "PROBE-RESULT "


def run_probe(binary: Path) -> dict:
    """The probe itself, run in a child process (see live_check)."""
    from camoufox.sync_api import Camoufox

    urls = [u for u, _ in LIVE_PROBES]
    # i_know_what_im_doing is what makes executable_path stick; without it the
    # launcher falls back to the downloaded official build and this guard would
    # silently measure a different browser.
    # The caller's environment: an env= replaces it whole, and a Pro build
    # needs the caller's lease (CAMOU_LEASE_FILE) or the launcher asks the Pro
    # API for one.
    with Camoufox(
        headless=True, executable_path=str(binary), env=dict(os.environ), i_know_what_im_doing=True
    ) as browser:
        page = browser.new_page()
        page.goto("about:blank")
        return {
            "loads": page.evaluate(PROBE_JS, [urls]),
            "workers": page.evaluate(WORKER_PROBE_JS, [[u for u, _ in WORKER_PROBES]]),
        }


def live_check(binary: Path, failures: list) -> None:
    """Drive a real browser, in a CHILD PROCESS under a timeout.

    Playwright's sync API blocks without servicing Python signals, so an
    in-process alarm cannot bound a wedged launch -- only killing the process
    can, and an unbounded guard would hang the whole patch-guard run.
    """
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).resolve()), "--probe", str(binary)],
        capture_output=True, text=True, timeout=LIVE_TIMEOUT_S,
    )
    if proc.returncode != 0:
        failures.append(f"live probe failed: {proc.stderr.strip().splitlines()[-1:] or proc.stdout}")
        return
    # The launcher may print to stdout first -- on a fresh machine the first
    # launch downloads the default addons and reports progress -- so read the
    # marked result line, not the whole stream.
    marked = [line for line in proc.stdout.splitlines() if line.startswith(PROBE_MARK)]
    if not marked:
        failures.append(f"live probe printed no result line: {proc.stdout.strip()[-300:]!r}")
        return
    results = json.loads(marked[-1][len(PROBE_MARK):])

    for url, returns in WORKER_PROBES:
        got = results["workers"].get(url)
        if got != returns:
            verb = "throws but stock returns a Worker" if returns else "returns a Worker but stock throws"
            failures.append(f"live: new Worker({url!r}) {verb}")
        else:
            print(f"  ok  {'returns' if returns else 'throws':7} new Worker({url})")

    for url, must_load in LIVE_PROBES:
        got = results["loads"].get(url)
        if got is None:
            failures.append(f"live: {url} timed out")
        elif got != must_load:
            verb = "did not load but stock loads it" if must_load else "loaded but stock does not ship it"
            failures.append(f"live: {url} {verb}")
        else:
            print(f"  ok  {'loads' if must_load else 'absent':7} {url}")


def main() -> int:
    records = {}
    for os_name in CLAIMED_OSES:
        path = record_path(os_name)
        if not path.exists():
            print(f"FAIL: {path.relative_to(BROWSER_ROOT)} is missing; generate it first")
            return 1
        records[os_name] = json.loads(path.read_text())
    binary = resolve_binary()
    failures = []

    for os_name, record in records.items():
        print(f"Stock baseline ({os_name}): Firefox {record['firefox_version']}, "
              f"{len(record['files'])} page-loadable files")

    print("\nPage-loadable files, per claimed OS:")
    for os_name, record in records.items():
        static_check(binary, os_name, record, failures)
    print("\nLoaded from a page:")
    try:
        live_check(binary, failures)
    except subprocess.TimeoutExpired:
        failures.append(f"live probe did not finish within {LIVE_TIMEOUT_S}s (wedged launch?)")
    except Exception as exc:  # noqa: BLE001
        failures.append(f"live probe could not run: {exc}")

    print()
    if failures:
        for failure in failures:
            print(f"FAIL: {failure}")
        return 1
    print("PASS: the content-reachable surface matches a stock Firefox release under every claimed OS.")
    return 0


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--probe":
        print(PROBE_MARK + json.dumps(run_probe(Path(sys.argv[2]))))
        sys.exit(0)
    sys.exit(main())
