"""
Locate the browser inside an extracted release package.

`scripts/package.py` produces one zip per (os, arch) whose interior layout
differs by platform, so the caller only ever names the directory it unzipped
into and this resolves the executable within it.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List

# Relative to the root of an extracted package. Ordered: the first hit wins.
#
# Linux ships `camoufox` and `camoufox-bin` as identical copies of the same
# ELF (same BuildID -- the split is Firefox's own convention, not a wrapper).
# `camoufox-bin` is named first only to match what `make tests` and the
# Makefile already point at. macOS and Windows ship a single executable.
_CANDIDATES = {
    'linux': ['camoufox-bin', 'camoufox'],
    'windows': ['camoufox.exe', 'camoufox-bin.exe'],
    'macos': [
        'Camoufox.app/Contents/MacOS/camoufox',
        'Camoufox.app/Contents/MacOS/camoufox-bin',
    ],
}

TARGETS = tuple(_CANDIDATES)


class BinaryNotFound(RuntimeError):
    pass


def candidates(target: str) -> List[str]:
    try:
        return list(_CANDIDATES[target])
    except KeyError:
        raise ValueError(
            f"unknown target {target!r}; expected one of {', '.join(TARGETS)}"
        ) from None


def find_binary(package_dir: Path, target: str) -> Path:
    """
    Return the executable for `target` inside `package_dir`.

    The package may be nested one level down (a zip that carries its own top
    directory, or an artifact download that keeps the zip's basename), so a
    single level of descent is searched before giving up.
    """
    roots = [package_dir]
    if package_dir.is_dir():
        roots += sorted(child for child in package_dir.iterdir() if child.is_dir())

    for root in roots:
        for relative in candidates(target):
            found = root / relative
            if found.is_file():
                return found

    searched = ', '.join(str(root) for root in roots)
    raise BinaryNotFound(
        f"no {target} Camoufox binary under {searched} "
        f"(looked for {', '.join(candidates(target))})"
    )


def ensure_executable(path: Path) -> Path:
    """
    Restore the executable bit, which a zip round-trip through
    actions/upload-artifact drops on POSIX hosts.
    """
    if os.name != 'nt':
        path.chmod(path.stat().st_mode | 0o111)
    return path
