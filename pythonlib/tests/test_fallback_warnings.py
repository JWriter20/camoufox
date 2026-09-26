"""Every place an identity falls back to a substitute value says so.

A substitute is a value the rest of the identity was not drawn to match, which
a page can see. Each site warns with a report block the user can paste into a
GitHub issue, so the failure reaches us instead of shipping silently.
"""

import sqlite3

import pytest
from test_identity_salt import launch

from camoufox import fingerprints as fp
from camoufox import utils
from camoufox._warnings import FallbackWarning

REPORT = r"Please report this at https://github\.com/daijro/camoufox/issues/new"


def _fail(error):
    def raiser(*_args, **_kwargs):
        raise error

    return raiser


def _preset():
    preset = fp.load_presets("152")["presets"]["windows"][0]
    return {**preset, "fonts": ["Arial"]}


def _report(record):
    (warning,) = [w for w in record if w.category is FallbackWarning]
    text = str(warning.message)
    assert "camoufox:" in text and "python:" in text and "os:" in text
    return text


def test_preset_font_draw(monkeypatch):
    monkeypatch.setattr(fp, "_generate_random_font_subset", _fail(OSError("fonts.json missing")))
    with pytest.warns(FallbackWarning, match=REPORT) as record:
        config = fp.from_preset(_preset(), "152")
    assert "OSError: fonts.json missing" in _report(record)
    assert "Arial" in config["fonts"]


def test_preset_voice_draw(monkeypatch):
    monkeypatch.setattr(fp, "_generate_random_voice_subset", _fail(ValueError("bad manifest")))
    with pytest.warns(FallbackWarning, match=REPORT) as record:
        fp.from_preset(_preset(), "152")
    assert "ValueError: bad manifest" in _report(record)


@pytest.mark.parametrize(
    "target, error, key",
    [
        ("_generate_random_font_subset", OSError("fonts.json missing"), "fonts"),
        ("_generate_random_voice_subset", ValueError("bad manifest"), "voices"),
        ("sample_webgl_for_screen", sqlite3.OperationalError("no such table"), "webGl:renderer"),
    ],
)
def test_context_draws(monkeypatch, target, error, key):
    monkeypatch.setattr(fp, target, _fail(error))
    with pytest.warns(FallbackWarning, match=REPORT) as record:
        context = fp.generate_context_fingerprint(os="linux")
    assert f"{type(error).__name__}: {error}" in _report(record)
    assert key not in context["config"]


@pytest.mark.parametrize(
    "loader, cache", [("_load_font_groups", "_FONT_GROUPS_CACHE"), ("_load_font_bases", "_FONT_BASES_CACHE")]
)
def test_font_data_loaders(monkeypatch, tmp_path, loader, cache):
    monkeypatch.setattr(fp, cache, None)
    monkeypatch.setattr(fp, "__file__", str(tmp_path / "fingerprints.py"))
    with pytest.warns(FallbackWarning, match=REPORT) as record:
        assert getattr(fp, loader)() == {}
    assert "FileNotFoundError" in _report(record)


def test_launch_font_draw(monkeypatch):
    monkeypatch.setattr(utils, "_generate_random_font_subset", _fail(OSError("font-bases.json missing")))
    with pytest.warns(FallbackWarning, match=REPORT):
        config = launch()
    assert config["fonts"]


def test_preset_gpu_not_in_webgl_data():
    # Windows reports every GPU through ANGLE, so only an ANGLE string reaches the lookup.
    GPU = "ANGLE (Acme, Acme GPU 9000 Direct3D11 vs_5_0 ps_5_0)"
    preset = fp.load_presets("152")["presets"]["windows"][0]
    preset = {**preset, "webgl": {"unmaskedVendor": "Google Inc. (Acme)", "unmaskedRenderer": GPU}}
    with pytest.warns(FallbackWarning, match=REPORT) as record:
        config = launch(os="windows", fingerprint_preset=preset)
    assert GPU in _report(record)
    assert config["webGl:renderer"] != GPU
