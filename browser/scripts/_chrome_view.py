"""
What a web page can load from an install's chrome:// and resource:// URLs.

Packages flagged `contentaccessible=yes` can be loaded by any page as a script,
stylesheet or image (caps/nsScriptSecurityManager.cpp checks the target
package's flag, never the source), so the bytes behind every such URL are part
of what a page sees. This module works out those bytes the way the chrome
registry resolves them:

- manifest lines are filtered by their flags: `os=` against the build's own OS
  (xpcom/components/ManifestParser.cpp, `likeunix` matching Linux), and
  Camoufox's `camouos=` against the OS the identity claims;
- the last registration of a package or resource host wins;
- `override` lines redirect a URL, and Camoufox's `camoucontent` overrides do so
  only for loads a page triggers (chrome/nsChromeRegistry.cpp
  ConvertChromeURLForLoad), checked before the regular ones;
- an override whose target does not exist makes the URL absent.

Used by scripts/gen-os-overlay.py (what stock serves each OS),
scripts/gen-contentaccessible-manifest.py (the stock record) and
tests/playwright/patches/contentaccessible-parity.py (what a build serves).
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Tuple

# Launcher / harness OS names -> the value a manifest's os= and camouos= flags
# compare against (nsIXULRuntime.OS, lowercased).
MANIFEST_OS = {"windows": "winnt", "linux": "linux", "macos": "darwin"}

_FLAG = re.compile(r"^([a-z]+)(!=|=)(.*)$")


def zip_entries(jar: Path) -> Dict[str, int]:
    """Map member -> size. Uses unzip, not zipfile: Firefox's optimized jars
    have the central directory at the front and zipfile cannot open them."""
    out = subprocess.run(["unzip", "-l", str(jar)], capture_output=True, text=True).stdout
    entries = {}
    for line in out.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4 and parts[0].isdigit() and not parts[3].endswith("/"):
            entries[parts[3]] = int(parts[0])
    return entries


def extract_jar(jar: Path, dest: Path) -> None:
    """Every member of `jar` under `dest`, checked complete: unzip exits
    non-zero on an optimized jar's front-loaded central directory while
    extracting everything, so its status says nothing."""
    dest.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(["unzip", "-qo", str(jar), "-d", str(dest)], capture_output=True, text=True)
    listed = set(zip_entries(jar))
    got = {f.relative_to(dest).as_posix() for f in dest.rglob("*") if f.is_file()}
    if not listed or not listed <= got:
        raise RuntimeError(f"unzip {jar} extracted {len(got & listed)} of {len(listed)} files: "
                           f"{proc.stderr.strip()[-300:]}")


def install_os(root: Path) -> str:
    """'windows', 'macos' or 'linux': the OS a Firefox install was built for."""
    if (root / "firefox.exe").exists() or (root / "camoufox.exe").exists() or (root / "xul.dll").exists():
        return "windows"
    if root.name == "Resources" or (root / "XUL").exists() or (root.parent / "MacOS").is_dir():
        return "macos"
    return "linux"


@contextmanager
def chrome_roots(install: Path) -> Iterator[Tuple[Path, Path]]:
    """(gre root, app root) holding the install's manifests and chrome files.

    A packaged install has them in omni.ja and browser/omni.ja, extracted here
    to a temporary directory. An unpackaged `mach build` dist/bin has them
    loose at the same paths, under dist/bin/ and dist/bin/browser/.
    """
    gre_jar, app_jar = install / "omni.ja", install / "browser" / "omni.ja"
    if not gre_jar.exists():
        yield install, install / "browser"
        return
    tmp = Path(tempfile.mkdtemp(prefix="chrome-view-"))
    try:
        extract_jar(gre_jar, tmp / "gre")
        if app_jar.exists():
            extract_jar(app_jar, tmp / "app")
        else:
            (tmp / "app").mkdir()
        yield tmp / "gre", tmp / "app"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@dataclass
class Line:
    kind: str
    args: List[str]
    flags: List[str]
    base: Path  # the directory of the manifest the line is in


def _manifests(root: Path, exclude: Optional[Path]) -> List[Path]:
    out = []
    for m in sorted(root.rglob("*.manifest")):
        if exclude is not None and exclude in m.parents:
            continue
        out.append(m)
    return out


_ARGC = {"content": 2, "resource": 2, "override": 2, "locale": 3, "skin": 3}


def manifest_lines(gre: Path, app: Path) -> List[Line]:
    """Every registration line, GRE manifests first as Gecko reads them."""
    out = []
    # In an unpackaged dist/bin the app root sits inside the GRE root.
    for root, exclude in ((gre, app if app != gre else None), (app, None)):
        if root == app and app == gre:
            break
        if not root.is_dir():
            continue
        for manifest in _manifests(root, exclude):
            for raw in manifest.read_text(encoding="utf-8", errors="replace").splitlines():
                parts = raw.split()
                if not parts or parts[0].startswith("#") or parts[0] not in _ARGC:
                    continue
                n = _ARGC[parts[0]]
                if len(parts) < n + 1:
                    continue
                out.append(Line(parts[0], parts[1:n + 1], parts[n + 1:], manifest.parent))
    return out


def applies(flags: List[str], host_os: str, claimed_os: str) -> bool:
    """Whether a line with these flags registers, as ManifestParser decides it
    in the parent process. `host_os`/`claimed_os` are launcher names."""
    host, claimed = MANIFEST_OS[host_os], MANIFEST_OS[claimed_os]
    states: Dict[str, bool] = {}
    for flag in flags:
        m = _FLAG.match(flag.lower())
        if not m:
            continue
        name, op, value = m.groups()
        if name == "os":
            hit = value == host or (value == "likeunix" and host == "linux")
        elif name == "camouos":
            hit = value == claimed
        elif name == "process":
            hit = value == "main"
        else:
            continue
        if not states.get(name):
            states[name] = hit if op == "=" else not hit
    return all(states.values())


def _content_accessible(flags: List[str]) -> bool:
    vals = [f.split("=", 1)[1].lower() for f in flags if f.lower().startswith("contentaccessible=")]
    vals += ["yes" for f in flags if f.lower() == "contentaccessible"]
    return bool(vals) and vals[-1] not in ("no", "false", "0")


def _content_only(flags: List[str]) -> bool:
    for f in flags:
        f = f.lower()
        if f == "camoucontent" or f in ("camoucontent=yes", "camoucontent=true", "camoucontent=1"):
            return True
    return False


@dataclass
class Registry:
    """The chrome registry a process ends up with for one claimed OS."""

    gre: Path
    app: Path
    packages: Dict[str, Tuple[Path, bool]] = field(default_factory=dict)   # name -> (content dir, accessible)
    resources: Dict[str, Tuple[str, bool]] = field(default_factory=dict)   # host -> (target, accessible)
    overrides: Dict[str, str] = field(default_factory=dict)
    content_overrides: Dict[str, str] = field(default_factory=dict)
    names: Dict[str, set] = field(default_factory=lambda: {k: set() for k in ("content", "locale", "skin", "resource")})

    # -- resolution ----------------------------------------------------------

    def _target(self, base: Path, target: str) -> str:
        """A manifest target as a URL or an absolute path string."""
        if "://" in target or target.startswith("jar:"):
            return target
        return str((base / target).resolve())

    def resolve(self, url: str, content: bool, depth: int = 0) -> Optional[Path]:
        """The file `url` loads for a page (`content`) or the browser, or None."""
        if depth > 8:
            return None
        if content and url in self.content_overrides:
            return self._resolve_target(self.content_overrides[url], content, depth)
        if url.startswith("chrome://") and url in self.overrides:
            return self._resolve_target(self.overrides[url], content, depth)
        m = re.match(r"^chrome://([^/]+)/content/(.*)$", url)
        if m:
            pkg = self.packages.get(m.group(1).lower())
            return pkg[0] / m.group(2) if pkg else None
        m = re.match(r"^resource://([^/]+)/(.*)$", url)
        if m:
            host, rest = m.group(1).lower(), m.group(2)
            if host == "gre":
                return self.gre / rest
            if host in ("app", ""):
                return self.app / rest
            res = self.resources.get(host)
            if not res:
                return None
            return self._resolve_target(res[0].rstrip("/") + "/" + rest, content, depth)
        return None

    def _resolve_target(self, target: str, content: bool, depth: int) -> Optional[Path]:
        if target.startswith(("chrome://", "resource://")):
            return self.resolve(target, content, depth + 1)
        if "://" in target or target.startswith("jar:"):
            return None
        return Path(target)

    # -- the page's view -----------------------------------------------------

    def accessible(self, url: str) -> bool:
        m = re.match(r"^chrome://([^/]+)/content/", url)
        if m:
            pkg = self.packages.get(m.group(1).lower())
            return bool(pkg and pkg[1])
        m = re.match(r"^resource://([^/]+)/", url)
        if m:
            res = self.resources.get(m.group(1).lower())
            return bool(res and res[1])
        return False

    def page_files(self) -> Dict[str, Path]:
        """{url: file} for every URL a page can load, as a page loads it."""
        urls = set()
        for name, (d, ok) in self.packages.items():
            if ok and d.is_dir():
                urls |= {f"chrome://{name}/content/{f.relative_to(d).as_posix()}" for f in d.rglob("*") if f.is_file()}
        for host, (target, ok) in self.resources.items():
            if not ok:
                continue
            d = self.resolve(f"resource://{host}/", True)
            if d is not None and d.is_dir():
                urls |= {f"resource://{host}/{f.relative_to(d).as_posix()}" for f in d.rglob("*") if f.is_file()}
        urls |= {u for u in (*self.overrides, *self.content_overrides) if self.accessible(u)}
        out = {}
        for url in sorted(urls):
            path = self.resolve(url, content=True)
            if path is not None and path.is_file():
                out[url] = path
        return out


def registry(gre: Path, app: Path, host_os: str, claimed_os: str) -> Registry:
    reg = Registry(gre, app)
    for line in manifest_lines(gre, app):
        if not applies(line.flags, host_os, claimed_os):
            continue
        name = line.args[0].lower()
        if line.kind in ("locale", "skin"):
            reg.names[line.kind].add(name)
        elif line.kind == "content":
            reg.names["content"].add(name)
            reg.packages[name] = (Path(reg._target(line.base, line.args[1])), _content_accessible(line.flags))
        elif line.kind == "resource":
            reg.names["resource"].add(name)
            reg.resources[name] = (reg._target(line.base, line.args[1]), _content_accessible(line.flags))
        elif line.kind == "override":
            table = reg.content_overrides if _content_only(line.flags) else reg.overrides
            table[line.args[0]] = reg._target(line.base, line.args[1])
    return reg


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def packaged_sha256(path: Path) -> str:
    """The SHA-256 of a loose dist/bin file as the packager writes it into
    omni.ja: it strips the `#` comment lines of .ftl and .properties files
    (mozpack's MinifiedCommentStripped), which an unpackaged tree still carries."""
    data = path.read_bytes()
    if path.suffix in (".ftl", ".properties"):
        data = b"".join(l for l in data.splitlines(keepends=True) if not l.startswith(b"#"))
    return hashlib.sha256(data).hexdigest()


def page_view(install: Path, claimed_os: Optional[str] = None) -> Tuple[Dict[str, str], Dict[str, List[str]]]:
    """({url: sha256} a page can load, {kind: registered names}) for an install
    launched with an identity claiming `claimed_os` (default: its own OS)."""
    host = install_os(install)
    digest = sha256 if (install / "omni.ja").exists() else packaged_sha256
    with chrome_roots(install) as (gre, app):
        reg = registry(gre, app, host, claimed_os or host)
        files = {url: digest(path) for url, path in reg.page_files().items()}
        names = {kind: sorted(v) for kind, v in reg.names.items()}
    return files, names
