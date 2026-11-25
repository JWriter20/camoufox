"""
The launch defaults: camoufox.defaults.json in this package (a copy of the one
at the repository root), overlaid with the user's own file (`camoufox defaults`
writes it). Every launch starts from them; an option the caller passes wins.
"""

from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

from camoucore import FIREFOX, Product
from camoucore.settings import apply_defaults, load_defaults, write_user_defaults

PRODUCT = Product('camoufox', 'Camoufox', FIREFOX, package=Path(__file__).parent)
DEFAULTS_FILE = Path(__file__).with_name('camoufox.defaults.json')


def _from_json(name: str, value: Any) -> Any:
    """A value read from JSON, as the launch option takes it."""
    if name == 'screen':
        from .fingerprints import Screen

        return Screen(**value)
    if name == 'exclude_addons':
        from .addons import DefaultAddons

        return [DefaultAddons[addon] for addon in value]
    if name in ('window', 'webgl_config'):
        return tuple(value)
    return value


def with_defaults(options: Mapping[str, Any], names: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """`options` with each option the caller left unset taken from the defaults,
    of those in `names` (all of them by default). A null default leaves the
    option out, as the caller did."""
    defaults = {
        name: value
        for name, value in load_defaults(PRODUCT, DEFAULTS_FILE).items()
        if (names is None or name in names) and value is not None
    }
    filled = apply_defaults(defaults, options)
    for name in defaults:
        if options.get(name) is None:
            filled[name] = _from_json(name, filled[name])
    return filled


def write_user_file() -> Path:
    """Write the user's own defaults file, unless there is one, and return its path."""
    return write_user_defaults(PRODUCT, DEFAULTS_FILE)
