"""What fonts are actually installed on THIS machine.

Camoufox ships its own font bundle and spoofs the claimed OS's font list from
it, so nothing in a normal launch depends on the host's fonts. This module is
the diagnostic half: it answers "what does this machine really have", which is
what a user needs to know when deciding whether a claimed identity is plausible
here, and what `camoufox fonts` reports.

Enumeration is per platform because the authoritative list is:

    Linux     fontconfig (`fc-list`), which is what Gecko itself asks
    Windows   the font registry, plus the per-user font directory that
              Windows 10 1809 and later install into without touching HKLM
    macOS     the three font directories CoreText reads

The result is cached under the Camoufox cache directory and invalidated by the
(path, mtime, size) of every directory scanned, so a call after the first costs
a handful of stats.
"""

import json
import os
import platform
import subprocess
import sys
import time
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

CACHE_VERSION = 1
FONT_EXT = (".ttf", ".otf", ".ttc", ".otc", ".dfont", ".pfb")

# fc-list can be slow on a machine with thousands of fonts, and a launch should
# never hang on it.
PROBE_TIMEOUT_S = 20


def host_os() -> str:
    """'linux', 'macos' or 'windows' -- the keys the font data is filed under."""
    return {"Linux": "linux", "Darwin": "macos", "Windows": "windows"}.get(
        platform.system(), "linux")


def normalise(name: str) -> str:
    """The form two family names are compared in.

    Case and surrounding space only. Nothing clever: "Segoe UI" and
    "Segoe UI Semibold" are different families to DirectWrite and must stay
    different here, or a host with one would be credited with the other.
    """
    return " ".join(name.split()).lower()


# --------------------------------------------------------------------------
# per-platform enumeration
# --------------------------------------------------------------------------

def _linux_font_dirs() -> List[str]:
    dirs = ["/usr/share/fonts", "/usr/local/share/fonts",
            os.path.expanduser("~/.local/share/fonts"),
            os.path.expanduser("~/.fonts")]
    return [d for d in dirs if os.path.isdir(d)]


def _windows_font_dirs() -> List[str]:
    dirs = [os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts")]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        dirs.append(os.path.join(local, "Microsoft", "Windows", "Fonts"))
    return [d for d in dirs if os.path.isdir(d)]


def _macos_font_dirs() -> List[str]:
    dirs = ["/System/Library/Fonts", "/Library/Fonts",
            os.path.expanduser("~/Library/Fonts"),
            "/System/Library/Fonts/Supplemental"]
    return [d for d in dirs if os.path.isdir(d)]


def font_dirs() -> List[str]:
    return {"linux": _linux_font_dirs, "windows": _windows_font_dirs,
            "macos": _macos_font_dirs}[host_os()]()


def _from_fc_list() -> Optional[Set[str]]:
    """Ask fontconfig, which is the same source Gecko uses on Linux.

    Deliberately run with the caller's own environment stripped of Camoufox's
    FONTCONFIG_FILE: that variable points at the bundle, and the question here
    is what the HOST has.
    """
    env = dict(os.environ)
    env.pop("FONTCONFIG_FILE", None)
    env.pop("FONTCONFIG_PATH", None)
    try:
        out = subprocess.run(["fc-list", "--format", "%{family}\\n"],
                             capture_output=True, timeout=PROBE_TIMEOUT_S,
                             env=env)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    families: Set[str] = set()
    for line in out.stdout.decode("utf-8", "replace").splitlines():
        # fontconfig prints every alias of a family, comma separated.
        for name in line.split(","):
            name = name.strip()
            if name:
                families.add(name)
    return families or None


def _from_windows_registry() -> Optional[Set[str]]:
    """The font registry: what GDI and DirectWrite enumerate."""
    try:
        import winreg                                     # noqa: WPS433
    except ImportError:
        return None
    families: Set[str] = set()
    keys = [(winreg.HKEY_LOCAL_MACHINE,
             r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts"),
            (winreg.HKEY_CURRENT_USER,
             r"SOFTWARE\Microsoft\Windows NT\CurrentVersion\Fonts")]
    for root, path in keys:
        try:
            handle = winreg.OpenKey(root, path)
        except OSError:
            continue
        try:
            index = 0
            while True:
                try:
                    name, _value, _kind = winreg.EnumValue(handle, index)
                except OSError:
                    break
                index += 1
                # Values read "Segoe UI Bold (TrueType)" or
                # "MS Gothic & MS PGothic & MS UI Gothic (TrueType)".
                for part in name.split("(")[0].split("&"):
                    part = part.strip()
                    if part:
                        families.add(part)
        finally:
            winreg.CloseKey(handle)
    return families or None


def _from_font_files(dirs: Iterable[str], limit: int = 0) -> Set[str]:
    """Read the name table of every font file. The portable fallback."""
    try:
        from fontTools.ttLib import TTCollection, TTFont     # noqa: WPS433
    except ImportError:
        return set()

    families: Set[str] = set()
    seen = 0
    for directory in dirs:
        for root, _subdirs, names in os.walk(directory):
            for name in sorted(names):
                if not name.lower().endswith(FONT_EXT):
                    continue
                path = os.path.join(root, name)
                seen += 1
                if limit and seen > limit:
                    return families
                try:
                    if name.lower().endswith((".ttc", ".otc")):
                        with TTCollection(path, lazy=True) as collection:
                            fonts = list(collection.fonts)
                    else:
                        fonts = [TTFont(path, lazy=True,
                                        ignoreDecompileErrors=True)]
                except Exception:
                    continue
                for font in fonts:
                    try:
                        table = font["name"] if "name" in font else None
                        if table is None:
                            continue
                        for name_id in (16, 1):
                            value = table.getDebugName(name_id)
                            if value:
                                families.add(value.strip())
                    except Exception:
                        pass
                    finally:
                        try:
                            font.close()
                        except Exception:
                            pass
    return families


# --------------------------------------------------------------------------
# the cached inventory
# --------------------------------------------------------------------------

def _signature(dirs: Iterable[str]) -> str:
    """Cheap invalidation: the directories and their mtimes.

    Installing or removing a font changes the mtime of the directory it is in,
    which is enough -- and costs four stats instead of reading 3000 name
    tables.
    """
    parts = []
    for directory in sorted(dirs):
        try:
            stat = os.stat(directory)
            parts.append("%s:%d:%d" % (directory, int(stat.st_mtime), stat.st_size))
        except OSError:
            parts.append("%s:-" % directory)
    return "|".join(parts)


def _cache_path() -> Optional[str]:
    try:
        from .pkgman import INSTALL_DIR                     # noqa: WPS433
        base = str(INSTALL_DIR)
    except Exception:
        base = os.path.join(os.path.expanduser("~"), ".cache", "camoufox")
    try:
        os.makedirs(os.path.join(base, "fontcache"), exist_ok=True)
    except OSError:
        return None
    return os.path.join(base, "fontcache", "host-fonts.json")


def installed_families(refresh: bool = False,
                       use_cache: bool = True) -> Dict[str, Any]:
    """Every font family this machine has, with where the answer came from.

    Returns {"os", "families": [...], "source", "dirs", "seconds", "cached"}.
    """
    dirs = font_dirs()
    signature = _signature(dirs)
    path = _cache_path() if use_cache else None

    if path and not refresh and os.path.exists(path):
        try:
            with open(path, "rb") as fh:
                cached = json.loads(fh.read())
            if (cached.get("version") == CACHE_VERSION
                    and cached.get("signature") == signature):
                cached["cached"] = True
                return cached
        except (OSError, ValueError):
            pass

    started = time.time()
    families: Optional[Set[str]] = None
    source = "files"
    system = host_os()
    if system == "linux":
        families = _from_fc_list()
        source = "fontconfig"
    elif system == "windows":
        families = _from_windows_registry()
        source = "registry"
    if not families:
        families = _from_font_files(dirs)
        source = "files"

    result = {
        "version": CACHE_VERSION,
        "signature": signature,
        "os": system,
        "source": source,
        "dirs": dirs,
        "families": sorted(families),
        "seconds": round(time.time() - started, 3),
        "cached": False,
    }
    if path:
        try:
            tmp = path + ".tmp%d" % os.getpid()
            with open(tmp, "w") as fh:
                json.dump(result, fh)
            os.replace(tmp, path)
        except OSError:
            pass
    return result


def family_index(inventory: Optional[Dict[str, Any]] = None) -> Set[str]:
    """The installed families, normalised for comparison."""
    inventory = inventory or installed_families()
    return {normalise(name) for name in inventory.get("families", [])}


def partition(claimed: Iterable[str],
              inventory: Optional[Dict[str, Any]] = None
              ) -> Tuple[List[str], List[str]]:
    """Split the families an identity claims into (on this host, not on it)."""
    index = family_index(inventory)
    real, missing = [], []
    for name in claimed:
        (real if normalise(name) in index else missing).append(name)
    return real, missing
