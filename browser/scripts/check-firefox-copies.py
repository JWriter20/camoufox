#!/usr/bin/env python3
"""Fail when a copy of Firefox source in this repo no longer matches the
Firefox release it builds on (browser/upstream.sh).

Two things are copied out of Firefox rather than patched in place, so a
Firefox bump changes the original without touching the copy, and nothing else
would notice:

- python/src/camoufox/font-prefs.json, which browser/scripts/gen-font-prefs.py
  reads out of modules/libpref/init/all.js. It must be byte-for-byte what the
  script writes from this release's all.js.
- the per-script fallback tables in browser/patches/fallback-fonts-claimed-os.patch:
  gfxWindowsPlatform::GetCommonFallbackFonts and
  gfxPlatformMac::GetCommonFallbackFonts, copied into gfxPlatformGtk.cpp. Each
  copy's body must be the release's, comments and whitespace aside, except
  for the additions the patch marks with a "// Camoufox:" comment, which must
  be exactly the ones listed in ADDITIONS.

The sources come from --tree (a Firefox source tree) or, without it, from the
release tag on hg.mozilla.org, then the GitHub mirror. Neither reachable is a
failure, not a pass.

    python3 browser/scripts/check-firefox-copies.py [--tree <firefox source>]
"""

import argparse
import importlib.util
import re
import sys
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PATCH = REPO / "browser" / "patches" / "fallback-fonts-claimed-os.patch"
FONT_PREFS = REPO / "python" / "src" / "camoufox" / "font-prefs.json"
SOURCES = {
    "all.js": "modules/libpref/init/all.js",
    "windows": "gfx/thebes/gfxWindowsPlatform.cpp",
    "mac": "gfx/thebes/gfxPlatformMac.cpp",
}
MIRRORS = (
    "https://hg.mozilla.org/releases/mozilla-release/raw-file/{tag}/{path}",
    "https://raw.githubusercontent.com/mozilla-firefox/firefox/{tag}/{path}",
)
# (copy, statement) the patch adds to stock's table, each under a
# "// Camoufox:" comment saying why. See docs/FONTS.md.
ADDITIONS = {
    ("windows", 'aFontList.AppendElement("Leelawadee UI");'),
    ("windows", 'aFontList.AppendElement("Gadugi");'),
}
# Where each table starts: the patch's copy, then the release's original.
COPIES = {
    "windows": ("static void CamouWindowsCommonFallbackFonts(", "void gfxWindowsPlatform::GetCommonFallbackFonts("),
    "mac": ("static void CamouMacCommonFallbackFonts(", "void gfxPlatformMac::GetCommonFallbackFonts("),
}


def release_tag():
    text = (REPO / "browser" / "upstream.sh").read_text(encoding="utf-8")
    version = re.search(r"^version=(\S+)", text, re.M).group(1)
    return version, "FIREFOX_" + version.replace(".", "_") + "_RELEASE"


def fetch(tree, tag, path):
    if tree:
        return (Path(tree) / path).read_text(encoding="utf-8")
    errors = []
    for url in MIRRORS:
        url = url.format(tag=tag, path=path)
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return r.read().decode("utf-8")
        except OSError as e:
            errors.append(f"{url}: {e}")
    sys.exit("FAIL: cannot read the Firefox source to compare against:\n  " + "\n  ".join(errors))


def body(lines, start):
    """The statements of the function whose signature begins with `start`."""
    i = next((n for n, line in enumerate(lines) if line.startswith(start)), None)
    if i is None:
        sys.exit(f"FAIL: no `{start}` -- the table moved; update check-firefox-copies.py")
    while not lines[i].rstrip().endswith("{"):
        i += 1
    out = []
    for line in lines[i + 1:]:
        if line.startswith("}"):
            return out
        out.append(line)
    sys.exit(f"FAIL: `{start}` never closes")


def statements(lines, copy=None, additions=None):
    """Code lines without comments or blank space; the copy's marked additions
    are taken out (into `additions`) rather than compared."""
    out, skip_next = [], False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("// Camoufox:"):
            skip_next = True
            continue
        code = re.sub(r"//.*", "", stripped).strip()
        if not code:
            continue
        if skip_next and additions is not None:
            additions.add((copy, code))
            skip_next = False
            continue
        out.append(re.sub(r"\s+", " ", code))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--tree", help="a Firefox source tree of the release in browser/upstream.sh")
    args = ap.parse_args()
    version, tag = release_tag()
    source = {key: fetch(args.tree, tag, path) for key, path in SOURCES.items()}
    failures = []

    spec = importlib.util.spec_from_file_location("gen_font_prefs", REPO / "browser" / "scripts" / "gen-font-prefs.py")
    gen = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gen)
    want, _ = gen.render(source["all.js"])
    if FONT_PREFS.read_text(encoding="utf-8") != want:
        failures.append(f"{FONT_PREFS.relative_to(REPO)} is not what gen-font-prefs.py writes from {version}'s all.js: "
                        "regenerate it with browser/scripts/gen-font-prefs.py <all.js>")
    else:
        print(f"ok  font-prefs.json is {version}'s all.js")

    patch = [line[1:] for line in PATCH.read_text(encoding="utf-8").splitlines() if line.startswith("+")]
    found = set()
    for copy, (ours, theirs) in COPIES.items():
        mine = statements(body(patch, ours), copy, found)
        stock = statements(body(source[copy].splitlines(), theirs))
        if mine != stock:
            first = next((n for n, (a, b) in enumerate(zip(mine, stock)) if a != b), min(len(mine), len(stock)))
            failures.append(
                f"the {copy} fallback table in {PATCH.name} differs from {version}'s {SOURCES[copy]} "
                f"at statement {first + 1}: copy {mine[first:first + 1]} vs stock {stock[first:first + 1]}. "
                "Re-copy it from the release (keep the // Camoufox: additions) and regenerate the patch.")
        else:
            print(f"ok  the {copy} fallback table is {version}'s ({len(stock)} statements)")
    if found != ADDITIONS:
        failures.append(f"{PATCH.name}'s marked additions are {sorted(found)}, not the documented {sorted(ADDITIONS)}")
    else:
        print(f"ok  {len(found)} documented additions")

    for f in failures:
        print("FAIL:", f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
