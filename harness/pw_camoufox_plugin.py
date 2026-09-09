"""pytest plugin that lets upstream's own Playwright suite drive a Camoufox build.

Loaded with `-p` against an *unmodified* checkout of playwright-python's tests,
so upstream can refactor its conftest freely without breaking us. Two
adaptations are needed and no more:

  1. Upstream's `launch_arguments` fixture has no way to point at a browser
     binary, so every launch is intercepted at the implementation layer and
     given `executablePath`. Hooking `_impl` rather than the fixture is what
     makes this survive upstream reshuffling its fixtures.

  2. Camoufox evaluates in an isolated world by default -- the reason the fork
     exists. Upstream's suite asserts upstream semantics: tests read globals
     their own page scripts defined and pass handles into evaluate(). Isolation
     is therefore switched off for this suite alone, exactly as the vendored
     suite does it. Camoufox's isolated-world behaviour keeps its own coverage
     in tests/patches/isolated-evaluate.py, which must go on passing without
     this flag.
"""

from __future__ import annotations

import json
import os
from typing import Any

_EXECUTABLE_ENV = "CAMOUFOX_EXECUTABLE_PATH"


def _disable_world_isolation() -> None:
    raw = os.environ.get("CAMOU_CONFIG")
    config = json.loads(raw) if raw else {}
    config["disableWorldIsolation"] = True
    os.environ["CAMOU_CONFIG"] = json.dumps(config)


def _install_executable_path(path: str) -> None:
    """Inject executablePath into every browser launch, however it is called."""
    from playwright._impl._browser_type import BrowserType

    for name in ("launch", "launch_persistent_context"):
        original = getattr(BrowserType, name, None)
        if original is None or getattr(original, "_camoufox_wrapped", False):
            continue

        def make(original: Any):  # noqa: ANN401
            async def wrapper(self, *args: Any, **kwargs: Any):  # noqa: ANN401
                # playwright's _impl uses camelCase; accept either spelling so a
                # rename upstream degrades to "we set it twice", not "we set
                # nothing".
                if not kwargs.get("executablePath") and not kwargs.get("executable_path"):
                    kwargs["executablePath"] = path
                return await original(self, *args, **kwargs)

            wrapper._camoufox_wrapped = True  # type: ignore[attr-defined]
            return wrapper

        setattr(BrowserType, name, make(original))


def pytest_configure(config) -> None:  # noqa: ANN001
    _disable_world_isolation()
    executable = os.environ.get(_EXECUTABLE_ENV)
    if not executable:
        raise RuntimeError(
            f"{_EXECUTABLE_ENV} is not set. The upstream conformance suite has no way to "
            "select a browser binary, so without it pytest would silently test a "
            "downloaded stock Firefox and report a meaningless pass."
        )
    _install_executable_path(os.path.abspath(executable))


def pytest_report_header(config) -> str:  # noqa: ANN001
    return (
        f"camoufox: binary={os.environ.get(_EXECUTABLE_ENV)} "
        "world-isolation=disabled-for-this-suite"
    )
