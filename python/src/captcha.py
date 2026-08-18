"""
CAPTCHA solving via CaptchaKraken.

Camoufox gets you a browser a site cannot fingerprint as automated. It does not,
on its own, get you past a challenge that is already on screen. This module
bridges the two: hand it a page and it drives the visible captcha to completion
using CaptchaKraken (https://github.com/JWriter20/CaptchaKraken) — OpenCV grid
detection plus a fine-tuned vision model.

    from camoufox.sync_api import Camoufox
    from camoufox.captcha import solve_captcha

    with Camoufox(headless=False) as browser:
        page = browser.new_page()
        page.goto("https://example.com/protected")
        if solve_captcha(page).is_solved:
            ...

INSTALL
The solver is an OPTIONAL extra. Camoufox does not depend on it, and nothing in
this module is imported unless you call it:

    pip install "camoufox[captcha]"

SELF-HOSTED OR HOSTED
CaptchaKraken is source-available and runs entirely on your own GPU — point
`VLLM_BASE_URL` at your server and no request leaves your machine. The hosted
API at api.captchakraken.com is a convenience for people who don't want to run a
9B vision model; set `CAPTCHA_KRAKEN_API_KEY` to use it. Both paths use the same
code and the same weights.

ATTRIBUTION
Every request this module makes is tagged `camoufox/<version>`, and that tag is
how camoufox-originated usage is identified and credited. See `_CLIENT_ENV`
below for the mechanics and why it is set unconditionally.

LICENSING
CaptchaKraken is source-available, and its license restricts SHIPPING the solver
as a built-in feature of a stealth browser distributed to third parties (v1.1
§3(d)) — not USING it with one (§2(c)). Calling into it from your own automation
is permitted, commercially or otherwise. This module is published by the
CaptchaKraken copyright holder, which is what makes Camoufox's own integration
fine; a third-party fork that redistributes it is not automatically covered.
https://github.com/JWriter20/CaptchaKraken/blob/main/LICENSE
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Dict, Iterator, Optional, Union

__all__ = [
    "solve_captcha",
    "watch_captcha",
    "CaptchaSolverUnavailable",
    "CaptchaCredentialsError",
    "CaptchaConfig",
    "client_tag",
    "configure",
    "resolve_config",
    "configured",
    "verify_credentials",
    "HOSTED_BASE_URL",
]

# Where a hosted (token-bearing) user's inference goes. CaptchaKraken's own
# `config.base_url()` falls back to LOCALHOST when it finds no explicit endpoint
# and no credentials file — correct for a self-hoster, useless for someone who
# was handed a cloud token and nothing else: every solve dials a dead local port
# and fails with connection-refused, which says nothing about the fact that the
# request was meant to go to the cloud. So when a token is supplied here and no
# URL is, we resolve the endpoint rather than letting that fallback decide.
HOSTED_BASE_URL = "https://api.captchakraken.com/v1"

# The two variables CaptchaKraken reads for "where do I send this, and as whom".
# Set per-solve rather than globally: see `_applied`.
_BASE_URL_ENV = "VLLM_BASE_URL"
_API_KEY_ENV = "CAPTCHA_KRAKEN_API_KEY"

# The served adapter name CaptchaKraken asks vLLM for, as the `model` field.
#
# WHY THIS IS EXPOSED. It defaults, via CaptchaKraken's pinned manifest, to
# whatever that release pinned — which is NOT necessarily the adapter you just
# deployed. A fleet can serve several at once (`captcha`, `captcha-v12`, ...),
# and picking the wrong one is silent: the prompts and the weights come from
# different generations and every puzzle just answers worse. Naming the model
# here is how you target a specific adapter without editing the installed
# CaptchaKraken package.
_MODEL_ENV = "CAPTCHA_LORA_NAME"


def _package_version() -> str:
    """
    The camoufox PACKAGE version, from installed metadata.

    Deliberately not `camoufox.__version__` — that module holds the supported
    BROWSER version range (`CONSTRAINTS.MIN_VERSION`/`MAX_VERSION`), which is a
    different number entirely. Reporting a Firefox build range as the client tag
    would make attribution unreadable.
    """
    try:
        from importlib.metadata import version

        return version("camoufox")
    except Exception:
        # Running from a source checkout with no installed dist. Better an
        # honest "unknown" than a fabricated version number in the audit trail.
        return "unknown"


# Read by CaptchaKraken's planner, which sends it as the `X-CK-Client` header.
#
# WHY IT IS SET UNCONDITIONALLY, overwriting whatever is already in the
# environment: this tag is the attribution signal for the CaptchaKraken/camoufox
# partnership — it is what distinguishes camoufox-originated solves from
# everyone else's, and the revenue share is computed from it. An integration
# that let the tag be replaced would let attribution be silently redirected,
# which is precisely what both parties agreed the tracking must not permit. It
# is restored on the way out so it never leaks into unrelated code in the same
# process.
#
# It carries NO pricing power: the server derives the billable puzzle class from
# the request body, never from this header, so a forged tag cannot buy cheaper
# inference. Attribution only.
_CLIENT_ENV = "CAPTCHA_KRAKEN_CLIENT"


class CaptchaSolverUnavailable(RuntimeError):
    """Raised when the optional CaptchaKraken dependency is not installed."""


class CaptchaCredentialsError(ValueError):
    """Raised for a `captcha=` option that names neither an endpoint nor a key."""


@dataclass(frozen=True)
class CaptchaConfig:
    """Resolved answer to "where do solves go, and as whom"."""

    base_url: Optional[str] = None
    api_key: Optional[str] = None
    model: Optional[str] = None

    @property
    def is_hosted(self) -> bool:
        return self.base_url == HOSTED_BASE_URL


# A cloud key is `ck_live_…` / `ck_test_…`; anything with a scheme is an
# endpoint. Distinguishing them lets `captcha="..."` take either without the
# caller having to remember which keyword it was.
def _looks_like_url(value: str) -> bool:
    return "://" in value


def resolve_config(option: Union[bool, str, Dict[str, Any], "CaptchaConfig"]) -> CaptchaConfig:
    """
    Normalise the `captcha=` launch option into a `CaptchaConfig`.

    Accepts, in rough order of how often it is used:
      "ck_live_…"                     a cloud token   -> hosted endpoint
      "http://localhost:8000/v1"      a self-hosted endpoint
      {"token": …}                    same as the first
      {"url": …, "token": …}          self-hosted behind an auth proxy
      {"model": "captcha-v12"}        a specific served adapter; combine with
                                      either of the above, or use alone when the
                                      endpoint and key are already in the env
      True                            defer entirely to CaptchaKraken's own
                                      resolution (env vars, credentials file)

    A token with no URL resolves to `HOSTED_BASE_URL` — that is the "camoufox
    queries CaptchaKraken by default when you give it a token" rule, and the
    reason this function exists rather than passing the values straight through.
    """
    if isinstance(option, CaptchaConfig):
        return option
    if option is True:
        return CaptchaConfig()
    if isinstance(option, str):
        value = option.strip()
        if not value:
            raise CaptchaCredentialsError("captcha= was given an empty string.")
        option = {"url": value} if _looks_like_url(value) else {"token": value}
    if not isinstance(option, dict):
        raise CaptchaCredentialsError(
            f"captcha= expects a token, a URL, a dict or True — got {type(option).__name__}."
        )

    unknown = set(option) - {"token", "url", "api_key", "base_url", "model", "verify"}
    if unknown:
        raise CaptchaCredentialsError(
            f"Unknown captcha= option(s): {', '.join(sorted(unknown))}. "
            "Use 'token' for a cloud key and 'url' for a self-hosted endpoint."
        )

    token = option.get("token") or option.get("api_key")
    url = option.get("url") or option.get("base_url")
    model = option.get("model")

    # A model alone is a legitimate config: it says "the endpoint and key are
    # already in my environment, just point me at a different adapter".
    if not token and not url and model:
        return CaptchaConfig(model=model)

    if not token and not url:
        raise CaptchaCredentialsError(
            "captcha= needs either a cloud token or a self-hosted URL.\n"
            "  Camoufox(captcha={'token': 'ck_live_…'})          # hosted\n"
            "  Camoufox(captcha={'url': 'http://host:8000/v1'})  # self-hosted\n"
            "Get a key at https://captchakraken.com, or pass captcha=True to use "
            "the CAPTCHA_KRAKEN_API_KEY / VLLM_BASE_URL already in your environment."
        )

    # The rule the whole option exists for: a token alone means the cloud.
    return CaptchaConfig(base_url=url or HOSTED_BASE_URL, api_key=token or None, model=model)


_CONFIG: Optional[CaptchaConfig] = None


def configure(option: Union[bool, str, Dict[str, Any], CaptchaConfig, None]) -> Optional[CaptchaConfig]:
    """Register the config every later `solve_captcha` should use (None clears)."""
    global _CONFIG
    _CONFIG = None if option is None or option is False else resolve_config(option)
    return _CONFIG


def configured() -> Optional[CaptchaConfig]:
    """The config registered by the last `Camoufox(captcha=…)`, if any."""
    return _CONFIG


def client_tag() -> str:
    """The attribution tag this camoufox build reports, e.g. `camoufox/0.5.4`."""
    return f"camoufox/{_package_version()}"


@contextmanager
def _env(values: Dict[str, Optional[str]]) -> Iterator[None]:
    """Apply env vars for the duration of a block, restoring exactly what was there."""
    previous = {name: os.environ.get(name) for name in values}
    for name, value in values.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
    try:
        yield
    finally:
        for name, was in previous.items():
            if was is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = was


@contextmanager
def _applied(config: Optional[CaptchaConfig]) -> Iterator[None]:
    """
    Attribution tag, plus the launch-time endpoint/key if one was registered.

    Scoped to the solve rather than written once at launch: a launcher that
    permanently mutated the process environment would silently redirect any
    OTHER CaptchaKraken usage in the same program, and would make two Camoufox
    instances with different credentials impossible.

    An explicit env var still wins — `_env` only sets what the config actually
    carries, so `VLLM_BASE_URL=… python app.py` overrides the launch option,
    which is the precedence CaptchaKraken's own `config.base_url()` documents.
    """
    values: Dict[str, Optional[str]] = {_CLIENT_ENV: client_tag()}
    if config is not None:
        if config.base_url and not os.environ.get(_BASE_URL_ENV):
            values[_BASE_URL_ENV] = config.base_url
        if config.api_key and not os.environ.get(_API_KEY_ENV):
            values[_API_KEY_ENV] = config.api_key
        if config.model and not os.environ.get(_MODEL_ENV):
            values[_MODEL_ENV] = config.model
    with _env(values):
        yield


def _load_solver():
    try:
        from captchakraken.page_solver import solve_captcha_on_page
    except ModuleNotFoundError as exc:  # pragma: no cover - trivial branch
        raise CaptchaSolverUnavailable(
            "CAPTCHA solving needs the optional CaptchaKraken dependency.\n"
            '  pip install "camoufox[captcha]"\n'
            "Docs: https://github.com/JWriter20/CaptchaKraken"
        ) from exc
    return solve_captcha_on_page


def verify_credentials(option: Union[bool, str, Dict[str, Any], CaptchaConfig, None] = None) -> Dict[str, Any]:
    """
    Check that the configured endpoint answers, the key is accepted, and the
    account has credit — before a solve rather than in the middle of one.

    Asks the endpoint for its model list, which is the cheapest authenticated
    call it serves and is metered by nobody. Returns a dict with `ok`, `status`,
    and either `models` or `error`; it does not raise on a rejected key, because
    "your key is invalid" is an answer, not a failure to get one.

    A 402 is the credits signal: the gateway refuses before spending anything
    when the balance is at or below zero.
    """
    import json as _json
    import urllib.error
    import urllib.request

    config = resolve_config(option) if option is not None else (configured() or CaptchaConfig())
    base = (config.base_url or os.environ.get(_BASE_URL_ENV) or HOSTED_BASE_URL).rstrip("/")
    key = config.api_key or os.environ.get(_API_KEY_ENV)

    request = urllib.request.Request(f"{base}/models", method="GET")
    request.add_header("User-Agent", client_tag())
    if key:
        request.add_header("Authorization", f"Bearer {key}")

    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            body = _json.loads(response.read().decode("utf-8") or "{}")
        return {
            "ok": True,
            "status": response.status,
            "base_url": base,
            "models": [m.get("id") for m in body.get("data", [])],
        }
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        hint = {
            401: "The key was rejected. Check it, or issue a new one from your dashboard.",
            402: "Out of credit — top up at https://captchakraken.com before solving.",
            404: "Endpoint not found. A self-hosted URL must end in /v1.",
        }.get(exc.code)
        return {"ok": False, "status": exc.code, "base_url": base, "error": hint or detail}
    except Exception as exc:  # URLError, timeout, bad host, TLS ...
        return {"ok": False, "status": None, "base_url": base, "error": str(exc)}


def solve_captcha(page: Any, **kwargs: Any) -> Any:
    """
    Solve whatever captcha is currently on `page`.

    `page` is a synchronous Playwright page — what `Camoufox()` hands you.
    Keyword arguments are forwarded to `captchakraken.PageSolver`; the useful
    ones are `config=PageSolverConfig(...)` for timeouts and retry budgets, and
    `api_key=` for the hosted API.

    Credentials come from the `captcha=` option the browser was launched with,
    unless the environment already sets them.

    Returns CaptchaKraken's `SolveResult` (`.is_solved`, `.final_mouse_position`,
    `.token_usage`). Raises `CaptchaSolverUnavailable` if the extra is not
    installed, or one of CaptchaKraken's errors — `NoCaptchaFoundError`,
    `UnsupportedChallengeError`, `AnimatedChallengeError` — which are worth
    catching separately because they mean genuinely different things about the
    page.

    NOTE: synchronous only, mirroring CaptchaKraken's driver. `AsyncCamoufox`
    users cannot call this — a sync Playwright handle cannot be driven from
    inside an event loop.
    """
    solve = _load_solver()
    with _applied(configured()):
        return solve(page, **kwargs)


class _TaggedSolver:
    """
    A `PageSolver` whose every solve runs inside `_applied`.

    WHY THE WRAPPER EXISTS. `_applied` is a context manager: it restores the
    environment the moment its block unwinds. A watcher installed inside one
    would hold the attribution tag and the launch-time credentials only for as
    long as it took to build the watcher and hand it back — long before any
    captcha appeared. Every later solve would run untagged, and a hosted user's
    key would be absent entirely, so the request would go out anonymous and
    fail. Scoping it per-solve puts them in place exactly when a request is
    made, and covers `poll_once()` in a caller's own loop as well as `run()`.
    """

    def __init__(self, solver: Any, config: Optional[CaptchaConfig]) -> None:
        self._solver = solver
        self._config = config

    def detect_captcha(self, page: Any) -> Any:
        # Pure DOM reads — no request leaves the process, so no tag is needed.
        return self._solver.detect_captcha(page)

    def solve(self, page: Any) -> Any:
        with _applied(self._config):
            return self._solver.solve(page)


def watch_captcha(page: Any, config: Any = None, **options: Any) -> Any:
    """
    A watcher that solves captchas on `page` as they appear.

        from camoufox.sync_api import Camoufox
        from camoufox.captcha import watch_captcha

        with Camoufox(headless=False, captcha="ck_live_…") as browser:
            page = browser.new_page()
            watch_captcha(page).run()          # blocking: hold this page clean

    Or cooperatively, inside your own loop:

        watcher = watch_captcha(page)
        while working():
            watcher.poll_once()

    `config` is a `PageSolverConfig` for the underlying driver; `options` are
    the watcher's own (`interval_ms`, `max_solves`, `error_backoff_ms`,
    `on_solved`, `on_error`).

    ISOLATED WORLD, FOR FREE
    Nothing is injected into the page — the watcher drives CaptchaKraken's own
    `detect_captcha()` from the driver side on a timer. Under Camoufox the DOM
    reads it performs run in the sandboxed Juggler world, because that is
    Camoufox's default for ALL Playwright evaluation (`main_world_eval` and an
    "mw:" prefix are the opt-OUT). Nothing here opts out, so the page can no
    more see the watcher than it can see Playwright itself.

    NOTE: synchronous, like `solve_captcha`, and for the same reason — a sync
    Playwright handle cannot be driven from an event loop or a worker thread.
    The TypeScript twin's `watchCaptcha` returns a background handle instead;
    that difference is Playwright's, not ours.
    """
    try:
        from captchakraken.page_solver import PageSolver
        from captchakraken.watcher import CaptchaWatcher
    except ModuleNotFoundError as exc:  # pragma: no cover - trivial branch
        raise CaptchaSolverUnavailable(
            "CAPTCHA solving needs the optional CaptchaKraken dependency.\n"
            '  pip install "camoufox[captcha]"\n'
            "Docs: https://github.com/JWriter20/CaptchaKraken"
        ) from exc

    solver = PageSolver(config) if config is not None else PageSolver()
    return CaptchaWatcher(solver=_TaggedSolver(solver, configured()), page=page, **options)
