"""The CPU vendor an identity claims (cpu_vendor.py, skia-cpu-vendor-estimates.patch).

Skia blends that divide (color-burn, color-dodge, hue, saturation, color,
luminosity) use the x86 reciprocal estimate, whose low bits differ between
Intel and AMD: cfparity gate 11 found exactly those 5 composite ops off on an
AMD host claiming win-i9 (Intel i9-11900H). The browser emulates the claimed
vendor; the launcher claims one only where the identity implies it.
"""

import pytest

from camoufox import cpu_vendor as cv
from camoufox import utils

from test_launch_environment import isolated_launch_dependencies  # noqa: F401

INTEL_IGPU = "ANGLE (Intel, Intel(R) HD Graphics Direct3D11 vs_5_0 ps_5_0), or similar"
INTEL_ARC_IGPU = "ANGLE (Intel, Intel(R) Arc(TM) Graphics Direct3D11 vs_5_0 ps_5_0), or similar"
INTEL_ARC_DISCRETE = "ANGLE (Intel, Intel(R) Arc(TM) A770 Graphics Direct3D11 vs_5_0 ps_5_0), or similar"
AMD_APU = "ANGLE (AMD, AMD Radeon(TM) Graphics Direct3D11 vs_5_0 ps_5_0), or similar"
AMD_780M = "ANGLE (AMD, AMD Radeon 780M Graphics Direct3D11 vs_5_0 ps_5_0), or similar"
AMD_DISCRETE = "ANGLE (AMD, AMD Radeon RX 6600 Direct3D11 vs_5_0 ps_5_0), or similar"
NVIDIA = "ANGLE (NVIDIA, NVIDIA GeForce GTX 980 Direct3D11 vs_5_0 ps_5_0), or similar"


class TestImplied:
    @pytest.mark.parametrize("renderer,vendor", [
        (INTEL_IGPU, "intel"), (INTEL_ARC_IGPU, "intel"), (INTEL_ARC_DISCRETE, None),
        (AMD_APU, "amd"), (AMD_780M, "amd"), (AMD_DISCRETE, None), (NVIDIA, None), (None, None),
        ("Apple M1, or similar", None),
        ("ANGLE (AMD, AMD Radeon (TM) Graphics (0x000015E7) Direct3D11 vs_5_0 ps_5_0, D3D11)", "amd"),
        ("ANGLE (AMD, AMD Radeon(TM) R4 Graphics (0x00009851) Direct3D11 vs_5_0 ps_5_0, D3D11)", "amd"),
        ("ANGLE (AMD, AMD Radeon(TM) RX Vega 11 Graphics (0x000015D8) Direct3D11 vs_5_0 ps_5_0, D3D11)", "amd"),
        ("ANGLE (AMD, Radeon HD 3200 Graphics Direct3D11 vs_5_0 ps_5_0)", "amd"),
        ("ANGLE (AMD, Radeon(TM) RX 460 Graphics (0x000067EF) Direct3D11 vs_5_0 ps_5_0, D3D11)", None),
        ("ANGLE (AMD, AMD Radeon 610 Series (0x00006665) Direct3D11 vs_5_0 ps_5_0, D3D11)", None),
        ("ANGLE (AMD, AMD Radeon R7 M265 Series (0x00006604) Direct3D11 vs_5_0 ps_5_0, D3D11)", None),
        ("ANGLE (AMD, REMBRANDT (rembrandt LLVM 15.0.7 DRM 3.61 6.14.0-37-generic), OpenGL 4.6)", "amd"),
        ("ANGLE (AMD, NAVI23 (navi23 LLVM 15.0.7 DRM 3.61 6.14.0-37-generic), OpenGL ES 3.2)", None),
    ])
    def test_only_an_integrated_gpu_names_the_cpu(self, renderer, vendor):
        assert cv.implied_cpu_vendor(renderer) == vendor


class TestSet:
    def test_intel_igpu_on_an_amd_host_claims_intel(self):
        config = {"webGl:renderer": INTEL_IGPU}
        cv.set_cpu_vendor(config, "win", "lin", "amd")
        assert config["cpu:vendor"] == "intel"

    def test_the_hosts_own_vendor_needs_no_claim(self):
        config = {"webGl:renderer": INTEL_IGPU}
        cv.set_cpu_vendor(config, "win", "lin", "intel")
        assert "cpu:vendor" not in config

    def test_a_discrete_gpu_leaves_the_host_vendor(self):
        config = {"webGl:renderer": NVIDIA}
        cv.set_cpu_vendor(config, "win", "lin", "amd")
        assert "cpu:vendor" not in config

    def test_an_identity_of_the_host_os_claims_nothing(self):
        config = {"webGl:renderer": INTEL_IGPU}
        cv.set_cpu_vendor(config, "lin", "lin", "amd")
        assert "cpu:vendor" not in config

    def test_the_callers_value_is_kept_and_checked(self):
        config = {"cpu:vendor": "Intel", "webGl:renderer": AMD_APU}
        cv.set_cpu_vendor(config, "win", "lin", "intel")
        assert config["cpu:vendor"] == "intel"
        with pytest.raises(ValueError, match="cpu:vendor"):
            cv.set_cpu_vendor({"cpu:vendor": "via"}, "win", "lin", "amd")


def test_host_vendor_is_read_from_cpuinfo(monkeypatch, tmp_path):
    import builtins

    real_open = builtins.open
    fake = tmp_path / "cpuinfo"
    for text, want in (("vendor_id\t: AuthenticAMD\n", "amd"), ("vendor_id\t: GenuineIntel\n", "intel"),
                       ("processor\t: 0\n", None)):
        fake.write_text(text)
        monkeypatch.setattr(builtins, "open",
                            lambda p, *a, **k: real_open(fake if p == "/proc/cpuinfo" else p, *a, **k))
        assert cv.host_cpu_vendor() == want


def test_launch_options_claims_the_identitys_vendor(monkeypatch, isolated_launch_dependencies):  # noqa: F811
    seen = []
    monkeypatch.setattr(utils, "_host_os_key", lambda: "lin")
    monkeypatch.setattr(utils.cpu_vendor, "host_cpu_vendor", lambda: "amd")
    monkeypatch.setattr(utils, "set_cpu_vendor",
                        lambda config, *a: (seen.append(a), cv.set_cpu_vendor(config, *a), seen.append(dict(config))))
    ua = "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:158.0) Gecko/20100101 Firefox/158.0"
    utils.launch_options(config={"navigator.userAgent": ua, "cpu:vendor": "intel"}, i_know_what_im_doing=True)
    assert seen[0] == ("win", "lin", "amd")
    assert seen[1]["cpu:vendor"] == "intel"
