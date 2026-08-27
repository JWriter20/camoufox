"""
Tests for camoufox.captcha, the optional CaptchaKraken bridge.

Mirrors typescript/tests/captcha.test.ts.

Run with:
    cd python && python -m pytest tests/test_captcha.py -v

Two regressions these guard:

1. Importing camoufox must never require the optional extra. The bridge
   resolves CaptchaKraken inside the call, so a user without it installed can
   still `import camoufox`.
2. The `CAPTCHA_KRAKEN_CLIENT` tag must be set for the duration of a solve and
   restored afterwards. That tag is the attribution signal for the
   CaptchaKraken/camoufox partnership -- if it stops being sent, solves stop
   being credited, and nothing else in the system would notice.
"""

import os
import sys
import types

import pytest

from camoufox.captcha import (
    _API_KEY_ENV,
    _BASE_URL_ENV,
    _CLIENT_ENV,
    _MODEL_ENV,
    HOSTED_BASE_URL,
    CaptchaConfig,
    CaptchaCredentialsError,
    CaptchaSolverUnavailable,
    client_tag,
    configure,
    configured,
    resolve_config,
    solve_captcha,
    watch_captcha,
)

_SENTINEL = object()


@pytest.fixture
def fake_captchakraken(monkeypatch):
    """
    Install a stand-in `captchakraken.page_solver` that records the env it saw.

    The real package pulls opencv and a 9B-model client; the bridge's contract
    is only that it calls `solve_captcha_on_page` with the tag in the
    environment, which a stub can observe exactly.
    """
    seen = {}

    def solve_captcha_on_page(page, **kwargs):
        seen["client"] = os.environ.get(_CLIENT_ENV)
        seen["page"] = page
        seen["kwargs"] = kwargs
        return _SENTINEL

    pkg = types.ModuleType("captchakraken")
    page_solver = types.ModuleType("captchakraken.page_solver")
    page_solver.solve_captcha_on_page = solve_captcha_on_page
    pkg.page_solver = page_solver

    monkeypatch.setitem(sys.modules, "captchakraken", pkg)
    monkeypatch.setitem(sys.modules, "captchakraken.page_solver", page_solver)
    return seen


def test_client_tag_shape():
    tag = client_tag()
    assert tag.startswith("camoufox/")
    # The PACKAGE version, never the browser version range from
    # camoufox.__version__ -- a Firefox build range here makes the audit
    # trail unreadable. "unknown" is the honest source-checkout answer.
    assert tag != "camoufox/"


def test_importing_camoufox_does_not_import_captchakraken():
    # The whole point of the lazy resolve: the extra is optional.
    assert "captchakraken" not in sys.modules


def test_tag_is_set_during_the_solve(fake_captchakraken):
    solve_captcha("page-handle")
    assert fake_captchakraken["client"] == client_tag()


def test_arguments_are_forwarded(fake_captchakraken):
    result = solve_captcha("page-handle", api_key="k", config={"x": 1})
    assert result is _SENTINEL
    assert fake_captchakraken["page"] == "page-handle"
    assert fake_captchakraken["kwargs"] == {"api_key": "k", "config": {"x": 1}}


def test_env_is_restored_when_previously_unset(monkeypatch, fake_captchakraken):
    monkeypatch.delenv(_CLIENT_ENV, raising=False)
    solve_captcha("page-handle")
    assert _CLIENT_ENV not in os.environ


def test_env_is_restored_to_its_previous_value(monkeypatch, fake_captchakraken):
    monkeypatch.setenv(_CLIENT_ENV, "someone-else/1.0")
    solve_captcha("page-handle")
    # Overwritten for the solve (attribution may not be redirected), and put
    # back on the way out so it never leaks into unrelated code.
    assert fake_captchakraken["client"] == client_tag()
    assert os.environ[_CLIENT_ENV] == "someone-else/1.0"


def test_env_is_restored_even_when_the_solve_raises(monkeypatch):
    monkeypatch.delenv(_CLIENT_ENV, raising=False)

    def boom(page, **kwargs):
        raise RuntimeError("no captcha here")

    pkg = types.ModuleType("captchakraken")
    page_solver = types.ModuleType("captchakraken.page_solver")
    page_solver.solve_captcha_on_page = boom
    pkg.page_solver = page_solver
    monkeypatch.setitem(sys.modules, "captchakraken", pkg)
    monkeypatch.setitem(sys.modules, "captchakraken.page_solver", page_solver)

    with pytest.raises(RuntimeError):
        solve_captcha("page-handle")
    assert _CLIENT_ENV not in os.environ


def test_missing_dependency_raises_a_useful_error(monkeypatch):
    monkeypatch.setitem(sys.modules, "captchakraken", None)
    with pytest.raises(CaptchaSolverUnavailable) as excinfo:
        solve_captcha("page-handle")
    assert "camoufox[captcha]" in str(excinfo.value)


# ── the `captcha=` launch option ────────────────────────────────────────────


@pytest.fixture(autouse=True)
def _clear_registered_config():
    """Each test starts with no launch-time config registered."""
    configure(None)
    yield
    configure(None)


def test_a_token_alone_resolves_to_the_hosted_service():
    # The rule the option exists for. CaptchaKraken's own fallback would send
    # this user to localhost, where nothing is listening.
    cfg = resolve_config({"token": "ck_live_abc"})
    assert cfg.base_url == HOSTED_BASE_URL
    assert cfg.api_key == "ck_live_abc"
    assert cfg.is_hosted


def test_a_url_alone_stays_self_hosted():
    cfg = resolve_config({"url": "http://localhost:8000/v1"})
    assert cfg.base_url == "http://localhost:8000/v1"
    assert cfg.api_key is None
    assert not cfg.is_hosted


def test_an_explicit_url_is_not_overridden_by_a_token():
    cfg = resolve_config({"token": "ck_live_abc", "url": "http://gpu.internal:8000/v1"})
    assert cfg.base_url == "http://gpu.internal:8000/v1"
    assert cfg.api_key == "ck_live_abc"


@pytest.mark.parametrize(
    "value, expected",
    [
        ("ck_live_abc", CaptchaConfig(HOSTED_BASE_URL, "ck_live_abc", None)),
        ("https://gpu.example/v1", CaptchaConfig("https://gpu.example/v1", None, None)),
    ],
)
def test_the_string_shorthand_tells_a_key_from_an_endpoint(value, expected):
    assert resolve_config(value) == expected


def test_true_defers_to_the_environment():
    # Nothing resolved here: CaptchaKraken's own env/credentials-file lookup wins.
    assert resolve_config(True) == CaptchaConfig(None, None, None)


def test_neither_a_token_nor_a_url_is_an_error():
    with pytest.raises(CaptchaCredentialsError) as excinfo:
        resolve_config({})
    assert "token" in str(excinfo.value) and "url" in str(excinfo.value)


def test_a_misspelled_option_is_reported_not_ignored():
    # Silently dropping `tokne=` would send unauthenticated requests to
    # localhost and report it as connection-refused.
    with pytest.raises(CaptchaCredentialsError) as excinfo:
        resolve_config({"tokne": "ck_live_abc"})
    assert "tokne" in str(excinfo.value)


def test_configure_registers_and_clears():
    assert configured() is None
    configure({"token": "ck_live_abc"})
    assert configured().api_key == "ck_live_abc"
    configure(None)
    assert configured() is None


# ── picking a served adapter ────────────────────────────────────────────────


def test_a_model_can_be_named_alongside_credentials():
    cfg = resolve_config({"token": "ck_live_abc", "model": "captcha-v12"})
    assert cfg.model == "captcha-v12"
    assert cfg.base_url == HOSTED_BASE_URL


def test_a_model_alone_is_valid():
    # "endpoint and key are already in my env, just use a different adapter".
    assert resolve_config({"model": "captcha-v12"}) == CaptchaConfig(None, None, "captcha-v12")


# ── what reaches the solver ─────────────────────────────────────────────────


def test_the_launch_option_reaches_the_solver(monkeypatch, fake_captchakraken):
    for var in (_BASE_URL_ENV, _API_KEY_ENV, _MODEL_ENV):
        monkeypatch.delenv(var, raising=False)

    def record(page, **kwargs):
        fake_captchakraken["base_url"] = os.environ.get(_BASE_URL_ENV)
        fake_captchakraken["api_key"] = os.environ.get(_API_KEY_ENV)
        fake_captchakraken["model"] = os.environ.get(_MODEL_ENV)
        return None

    sys.modules["captchakraken.page_solver"].solve_captcha_on_page = record

    configure({"token": "ck_live_abc", "model": "captcha-v12"})
    solve_captcha("page-handle")
    assert fake_captchakraken["base_url"] == HOSTED_BASE_URL
    assert fake_captchakraken["api_key"] == "ck_live_abc"
    assert fake_captchakraken["model"] == "captcha-v12"

    # ...and gone again afterwards, so it cannot leak into other code.
    for var in (_BASE_URL_ENV, _API_KEY_ENV, _MODEL_ENV):
        assert var not in os.environ


def test_an_explicit_env_var_beats_the_launch_option(monkeypatch, fake_captchakraken):
    # config.base_url() documents env as the highest-precedence source; a
    # launcher that overrode it would silently redirect a self-hoster.
    monkeypatch.setenv(_BASE_URL_ENV, "http://my-own-box:8000/v1")

    def record(page, **kwargs):
        fake_captchakraken["base_url"] = os.environ.get(_BASE_URL_ENV)
        return None

    sys.modules["captchakraken.page_solver"].solve_captcha_on_page = record

    configure({"token": "ck_live_abc"})
    solve_captcha("page-handle")
    assert fake_captchakraken["base_url"] == "http://my-own-box:8000/v1"


# ---------------------------------------------------------------------------
# watch_captcha — the auto-solve listener
# ---------------------------------------------------------------------------
#
# The listener's own loop is tested in CaptchaKraken (python/tests/test_watcher.py).
# What belongs HERE is the one thing this bridge adds: the environment a solve
# runs under. A watcher solves LATER, long after `watch_captcha` returned, so
# the naive implementation — building it inside `_applied(...)` — would restore
# the tag and the key before any captcha ever appeared, and every solve would go
# out unattributed and, for a hosted user, unauthenticated.


@pytest.fixture
def clean_env(monkeypatch):
    """No managed variable set, so "was it applied?" is a real question."""
    for name in (_CLIENT_ENV, _API_KEY_ENV, _BASE_URL_ENV, _MODEL_ENV):
        monkeypatch.delenv(name, raising=False)


class _RecordingSolver:
    """Stands in for `PageSolver`, recording the environment at each call."""

    def __init__(self) -> None:
        self.seen = []

    def detect_captcha(self, page):
        self.seen.append(("detect", os.environ.get(_CLIENT_ENV)))
        return object()

    def solve(self, page):
        self.seen.append(
            ("solve", os.environ.get(_CLIENT_ENV), os.environ.get(_API_KEY_ENV))
        )
        return {"is_solved": True}


def test_tagged_solver_applies_the_attribution_tag_at_solve_time(clean_env):
    from camoufox.captcha import _TaggedSolver

    inner = _RecordingSolver()
    tagged = _TaggedSolver(inner, resolve_config({"token": "ck_live_abc"}))

    # Nothing applied yet — this is the state a background watcher idles in.
    assert os.environ.get(_CLIENT_ENV) is None

    tagged.solve(object())

    kind, tag, key = inner.seen[-1]
    assert kind == "solve"
    assert tag == client_tag(), "the solve ran without the attribution tag: revenue share breaks silently"
    assert key == "ck_live_abc", "the launch-time key was not in scope for the solve"


def test_tagged_solver_restores_the_environment_after_each_solve(clean_env):
    from camoufox.captcha import _TaggedSolver

    tagged = _TaggedSolver(_RecordingSolver(), resolve_config({"token": "ck_live_abc"}))
    tagged.solve(object())

    assert os.environ.get(_CLIENT_ENV) is None, "the tag leaked out of the solve"
    assert os.environ.get(_API_KEY_ENV) is None, "the key leaked into the wider process"


def test_tagged_solver_applies_the_tag_on_every_solve_not_just_the_first(clean_env):
    from camoufox.captcha import _TaggedSolver

    inner = _RecordingSolver()
    tagged = _TaggedSolver(inner, resolve_config({"token": "ck_live_abc"}))

    for _ in range(3):
        tagged.solve(object())

    tags = [tag for kind, tag, _ in inner.seen if kind == "solve"]
    assert tags == [client_tag()] * 3, f"attribution dropped after the first solve: {tags}"


def test_detection_does_not_need_the_tag(clean_env):
    """Detection is pure DOM reads — no request leaves the process."""
    from camoufox.captcha import _TaggedSolver

    inner = _RecordingSolver()
    _TaggedSolver(inner, resolve_config({"token": "ck_live_abc"})).detect_captcha(object())

    assert inner.seen == [("detect", None)]


def test_watch_captcha_raises_a_helpful_error_without_the_optional_dependency(clean_env):
    """
    No monkeypatching: `captchakraken` really is absent from this environment —
    that is why every other test here stubs it — so the genuine
    ModuleNotFoundError path is what runs. Stubbing `sys.modules[x] = None`
    would raise plain ImportError instead and test a branch that cannot happen.
    """
    assert "captchakraken" not in sys.modules, "a leaked stub would make this test vacuous"

    with pytest.raises(CaptchaSolverUnavailable) as excinfo:
        watch_captcha(object())
    assert "camoufox[captcha]" in str(excinfo.value)


def test_watch_captcha_installs_a_tagged_solver(monkeypatch, clean_env):
    """
    The end-to-end version of the `_TaggedSolver` cases above: it is not enough
    that the wrapper works, the watcher has to be holding it. A watcher built
    around the bare `PageSolver` would solve untagged every time, and -- for a
    hosted user, whose key is registered the same way -- unauthenticated.
    """
    seen = []

    class _FakePageSolver:
        def __init__(self, config=None):
            self.config = config

        def solve(self, page):
            seen.append(os.environ.get(_CLIENT_ENV))
            return _SENTINEL

        def detect_captcha(self, page):
            return None

    class _FakeWatcher:
        def __init__(self, solver, page, **options):
            self.solver = solver
            self.page = page
            self.options = options

    pkg = types.ModuleType("captchakraken")
    page_solver = types.ModuleType("captchakraken.page_solver")
    page_solver.PageSolver = _FakePageSolver
    watcher_mod = types.ModuleType("captchakraken.watcher")
    watcher_mod.CaptchaWatcher = _FakeWatcher
    pkg.page_solver = page_solver
    pkg.watcher = watcher_mod
    monkeypatch.setitem(sys.modules, "captchakraken", pkg)
    monkeypatch.setitem(sys.modules, "captchakraken.page_solver", page_solver)
    monkeypatch.setitem(sys.modules, "captchakraken.watcher", watcher_mod)

    configure("ck_live_abc")
    page = object()
    watcher = watch_captcha(page, interval_ms=10_000)

    assert watcher.page is page
    assert watcher.options == {"interval_ms": 10_000}
    # Installing is not solving: nothing applied, nothing billed.
    assert seen == []
    assert os.environ.get(_CLIENT_ENV) is None

    # Now drive it the way the real poll loop would.
    watcher.solver.solve(page)
    assert seen == [client_tag()]
    # And back out again once that solve unwound.
    assert os.environ.get(_CLIENT_ENV) is None


def test_watch_captcha_says_so_when_the_installed_captchakraken_is_too_old(monkeypatch, clean_env):
    """
    2.3.0 through 2.4.0 carry the page driver but no `captchakraken.watcher`:
    installed, importable, and still unable to watch. Sending that user to
    `pip install "camoufox[captcha]"` would have them hunting for a package
    they already have, so the message names the upgrade instead -- and says
    which half still works, because `solve_captcha` is unaffected.
    """

    def solve_captcha_on_page(page, **kwargs):
        return _SENTINEL

    pkg = types.ModuleType("captchakraken")
    page_solver = types.ModuleType("captchakraken.page_solver")
    page_solver.PageSolver = object
    page_solver.solve_captcha_on_page = solve_captcha_on_page
    pkg.page_solver = page_solver
    monkeypatch.setitem(sys.modules, "captchakraken", pkg)
    monkeypatch.setitem(sys.modules, "captchakraken.page_solver", page_solver)
    # captchakraken.watcher deliberately absent, as in every release before 2.6.0.

    with pytest.raises(CaptchaSolverUnavailable) as excinfo:
        watch_captcha(object())

    message = str(excinfo.value)
    assert "2.6.0" in message
    assert "--upgrade" in message
    # The claim the message makes about the other half has to be true.
    assert solve_captcha(object()) is _SENTINEL

