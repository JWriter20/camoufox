"""The data files that ship must not contain an identity we would refuse to use.

`camoufox.coherence` filters at draw time, so an impossible row can never reach
a page either way. This is the second filter, on the data itself: a row that
survives here is one a future refresh could ship, a consumer reading the JSON
directly would get, and a reviewer would take as real. Both filters run the same
rules -- `scripts/clean-fingerprint-data.py --write` is what makes these pass.

Dropped on 2026-09-17: 38 of 435 presets (26 with a GPU their OS cannot report,
7 pairing Apple Silicon with a core count Apple never shipped, 4 with a colour
depth their GPU contradicts, 3 with a phone viewport, 1 claiming 40 touch
points), and 2 impossible macOS weights in webgl_data.db.
"""

import json
import sqlite3
import sys
from os.path import dirname, join
from pathlib import Path

import pytest

sys.path.insert(0, join(dirname(__file__), ".."))

from camoufox import coherence  # noqa: E402
from camoufox.fingerprints import from_preset  # noqa: E402
from camoufox.webgl.sample import DB_PATH  # noqa: E402

DATA = Path(__file__).parent.parent / "camoufox"
PRESET_FILES = ("fingerprint-presets.json", "fingerprint-presets-v150.json")
OS_KEY = {"macos": "mac", "windows": "win", "linux": "lin"}


@pytest.mark.parametrize("filename", PRESET_FILES)
def test_every_bundled_preset_is_coherent_as_stored(filename):
    presets = json.loads((DATA / filename).read_text())["presets"]
    for os_name, entries in presets.items():
        assert entries, f"{filename}/{os_name} has no presets left"
        for index, preset in enumerate(entries):
            config = from_preset(preset, "152")
            violations = coherence.validate(config, OS_KEY[os_name])
            assert violations == [], (
                f"{filename} {os_name}[{index}]: "
                + "; ".join(v.detail for v in violations)
                + " -- run scripts/clean-fingerprint-data.py --write"
            )


def test_no_gpu_is_offered_to_an_os_that_cannot_report_it():
    connection = sqlite3.connect(DB_PATH)
    try:
        rows = connection.execute(
            "SELECT vendor, renderer, win, mac, lin FROM webgl_fingerprints"
        ).fetchall()
    finally:
        connection.close()
    assert rows, "webgl_data.db is empty"
    for vendor, renderer, *weights in rows:
        for os_key, weight in zip(("win", "mac", "lin"), weights):
            if weight and weight > 0:
                assert coherence.gpu_fits_os(renderer, os_key), (
                    f"{renderer!r} is offered to {os_key} at {weight}"
                )


def test_each_os_still_has_gpus_to_draw_from():
    """The filter must not empty a pool -- a single GPU per OS is its own tell."""
    connection = sqlite3.connect(DB_PATH)
    try:
        for os_key in ("win", "mac", "lin"):
            count = connection.execute(
                f"SELECT COUNT(*) FROM webgl_fingerprints WHERE {os_key} > 0"  # nosec
            ).fetchone()[0]
            assert count >= 2, f"{os_key} has {count} GPU(s) left"
    finally:
        connection.close()
