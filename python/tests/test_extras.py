"""The install extras cover what the launcher turns on by itself.

A leased launch behind the lease's own proxy, or of a stored profile, turns geoip on (the exit decides the
timezone and locale), so `pip install camoufox[pro]` must bring the geoip extra's requirements with it.
"""

import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).resolve().parents[1] / "pyproject.toml"


def test_the_pro_extra_carries_the_geoip_extra() -> None:
    extras = tomllib.loads(PYPROJECT.read_text())["tool"]["poetry"]["extras"]
    assert set(extras["geoip"]) <= set(extras["pro"])
