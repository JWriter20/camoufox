"""Screenshots that leave the page alone.

The caret default lives in driver_defaults, beside the launcher's other driver
defaults (fill() clicks the field first), so one mark covers both and the
screenshot methods are wrapped once. This module keeps the name importable.
"""

from .driver_defaults import _install, _launched_by_camoufox, _with_caret_default, mark

__all__ = ['mark']
