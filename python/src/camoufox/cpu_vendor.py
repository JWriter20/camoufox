"""
The CPU vendor an identity claims (cpu:vendor), as far as a page can tell it.

Skia's raster pipeline divides with the x86 reciprocal estimate (RCPPS), whose
low bits differ between Intel and AMD; a page reads them back from any canvas
blend that divides (color-burn, color-dodge, hue, saturation, color,
luminosity). The browser gives the claimed vendor's estimate when it is not
the host's (skia-cpu-vendor-estimates.patch), so an identity that claims an
Intel machine blends as Intel CPUs do on an AMD host, and the reverse.

Most identities name no CPU: the host's vendor shows, as it always did. The
claim is set only where the identity itself implies a vendor: a caller's
`cpu:vendor`, or a WebGL renderer that is an integrated GPU, which only comes
with its own vendor's CPU (Intel HD/UHD/Iris/Arc integrated graphics; AMD's
APU "Radeon(TM) Graphics", "Radeon Vega" and "Radeon 780M"-style parts). A
discrete GPU says nothing about the CPU and leaves it unset. Nothing is set for
an identity of the host's own OS, which claims the host's GPU.
"""

import platform
import re
from typing import Any, Dict, Optional

CPU_VENDOR_KEY = 'cpu:vendor'
VENDORS = ('intel', 'amd')

# Intel's discrete Arc cards (A380/A750/A770, B570/B580, Pro A-series) pair
# with any CPU; every other Intel renderer is the CPU's own graphics.
_INTEL_DISCRETE = re.compile(r'Arc\(TM\)\s+(Pro\s+)?[AB]\d{2,3}', re.I)
# AMD integrated graphics, every APU name in fpgen's corpus: the APU's "AMD
# Radeon(TM) Graphics" (also "Radeon (TM)"), Vega N and RX Vega N, the
# Kabini/Mullins/Carrizo "R2"-"R7 Graphics", the 6x0M-8x0M/890M parts, and the
# AMD-chipset "Radeon HD NNNN Graphics". Discrete Radeons have RX NNN, R7 M265,
# "610 Series"-style names. Older Mesa names an APU by its codename only
# ("AMD RENOIR", "REMBRANDT (rembrandt LLVM ...)").
_AMD_INTEGRATED = re.compile(
    r'Radeon\s?(\(TM\))?\s+(Graphics\b|(RX\s+)?Vega\b|R[2-7]\s+Graphics\b|HD\s+\d{4}\s+Graphics\b|\d{3}M\b)|\b(RENOIR|REMBRANDT|RAVEN2?|PICASSO|CEZANNE|LUCIENNE|BARCELO|PHOENIX|MENDOCINO|VANGOGH)\b', re.I
)


def host_cpu_vendor() -> Optional[str]:
    """'intel' or 'amd' for this machine's CPU, None for anything else (ARM, unknown)."""
    text = ''
    try:
        with open('/proc/cpuinfo', encoding='ascii', errors='replace') as f:
            for line in f:
                if line.startswith('vendor_id'):
                    text = line
                    break
    except OSError:
        text = platform.processor() or ''
    if 'GenuineIntel' in text or text.startswith('Intel'):
        return 'intel'
    if 'AuthenticAMD' in text or text.startswith('AMD'):
        return 'amd'
    return None


def implied_cpu_vendor(renderer: Optional[str]) -> Optional[str]:
    """The CPU vendor a WebGL renderer string implies, or None (a discrete GPU, or unknown)."""
    if not renderer:
        return None
    if 'Intel' in renderer and not _INTEL_DISCRETE.search(renderer):
        return 'intel'
    if _AMD_INTEGRATED.search(renderer):
        return 'amd'
    return None


def set_cpu_vendor(
    config: Dict[str, Any], target_os: str, host_os: Optional[str], host_vendor: Optional[str]
) -> None:
    """Validate a caller's cpu:vendor, or claim the one the identity's GPU implies."""
    if CPU_VENDOR_KEY in config:
        value = str(config[CPU_VENDOR_KEY]).lower()
        if value not in VENDORS:
            raise ValueError(f'cpu:vendor must be one of {", ".join(VENDORS)}, not {config[CPU_VENDOR_KEY]!r}')
        config[CPU_VENDOR_KEY] = value
        return
    if target_os == host_os:
        return
    implied = implied_cpu_vendor(config.get('webGl:renderer'))
    # The host's own vendor needs no claim: the browser's estimate is already its.
    if implied and implied != host_vendor:
        config[CPU_VENDOR_KEY] = implied
