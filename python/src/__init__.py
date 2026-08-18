from .addons import DefaultAddons
from .async_api import AsyncCamoufox, AsyncNewBrowser, AsyncNewContext

# Safe to import eagerly: `captcha` resolves CaptchaKraken lazily, inside the
# call, so importing camoufox never requires the optional extra to be present.
from .captcha import (
    CaptchaConfig,
    CaptchaCredentialsError,
    CaptchaSolverUnavailable,
    solve_captcha,
    watch_captcha,
    verify_credentials,
)
from .sync_api import Camoufox, NewBrowser, NewContext
from .utils import launch_options

__all__ = [
    "Camoufox",
    "solve_captcha",
    "watch_captcha",
    "verify_credentials",
    "CaptchaConfig",
    "CaptchaCredentialsError",
    "CaptchaSolverUnavailable",
    "NewBrowser",
    "NewContext",
    "AsyncCamoufox",
    "AsyncNewBrowser",
    "AsyncNewContext",
    "DefaultAddons",
    "launch_options",
]
