"""
Per-channel humanized input: the engine factories and the launch-time config.

    from camoufox.humanize import cursory, raw

    Camoufox(humanize={
        "mouse": cursory(max_time=1.0),
        "scroll": raw(),
        "seed": 1234,
    })

The browser intercepts the Juggler input commands Playwright already sends, so
the same Playwright script drives every build. Each channel (mouse, keyboard,
scroll) names the engine that plans its input. Which engines exist, and which
options each takes, is the build's humanize-engines.json (docs/humanize.md);
`engine(name, **options)` names any of them. A factory returns plain data,
never behaviour, so a humanize setting can also come from JSON.

`humanize` itself may also be:

    None / False    every channel raw(): Playwright's own dispatch
    True            every channel auto()
    a number        {"mouse": cursory(max_time=<number>)}, the rest auto()
    a dict          per channel; an omitted channel is auto()
"""

from typing import Any, Dict, Mapping, Optional, Tuple, Union

from ._warnings import LeakWarning
from .exceptions import HumanizeEngineUnavailable

CHANNELS = ('mouse', 'keyboard', 'scroll')

# What a build without humanize-engines.json runs. It predates per-channel
# humanize: it reads only `humanize` (cursory mouse plus notched wheel) and
# humanize:maxTime / minTime, so it runs every base engine or none.
LEGACY_MANIFEST: Dict[str, Any] = {
    'version': 0,
    'mouse': ['raw', 'cursory'],
    'keyboard': ['raw'],
    'scroll': ['raw', 'notches'],
    'auto': {'mouse': ['cursory'], 'keyboard': ['raw'], 'scroll': ['notches']},
    'engines': {
        'cursory': {
            'options': {
                'maxTime': {'key': 'humanize:maxTime', 'min': 0},
                'minTime': {'key': 'humanize:minTime', 'min': 0},
            }
        },
        'notches': {},
    },
}

Engine = Dict[str, Any]
HumanizeSetting = Union[None, bool, float, int, Mapping[str, Any]]


def engine(name: str, **options: float) -> Engine:
    """Any engine the build's humanize-engines.json lists, with its options.
    The options are checked against the manifest at launch."""
    return {'engine': name, 'options': options}


def auto() -> Engine:
    """The first engine the build lists for the channel that is available."""
    return engine('auto')


def raw() -> Engine:
    """Playwright's own dispatch, byte for byte."""
    return engine('raw')


def cursory(max_time: Optional[float] = None, min_time: Optional[float] = None) -> Engine:
    """Mouse: replayed human cursor paths (Cursory), scaled into
    [min_time, max_time] seconds. Defaults: 0 and 1.5."""
    options = {'maxTime': max_time, 'minTime': min_time}
    return engine('cursory', **{k: v for k, v in options.items() if v is not None})


def notches() -> Engine:
    """Scroll: a wheel call arrives as native notches of 3 lines, tens of ms apart."""
    return engine('notches')


def _as_engine(channel: str, value: Any) -> Engine:
    candidate = engine(value) if isinstance(value, str) else value
    if not isinstance(candidate, Mapping) or not isinstance(candidate.get('engine'), str):
        raise ValueError(
            f'humanize["{channel}"] must be an engine such as auto(), raw() or cursory(), got {value!r}'
        )
    options = dict(candidate.get('options') or {})
    for key, option in options.items():
        if isinstance(option, bool) or not isinstance(option, (int, float)):
            raise ValueError(f'{candidate["engine"]}() option {key} must be a number, got {option!r}')
    return {'engine': candidate['engine'], 'options': options}


def normalize(humanize: HumanizeSetting) -> Tuple[Dict[str, Engine], Optional[int]]:
    """The four channel values of a `humanize` setting: the engine per channel,
    and the seed (None when not given)."""
    if not humanize and not isinstance(humanize, Mapping):
        return {channel: raw() for channel in CHANNELS}, None
    if humanize is True:
        return {channel: auto() for channel in CHANNELS}, None
    if isinstance(humanize, (int, float)):
        return {'mouse': cursory(max_time=humanize), 'keyboard': auto(), 'scroll': auto()}, None
    if not isinstance(humanize, Mapping):
        raise ValueError(f'humanize must be None, a bool, a number or a dict, got {humanize!r}')
    unknown = set(humanize) - {*CHANNELS, 'seed'}
    if unknown:
        raise ValueError(f'humanize has no channel {sorted(unknown)}; the channels are {CHANNELS} and "seed"')
    seed = humanize.get('seed')
    if seed is not None and (isinstance(seed, bool) or not isinstance(seed, int) or not 0 <= seed < 2**64):
        raise ValueError(f'humanize["seed"] must be an integer in [0, 2**64), got {seed!r}')
    engines = {channel: _as_engine(channel, humanize.get(channel, 'auto')) for channel in CHANNELS}
    return engines, seed


def _option_keys(name: str, options: Mapping[str, float], manifest: Mapping[str, Any]) -> Dict[str, float]:
    """An engine's options as config keys, checked against what the manifest declares."""
    declared = (manifest.get('engines', {}).get(name) or {}).get('options', {})
    keys = {}
    for option, value in options.items():
        spec = declared.get(option)
        if spec is None:
            raise ValueError(f'{name}() has no option {option!r}; it takes {sorted(declared)}')
        low, high = spec.get('min', 0), spec.get('max')
        if value < low or (high is not None and value > high):
            bounds = f'in [{low}, {high}]' if high is not None else f'at least {low}'
            raise ValueError(f'{name}() {option} must be {bounds}, got {value!r}')
        keys[spec['key']] = float(value)
    return keys


def humanize_config(
    humanize: HumanizeSetting,
    manifest: Optional[Mapping[str, Any]],
    i_know_what_im_doing: Optional[bool] = None,
) -> Dict[str, Any]:
    """
    The camoucfg keys for a `humanize` setting on a build whose
    humanize-engines.json is `manifest` (None for a build without one).

    Raises HumanizeEngineUnavailable for an engine the build does not ship.
    """
    engines, seed = normalize(humanize)
    names = {channel: engines[channel]['engine'] for channel in CHANNELS}
    legacy = manifest is None
    available = LEGACY_MANIFEST if legacy else manifest

    for channel in CHANNELS:
        if names[channel] not in ('auto', 'raw') and names[channel] not in available.get(channel, ()):
            raise HumanizeEngineUnavailable(channel, names[channel], list(available.get(channel, ())))

    scroll = available.get('engines', {}).get(names['scroll']) or {}
    if scroll.get('movesCursor') and names['mouse'] == 'raw':
        LeakWarning.warn('humanize_scroll_teleports', i_know_what_im_doing)

    options: Dict[str, float] = {}
    for channel in CHANNELS:
        for key, value in _option_keys(names[channel], engines[channel]['options'], available).items():
            if options.get(key, value) != value:
                raise ValueError(f'humanize sets {key} twice, to {options[key]} and {value}')
            options[key] = value

    enabled = any(name != 'raw' for name in names.values())
    if legacy:
        on = {channel: available['auto'][channel][0] for channel in CHANNELS}
        unsupported = [c for c in CHANNELS if names[c] not in ('auto', on[c])]
        if enabled and unsupported:
            channel = unsupported[0]
            raise HumanizeEngineUnavailable(
                channel,
                names[channel],
                [on[channel]],
                'this build predates per-channel humanize: it runs every base engine or none',
            )
        if seed is not None:
            raise HumanizeEngineUnavailable(
                'seed', str(seed), [], 'this build predates per-channel humanize and has no seeded input'
            )
        return {'humanize': True, **options} if enabled else {}

    # Every key on every launch, so nothing depends on what an earlier launch set.
    config: Dict[str, Any] = {'humanize': enabled}
    for channel in CHANNELS:
        config[f'humanize:{channel}'] = names[channel]
    config.update(options)
    if seed is not None:
        # A string: Juggler reads config numbers as doubles, which cannot hold
        # every 64-bit seed exactly.
        config['humanize:seed'] = str(seed)
    return config
