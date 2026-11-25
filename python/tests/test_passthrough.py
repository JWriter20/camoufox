"""passthrough=True: the browser presents this machine, so nothing is drawn or
injected, and the behaviour options still apply."""

import warnings
from unittest import mock

import pytest
from camoucore.passthrough import IDENTITY_OPTIONS

from camoufox import pro, pro_update, sync_api, utils
from camoufox.virtdisplay import SCREEN_ENV_VAR

from test_identity_salt import config_of, host


def passthrough(**kwargs):
    kwargs.setdefault('headless', True)
    with host():
        return utils.launch_options(passthrough=True, exclude_addons=None, **kwargs)


def test_no_identity_reaches_the_browser():
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        options = passthrough(i_know_what_im_doing=True)
    assert config_of(options) == {}
    assert 'FONTCONFIG_FILE' not in options['env']
    assert options['firefox_user_prefs'] == {}


def test_behaviour_options_still_apply():
    with warnings.catch_warnings():
        warnings.simplefilter('ignore')
        options = passthrough(
            block_images=True, main_world_eval=True, i_know_what_im_doing=True,
            proxy={'server': 'http://proxy.example.com:8080'},
        )
    assert config_of(options) == {'allowMainWorld': True}
    assert options['firefox_user_prefs']['permissions.default.image'] == 2
    assert options['proxy']['server'] == 'http://proxy.example.com:8080'


def test_a_proxy_warns_that_the_machine_shows_through():
    with pytest.warns(Warning, match='passthrough'):
        passthrough(proxy={'server': 'http://proxy.example.com:8080'})


@pytest.mark.parametrize('option, value', [
    ('os', 'windows'), ('locale', 'en-US'), ('geoip', True), ('window', (1280, 800)),
    ('config', {'navigator.platform': 'Win32'}), ('ff_version', 140), ('display_profile', '/x.icc'),
])
def test_an_identity_option_raises(option, value):
    with pytest.raises(ValueError, match='passthrough'):
        passthrough(**{option: value})


def test_every_identity_option_camoufox_takes_is_checked():
    # Those camoucore names that are another launcher's (chrome_version) aside.
    checked = {'config', 'os', 'fingerprint', 'fingerprint_preset', 'webgl_config', 'cpu', 'screen', 'window',
               'fonts', 'custom_fonts_only', 'locale', 'geoip', 'canvas_noise', 'pin_cpu_cores', 'ff_version'}
    assert checked == set(IDENTITY_OPTIONS) - {'allow_os_mismatch', 'chrome_version'}


def test_a_pro_build_refuses_it(monkeypatch):
    monkeypatch.setattr(pro, 'read_build', lambda path: object())
    monkeypatch.setattr(pro_update, 'newest', lambda build, binary, key: binary)
    with pytest.raises(ValueError, match='standard build'):
        passthrough()


def test_its_virtual_display_is_a_common_screen(monkeypatch):
    monkeypatch.delenv(SCREEN_ENV_VAR, raising=False)
    started = []
    monkeypatch.setattr(utils, 'VirtualDisplay', lambda debug, screen: started.append(screen) or mock.Mock())
    utils.start_virtual_display('virtual', False, {'passthrough': True})
    utils.start_virtual_display('virtual', False, {})
    assert started == [utils.PASSTHROUGH_SCREEN, None]


def test_its_contexts_claim_nothing():
    browser = mock.Mock(_camoufox_passthrough=True)
    sync_api.NewContext(browser, proxy={'server': 'http://proxy.example.com:8080'}, locale='fr-FR')
    browser.new_context.assert_called_once_with(locale='fr-FR', proxy={'server': 'http://proxy.example.com:8080'})
    with pytest.raises(ValueError, match='os'):
        sync_api.NewContext(browser, os='windows')
