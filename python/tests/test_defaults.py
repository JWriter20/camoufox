"""camoufox.defaults.json: one file, at the repository root and in the package,
that every launch starts from, and that the user's own copy overlays."""

import inspect
import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from camoufox import defaults, utils
from camoufox.__main__ import cli
from camoufox.addons import DefaultAddons
from camoufox.fingerprints import Screen

ROOT_FILE = Path(__file__).resolve().parents[2] / 'camoufox.defaults.json'
# Launch arguments that are not defaults: a secret, an internal value, and the
# Pro pool warmer's own.
NOT_DEFAULTS = {'pro_key', 'virtual_display', 'pool_profile'}


def entries():
    return {k: v for k, v in json.loads(defaults.DEFAULTS_FILE.read_text()).items() if not k.startswith('$')}


@pytest.fixture
def user_file(tmp_path, monkeypatch):
    path = tmp_path / 'defaults.json'
    monkeypatch.setenv('CAMOUFOX_DEFAULTS', str(path))
    return path


def test_the_packaged_file_is_the_repository_file():
    assert defaults.DEFAULTS_FILE.read_bytes() == ROOT_FILE.read_bytes()


def test_every_launch_option_has_a_default_and_says_what_it_does():
    options = {
        name for name, parameter in inspect.signature(inspect.unwrap(utils.launch_options)).parameters.items()
        if parameter.kind is inspect.Parameter.KEYWORD_ONLY
    }
    assert options - NOT_DEFAULTS <= set(entries())
    assert set(entries()) - options == {'telemetry', 'persistent_context', 'gpu', 'require_gpu'}
    for name, entry in entries().items():
        assert entry['about'] and entry['examples'], name


def test_the_telemetry_entry_says_what_is_sent_and_how_to_stop_it():
    about = entries()['telemetry']['about']
    for words in ('Collected', 'Never collected', 'Used for', 'CAMOUFOX_TELEMETRY=0', 'DO_NOT_TRACK=1',
                  'success', 'challenge', 'block', 'unknown', 'domain only'):
        assert words in about


def test_the_users_file_overlays_the_packaged_one_and_the_caller_wins(user_file):
    user_file.write_text(json.dumps({'telemetry': False, 'block_images': True, 'headless': 'virtual'}))
    filled = defaults.with_defaults({'headless': True})
    assert filled['headless'] is True
    assert filled['telemetry'] is False and filled['block_images'] is True
    assert filled['passthrough'] is False


def test_a_null_default_leaves_the_option_out(user_file):
    assert 'proxy' not in defaults.with_defaults({})


def test_only_the_named_options_are_filled():
    assert set(defaults.with_defaults({}, {'debug'})) == {'debug'}


def test_launch_options_takes_the_defaults(user_file, monkeypatch):
    user_file.write_text(json.dumps({'passthrough': True}))
    seen = {}
    monkeypatch.setattr(utils, '_passthrough_options', lambda **kwargs: seen.update(kwargs))
    monkeypatch.setattr(utils, 'launch_path', lambda **kwargs: '/nonexistent/camoufox')
    utils.launch_options(headless=True)
    assert seen['headless'] is True


@pytest.mark.parametrize('content', [{'no_such_option': 1}, {'block_images': 'yes'}])
def test_a_wrong_users_file_raises(user_file, content):
    user_file.write_text(json.dumps(content))
    with pytest.raises(ValueError):
        defaults.with_defaults({})


def test_json_values_become_what_the_option_takes(user_file):
    user_file.write_text(json.dumps({
        'screen': {'max_width': 1920, 'max_height': 1080},
        'exclude_addons': ['UBO'],
        'window': [1280, 800],
    }))
    filled = defaults.with_defaults({})
    assert filled['screen'] == Screen(max_width=1920, max_height=1080)
    assert filled['exclude_addons'] == [DefaultAddons.UBO]
    assert filled['window'] == (1280, 800)


def test_the_cli_writes_the_users_copy_once(user_file):
    result = CliRunner().invoke(cli, ['defaults'])
    assert result.exit_code == 0, result.output
    assert result.output.strip() == str(user_file)
    assert user_file.read_bytes() == defaults.DEFAULTS_FILE.read_bytes()

    user_file.write_text('{"telemetry": false}')
    CliRunner().invoke(cli, ['defaults'])
    assert user_file.read_text() == '{"telemetry": false}'
