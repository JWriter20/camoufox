"""A viewport the caller asks for sits on a screen and in a window that can hold it.

Playwright's viewport= makes Juggler size the page, not the window, and the
page then reports innerWidth/innerHeight from the viewport, outerWidth/
outerHeight from the browser's real window and screen.* from the identity. A
1000x700 viewport on a drawn 1366x768 Linux screen read innerHeight 700 with
availHeight 768, where a real Firefox window draws 86 px of chrome above the
page: a machine that cannot exist, and the recorded 1366x768 Firefox windows
never show a page taller than 683 px.

The launchers give such a context a recorded screen that holds the viewport in
the recorded chrome, through the per-context screen setter, draw a persistent
launch around the viewport it is given, and warn where only the window could
make it consistent (set_viewport_size(), a page larger than its window). The
browser side is browser/tests/playwright/patches/viewport-fits-screen.py.
"""

import asyncio
import collections
import json
import warnings
from contextlib import contextmanager
from unittest import mock

import orjson
import pytest

from camoufox import fingerprints as fp
from camoufox import utils
from camoufox._warnings import LeakWarning

LINUX_1366 = {'os': 'lin', 'screen': (1366, 768), 'window': {'x': 0, 'y': 0, 'width': 1366, 'height': 768}}
VIEWPORT = {'width': 1000, 'height': 700}


def _recorded_firefox_windows():
    """(os, screen, window) for every Firefox (screen, window) pair fpgen's
    network records: the window node's conditional table under its parents."""
    from camoufox.fpgen_model import load_fpgen

    load_fpgen()
    from fpgen.unpacker import lookup_value_list
    from fpgen.utils import NETWORK

    def decode(name):
        node = NETWORK.nodes_by_name[name]
        return node, dict(zip(node.possible_values, lookup_value_list(node.possible_values)))

    _, browsers = decode('browser')
    os_node, oses = decode('os')
    _, screens = decode('screen')
    window_node, windows = decode('window')
    assert window_node.parent_names[:3] == ['browser', 'platformInfo', 'screen']

    def walk(table, depth, keys):
        if depth == len(window_node.parent_names):
            yield from ((keys, value) for value, p in table.items() if p > 0)
            return
        for key, sub in table.items():
            yield from walk(sub, depth + 1, keys + [key])

    for (browser, platform, screen, _), window in walk(window_node.probabilities, 0, []):
        if json.loads(browsers[browser]) != 'Firefox':
            continue
        os_table = os_node.probabilities[browser][platform]
        os_name = json.loads(oses[max(os_table, key=os_table.get)])
        yield os_name, json.loads(screens[screen]), json.loads(windows[window])


@pytest.fixture(scope='module')
def recorded():
    return list(_recorded_firefox_windows())


class TestTheRuleComesFromTheRecords:
    OS_KEY = {'Linux': 'lin', 'MacOS': 'mac', 'Windows': 'win'}

    def test_recorded_window_chrome_is_the_models(self, recorded):
        chrome = collections.defaultdict(lambda: ([], []))
        for os_name, _, window in recorded:
            if window['outerHeight'] > window['innerHeight']:  # a window with a toolbar
                for axis, values in zip(('Width', 'Height'), chrome[self.OS_KEY[os_name]]):
                    values.append(window[f'outer{axis}'] - window[f'inner{axis}'])
        derived = {}
        for key, axes in chrome.items():
            derived[key] = []
            for values in axes:
                counts = collections.Counter(values)
                mode = max(counts, key=lambda v: (counts[v], -v))
                derived[key].append(max(v for v in counts if abs(v - mode) <= 2))
        assert derived == fp.RECORDED_WINDOW_CHROME

    def test_no_1366x768_record_shows_a_700_px_page(self, recorded):
        heights = [w['innerHeight'] for _, s, w in recorded if (s['width'], s['height']) == (1366, 768)]
        assert heights and max(heights) < 700

    def test_no_record_shows_a_page_larger_than_its_window(self, recorded):
        for _, _, window in recorded:
            assert window['innerWidth'] <= window['outerWidth']
            assert window['innerHeight'] <= window['outerHeight']


class TestScreenFloor:
    def test_viewport_in_the_recorded_chrome_plus_the_taskbar(self):
        assert fp.screen_floor('lin', VIEWPORT) == (1002, 786)
        assert fp.screen_floor('win', VIEWPORT) == (1018, 830 + 48)
        assert fp.screen_floor('mac', VIEWPORT) == (1000, 786 + 76)

    def test_the_window_has_to_be_on_the_screen_too(self):
        window = {'x': 100, 'y': 20, 'width': 1300, 'height': 700}
        assert fp.screen_floor('lin', VIEWPORT, window) == (1400, 786)
        assert fp.screen_floor('lin', None, window) == (1400, 720)

    def test_a_drawn_screen_holds_the_floor(self):
        for os_key in ('lin', 'win', 'mac'):
            for _ in range(20):
                width, height = fp.draw_screen(os_key, (1300, 1000))
                assert width >= 1300 and height >= 1000

    def test_the_width_bound_holds_with_the_height_bound(self):
        # fpgen gives screen.width and screen.height one node; a predicate per
        # field let the height one replace the width one, and 1280x1024 met
        # min_width 1300 in about half these draws.
        bound = fp.Screen(min_width=1300, min_height=1000, max_height=1024)
        conditions = bound.as_conditions()
        assert not conditions['screen']({'width': 1280, 'height': 1024})
        for _ in range(60):
            screen = fp._generator().generate(
                browser='Firefox', os=fp._FPGEN_OS['win'], target='screen', **conditions
            )
            assert screen['width'] >= 1300 and 1000 <= screen['height'] <= 1024

    def test_no_recorded_screen_that_large(self):
        assert fp.draw_screen('lin', (20000, 20000)) is None


def _no_warnings(fn, *args):
    with warnings.catch_warnings():
        warnings.simplefilter('error', LeakWarning)
        return fn(*args)


class TestFitContextScreen:
    def test_a_viewport_too_tall_for_the_screen_gets_a_screen_that_holds_it(self):
        screen = _no_warnings(utils.fit_context_screen, LINUX_1366, {'viewport': VIEWPORT})
        assert screen is not None
        assert screen[0] >= 1366 and screen[1] >= 786  # the window, and the viewport in its chrome

    def test_a_screen_that_holds_it_stays(self):
        geometry = {**LINUX_1366, 'screen': (1920, 1080)}
        assert _no_warnings(utils.fit_context_screen, geometry, {'viewport': VIEWPORT}) is None

    def test_no_viewport_is_the_window_the_screen_already_holds(self):
        assert _no_warnings(utils.fit_context_screen, LINUX_1366, {'noViewport': True}) is None

    def test_playwrights_default_viewport_counts_as_asked_for(self):
        geometry = {'os': 'lin', 'screen': (1280, 720), 'window': None}
        screen = _no_warnings(utils.fit_context_screen, geometry, {'viewport': None})
        assert screen[0] >= 1282 and screen[1] >= 806

    def test_a_viewport_larger_than_the_window_warns(self):
        with pytest.warns(LeakWarning, match=r'1300x900 viewport is larger than the 1366x768 browser window'):
            screen = utils.fit_context_screen(LINUX_1366, {'viewport': {'width': 1300, 'height': 900}})
        assert screen[1] >= 986

    def test_the_callers_screen_option_is_claimed(self):
        options = {'viewport': VIEWPORT, 'screen': {'width': 2560, 'height': 1440}}
        assert _no_warnings(utils.fit_context_screen, LINUX_1366, options) == (2560, 1440)

    def test_a_callers_screen_that_cannot_hold_it_is_kept_and_warned_about(self):
        options = {'viewport': VIEWPORT, 'screen': {'width': 1366, 'height': 700}}
        with pytest.warns(LeakWarning, match=r'1000x700 viewport does not fit the 1366x700 screen'):
            assert utils.fit_context_screen(LINUX_1366, options) == (1366, 700)

    def test_no_screen_that_large_warns_and_leaves_the_viewport(self):
        options = {'viewport': {'width': 20000, 'height': 700}}
        with pytest.warns(LeakWarning, match='no recorded linux screen'):
            assert utils.fit_context_screen({**LINUX_1366, 'window': None}, options) is None


class _Context:
    def __init__(self):
        self.scripts = []

    async def add_init_script(self, script=None):
        self.scripts.append(script)


class _Browser:
    def __init__(self):
        self.calls = []

    async def new_context(self, **kwargs):
        self.calls.append(kwargs)
        return _Context()


class TestAttachViewportFit:
    def test_the_context_claims_the_fitted_screen(self):
        browser = utils.attach_viewport_fit(_Browser(), LINUX_1366)
        context = asyncio.run(browser.new_context(viewport=VIEWPORT))
        (script,) = context.scripts
        width, height = map(int, script.split('setScreenDimensions(')[1].rstrip(');').split(', '))
        assert width >= 1366 and height >= 786
        assert vars(context)[utils._GEOMETRY]['screen'] == (width, height)
        assert browser.calls == [{'viewport': VIEWPORT}]  # the viewport is the caller's

    def test_a_context_that_fits_gets_no_script(self):
        browser = utils.attach_viewport_fit(_Browser(), LINUX_1366)
        context = asyncio.run(browser.new_context(noViewport=True))
        assert context.scripts == []
        assert vars(context)[utils._GEOMETRY] == LINUX_1366

    def test_attaching_twice_checks_once(self):
        browser = utils.attach_viewport_fit(utils.attach_viewport_fit(_Browser(), LINUX_1366))
        context = asyncio.run(browser.new_context(viewport=VIEWPORT))
        assert len(context.scripts) == 1

    def test_a_handed_over_identity_is_what_the_context_claims(self):
        browser = utils.attach_viewport_fit(_Browser(), LINUX_1366)
        identity = {'config': {'navigator.platform': 'Win32'}, 'preset': {'screen': {'width': 1920, 'height': 1080}}}
        utils.hand_over_context_geometry(browser, identity)
        context = asyncio.run(browser.new_context(noViewport=True))
        assert context.scripts == []
        assert vars(context)[utils._GEOMETRY] == {'os': 'win', 'screen': (1920, 1080), 'window': LINUX_1366['window']}
        # Taken: the next context claims the launch's again.
        assert utils._NEXT_CONTEXT not in vars(browser)


class TestNewContextIdentity:
    def test_the_window_is_the_page_and_the_screen_holds_it(self):
        bound = fp.Screen(min_width=1600, min_height=1000)
        for _ in range(10):
            ident = fp.generate_context_fingerprint(os='linux', screen_bound=bound)
            opts, config = ident['context_options'], ident['config']
            # Playwright refuses deviceScaleFactor with a null viewport (system-ui-font-spoofing).
            assert not (opts.get('no_viewport') and 'device_scale_factor' in opts)
            if 'device_scale_factor' in opts:
                assert opts['viewport'] == {'width': int(config['window.innerWidth']),
                                            'height': int(config['window.innerHeight'])}
                assert opts['viewport']['width'] <= config['screen.width']
                assert opts['viewport']['height'] <= config['screen.height']
            else:
                assert opts['no_viewport'] is True and 'viewport' not in opts
            assert config['screen.width'] >= 1600 and config['screen.height'] >= 1000

    def test_a_callers_viewport_replaces_no_viewport(self):
        ident = {'context_options': {'no_viewport': True, 'user_agent': 'x'}}
        assert utils.new_context_options(ident, {'viewport': VIEWPORT}, None, None) == {
            'user_agent': 'x', 'viewport': VIEWPORT,
        }

    def test_the_bound_holds_the_window_and_the_viewport(self):
        browser = utils.attach_viewport_fit(_Browser(), LINUX_1366)
        bound = utils.context_screen_bound(browser, 'windows', {'viewport': VIEWPORT})
        assert (bound.min_width, bound.min_height) == (1366, 830 + 48)
        # Any OS: the largest floor.
        bound = utils.context_screen_bound(browser, None, {})
        assert (bound.min_width, bound.min_height) == (1366, 768 + 76)


def _options(config):
    blob = orjson.dumps(config).decode()
    return {'env': {f'CAMOU_CONFIG_{i // 7 + 1}': blob[i:i + 7] for i in range(0, len(blob), 7)}}


class TestLaunchGeometry:
    def test_read_from_the_chunked_config(self):
        config = {'navigator.userAgent': 'Mozilla/5.0 (X11; Linux x86_64; rv:150.0) Gecko/20100101 Firefox/150.0',
                  'screen.width': 1366, 'screen.height': 768, 'window.outerWidth': 1300,
                  'window.outerHeight': 700, 'window.screenX': 33, 'window.screenY': 0}
        assert utils.launch_geometry(_options(config)) == {
            'os': 'lin', 'screen': (1366, 768), 'window': {'x': 33, 'y': 0, 'width': 1300, 'height': 700}}

    def test_an_inner_size_is_the_window_less_its_chrome(self):
        config = {'navigator.userAgent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:150.0) Gecko/20100101 Firefox/150.0',
                  'screen.width': 1920, 'screen.height': 1080, 'window.innerWidth': 1200, 'window.innerHeight': 800}
        assert utils.launch_geometry(_options(config))['window'] == {'x': 0, 'y': 0, 'width': 1218, 'height': 930}

    def test_no_screen_claims_nothing(self):
        assert utils.launch_geometry(_options({'window.outerWidth': 1300})) is None
        assert utils.launch_geometry({}) is None


@contextmanager
def _host():
    with mock.patch.object(utils, 'get_screen_cons', lambda headless: None), (
        mock.patch.object(utils, 'has_display', lambda env: False)
    ), mock.patch.object(utils, 'installed_verstr', lambda: '150.0.2'), (
        mock.patch.object(utils, 'launch_path', lambda **kwargs: '/nonexistent/camoufox')
    ):
        yield


def _config(options):
    chunks = sorted((int(k.rsplit('_', 1)[1]), v) for k, v in options['env'].items() if k.startswith('CAMOU_CONFIG_'))
    return orjson.loads(''.join(v for _, v in chunks))


class TestPersistentLaunchIsDrawnAroundItsViewport:
    @pytest.mark.parametrize('os_name, key', [('linux', 'lin'), ('windows', 'win'), ('macos', 'mac')])
    def test_window_and_screen_hold_the_viewport(self, os_name, key):
        chrome_w, chrome_h = fp.RECORDED_WINDOW_CHROME[key]
        for _ in range(5):
            with _host(), warnings.catch_warnings():
                warnings.simplefilter('ignore')
                options = utils.launch_options(os=os_name, headless=True, i_know_what_im_doing=True, viewport=VIEWPORT)
            config = _config(options)
            assert options['viewport'] == VIEWPORT  # the caller's, unchanged
            assert (config['window.outerWidth'], config['window.outerHeight']) == (1000 + chrome_w, 700 + chrome_h)
            assert config['screen.availWidth'] >= 1000 + chrome_w
            assert config['screen.availHeight'] >= 700 + chrome_h
            assert config['window.screenX'] + config['window.outerWidth'] <= config['screen.width']

    def test_a_callers_screen_holds_the_window_the_launcher_placed(self):
        # The caller sets win-i9's screen and window size but no position: the generated screenX/Y,
        # drawn against a wider screen, put the window past the caller's edge (x=206 + 1280 > 1382)
        # in about half the draws, and every new context then drew a larger screen.
        caller = {'window.devicePixelRatio': 2.5, 'screen.width': 1382, 'screen.height': 864,
                  'screen.availWidth': 1382, 'screen.availHeight': 816, 'window.outerWidth': 1280,
                  'window.outerHeight': 816}
        for _ in range(12):
            with _host(), warnings.catch_warnings():
                warnings.simplefilter('ignore')
                options = utils.launch_options(os='windows', headless=True, i_know_what_im_doing=True,
                                               config=dict(caller))
            config = _config(options)
            assert {k: config[k] for k in caller} == caller  # the caller's, unchanged
            geometry = utils.launch_geometry(options)
            assert utils.fit_context_screen(geometry, {'noViewport': True}) is None, geometry
        with _host(), warnings.catch_warnings():
            warnings.simplefilter('ignore')
            options = utils.launch_options(os='windows', headless=True, i_know_what_im_doing=True,
                                           config={**caller, 'window.screenX': 300})
        assert _config(options)['window.screenX'] == 300  # a position the caller set is theirs

    def test_a_prebuilt_identity_that_cannot_hold_it_warns(self):
        options = _options({'screen.width': 1366, 'screen.height': 768, 'window.outerWidth': 1366,
                            'window.outerHeight': 768})
        options['viewport'] = VIEWPORT
        with pytest.warns(LeakWarning, match='does not fit the 1366x768 screen'):
            utils.attach_persistent_geometry(_Context(), options)


class TestSetViewportSize:
    def test_a_size_the_screen_cannot_hold_warns(self):
        with pytest.warns(LeakWarning, match='1000x700 viewport does not fit the 1366x768 screen'):
            utils.check_viewport_size(LINUX_1366, VIEWPORT)

    def test_a_size_that_fits_is_quiet(self):
        _no_warnings(utils.check_viewport_size, {**LINUX_1366, 'screen': (1920, 1080)}, VIEWPORT)
        _no_warnings(utils.check_viewport_size, None, VIEWPORT)
