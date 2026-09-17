"""Whole-identity coherence: the checks that look at more than one field.

Camoufox assembles an identity from several pools -- the navigator and screen
from the fingerprint generator, the GPU from `webgl_data.db`, fonts and voices
from its own catalogues, media devices from `media-devices.json`. Each pool is
sampled on its own, so a combination that no machine has ever had can be built
out of individually plausible parts: an Apple M1 with 2 cores, a Mac reporting a
Braswell Atom GPU, a Linux identity whose platform says armv81 while its user
agent says x86_64.

That class of defect cannot be removed by cleaning the pools, because it is
created when they are combined. So every identity passes through here, whatever
it was built from: a generated fingerprint, a bundled preset, or a config the
caller wrote by hand.

Each rule states an invariant that holds on real machines, says how to repair an
identity that breaks it where a correct value is determined, and is checked
again afterwards. `validate()` reports what is still broken; `apply()` repairs
what it can. Rules are written against measured behaviour, and the measurement
is cited in the rule -- a rule nobody can trace is a rule nobody can revise.

Ground truth used below was captured from stock Firefox 152.0.4, headed, on
three machines on 2026-09-17: a 16-core Linux box (1920x1080, colorDepth 24,
dpr 1, "Radeon HD 3200 Graphics, or similar"), a 16-core Windows 11 laptop with
a touchscreen (3456x2160 at dpr 2.5 -> 1382x864 CSS, colorDepth 24, maxTouchPoints
5, ANGLE/Intel), and a 10-core Apple Silicon Mac mini (2560x1440, colorDepth 30,
dpr 2, "Apple M1, or similar" -- the machine is an M4, and Firefox generalises
the renderer string itself).
"""

from typing import Any, Callable, Dict, List, NamedTuple, Optional

# Core counts Apple Silicon actually ships. The M1 is the floor at 8; nothing
# Apple has made has fewer. Firefox reports "Apple M1, or similar" for every
# M-series part, so the renderer string cannot narrow it further than this.
APPLE_SILICON_CORES = frozenset({8, 10, 12, 14, 16, 20, 24, 28, 32})

# devicePixelRatio by platform. Windows exposes the display-scaling steps
# (100/125/150/175/200/250/300%); macOS reports 1 or 2 and nothing between;
# Linux reports 1, or 2 under HiDPI, with GNOME fractional scaling giving the
# 1.25/1.5/1.75 steps. Values outside these (1.818, 1.09, 1.36) are scraped
# artefacts -- a browser zoom level folded into the ratio, not a display mode.
PLAUSIBLE_DPR = {
    'win': frozenset({1, 1.25, 1.5, 1.75, 2, 2.5, 3}),
    'mac': frozenset({1, 2}),
    'lin': frozenset({1, 1.25, 1.5, 1.75, 2}),
}

# colorDepth: Firefox reports 24, or 30 on a deep-colour display. macOS defaults
# to deep colour, so an Apple Silicon Mac reports 30 (measured). 32 appears in
# scraped data and is not a value Firefox emits.
PLAUSIBLE_COLOR_DEPTH = frozenset({24, 30})

# maxTouchPoints: 0 on a machine with no digitiser, 5 or 10 on a touchscreen
# (measured 5 on the Windows laptop). Macs have no touchscreen.
PLAUSIBLE_TOUCH_POINTS = frozenset({0, 1, 5, 10})

# GPU strings that are not possible on macOS. Firefox on a Mac reports Apple
# Silicon as "Apple M1, or similar", and Intel Macs as an Intel Iris/UHD/HD
# 4000-6000 part; ANGLE is Windows-only (Direct3D), and these two rows in
# webgl_data.db are a Braswell Atom IGP and a desktop PC card, neither of which
# shipped in any Mac.
_NOT_A_MAC_GPU = ('ANGLE', 'Intel(R) HD Graphics 400', 'Radeon R9 200 Series', 'llvmpipe')


class Violation(NamedTuple):
    rule: str
    detail: str


class Rule(NamedTuple):
    name: str
    #: Returns a description of the breakage, or None when the identity holds.
    check: Callable[[Dict[str, Any], str], Optional[str]]
    #: Repairs the identity in place. None where no correct value is determined.
    repair: Optional[Callable[[Dict[str, Any], str], None]]


def _renderer(config: Dict[str, Any]) -> str:
    return str(config.get('webGl:renderer') or '')


def _is_apple_silicon(config: Dict[str, Any]) -> bool:
    return 'Apple M' in _renderer(config)


def _check_apple_silicon_cores(config: Dict[str, Any], target_os: str) -> Optional[str]:
    if not _is_apple_silicon(config):
        return None
    cores = config.get('navigator.hardwareConcurrency')
    if isinstance(cores, int) and cores not in APPLE_SILICON_CORES:
        return f'{_renderer(config)!r} with hardwareConcurrency {cores}; Apple Silicon starts at 8'
    return None


def _repair_apple_silicon_cores(config: Dict[str, Any], target_os: str) -> None:
    cores = config.get('navigator.hardwareConcurrency')
    if not isinstance(cores, int):
        return
    # Snap UP to the nearest count Apple ships: a machine claiming an M-series
    # GPU has at least 8 cores, and a page timing workers can see how many.
    config['navigator.hardwareConcurrency'] = min(
        (c for c in sorted(APPLE_SILICON_CORES) if c >= cores), default=max(APPLE_SILICON_CORES)
    )


def gpu_fits_os(renderer: Optional[str], target_os: str) -> bool:
    """Whether this renderer string is one the OS can report.

    Used both to check a finished identity and to filter `webgl_data.db` before
    sampling, so the two can never disagree about what a Mac may claim.
    """
    renderer = str(renderer or '')
    if not renderer:
        return True
    if target_os == 'mac':
        return not any(bad in renderer for bad in _NOT_A_MAC_GPU)
    if target_os == 'win':
        return renderer.startswith('ANGLE')
    if target_os == 'lin':
        return 'ANGLE' not in renderer and 'Apple M' not in renderer
    return True


def _check_gpu_matches_os(config: Dict[str, Any], target_os: str) -> Optional[str]:
    renderer = _renderer(config)
    if not renderer or gpu_fits_os(renderer, target_os):
        return None
    if target_os == 'mac':
        return f'macOS identity with {renderer!r}, which no Mac reports'
    if target_os == 'win':
        return f'Windows identity with {renderer!r}; Firefox on Windows renders through ANGLE'
    return f'Linux identity with {renderer!r}'


def _check_color_depth(config: Dict[str, Any], target_os: str) -> Optional[str]:
    depth = config.get('screen.colorDepth')
    if depth is None:
        return None
    if depth not in PLAUSIBLE_COLOR_DEPTH:
        return f'screen.colorDepth {depth}; Firefox reports 24 or 30'
    if target_os == 'mac' and _is_apple_silicon(config) and depth != 30:
        return f'Apple Silicon Mac with colorDepth {depth}; deep colour is the macOS default'
    return None


def _repair_color_depth(config: Dict[str, Any], target_os: str) -> None:
    depth = config.get('screen.colorDepth')
    if depth is not None and depth not in PLAUSIBLE_COLOR_DEPTH:
        config['screen.colorDepth'] = 24
    if target_os == 'mac' and _is_apple_silicon(config):
        config['screen.colorDepth'] = 30
    # pixelDepth is the same number in every browser that reports both.
    if 'screen.pixelDepth' in config or 'screen.colorDepth' in config:
        config['screen.pixelDepth'] = config.get('screen.colorDepth', 24)


def _check_touch_points(config: Dict[str, Any], target_os: str) -> Optional[str]:
    touch = config.get('navigator.maxTouchPoints')
    if touch is None:
        return None
    if touch not in PLAUSIBLE_TOUCH_POINTS:
        return f'navigator.maxTouchPoints {touch}'
    if target_os == 'mac' and touch:
        return f'macOS identity with maxTouchPoints {touch}; no Mac has a touchscreen'
    return None


def _repair_touch_points(config: Dict[str, Any], target_os: str) -> None:
    touch = config.get('navigator.maxTouchPoints')
    if target_os == 'mac' or (touch is not None and touch not in PLAUSIBLE_TOUCH_POINTS):
        config['navigator.maxTouchPoints'] = 0


def _check_screen_shape(config: Dict[str, Any], target_os: str) -> Optional[str]:
    width, height = config.get('screen.width'), config.get('screen.height')
    if not width or not height:
        return None
    if height > width:
        return f'portrait screen {width}x{height}; desktop panels are landscape'
    if width < 1024:
        return f'screen {width}x{height} is smaller than any current desktop panel'
    return None


def _check_avail_bounds(config: Dict[str, Any], target_os: str) -> Optional[str]:
    width, height = config.get('screen.width'), config.get('screen.height')
    avail_w, avail_h = config.get('screen.availWidth'), config.get('screen.availHeight')
    if width and avail_w and avail_w > width:
        return f'screen.availWidth {avail_w} exceeds screen.width {width}'
    if height and avail_h and avail_h > height:
        return f'screen.availHeight {avail_h} exceeds screen.height {height}'
    return None


def _repair_avail_bounds(config: Dict[str, Any], target_os: str) -> None:
    width, height = config.get('screen.width'), config.get('screen.height')
    if width and config.get('screen.availWidth', 0) > width:
        config['screen.availWidth'] = width
    if height and config.get('screen.availHeight', 0) > height:
        config['screen.availHeight'] = height


def _check_arch_agreement(config: Dict[str, Any], target_os: str) -> Optional[str]:
    ua = str(config.get('navigator.userAgent') or '')
    platform = str(config.get('navigator.platform') or '')
    oscpu = str(config.get('navigator.oscpu') or '')
    if not ua:
        return None
    if 'x86_64' in ua and ('armv' in platform or 'armv' in oscpu):
        return f'user agent claims x86_64 while platform/oscpu say {platform!r}/{oscpu!r}'
    return None


RULES: List[Rule] = [
    Rule('apple-silicon-cores', _check_apple_silicon_cores, _repair_apple_silicon_cores),
    Rule('gpu-matches-os', _check_gpu_matches_os, None),
    Rule('color-depth', _check_color_depth, _repair_color_depth),
    Rule('touch-points', _check_touch_points, _repair_touch_points),
    Rule('screen-shape', _check_screen_shape, None),
    Rule('avail-bounds', _check_avail_bounds, _repair_avail_bounds),
    Rule('arch-agreement', _check_arch_agreement, None),
]


def screen_is_implausible(config: Dict[str, Any]) -> bool:
    """Whether the screen is one no desktop reports (portrait, or tiny)."""
    return _check_screen_shape(config, '') is not None


def repair_screen_orientation(config: Dict[str, Any]) -> bool:
    """Turn a portrait screen landscape, keeping the panel's own dimensions.

    One bundled preset reports 1440x2560: a phone panel, or a desktop one
    captured while rotated. Either way a desktop identity that claims it is
    answering a `matchMedia('(orientation: portrait)')` the way no desktop
    does, so the axes are swapped rather than the numbers invented. Run before
    the window clamps, which then bound the window to the new screen.
    """
    width, height = config.get('screen.width'), config.get('screen.height')
    if not width or not height or height <= width:
        return False
    config['screen.width'], config['screen.height'] = height, width
    avail_w, avail_h = config.get('screen.availWidth'), config.get('screen.availHeight')
    if avail_w and avail_h:
        config['screen.availWidth'], config['screen.availHeight'] = avail_h, avail_w
    return True


def drop_incoherent_source_values(config: Dict[str, Any], target_os: str) -> List[Violation]:
    """Discard values a source supplied that this identity cannot keep.

    Run before the pools that would otherwise defer to them. A preset carries
    its own GPU pair, so a macOS preset naming a Braswell Atom IGP keeps it all
    the way to the page unless the pair is dropped here -- at which point the
    normal WebGL sampling draws a coherent one instead. 15 of the 312 bundled
    presets need this; they are scraped rows, not real machines.

    Only values that another pool can replace are dropped. Everything else is
    left for `apply()` to repair or report.
    """
    dropped = []
    renderer = _renderer(config)
    if renderer and not gpu_fits_os(renderer, target_os):
        config.pop('webGl:renderer', None)
        config.pop('webGl:vendor', None)
        dropped.append(Violation('gpu-matches-os', f'dropped {renderer!r} for a {target_os} identity'))
    return dropped


def validate(config: Dict[str, Any], target_os: str) -> List[Violation]:
    """Every invariant this identity breaks. Empty means coherent."""
    violations = []
    for rule in RULES:
        detail = rule.check(config, target_os)
        if detail:
            violations.append(Violation(rule.name, detail))
    return violations


def apply(config: Dict[str, Any], target_os: str) -> List[Violation]:
    """Repair what is determined, and report what is left.

    A rule with no repair -- a Windows GPU on a macOS identity, a portrait
    screen -- cannot be corrected without inventing a machine, so it is
    returned for the caller to decide about. The caller that draws the identity
    can simply draw again.
    """
    for rule in RULES:
        if rule.repair and rule.check(config, target_os):
            rule.repair(config, target_os)
    return validate(config, target_os)
