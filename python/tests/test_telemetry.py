"""Anti-bot telemetry: on unless turned off, reports what the defaults file says
it does, watches every context of a launch, and never breaks one."""

import asyncio
from types import SimpleNamespace
from importlib.metadata import version as package_version

import pytest

from camoufox import async_api, sync_api, telemetry, utils


class Emitter:
    def __init__(self):
        self.handlers = {}

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def emit(self, event, *args):
        for handler in self.handlers.get(event, []):
            handler(*args)


class FakeContext(Emitter):
    pages = []
    browser = None

    def new_page(self):
        pass


class FakeAsyncContext(FakeContext):
    async def new_page(self):
        pass


class FakePage:
    def __init__(self, context):
        self.context = context


class FakeBrowser(Emitter):
    version = '150.0.2'
    contexts = []

    def new_context(self, **kwargs):
        return FakeContext()

    def new_page(self, **kwargs):
        return FakePage(FakeContext())

    def close(self):
        self.emit('disconnected')


class FakeAsyncBrowser(FakeBrowser):
    async def new_context(self, **kwargs):
        return FakeAsyncContext()

    async def new_page(self, **kwargs):
        return FakePage(FakeAsyncContext())


@pytest.fixture
def sent(monkeypatch):
    """Turn telemetry on, as it is outside this suite, and keep what it sends."""
    monkeypatch.delenv('CAMOUFOX_TELEMETRY')
    monkeypatch.delenv('DO_NOT_TRACK', raising=False)
    payloads, built = [], []

    class Recording(telemetry.Telemetry):
        def __init__(self, product, **dimensions):
            super().__init__(product, **dimensions, send=lambda url, payload: payloads.append(payload), interval=0)
            built.append(self)

    monkeypatch.setattr(telemetry, 'Telemetry', Recording)
    monkeypatch.setattr(utils, '_firefox_version', lambda path: '150.0.2')
    return SimpleNamespace(payloads=payloads, built=built)


OPTIONS = {
    'executable_path': '/nonexistent/camoufox-bin',
    'env': {'CAMOU_CONFIG_1': '{"navigator.userAgent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:150.0)"}'},
    'proxy': {'server': 'http://user:pass@gate.proxy.example.co.uk:8080'},
}


def test_its_dimensions(sent, monkeypatch):
    monkeypatch.setattr(utils, '_host_os_key', lambda: 'lin')
    reporter = telemetry.start(OPTIONS, None, passthrough=False)
    assert reporter.dimensions == {
        'product': 'camoufox', 'version': package_version('camoufox'), 'browser_version': '150.0.2',
        'pro': False, 'cross_os': True, 'passthrough': False, 'proxy_provider': 'example.co.uk',
    }
    reporter.close()


@pytest.mark.parametrize('setting, env', [
    (False, {}), (None, {'CAMOUFOX_TELEMETRY': '0'}), (None, {'DO_NOT_TRACK': '1'}),
])
def test_each_way_to_turn_it_off(sent, monkeypatch, setting, env):
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    assert telemetry.start(OPTIONS, setting, passthrough=False) is None


def test_a_failure_to_set_it_up_leaves_the_launch_without_it(sent, monkeypatch):
    monkeypatch.setattr(utils, '_firefox_version', lambda path: 1 / 0)
    assert telemetry.start(OPTIONS, None, passthrough=False) is None


def test_the_suite_runs_with_it_off():
    assert telemetry.start(OPTIONS, None, passthrough=False) is None


def watched(context):
    return {'response', 'page', 'close'} <= set(context.handlers)


def test_a_browser_has_every_context_watched_and_sends_at_close(sent):
    browser = FakeBrowser()
    reporter = telemetry.start(OPTIONS, None, passthrough=False)
    telemetry.attach(browser, reporter, is_async=False)
    assert watched(browser.new_context()) and watched(browser.new_page().context)

    reporter.tally.observe(1, 'example.com', ('cloudflare', 'block', False, True))
    browser.close()
    assert sent.payloads == [reporter.payload({'cloudflare': {'success': 0, 'challenge': 0, 'block': 1, 'unknown': 0}})]


def test_an_async_browser_has_every_context_watched(sent):
    browser = FakeAsyncBrowser()
    telemetry.attach(browser, telemetry.start(OPTIONS, None, passthrough=False), is_async=True)

    async def contexts():
        return await browser.new_context(), (await browser.new_page()).context

    assert all(watched(context) for context in asyncio.run(contexts()))


def test_a_persistent_context_is_watched_and_sends_at_close(sent):
    context = FakeContext()
    reporter = telemetry.start(OPTIONS, None, passthrough=False)
    telemetry.attach(context, reporter, is_async=False)
    assert watched(context)
    context.emit('close')
    assert reporter._closed


def test_an_error_while_attaching_does_not_break_the_launch(sent):
    reporter = telemetry.start(OPTIONS, None, passthrough=False)
    telemetry.attach(object(), reporter, is_async=False)
    assert reporter._closed


def launched(monkeypatch, browser):
    monkeypatch.setattr(sync_api, 'launch_options', lambda **kwargs: dict(OPTIONS))
    monkeypatch.setattr(async_api, 'launch_options', lambda **kwargs: dict(OPTIONS))
    monkeypatch.setattr(sync_api, 'host_identity', lambda *args: None)
    monkeypatch.setattr(async_api, 'host_identity', lambda *args: None)

    class Firefox:
        def launch(self, **options):
            return browser

    class AsyncFirefox:
        async def launch(self, **options):
            return browser

    class Playwright:
        firefox = Firefox()

    class AsyncPlaywright:
        firefox = AsyncFirefox()

    return Playwright(), AsyncPlaywright()


def test_new_browser_reports_on_its_browser(sent, monkeypatch):
    browser = FakeBrowser()
    playwright, _ = launched(monkeypatch, browser)
    sync_api.NewBrowser(playwright, headless=True, passthrough=True)
    assert 'disconnected' in browser.handlers
    assert sent.built[0].dimensions['passthrough'] is True
    assert browser._camoufox_passthrough is True


def test_async_new_browser_reports_on_its_browser(sent, monkeypatch):
    browser = FakeAsyncBrowser()
    _, playwright = launched(monkeypatch, browser)
    asyncio.run(async_api.AsyncNewBrowser(playwright, headless=True))
    assert 'disconnected' in browser.handlers
    assert sent.built[0].dimensions['passthrough'] is False


def test_telemetry_false_launches_without_it(sent, monkeypatch):
    browser = FakeBrowser()
    playwright, _ = launched(monkeypatch, browser)
    sync_api.NewBrowser(playwright, headless=True, telemetry=False)
    assert not sent.built and 'disconnected' not in browser.handlers
