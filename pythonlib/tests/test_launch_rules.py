"""What a build declares beside its binary is what the launcher applies.

A build can compile in a feature that only does something once a pref or an
environment variable is set at launch, or read a config key the stock build
does not have. The launcher learns both from the build itself -- launch.json
and properties.json beside the executable -- so a caller pointing
`executable_path` at such a build gets it configured, and every other build is
unaffected.
"""

import json

import pytest

from camoufox import fingerprints, utils

RULES = {
    "rules": [
        {"prefs": {"browser.sessionhistory.max_entries": 10}},
        {
            "target": ["win"],
            "host": ["lin", "mac"],
            "prefs": {"example.cross-os-feature": True},
            "env": {"EXAMPLE_CROSS_OS": "1"},
        },
    ]
}


@pytest.fixture
def build(tmp_path):
    """A build directory with its own properties.json and launch.json."""
    (tmp_path / "properties.json").write_text(json.dumps([
        {"property": "window.history.length", "type": "uint", "min": 2},
        {"property": "screen.width", "type": "uint"},
    ]))
    (tmp_path / "launch.json").write_text(json.dumps(RULES))
    return tmp_path / "camoufox-bin"


def _apply(build, target_os, prefs=None, env=None, host="lin", monkeypatch=None):
    monkeypatch.setattr(utils, "_host_os_key", lambda: host)
    prefs = {} if prefs is None else prefs
    env = {} if env is None else env
    utils.apply_launch_rules(target_os, prefs, set(prefs), env, path=build)
    return prefs, env


def test_unconditional_rule_applies_to_every_identity(build, monkeypatch):
    prefs, env = _apply(build, "mac", monkeypatch=monkeypatch)
    assert prefs == {"browser.sessionhistory.max_entries": 10}
    assert env == {}


def test_conditional_rule_applies_when_target_and_host_match(build, monkeypatch):
    prefs, env = _apply(build, "win", host="lin", monkeypatch=monkeypatch)
    assert prefs["example.cross-os-feature"] is True
    assert env == {"EXAMPLE_CROSS_OS": "1"}


def test_conditional_rule_skipped_on_its_own_host(build, monkeypatch):
    prefs, env = _apply(build, "win", host="win", monkeypatch=monkeypatch)
    assert "example.cross-os-feature" not in prefs
    assert env == {}


def test_callers_pref_and_environment_win(build, monkeypatch):
    prefs, env = _apply(
        build, "win",
        prefs={"browser.sessionhistory.max_entries": 50, "example.cross-os-feature": False},
        env={"EXAMPLE_CROSS_OS": "0"},
        monkeypatch=monkeypatch,
    )
    assert prefs == {"browser.sessionhistory.max_entries": 50, "example.cross-os-feature": False}
    assert env == {"EXAMPLE_CROSS_OS": "0"}


def test_build_without_launch_json_gets_nothing(tmp_path, monkeypatch):
    prefs, env = _apply(tmp_path / "camoufox-bin", "win", monkeypatch=monkeypatch)
    assert prefs == {} and env == {}


def test_launch_options_applies_the_rules_of_the_supplied_build(build, monkeypatch):
    """End to end through launch_options: the build's rules reach the prefs
    handed to Playwright, and the build's own schema validates the config."""
    monkeypatch.setattr(utils, "_host_os_key", lambda: "lin")
    sent = {}
    monkeypatch.setattr(utils, "get_env_vars", lambda config, *a, **k: sent.update(config) or {})
    monkeypatch.setattr(utils, "resolve_verstr", lambda *a: "156.0.1-beta.32")
    opts = utils.launch_options(
        executable_path=build,
        os="windows",
        headless=True,
        i_know_what_im_doing=True,
        config={"window.history.length": 1},
        firefox_user_prefs={"example.cross-os-feature": False},
    )
    assert opts["firefox_user_prefs"]["browser.sessionhistory.max_entries"] == 10
    assert opts["firefox_user_prefs"]["example.cross-os-feature"] is False
    assert opts["env"]["EXAMPLE_CROSS_OS"] == "1"
    assert sent["window.history.length"] == 2


class TestPropertyFloor:
    def test_value_below_min_is_lifted(self, build):
        config = {"window.history.length": 1}
        utils.validate_config(config, path=build)
        assert config == {"window.history.length": 2}

    def test_value_at_or_above_min_is_kept(self, build):
        config = {"window.history.length": 4}
        utils.validate_config(config, path=build)
        assert config == {"window.history.length": 4}

    def test_property_without_min_is_untouched(self, build):
        config = {"screen.width": 0}
        utils.validate_config(config, path=build)
        assert config == {"screen.width": 0}


class TestContextScreenAvailRect:
    def test_avail_rect_is_passed_with_the_size(self):
        script = fingerprints._build_init_script({
            "screenWidth": 1920, "screenHeight": 1080,
            "screenAvailWidth": 1920, "screenAvailHeight": 1040,
        })
        assert "w.setScreenDimensions(1920, 1080, 1920, 1040);" in script

    def test_size_alone_without_an_avail_rect(self):
        script = fingerprints._build_init_script({"screenWidth": 1920, "screenHeight": 1080})
        assert "w.setScreenDimensions(1920, 1080);" in script

    def test_new_context_carries_the_identitys_avail_rect(self):
        fp = fingerprints.generate_context_fingerprint(os="windows")
        config = fp["config"]
        expected = (
            f"w.setScreenDimensions({config['screen.width']}, {config['screen.height']}, "
            f"{config['screen.availWidth']}, {config['screen.availHeight']});"
        )
        assert expected in fp["init_script"]
