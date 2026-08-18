"""The `captcha=` kwarg must be consumed at launch, not forwarded to Playwright."""
import pytest

from camoufox import sync_api
from camoufox.captcha import HOSTED_BASE_URL, configure, configured


class _FakeFirefox:
    def __init__(self): self.launch_kwargs = None
    def launch(self, **kw):
        self.launch_kwargs = kw
        return object()


class _FakePlaywright:
    def __init__(self): self.firefox = _FakeFirefox()


@pytest.fixture(autouse=True)
def _clean():
    configure(None); yield; configure(None)


def test_captcha_is_consumed_and_not_passed_to_launch_options(monkeypatch):
    seen = {}

    def fake_launch_options(**kwargs):
        seen.update(kwargs)
        return {"executable_path": "/nonexistent"}

    monkeypatch.setattr(sync_api, "launch_options", fake_launch_options)
    monkeypatch.setattr(sync_api, "spoofs_window_dimensions", lambda _o: False)

    pw = _FakePlaywright()
    sync_api.NewBrowser(pw, captcha={"token": "ck_live_xyz"})

    # Registered for later solves...
    assert configured().api_key == "ck_live_xyz"
    assert configured().base_url == HOSTED_BASE_URL
    # ...and never handed to launch_options or Playwright, which would reject it.
    assert "captcha" not in seen
    assert "captcha" not in (pw.firefox.launch_kwargs or {})
