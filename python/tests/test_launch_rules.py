"""What a build declares beside its binary is what the launcher applies.

A build can compile in a feature that only does something once a pref or an
environment variable is set at launch, or read a config key the stock build
does not have. The launcher learns both from the build itself -- launch.json
and properties.json beside the executable -- so a caller pointing
`executable_path` at such a build gets it configured, and every other build is
unaffected.
"""

import json
import warnings

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
            "envFromConfig": {"EXAMPLE_ARCH": "example:arch"},
        },
        {"target": ["win"], "host": ["lin"], "envPaths": {"EXAMPLE_LIB": "lib/example.dll"}},
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
    (tmp_path / "lib").mkdir()
    (tmp_path / "lib" / "example.dll").write_bytes(b"MZ")
    return tmp_path / "camoufox-bin"


def _apply(build, target_os, prefs=None, env=None, host="lin", monkeypatch=None, config=None):
    monkeypatch.setattr(utils, "_host_os_key", lambda: host)
    prefs = {} if prefs is None else prefs
    env = {} if env is None else env
    utils.apply_launch_rules(target_os, config or {}, prefs, set(prefs), env, path=build)
    return prefs, env


def test_unconditional_rule_applies_to_every_identity(build, monkeypatch):
    prefs, env = _apply(build, "mac", monkeypatch=monkeypatch)
    assert prefs == {"browser.sessionhistory.max_entries": 10}
    assert env == {}


def test_conditional_rule_applies_when_target_and_host_match(build, monkeypatch):
    prefs, env = _apply(build, "win", host="lin", monkeypatch=monkeypatch)
    assert prefs["example.cross-os-feature"] is True
    assert env == {"EXAMPLE_CROSS_OS": "1", "EXAMPLE_LIB": str(build.parent / "lib" / "example.dll")}


def test_env_from_config_only_when_the_identity_has_the_key(build, monkeypatch):
    _, env = _apply(build, "win", host="mac", monkeypatch=monkeypatch, config={"example:arch": "blackwell"})
    assert env == {"EXAMPLE_CROSS_OS": "1", "EXAMPLE_ARCH": "blackwell"}


def test_missing_env_path_fails_loudly(build, monkeypatch):
    (build.parent / "lib" / "example.dll").unlink()
    with pytest.raises(FileNotFoundError, match="example.dll"):
        _apply(build, "win", host="lin", monkeypatch=monkeypatch)


def test_conditional_rule_skipped_on_its_own_host(build, monkeypatch):
    prefs, env = _apply(build, "win", host="win", monkeypatch=monkeypatch)
    assert "example.cross-os-feature" not in prefs
    assert env == {}


def test_callers_pref_and_environment_win(build, monkeypatch):
    prefs, env = _apply(
        build, "win",
        prefs={"browser.sessionhistory.max_entries": 50, "example.cross-os-feature": False},
        env={"EXAMPLE_CROSS_OS": "0", "EXAMPLE_LIB": "/elsewhere.dll", "EXAMPLE_ARCH": "ampere"},
        monkeypatch=monkeypatch,
        config={"example:arch": "blackwell"},
    )
    assert prefs == {"browser.sessionhistory.max_entries": 50, "example.cross-os-feature": False}
    assert env == {"EXAMPLE_CROSS_OS": "0", "EXAMPLE_LIB": "/elsewhere.dll", "EXAMPLE_ARCH": "ampere"}


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
    assert opts["env"]["EXAMPLE_LIB"] == str(build.parent / "lib" / "example.dll")
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


# A feature that must exist on exactly one target/host pairing: here, a
# Windows identity on a Linux host. The rule is `exclusive`, so the variable
# cannot reach another pairing through the caller's environment, and the one
# host that cannot support it is warned instead of degrading quietly.
EXCLUSIVE_RULES = {
    "rules": [
        {
            "target": ["win"],
            "host": ["lin"],
            "exclusive": True,
            "envPaths": {"EXAMPLE_DLL": "lib/example.dll"},
        },
        {
            "target": ["win"],
            "host": ["mac"],
            "warn": "example.dll cannot load on a macOS host",
        },
    ]
}
OSES = ("win", "mac", "lin")


@pytest.fixture
def exclusive_build(build):
    (build.parent / "launch.json").write_text(json.dumps(EXCLUSIVE_RULES))
    return build


@pytest.mark.parametrize("host", OSES)
@pytest.mark.parametrize("target", OSES)
def test_exclusive_rule_matrix(exclusive_build, monkeypatch, target, host):
    """Every target/host pairing: the variable is set iff win on lin."""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        _, env = _apply(exclusive_build, target, host=host, monkeypatch=monkeypatch)
    if (target, host) == ("win", "lin"):
        assert env == {"EXAMPLE_DLL": str(exclusive_build.parent / "lib" / "example.dll")}
    else:
        assert env == {}
    warned = [str(w.message) for w in caught if issubclass(w.category, RuntimeWarning)]
    assert warned == (["example.dll cannot load on a macOS host"] if (target, host) == ("win", "mac") else [])


@pytest.mark.parametrize("host", OSES)
@pytest.mark.parametrize("target", OSES)
def test_exclusive_rule_strips_an_inherited_variable(exclusive_build, monkeypatch, target, host):
    """A variable left in the caller's environment reaches only the rule's own pairing."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        _, env = _apply(
            exclusive_build, target, host=host, monkeypatch=monkeypatch,
            env={"EXAMPLE_DLL": "/inherited.dll", "UNRELATED": "1"},
        )
    if (target, host) == ("win", "lin"):
        assert env == {"EXAMPLE_DLL": "/inherited.dll", "UNRELATED": "1"}
    else:
        assert env == {"UNRELATED": "1"}


def test_exclusive_rule_keeps_a_variable_another_matching_rule_sets(exclusive_build, monkeypatch):
    rules = json.loads((exclusive_build.parent / "launch.json").read_text())
    rules["rules"].append({"target": ["lin"], "env": {"EXAMPLE_DLL": "/other.dll"}})
    (exclusive_build.parent / "launch.json").write_text(json.dumps(rules))
    _, env = _apply(exclusive_build, "lin", host="lin", monkeypatch=monkeypatch)
    assert env == {"EXAMPLE_DLL": "/other.dll"}


def test_optional_env_path_set_only_when_the_file_exists(build, monkeypatch):
    launch = build.parent / "launch.json"
    launch.write_text(json.dumps({"rules": [{
        "target": ["win"], "host": ["lin"],
        "envPathsOptional": {"EXAMPLE_PRESENT": "lib/example.dll", "EXAMPLE_ABSENT": "lib/missing.dll"},
    }]}))
    _, env = _apply(build, "win", monkeypatch=monkeypatch)
    assert env == {"EXAMPLE_PRESENT": str(build.parent / "lib" / "example.dll")}


def test_rule_config_fills_keys_the_caller_did_not_set(build, monkeypatch):
    launch = build.parent / "launch.json"
    launch.write_text(json.dumps({"rules": [{
        "target": ["win"], "host": ["lin"], "config": {"example:on": True, "example:kept": True},
    }]}))
    config = {"example:kept": False}
    _apply(build, "win", monkeypatch=monkeypatch, config=config)
    assert config == {"example:on": True, "example:kept": False}
    other = {}
    _apply(build, "win", host="win", monkeypatch=monkeypatch, config=other)
    assert other == {}


def test_ld_preload_is_appended_only_when_the_library_exists(build, monkeypatch):
    launch = build.parent / "launch.json"
    launch.write_text(json.dumps({"rules": [{
        "target": ["win"], "host": ["lin"],
        "ldPreload": ["lib/example.dll", "lib/missing.so"],
    }]}))
    lib = str(build.parent / "lib" / "example.dll")
    _, env = _apply(build, "win", monkeypatch=monkeypatch)
    assert env == {"LD_PRELOAD": lib}
    _, env = _apply(build, "win", env={"LD_PRELOAD": "/opt/other.so"}, monkeypatch=monkeypatch)
    assert env["LD_PRELOAD"] == f"/opt/other.so:{lib}"
    _, env = _apply(build, "win", env={"LD_PRELOAD": lib}, monkeypatch=monkeypatch)
    assert env["LD_PRELOAD"] == lib
    _, env = _apply(build, "win", host="win", monkeypatch=monkeypatch)
    assert env == {}


def test_env_cache_dir_is_created_under_the_user_cache(build, monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    launch = build.parent / "launch.json"
    launch.write_text(json.dumps({"rules": [{
        "target": ["win"], "host": ["lin"],
        "envCacheDirs": {"EXAMPLE_CACHE": "example-cache"},
    }]}))
    _, env = _apply(build, "win", monkeypatch=monkeypatch)
    assert env == {"EXAMPLE_CACHE": str(tmp_path / "cache" / "camoufox-example-cache")}
    assert (tmp_path / "cache" / "camoufox-example-cache").is_dir()
    _, env = _apply(build, "win", env={"EXAMPLE_CACHE": "/mine"}, monkeypatch=monkeypatch)
    assert env == {"EXAMPLE_CACHE": "/mine"}
