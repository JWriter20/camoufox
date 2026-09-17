"""Which sampled WebGL extensions reach the page, per OS."""

import sqlite3

import orjson

from camoufox.webgl import sample
from camoufox.webgl.sample import DB_PATH, _load_webgl_data


def _row_with(ext, key="webGl2:supportedExtensions", os="win"):
    con = sqlite3.connect(DB_PATH)
    try:
        for (data,) in con.execute(f"SELECT data FROM webgl_fingerprints WHERE {os} > 0"):  # nosec
            if ext in (orjson.loads(data).get(key) or []):
                return data
    finally:
        con.close()
    raise AssertionError(f"no {os} row carries {ext}")


def test_windows_keeps_ovr_multiview2_on_webgl2():
    data = _load_webgl_data(_row_with("OVR_multiview2"), "win")
    assert "OVR_multiview2" in data["webGl2:supportedExtensions"]


def test_linux_filters_ovr_multiview2():
    data = _load_webgl_data(_row_with("OVR_multiview2", os="lin"), "lin")
    assert "OVR_multiview2" not in data["webGl2:supportedExtensions"]


def test_ovr_multiview2_never_on_webgl1_in_corpus():
    con = sqlite3.connect(DB_PATH)
    try:
        for (data,) in con.execute("SELECT data FROM webgl_fingerprints"):
            assert "OVR_multiview2" not in (orjson.loads(data).get("webGl:supportedExtensions") or [])
    finally:
        con.close()


def test_draft_extensions_filtered_on_every_os():
    blob = orjson.dumps(
        {
            "webGl:supportedExtensions": ["ANGLE_instanced_arrays", "WEBGL_multi_draw"],
            "webGl2:supportedExtensions": ["EXT_texture_norm16", "WEBGL_clip_cull_distance", "OVR_multiview2"],
        }
    )
    for os in ("win", "mac", "lin"):
        data = _load_webgl_data(blob, os)
        assert data["webGl:supportedExtensions"] == ["ANGLE_instanced_arrays"]
        assert "EXT_texture_norm16" not in data["webGl2:supportedExtensions"]
        assert "WEBGL_clip_cull_distance" not in data["webGl2:supportedExtensions"]


def test_sampled_windows_identities_can_carry_it():
    hits = sum(
        "OVR_multiview2" in (sample.sample_webgl("win", seed=s).get("webGl2:supportedExtensions") or [])
        for s in range(200)
    )
    assert hits > 0
