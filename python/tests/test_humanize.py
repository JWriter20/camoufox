"""Humanize launch configuration: the engine factories, the per-channel keys,
and the check against the engines a build ships."""

import json
from pathlib import Path

import pytest

from camoufox import utils
from camoufox.exceptions import HumanizeEngineUnavailable
from camoufox.humanize import auto, custom, cursory, engine, humanize_config, normalize, raw

REPO = Path(__file__).resolve().parents[2]
BASE_MANIFEST = json.loads((REPO / "browser" / "settings" / "humanize-engines.json").read_text())

# What a build that plans cursor paths in the browser ships.
OLD_MANIFEST = {
    "version": 1,
    "mouse": ["raw", "cursory"],
    "auto": {"mouse": ["cursory"]},
    "engines": {
        "cursory": {
            "module": "resource://gre/modules/juggler/input/CursorTrajectory.js",
            "options": {
                "maxTime": {"key": "humanize:maxTime", "min": 0},
                "minTime": {"key": "humanize:minTime", "min": 0},
            },
        }
    },
}

# A build that lists keyboard and scroll channels too, with one more engine,
# `fancy`, on every channel: its options, their config keys and ranges come
# from the manifest alone.
FANCY_MANIFEST = {
    **OLD_MANIFEST,
    "mouse": [*OLD_MANIFEST["mouse"], "fancy"],
    "keyboard": ["raw", "fancy"],
    "scroll": ["raw", "fancy"],
    "engines": {
        **OLD_MANIFEST["engines"],
        "fancy": {
            "module": "chrome://example/fancy.js",
            "options": {
                "budgetSeconds": {"key": "humanize:fancy:budgetSeconds", "min": 1, "max": 20},
                "speed": {"key": "humanize:fancy:speed", "min": 0.5, "max": 2},
            },
        },
    },
}


@pytest.fixture
def captured_launch_config(monkeypatch):
    captured = {}

    monkeypatch.setattr(utils, "generate_fingerprint", lambda **_kwargs: object())
    monkeypatch.setattr(utils, "from_fpgen", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(utils, "get_screen_cons", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        utils, "_generate_random_font_subset", lambda *_args, **_kwargs: []
    )
    monkeypatch.setattr(
        utils, "draw_voice_config", lambda *_args, **_kwargs: {"voices": []}
    )
    monkeypatch.setattr(utils, "validate_config", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(utils, "add_default_addons", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(utils, "fix_navigator_arch", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(
        utils, "follow_baseline_protection", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        utils, "clamp_window_dimensions", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(utils, "installed_verstr", lambda *_args, **_kwargs: "152.0")
    monkeypatch.setattr(utils, "launch_path", lambda *_args, **_kwargs: "/camoufox")
    monkeypatch.setattr(utils.LeakWarning, "warn", lambda *_args, **_kwargs: None)

    def capture_env(config, _target_os, **_kwargs):
        captured.clear()
        captured.update(config)
        return {}

    monkeypatch.setattr(utils, "get_env_vars", capture_env)

    def launch(humanize, manifest=None):
        monkeypatch.setattr(utils, "_load_humanize_engines", lambda *_args, **_kwargs: manifest)
        utils.launch_options(
            humanize=humanize,
            block_webgl=True,
            i_know_what_im_doing=True,
        )
        return captured.copy()

    return launch


def test_humanize_true_does_not_set_boolean_duration(captured_launch_config) -> None:
    config = captured_launch_config(True)

    assert config["humanize"] is True
    assert "humanize:maxTime" not in config


@pytest.mark.parametrize("duration", [1, 5, 1.25])
def test_humanize_duration_is_encoded_as_double(
    captured_launch_config, duration
) -> None:
    config = captured_launch_config(duration)

    assert config["humanize"] is True
    assert config["humanize:maxTime"] == float(duration)
    assert type(config["humanize:maxTime"]) is float


# A build without humanize-engines.json predates per-channel humanize: the
# launcher writes exactly what it always wrote.
@pytest.mark.parametrize(
    "humanize, expected",
    [
        (None, {}),
        (False, {}),
        (0, {}),
        (True, {"humanize": True}),
        (1.5, {"humanize": True, "humanize:maxTime": 1.5}),
        (2, {"humanize": True, "humanize:maxTime": 2.0}),
        ({"mouse": cursory(max_time=0.7)}, {"humanize": True, "humanize:maxTime": 0.7}),
        ({"mouse": raw(), "keyboard": raw(), "scroll": raw()}, {}),
    ],
)
def test_a_build_without_the_manifest_gets_the_legacy_keys(humanize, expected) -> None:
    assert humanize_config(humanize, None) == expected


@pytest.mark.parametrize(
    "humanize, channel",
    [
        ({"mouse": raw()}, "mouse"),
        ({"scroll": raw()}, "scroll"),
        ({"keyboard": engine("fancy")}, "keyboard"),
        ({"seed": 5}, "seed"),
    ],
)
def test_a_build_without_the_manifest_refuses_what_it_cannot_do(humanize, channel) -> None:
    with pytest.raises(HumanizeEngineUnavailable) as raised:
        humanize_config(humanize, None)
    assert raised.value.channel == channel


@pytest.mark.parametrize(
    "humanize, expected",
    [
        (False, {"humanize": False, "humanize:mouse": "raw"}),
        (True, {"humanize": True, "humanize:mouse": "auto"}),
        (1.5, {"humanize": True, "humanize:mouse": "cursory", "humanize:maxTime": 1.5}),
        (
            {"mouse": cursory(max_time=1, min_time=0.2), "scroll": "raw", "seed": 2**64 - 1},
            {
                "humanize": True,
                "humanize:mouse": "cursory",
                "humanize:maxTime": 1.0,
                "humanize:minTime": 0.2,
                "humanize:seed": "18446744073709551615",
            },
        ),
        # Keyboard and scroll have no browser engine: only the mouse turns humanize on.
        ({"mouse": raw()}, {"humanize": False, "humanize:mouse": "raw"}),
        ({}, {"humanize": True, "humanize:mouse": "auto"}),
    ],
)
def test_every_humanize_key_is_written_on_every_launch(humanize, expected) -> None:
    config = humanize_config(humanize, OLD_MANIFEST)
    assert config == expected
    assert list(config) == list(expected)


def test_this_build_lists_no_engine() -> None:
    assert BASE_MANIFEST == {"version": 2, "auto": {}, "engines": {}}


# The browser delivers input as sent and reads no humanize key: nothing is
# written, and asking it to humanize is refused, not ignored.
@pytest.mark.parametrize(
    "humanize",
    [None, False, 0, {}, {"mouse": raw()}, {"mouse": auto(), "seed": 7}, {"keyboard": custom(lambda *a, **k: None)}],
)
def test_a_build_with_no_engine_gets_no_humanize_key(humanize) -> None:
    assert humanize_config(humanize, BASE_MANIFEST) == {}


@pytest.mark.parametrize(
    "humanize, engine_name",
    [
        (True, "auto"),
        (1.5, "cursory"),
        ({"mouse": cursory()}, "cursory"),
        ({"mouse": "cursory"}, "cursory"),
    ],
)
def test_a_build_with_no_engine_refuses_to_humanize(humanize, engine_name) -> None:
    with pytest.raises(HumanizeEngineUnavailable) as raised:
        humanize_config(humanize, BASE_MANIFEST)
    assert (raised.value.channel, raised.value.engine, raised.value.available) == ("mouse", engine_name, [])


def test_launch_options_refuses_humanize_true_on_this_build(captured_launch_config) -> None:
    with pytest.raises(HumanizeEngineUnavailable, match="custom"):
        captured_launch_config(True, BASE_MANIFEST)


def test_an_engine_needs_a_build_that_ships_it() -> None:
    with pytest.raises(HumanizeEngineUnavailable) as raised:
        humanize_config({"keyboard": engine("fancy")}, OLD_MANIFEST)
    assert (raised.value.channel, raised.value.engine, raised.value.available) == ("keyboard", "fancy", [])


def test_engine_options_are_written_to_the_keys_the_manifest_declares() -> None:
    config = humanize_config(
        {"keyboard": engine("fancy", speed=1.5), "scroll": engine("fancy", budgetSeconds=6)},
        FANCY_MANIFEST,
    )
    assert config == {
        "humanize": True,
        "humanize:mouse": "auto",
        "humanize:keyboard": "fancy",
        "humanize:scroll": "fancy",
        "humanize:fancy:speed": 1.5,
        "humanize:fancy:budgetSeconds": 6.0,
    }


def test_one_option_key_cannot_take_two_values() -> None:
    with pytest.raises(ValueError, match="budgetSeconds twice"):
        humanize_config(
            {"keyboard": engine("fancy", budgetSeconds=5), "scroll": engine("fancy", budgetSeconds=6)}, FANCY_MANIFEST
        )


@pytest.mark.parametrize(
    "humanize, message",
    [
        ({"keyboard": engine("fancy", budgetSeconds=21)}, r"budgetSeconds must be in \[1, 20\]"),
        ({"keyboard": engine("fancy", speed=0.1)}, r"speed must be in \[0.5, 2\]"),
        ({"keyboard": engine("fancy", wpm=60)}, "has no option 'wpm'"),
        ({"mouse": cursory(max_time=-1)}, "maxTime must be at least 0"),
    ],
)
def test_engine_options_are_checked_against_the_manifest(humanize, message) -> None:
    with pytest.raises(ValueError, match=message):
        humanize_config(humanize, FANCY_MANIFEST)


@pytest.mark.parametrize(
    "humanize, message",
    [
        ({"scroll": engine("notches")}, "scroll: notches is not available"),
        ({"keyboard": cursory()}, "keyboard: cursory is not available"),
        ({"mouse": "fast"}, "mouse: fast is not available"),
    ],
)
def test_an_engine_the_channel_does_not_list_is_refused(humanize, message) -> None:
    with pytest.raises(HumanizeEngineUnavailable, match=message):
        humanize_config(humanize, OLD_MANIFEST)


@pytest.mark.parametrize(
    "humanize, message",
    [
        ({"touch": auto()}, "no channel"),
        ({"mouse": 3}, "must be an engine"),
        ({"notches": {"engine": "notches", "options": {"speed": 2}}}, "no channel"),
        ({"mouse": cursory(max_time="1")}, "must be a number"),
        ({"mouse": engine("cursory", maxTime=True)}, "must be a number"),
        ({"seed": -1}, "seed"),
        ({"seed": 2**64}, "seed"),
        ("yes", "must be None"),
    ],
)
def test_malformed_settings_are_refused(humanize, message) -> None:
    with pytest.raises(ValueError, match=message):
        normalize(humanize)


def test_factories_return_plain_data() -> None:
    assert cursory(max_time=1) == {"engine": "cursory", "options": {"maxTime": 1}}
    assert engine("fancy", speed=2) == {"engine": "fancy", "options": {"speed": 2}}
    assert json.loads(json.dumps(auto())) == auto()


def test_launch_options_writes_the_channel_keys(captured_launch_config) -> None:
    config = captured_launch_config({"mouse": cursory(max_time=0.8), "scroll": raw(), "seed": 9}, OLD_MANIFEST)
    assert config["humanize"] is True
    assert config["humanize:mouse"] == "cursory"
    assert "humanize:scroll" not in config and "humanize:keyboard" not in config
    assert config["humanize:maxTime"] == 0.8
    assert config["humanize:seed"] == "9"


def test_launch_options_refuses_an_engine_the_build_lacks(captured_launch_config) -> None:
    with pytest.raises(HumanizeEngineUnavailable):
        captured_launch_config({"scroll": engine("fancy")}, OLD_MANIFEST)


def test_the_manifest_is_read_beside_the_executable(tmp_path) -> None:
    binary = tmp_path / "camoufox-bin"
    assert utils._load_humanize_engines(binary) is None
    (tmp_path / "humanize-engines.json").write_text(json.dumps(BASE_MANIFEST))
    assert utils._load_humanize_engines(binary) == BASE_MANIFEST
