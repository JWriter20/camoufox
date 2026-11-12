"""
The display's ICC colour profile, which a page reads through every ICC-tagged
image it decodes.

Firefox colour-manages tagged images (gfx.color_management.mode 2, the default
on every OS) from the image's profile to the display's: the OS profile
(GetICMProfileW on Windows, the X root window's _ICC_PROFILE on Linux), or the
file named by gfx.color_management.display_profile, which every platform reads
first (gfxWindowsPlatform.cpp GetPlatformCMSOutputProfileData_Impl,
gfxPlatformGtk.cpp GetPlatformCMSOutputProfileData). A page draws a tagged PNG
onto a canvas and reads the pixels back: stock 158.0.1 on win-i9 (a wide-gamut
3456x2160 panel profile) decodes sRGB red as 217,54,41, while a Linux host
with no _ICC_PROFILE (Xvfb, headless) keeps qcms's built-in sRGB and decodes
255,0,0. Untagged images and CSS colours are not converted (mode 2), and
matchMedia('(color-gamut)') is "srgb" on every OS whatever the profile
(widget/Screen.cpp GetColorGamut; fpgen records srgb for 100% of Firefox rows),
so the profile is not tied to any other claimed value but the display itself.

So an identity of another OS carries its display's profile, either the caller's
(`display_profile=`: ICC bytes or a path, e.g. one recorded with
GetICMProfileW on the machine being presented), or one drawn from
display-profiles.json: per OS, recorded profiles, each either the OS default
(no `screens`) or a panel's own profile tied to the native device-pixel
resolutions it was recorded at. A panel profile is drawn only for an identity
whose screen is one of those panels; otherwise the OS default is. Only the
tags qcms reads (the colorants and the tone curves, qcms iccread.rs
new_from_slice with curves_only) need to be bundled, which is all
scripts/reduce-icc-profile.py keeps.

An identity of the host's own OS gets nothing: the browser reads the real
display's profile, as stock does.
"""

import hashlib
import json
import os
import tempfile
from base64 import b64decode
from pathlib import Path
from random import Random
from typing import Any, Dict, List, Optional, Set, Union

DISPLAY_PROFILE_PREF = 'gfx.color_management.display_profile'

DisplayProfileOption = Union[None, bool, bytes, str, Path]

_TABLE: Optional[Dict[str, Any]] = None


def load_display_profiles() -> Dict[str, Any]:
    """display-profiles.json: OS key -> recorded profiles."""
    global _TABLE
    if _TABLE is None:
        with open(os.path.join(os.path.dirname(__file__), 'display-profiles.json'), 'rb') as f:
            _TABLE = json.loads(f.read())
    return _TABLE


def check_icc(data: bytes) -> bytes:
    """`data` if it is an ICC profile (its declared size fits, 'acsp' at 36), else ValueError.

    A file that is not one would leave Firefox on its built-in sRGB without a
    word, so the launcher refuses it instead.
    """
    if len(data) < 132 or data[36:40] != b'acsp' or int.from_bytes(data[:4], 'big') > len(data):
        raise ValueError('display_profile is not an ICC profile')
    return data


def device_screen(config: Dict[str, Any]) -> Optional[List[int]]:
    """The identity's screen in device pixels: CSS screen size times devicePixelRatio."""
    w, h = config.get('screen.width'), config.get('screen.height')
    if not w or not h:
        return None
    dpr = float(config.get('window.devicePixelRatio') or 1)
    return [round(w * dpr), round(h * dpr)]


def draw_display_profile(config: Dict[str, Any], seed: int, target_os: str) -> Optional[bytes]:
    """The recorded profile an identity of `target_os` with this screen draws, or None (built-in sRGB)."""
    from .fingerprints import _weighted_choice

    entries = load_display_profiles().get(target_os) or []
    screen = device_screen(config)
    panels = [e for e in entries if e.get('screens') and screen in e['screens']]
    pool = panels or [e for e in entries if not e.get('screens')]
    if not pool:
        return None
    return b64decode(_weighted_choice(Random(seed ^ 0xC01C), pool)['icc'])


def _cache_dirs() -> List[Path]:
    """Where profiles are written: beside the installed browsers, else the temp dir."""
    from .pkgman import INSTALL_DIR

    return [Path(INSTALL_DIR) / 'display-profiles', Path(tempfile.gettempdir()) / 'camoufox-display-profiles']


def _write(data: bytes) -> str:
    """The profile's path in the cache (content-addressed, written once)."""
    name = hashlib.sha256(data).hexdigest()[:24] + '.icc'
    for base in _cache_dirs():
        path = base / name
        try:
            if not path.exists() or path.read_bytes() != data:
                base.mkdir(parents=True, exist_ok=True)
                tmp = base / f'.{name}.{os.getpid()}'
                tmp.write_bytes(data)
                os.replace(tmp, path)
            return str(path)
        except OSError:
            continue
    raise OSError('no writable directory for the display profile')


def apply_display_profile(
    prefs: Dict[str, Any],
    user_pref_keys: Set[str],
    option: DisplayProfileOption,
    config: Dict[str, Any],
    seed: int,
    target_os: str,
    host_os: Optional[str],
) -> Optional[str]:
    """Point gfx.color_management.display_profile at the identity's display profile.

    `option`: ICC bytes or a path (the caller's profile), False (none: the
    browser's own, built-in sRGB on a host without one), or None (drawn for an
    identity of another OS). A caller's own pref wins. Returns the path set.
    """
    if DISPLAY_PROFILE_PREF in user_pref_keys or option is False:
        return None
    if option is None or option is True:
        if target_os == host_os:
            return None
        data = draw_display_profile(config, seed, target_os)
        if data is None:
            return None
    elif isinstance(option, (bytes, bytearray)):
        data = bytes(option)
    else:
        data = Path(option).expanduser().read_bytes()
    path = _write(check_icc(data))
    prefs[DISPLAY_PROFILE_PREF] = path
    return path
