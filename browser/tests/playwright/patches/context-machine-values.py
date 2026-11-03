r"""
A NewContext() identity runs on its browser's machine.

One browser is one machine: a context is another user of it. A context drew
its own GPU, screen, fonts and voices, but the WebGL limits, window geometry,
glyph rendering and network adapter have no per-context setter and stayed the
launch identity's: cfparity's cf-ctx-other read "GeForce GTX 980" beside
another GPU's MAX_TEXTURE_SIZE and ALIASED_POINT_SIZE_RANGE, and screenX/Y,
outer size and the ICE candidates of the launch (gate p1-lin-it8, LEAKS 619).
A page reads a GPU name with foreign limits in one call. NewContext() now takes
every machine-level value from the launch identity (machine_config).

The guard reads the machine's values in a page of the launch's own context and
in a NewContext() page of the same browser; they must agree.

    python browser/tests/playwright/patches/context-machine-values.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import launch_kwargs  # noqa: E402

# Read in the page's own world, as a site does: the isolated world reads typed
# arrays over Xrays, which Firefox forbids.
PAGE = "data:text/html,<title>machine</title>"

READ = """() => {
  const gl = document.createElement('canvas').getContext('webgl');
  const ext = gl && gl.getExtension('WEBGL_debug_renderer_info');
  const p = (k) => { const v = gl.getParameter(k); return v && v.length !== undefined ? Array.from(v) : v; };
  return {
    renderer: ext ? gl.getParameter(ext.UNMASKED_RENDERER_WEBGL) : null,
    vendor: ext ? gl.getParameter(ext.UNMASKED_VENDOR_WEBGL) : null,
    maxTextureSize: gl && p(gl.MAX_TEXTURE_SIZE),
    aliasedPointSizeRange: gl && p(gl.ALIASED_POINT_SIZE_RANGE),
    maxViewportDims: gl && p(gl.MAX_VIEWPORT_DIMS),
    screen: [screen.width, screen.height, screen.availWidth, screen.availHeight, screen.colorDepth],
    window: [outerWidth, outerHeight, screenX, screenY],
    platform: navigator.platform,
    hardwareConcurrency: navigator.hardwareConcurrency,
  };
}"""


def main() -> int:
    from camoufox.sync_api import Camoufox, NewContext

    with Camoufox(**launch_kwargs(os="windows", canvas_noise=True, main_world_eval=True,
                                     i_know_what_im_doing=True)) as browser:
        launch_page = browser.new_page()
        launch_page.goto(PAGE)
        launch = launch_page.evaluate(f"mw:({READ})()")
        context = NewContext(browser)
        page = context.new_page()
        page.goto(PAGE)
        ctx = page.evaluate(f"mw:({READ})()")
        context.close()

    failures = []
    for key in launch:
        print(f"  {key:22s} launch {launch[key]!s:60.60s} context {ctx[key]!s:.60s}")
        if ctx[key] != launch[key]:
            failures.append(f"{key}: the context reads {ctx[key]!r}, its machine {launch[key]!r}")
    if ctx["renderer"] != launch["renderer"] and ctx["maxTextureSize"] == launch["maxTextureSize"]:
        failures.append(f"the context's GPU {ctx['renderer']!r} reports the launch GPU's limits "
                        f"({launch['renderer']!r}): an impossible GPU")
    for failure in failures:
        print(f"FAIL: {failure}")
    if failures:
        return 1
    print("PASS: a context reads its browser's machine.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
