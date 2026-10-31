#!/usr/bin/env python3
"""
Record the bytes stock Firefox serves web pages on each OS, for the claimed-OS overlay.

A page can load any file of a `contentaccessible=yes` chrome package, so the
files a build ships there say which OS built it: Windows ships
aboutThirdParty.*, aboutWindowsMessages.*, printPageSetup.*, four migration
brand PNGs and a default-agent image; Linux ships aboutWebauthn.*; and
browser.xhtml, places.xhtml, license.html, buildconfig.html and others are
preprocessed differently per OS. Windows also registers the gfxsanity package
and resource host, which a page detects by name (new Worker() returns for a
registered name and throws for an absent one).

A Camoufox identity claims an OS its build was not made for, so Camoufox serves
pages the claimed OS's stock bytes instead. This script writes them, per
Firefox version, as toolkit/camoufox-overlay in the source tree (from
browser/additions/toolkit/camoufox-overlay):

    override <url> %camoufox-overlay/<os>/<file> camouos=<claimed> camoucontent

`camouos=` registers a line only when the identity claims that OS
(chrome-overlay-claimed-os.patch, ManifestParser.cpp); `camoucontent` makes
the override apply only to loads a page (a web, null or moz-extension
principal) triggers, so the browser's own UI keeps the build's real files. A
URL one OS does not ship is overridden to a path that does not exist, which
fails exactly as a missing file does.

It also serves the stock bytes, for every claim, of the content-accessible
files Camoufox itself patches (STOCK_FOR_PAGES).

    python3 scripts/gen-os-overlay.py --windows <stock Windows Firefox dir> \\
        --linux <stock Linux Firefox dir>

Both installs must be the Firefox version upstream.sh pins. The autoupdate
pipeline runs this on every uplift with the stock archives it already
downloads; the build refuses an overlay made for another version
(scripts/patch.py and the overlay's moz.build).

Not covered, on purpose:
- MOZ_UPDATER: aboutDialog.xhtml's update deck is the same on both OSes, so
  it is not OS-keyed and stays Camoufox's (a build flag with a wide blast
  radius). The OS-keyed files that also carry MOZ_UPDATER content
  (aboutSupport.xhtml, license.html, preferences.xhtml) are served whole, as
  stock ships them.
- macOS: no stock macOS set is recorded; a macOS claim keeps the build's files.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path
from typing import Dict, List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _chrome_view as cv  # noqa: E402

BROWSER_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = BROWSER_ROOT / "additions" / "toolkit" / "camoufox-overlay"
OSES = ("windows", "linux")
ABSENT = "%camoufox-overlay/absent"

# Content-accessible files Camoufox patches, served to pages as stock ships
# them on every OS. Each must be identical on both stock OSes.
STOCK_FOR_PAGES = {
    "chrome://browser/content/parent/ext-browser.js": "disable-extension-newtab.patch",
    "chrome://browser/content/parent/ext-tabs.js": "disable-extension-newtab.patch",
}

# A content-only override is safe only where no cache can hand the page's
# bytes to the browser's UI or back. Images, scripts and stylesheets are
# cached per process, and chrome:// stylesheets by URL alone
# (layout/style/Loader.cpp SheetLoadDataHashKey::KeyEquals), so an overlaid
# subresource that the UI also loads would leak one side's bytes to the other.
# Markup (.html/.xhtml) only ever runs as a document, which web content cannot
# load from chrome:// (CheckLoadURI passes ALLOW_CHROME for subresources only)
# and the XUL prototype cache holds system-principal documents only. A file
# one OS does not ship is never loaded by the UI of a build for that OS. The
# ones below are loaded by the parent process only, where no page runs.
CACHE_SAFE_SUBRESOURCES = {
    "chrome://browser/content/parent/ext-browser.js": "an extension API script the parent loads",
    "chrome://browser/content/parent/ext-tabs.js": "an extension API script the parent loads",
}
MARKUP = (".html", ".xhtml")

# jar.mn entry paths: what mozbuild's jar.mn grammar accepts (python/mozbuild/mozbuild/jar.py).
_JAR_PATH = re.compile(r"^[\w\d.\-_/+@]+$")


def fail(msg: str) -> None:
    print(f"FAIL: {msg}", file=sys.stderr)
    sys.exit(1)


def firefox_version(root: Path) -> str:
    ini = (root / "application.ini").read_text(errors="replace")
    m = re.search(r"^Version=(.+)$", ini, re.M)
    return m.group(1).strip() if m else "unknown"


def pinned_version() -> str:
    text = (BROWSER_ROOT / "upstream.sh").read_text()
    m = re.search(r"^version=(\S+)$", text, re.M)
    return m.group(1) if m else "unknown"


def url_path(url: str) -> str:
    """chrome://global/content/a.js -> chrome/global/content/a.js."""
    scheme, rest = url.split("://", 1)
    return f"{scheme}/{rest}"


class Stock:
    def __init__(self, os_name: str, install: Path, tmp_ctx) -> None:
        self.os = os_name
        self.install = install
        self.gre, self.app = tmp_ctx
        self.reg = cv.registry(self.gre, self.app, os_name, os_name)
        self.files = self.reg.page_files()
        self.hashes = {u: cv.sha256(p) for u, p in self.files.items()}

    def registered(self) -> Dict[str, set]:
        return self.reg.names


def registration_lines(stock: Stock, kind: str, name: str) -> List[dict]:
    """The stock manifest lines registering `name` as `kind`, with their directory."""
    out = []
    for line in cv.manifest_lines(stock.gre, stock.app):
        if line.kind != kind or line.args[0].lower() != name or not cv.applies(line.flags, stock.os, stock.os):
            continue
        target = (line.base / line.args[1]).resolve()
        if not target.is_dir():
            fail(f"{stock.os}: {kind} {name} -> {line.args[1]} is not a directory in the stock jars")
        flags = [f for f in line.flags if not f.lower().startswith(("os=", "os!=", "camouos"))]
        out.append({"dir": target, "flags": flags})
    return out


def build(win: Stock, lin: Stock, out: Path, version: str, sources: Dict[str, dict]) -> dict:
    stocks = {"windows": win, "linux": lin}
    urls = sorted(set(win.files) | set(lin.files))

    entries: Dict[str, Dict[str, Optional[str]]] = {}
    for url in urls:
        hw, hl = win.hashes.get(url), lin.hashes.get(url)
        if hw == hl:
            continue
        if not url.startswith("chrome://"):
            fail(f"{url} differs between stock Windows and Linux, and overrides only redirect chrome:// "
                 f"URLs; the overlay cannot serve it (extend chrome-overlay-claimed-os.patch)")
        entries[url] = {"windows": hw and f"windows/{url_path(url)}", "linux": hl and f"linux/{url_path(url)}"}

    common: Dict[str, str] = {}
    for url, why in STOCK_FOR_PAGES.items():
        if url in entries:
            continue  # OS-keyed already: each claim gets its own stock copy
        if url not in win.hashes or url not in lin.hashes:
            fail(f"{url} ({why}) is no longer shipped by stock Firefox {version}; drop it from STOCK_FOR_PAGES")
        common[url] = f"stock/{url_path(url)}"

    # The cache invariant (see CACHE_SAFE_SUBRESOURCES).
    for url, per_os in entries.items():
        if url.endswith(MARKUP) or url in CACHE_SAFE_SUBRESOURCES:
            continue
        if all(per_os.values()):
            fail(f"{url} is a subresource both OSes ship with different bytes; a content-only override of it "
                 f"could be served from a per-process cache to the browser's own UI. Check the cache keying "
                 f"for its type, then list it in CACHE_SAFE_SUBRESOURCES with the reason")
    for url in common:
        if not url.endswith(MARKUP) and url not in CACHE_SAFE_SUBRESOURCES:
            fail(f"{url}: STOCK_FOR_PAGES subresource without a CACHE_SAFE_SUBRESOURCES reason")

    # Names one OS registers and the other does not.
    regs = []
    wn, ln = win.registered(), lin.registered()
    for kind in ("content", "resource", "locale", "skin"):
        only_linux = sorted(ln[kind] - wn[kind])
        if only_linux:
            fail(f"stock Linux registers {kind} {only_linux}, Windows does not; the overlay can add a "
                 f"registration for a claim but not remove a Linux build's own (extend the patch)")
        for name in sorted(wn[kind] - ln[kind]):
            if kind not in ("content", "resource"):
                fail(f"stock Windows registers {kind} {name}; only content and resource registrations "
                     f"are overlaid")
            for i, line in enumerate(registration_lines(win, kind, name)):
                regs.append({"kind": kind, "name": name, "os": "windows",
                             "dir": f"windows/register/{kind}/{name}" + (f"-{i}" if i else ""),
                             "src": line["dir"], "flags": line["flags"]})

    # Write the tree.
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    jar_files: Dict[str, str] = {}  # path inside camoufox-overlay -> sha256
    by_hash: Dict[str, str] = {}     # sha256 -> the path holding those bytes

    def copy(src: Path, rel: str, dedupe: bool = True) -> str:
        """Copy `src` to `rel`, or return the path already holding its bytes
        (chrome://global/content/license.html serves browser's license.html)."""
        digest = cv.sha256(src)
        if dedupe and digest in by_hash:
            return by_hash[digest]
        if not _JAR_PATH.match(rel):
            fail(f"{rel}: a path jar.mn cannot express")
        dst = out / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        jar_files[rel] = digest
        by_hash.setdefault(digest, rel)
        return rel

    for url, per_os in entries.items():
        for os_name, rel in per_os.items():
            if rel:
                per_os[os_name] = copy(stocks[os_name].files[url], rel)
    for url, rel in common.items():
        common[url] = copy(win.files[url], rel)
    for r in regs:
        for f in sorted(p for p in r["src"].rglob("*") if p.is_file()):
            # A registered directory is served whole, at its own paths.
            copy(f, f"{r['dir']}/{f.relative_to(r['src']).as_posix()}", dedupe=False)

    lines = [
        f"# Generated by browser/scripts/gen-os-overlay.py from stock Firefox {version}. Do not edit;",
        "# re-run the script on a Firefox uplift (the autoupdate pipeline does).",
        "",
        "toolkit.jar:",
    ]
    for url, per_os in entries.items():
        for os_name in OSES:
            rel = per_os[os_name]
            target = f"%camoufox-overlay/{rel}" if rel else ABSENT
            lines.append(f"% override {url} {target} camouos={cv.MANIFEST_OS[os_name]} camoucontent")
    for url, rel in common.items():
        lines.append(f"% override {url} %camoufox-overlay/{rel} camoucontent")
    for r in regs:
        flags = " ".join([*r["flags"], f"camouos={cv.MANIFEST_OS[r['os']]}"])
        lines.append(f"% {r['kind']} {r['name']} %camoufox-overlay/{r['dir']}/ {flags}")
    for rel in sorted(jar_files):
        lines.append(f"  camoufox-overlay/{rel} ({rel})")
    (out / "jar.mn").write_text("\n".join(lines) + "\n")

    (out / "moz.build").write_text(
        "# Generated by browser/scripts/gen-os-overlay.py. Do not edit.\n"
        "#\n"
        "# The stock bytes each claimed OS serves pages, for one Firefox version. A\n"
        "# build of another version would serve a stale OS's files, so it stops here.\n\n"
        f'if CONFIG["MOZ_APP_VERSION"].split("-")[0] != "{version}":\n'
        f'    error("toolkit/camoufox-overlay was generated from stock Firefox {version}, but this is "\n'
        '          "Firefox %s: run browser/scripts/gen-os-overlay.py against the stock Windows "\n'
        '          "and Linux releases of this version" % CONFIG["MOZ_APP_VERSION"].split("-")[0])\n\n'
        'JAR_MANIFESTS += ["jar.mn"]\n'
    )

    record = {
        "firefox_version": version,
        "stock": sources,
        "entries": entries,
        "stock_for_pages": common,
        "registrations": [{k: v for k, v in r.items() if k != "src"} for r in regs],
        "files": jar_files,
    }
    (out / "overlay.json").write_text(json.dumps(record, indent=1, sort_keys=True) + "\n")
    return record


def jar_digest(install: Path) -> Dict[str, str]:
    out = {}
    for rel in ("omni.ja", "browser/omni.ja"):
        p = install / rel
        if p.exists():
            out[rel] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--windows", type=Path, required=True, help="stock Windows Firefox install (the dir of firefox.exe)")
    ap.add_argument("--linux", type=Path, required=True, help="stock Linux Firefox install (the dir of firefox)")
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT)
    ap.add_argument("--version", help="the Firefox version to require (default: upstream.sh's)")
    args = ap.parse_args(argv)

    want = args.version or pinned_version()
    installs = {"windows": args.windows.resolve(), "linux": args.linux.resolve()}
    for os_name, root in installs.items():
        if root.is_file():
            installs[os_name] = root = root.parent
        if not (root / "application.ini").exists():
            fail(f"{root} is not a Firefox install (no application.ini)")
        got_os = cv.install_os(root)
        if got_os != os_name:
            fail(f"--{os_name} {root} is a {got_os} build")
        got = firefox_version(root)
        if got != want:
            fail(f"--{os_name} {root} is Firefox {got}, not {want}")

    with cv.chrome_roots(installs["windows"]) as wroots, cv.chrome_roots(installs["linux"]) as lroots:
        win, lin = Stock("windows", installs["windows"], wroots), Stock("linux", installs["linux"], lroots)
        sources = {os_name: jar_digest(root) for os_name, root in installs.items()}
        rec = build(win, lin, args.out, want, sources)

    n_keyed = len(rec["entries"])
    print(f"Firefox {want}: {n_keyed} OS-keyed URLs, {len(rec['stock_for_pages'])} stock-for-pages, "
          f"{len(rec['registrations'])} registrations, {len(rec['files'])} files")
    try:
        shown = args.out.resolve().relative_to(BROWSER_ROOT.parent)
    except ValueError:
        shown = args.out
    print(f"wrote {shown}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
