"""
A Windows identity that claims an Intel machine blends canvas pixels with
Intel's reciprocal estimates on any host (LEAKS 279, skia-cpu-vendor-estimates.patch).

Skia's raster pipeline divides with the hardware estimate (SkRasterPipeline_opts.h
rcp_approx = _mm256_rcp_ps on x86-64-v3): color-burn, color-dodge and the
non-separable blends (hue, saturation, color, luminosity) call rcp_fast. Only
12 bits of it are architectural, and Intel and AMD differ in the low bits of
2115 of the 4096 table entries. So the same canvas blended on an AMD host shows
AMD's pixels, whatever the identity claims. The patch gives the claimed vendor's
(cpu:vendor) estimate when it differs from the host's; both vendors' tables are
verified bit-exact (AMD for all 2^32 inputs on a Ryzen 7 9850X3D; Intel RCPPS on
[1,2) against an i9-11900H).

The page is cfparity's rendering.composite (48x48, every globalCompositeOperation)
hashed as cfparity hashes it; the expected hashes are stock Firefox 158.0.1 on
win-i9 (Intel i9-11900H; gate 11 reference, identical on its Intel and NVIDIA
halves and in all 3 launches). Every op must match, including the 22 that never
divide (the control: the host renders the rest exactly as Windows does).
Run on an AMD host it fails without the patch on the 5 dividing ops that gate 11
reported (color, color-burn, color-dodge, luminosity, saturation). On an Intel host
the claim is the host's own vendor and the guard passes either way.

    python browser/tests/playwright/patches/skia-cpu-vendor-estimates.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import launch_kwargs  # noqa: E402

# Stock 158.0.1 on win-i9, rendering.composite (keep-run11 references, window context).
STOCK_INTEL = {
    "color": "b439662eb4de56a9", "color-burn": "ef2564eab7af65cb", "color-dodge": "0ab89dd517a58bfc",
    "copy": "c917a245bb775e6f", "darken": "484e8f11e77a2df8", "destination-atop": "9f84d0661e7e2e8a",
    "destination-in": "3d924ce76e22011c", "destination-out": "b1883af79150e11c",
    "destination-over": "cf3d2f1e026bd90c", "difference": "232ba7584013b8f5", "exclusion": "0e89d2209da49bde",
    "hard-light": "9f9fced1c9ce7214", "hue": "6fe4dfdc1d85fe15", "lighten": "c53f43b1297cccc7",
    "lighter": "0c22f401f9659ece", "luminosity": "4e469fff90de7d42", "multiply": "215d040350d73d04",
    "overlay": "271ca7f10a642037", "plus-darker": "ignored->source-over:fc12bbeb95068493",
    "plus-lighter": "ignored->source-over:fc12bbeb95068493", "saturation": "3b6151c7cdb25275",
    "screen": "a0dc08781d67646c", "soft-light": "379f72f7d6537893", "source-atop": "d9b0d7338e6fd8f1",
    "source-in": "150bd66dba22c358", "source-out": "02969caed4770ff8", "source-over": "fc12bbeb95068493",
    "xor": "d03e659cf8c5b8a3",
}

SCRIPT = """() => {
  const hashBytes = (u8) => {
    let h1 = 0x811c9dc5, h2 = 0x1000193;
    for (let i = 0; i < u8.length; i++) {
      h1 = Math.imul(h1 ^ u8[i], 16777619);
      h2 = Math.imul(h2 + u8[i], 2246822507) ^ (h2 >>> 15);
    }
    return (h1 >>> 0).toString(16).padStart(8, '0') + (h2 >>> 0).toString(16).padStart(8, '0');
  };
  const OPS = %s;
  const out = {};
  for (const op of OPS) {
    const c = document.createElement('canvas'); c.width = 48; c.height = 48;
    const ctx = c.getContext('2d');
    const g = ctx.createLinearGradient(0, 0, 48, 48);
    g.addColorStop(0, 'rgba(255,40,0,0.85)'); g.addColorStop(1, 'rgba(0,90,255,0.6)');
    ctx.fillStyle = g; ctx.fillRect(4, 4, 36, 36);
    ctx.globalCompositeOperation = op;
    const set = ctx.globalCompositeOperation;
    const rg = ctx.createRadialGradient(34, 30, 2, 30, 30, 22);
    rg.addColorStop(0, 'rgba(250,250,0,0.9)'); rg.addColorStop(1, 'rgba(0,160,60,0.4)');
    ctx.fillStyle = rg; ctx.beginPath(); ctx.arc(30, 30, 20, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = 'rgba(120,0,200,0.7)'; ctx.fillRect(14, 34, 24, 10);
    out[op] = (set === op ? '' : 'ignored->' + set + ':') + hashBytes(ctx.getImageData(0, 0, 48, 48).data);
  }
  return out;
}"""


def main() -> int:
    from camoufox.sync_api import Camoufox

    with Camoufox(**launch_kwargs(os="windows", config={"cpu:vendor": "intel"}, canvas_noise=False,
                                  i_know_what_im_doing=True)) as browser:
        page = browser.new_page()
        page.goto("about:blank")
        got = page.evaluate(SCRIPT % json.dumps(sorted(STOCK_INTEL)))
    bad = sorted(op for op in STOCK_INTEL if got.get(op) != STOCK_INTEL[op])
    for op in sorted(STOCK_INTEL):
        print(f"  {op:18s} {got.get(op)}  {'ok' if op not in bad else 'stock ' + STOCK_INTEL[op]}")
    if bad:
        print(f"FAIL: claiming an Intel machine, {len(bad)} composite ops differ from stock win-i9: {', '.join(bad)}")
        return 1
    print("PASS: claiming an Intel machine, every composite op blends as on win-i9")
    return 0


if __name__ == "__main__":
    sys.exit(main())
