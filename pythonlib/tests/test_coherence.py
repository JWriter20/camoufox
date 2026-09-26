"""Every identity Camoufox can produce has to be a machine that could exist.

The pools are sampled independently -- navigator and screen from fpgen, the GPU
from fpgen's WebGL records, fonts and voices from their own catalogues -- so an
incoherent identity is assembled rather than inherited, and cleaning the pools
cannot prevent it. These tests run the assembled identity, from every source, past
camoufox.coherence.

Measured before the layer existed (2026-09-17): 14% of macOS identities drew
"Radeon R9 200 Series", a desktop PC card; 7% paired Apple Silicon with
colorDepth 24; 2% of Windows identities reported maxTouchPoints 256; and 18 of
the 312 bundled presets carried a GPU or a screen no desktop has.
"""

import sys
from os.path import dirname, join

import pytest

sys.path.insert(0, join(dirname(__file__), ".."))

from camoufox import coherence  # noqa: E402
from camoufox import fingerprints as fp  # noqa: E402
from camoufox.utils import get_target_os  # noqa: E402

from test_identity_salt import launch  # noqa: E402


class TestRules:
    """Each rule, against the value that motivated it."""

    def test_apple_silicon_never_has_fewer_than_eight_cores(self):
        config = {"webGl:renderer": "Apple M1, or similar", "navigator.hardwareConcurrency": 2}
        assert [v.rule for v in coherence.validate(config, "mac")] == ["apple-silicon-cores"]
        assert coherence.apply(config, "mac") == []
        assert config["navigator.hardwareConcurrency"] == 8

    def test_a_mac_cannot_report_a_braswell_atom_igp(self):
        # The retired WebGL database weighted this at 7.4% of the macOS pool,
        # and fpgen records it from macOS too.
        config = {"webGl:renderer": "Intel(R) HD Graphics 400, or similar"}
        assert [v.rule for v in coherence.validate(config, "mac")] == ["gpu-matches-os"]

    def test_a_mac_cannot_report_an_angle_renderer(self):
        config = {"webGl:renderer": "ANGLE (Intel, Intel(R) HD Graphics Direct3D11 vs_5_0), or similar"}
        assert [v.rule for v in coherence.validate(config, "mac")] == ["gpu-matches-os"]

    def test_windows_renders_through_angle(self):
        assert coherence.gpu_fits_os("ANGLE (NVIDIA, NVIDIA GeForce GTX 980 Direct3D11), or similar", "win")
        assert not coherence.gpu_fits_os("Apple M1, or similar", "win")

    def test_apple_silicon_reports_deep_colour(self):
        # Measured on jobharvest-mac: colorDepth 30, (color: 10).
        config = {"webGl:renderer": "Apple M1, or similar", "screen.colorDepth": 24}
        assert [v.rule for v in coherence.validate(config, "mac")] == ["color-depth"]
        assert coherence.apply(config, "mac") == []
        assert config["screen.colorDepth"] == 30
        assert config["screen.pixelDepth"] == 30

    def test_colour_depth_is_24_or_30(self):
        config = {"screen.colorDepth": 32}
        assert [v.rule for v in coherence.validate(config, "lin")] == ["color-depth"]
        assert coherence.apply(config, "lin") == []
        assert config["screen.colorDepth"] == 24

    def test_touch_points_are_a_digitiser_count(self):
        config = {"navigator.maxTouchPoints": 256}
        assert [v.rule for v in coherence.validate(config, "win")] == ["touch-points"]
        assert coherence.apply(config, "win") == []
        assert config["navigator.maxTouchPoints"] == 0
        # 5 is real -- measured on win-i9, a touchscreen laptop -- and so are
        # 2 and 10; fpgen offers none of 2/5 and the presets carry 40.
        for real in (0, 1, 2, 5, 10):
            assert coherence.validate({"navigator.maxTouchPoints": real}, "win") == [], real

    def test_a_mac_has_no_touchscreen(self):
        config = {"navigator.maxTouchPoints": 5}
        assert [v.rule for v in coherence.validate(config, "mac")] == ["touch-points"]

    def test_device_pixel_ratio_is_a_real_display_mode(self):
        # 1.818 is a scraped artefact: a zoom level folded into the ratio.
        config = {"window.devicePixelRatio": 1.8181818181818181}
        assert [v.rule for v in coherence.validate(config, "win")] == ["device-pixel-ratio"]
        assert coherence.apply(config, "win") == []
        assert config["window.devicePixelRatio"] == 1.75
        # The three measured machines: 1 on linux, 2.5 on win-i9, 2 on the Mac.
        assert coherence.validate({"window.devicePixelRatio": 1}, "lin") == []
        assert coherence.validate({"window.devicePixelRatio": 2.5}, "win") == []
        assert coherence.validate({"window.devicePixelRatio": 2}, "mac") == []
        # macOS has no fractional scaling.
        assert [v.rule for v in coherence.validate({"window.devicePixelRatio": 1.5}, "mac")] == [
            "device-pixel-ratio"
        ]

    def test_desktop_screens_are_landscape(self):
        config = {"screen.width": 1440, "screen.height": 2560}
        assert [v.rule for v in coherence.validate(config, "win")] == ["screen-shape"]
        assert coherence.repair_screen_orientation(config)
        assert (config["screen.width"], config["screen.height"]) == (2560, 1440)

    def test_a_phone_viewport_is_not_a_desktop_screen(self):
        # One bundled preset reports this.
        assert [v.rule for v in coherence.validate({"screen.width": 736, "screen.height": 414}, "mac")] == [
            "screen-shape"
        ]

    def test_avail_never_exceeds_the_screen(self):
        config = {"screen.width": 1920, "screen.height": 1080, "screen.availWidth": 2000}
        assert [v.rule for v in coherence.validate(config, "lin")] == ["avail-bounds"]
        assert coherence.apply(config, "lin") == []
        assert config["screen.availWidth"] == 1920

    def test_platform_agrees_with_the_user_agent_arch(self):
        config = {
            "navigator.userAgent": "Mozilla/5.0 (X11; Linux x86_64; rv:152.0) Gecko/20100101 Firefox/152.0",
            "navigator.platform": "Linux armv81",
        }
        assert [v.rule for v in coherence.validate(config, "lin")] == ["arch-agreement"]

    def test_the_real_machines_pass(self):
        """The three machines captured on 2026-09-17, as themselves."""
        real = [
            ("lin", {"navigator.userAgent": "Mozilla/5.0 (X11; Linux x86_64; rv:152.0) Gecko/20100101 Firefox/152.0",
                     "navigator.platform": "Linux x86_64", "navigator.hardwareConcurrency": 16,
                     "navigator.maxTouchPoints": 0, "screen.width": 1920, "screen.height": 1080,
                     "screen.colorDepth": 24, "webGl:renderer": "Radeon HD 3200 Graphics, or similar"}),
            ("win", {"navigator.platform": "Win32", "navigator.hardwareConcurrency": 16,
                     "navigator.maxTouchPoints": 5, "screen.width": 1382, "screen.height": 864,
                     "screen.colorDepth": 24,
                     "webGl:renderer": "ANGLE (Intel, Intel(R) HD Graphics Direct3D11 vs_5_0 ps_5_0), or similar"}),
            ("mac", {"navigator.platform": "MacIntel", "navigator.hardwareConcurrency": 10,
                     "navigator.maxTouchPoints": 0, "screen.width": 2560, "screen.height": 1440,
                     "screen.colorDepth": 30, "webGl:renderer": "Apple M1, or similar"}),
        ]
        for target_os, config in real:
            assert coherence.validate(config, target_os) == [], target_os


class TestEveryIdentityIsCoherent:
    """The assembled identity, from each source Camoufox draws one from."""

    @pytest.mark.parametrize("os_name", ["macos", "windows", "linux"])
    def test_generated_identities(self, os_name):
        for _ in range(40):
            config = launch(os=os_name)
            assert coherence.validate(config, get_target_os(config)) == []

    @pytest.mark.parametrize("os_name", ["macos", "windows", "linux"])
    def test_every_bundled_preset(self, os_name):
        for i, preset in enumerate(fp.load_presets("150")["presets"][os_name]):
            config = launch(os=os_name, fingerprint_preset=preset)
            assert coherence.validate(config, get_target_os(config)) == [], (os_name, i)


_MIDPOINT_REPAIRS = """
from camoufox import coherence
out = []
for os_key, steps in sorted(coherence.PLAUSIBLE_DPR.items()):
    steps = sorted(steps)
    for low, high in zip(steps, steps[1:]):
        config = {"window.devicePixelRatio": (low + high) / 2}
        coherence.apply(config, os_key)
        out.append(config["window.devicePixelRatio"])
print(out)
"""


def test_a_midpoint_repairs_to_the_lower_step_whether_or_not_bytecode_is_cached(tmp_path):
    """The steps were frozensets, and min() keeps the first of equal distances.
    A frozenset literal iterates in one order when compiled from source and in
    another when loaded back from a .pyc, so the same identity repaired
    differently on its first launch than on later ones."""
    import subprocess
    import sys
    from pathlib import Path

    env = {"PYTHONPYCACHEPREFIX": str(tmp_path), "PYTHONPATH": str(Path(coherence.__file__).parents[1])}
    runs = [
        subprocess.run([sys.executable, "-c", _MIDPOINT_REPAIRS], env=env, capture_output=True,
                       text=True, check=True).stdout
        for _ in range(2)  # the first compiles and writes the .pyc, the second loads it
    ]
    assert runs[0] == runs[1]
    lower = [low for _, steps in sorted(coherence.PLAUSIBLE_DPR.items())
             for low in sorted(steps)[:-1]]
    assert runs[0].strip() == str(lower)
