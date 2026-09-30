from .addons import DefaultAddons
# scope demo: a pythonlib-only change
from .async_api import AsyncCamoufox, AsyncNewBrowser, AsyncNewContext
from .sync_api import Camoufox, NewBrowser, NewContext
from .utils import launch_options

__all__ = [
    "Camoufox",
    "NewBrowser",
    "NewContext",
    "AsyncCamoufox",
    "AsyncNewBrowser",
    "AsyncNewContext",
    "DefaultAddons",
    "launch_options",
]
