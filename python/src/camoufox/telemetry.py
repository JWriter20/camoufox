"""
Anti-bot telemetry for a launch (camoucore.telemetry): what it reports is in the
"telemetry" entry of camoufox.defaults.json. Nothing here may break a launch,
so a failure to set it up leaves the launch without telemetry.
"""

from importlib.metadata import version as package_version
from pathlib import Path
from typing import Any, Dict, Optional

from camoucore.telemetry import Telemetry, telemetry_enabled

from . import pro
from .defaults import PRODUCT
from .pkgman import build_file


def start(from_options: Dict[str, Any], setting: Optional[bool], passthrough: bool) -> Optional[Telemetry]:
    """The reporter for a launch with these options, or None when telemetry is off."""
    if not telemetry_enabled(PRODUCT, setting):
        return None
    from .utils import OS_NAMES, _firefox_version, _host_os_key, launch_os

    try:
        binary = Path(from_options['executable_path'])
        return Telemetry(
            PRODUCT,
            version=package_version('camoufox'),
            browser_version=_firefox_version(binary),
            pro=pro.read_build(build_file(binary, 'pro-build.json')) is not None,
            cross_os=launch_os(from_options) != OS_NAMES[_host_os_key()],
            passthrough=passthrough,
            proxy=from_options.get('proxy'),
        )
    except Exception:
        return None


def attach(target: Any, telemetry: Optional[Telemetry], is_async: bool) -> None:
    """Report on `target`, a launched browser or persistent context, until it closes."""
    if telemetry is None:
        return
    try:
        if hasattr(target, 'new_context'):
            telemetry.watch_browser(target)
            _watch_new_contexts(target, telemetry, is_async)
            return
        telemetry.watch(target)
        if target.browser is not None:
            telemetry.watch_browser(target.browser)
        else:
            target.on('close', lambda *_: telemetry.close())
    except Exception:
        telemetry.close()


def _watch_new_contexts(browser: Any, telemetry: Telemetry, is_async: bool) -> None:
    """Watch every context the browser opens: new_context(), and the one new_page() opens."""
    new_context, new_page = browser.new_context, browser.new_page

    def watch(context: Any) -> None:
        try:
            telemetry.watch(context)
        except Exception:
            pass

    if is_async:
        async def new_context_async(*args: Any, **kwargs: Any) -> Any:
            context = await new_context(*args, **kwargs)
            watch(context)
            return context

        async def new_page_async(*args: Any, **kwargs: Any) -> Any:
            page = await new_page(*args, **kwargs)
            watch(page.context)
            return page

        browser.new_context, browser.new_page = new_context_async, new_page_async
        return

    def new_context_sync(*args: Any, **kwargs: Any) -> Any:
        context = new_context(*args, **kwargs)
        watch(context)
        return context

    def new_page_sync(*args: Any, **kwargs: Any) -> Any:
        page = new_page(*args, **kwargs)
        watch(page.context)
        return page

    browser.new_context, browser.new_page = new_context_sync, new_page_sync
