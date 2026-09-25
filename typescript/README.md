# camoufox (TypeScript)

This is the JavaScript/TypeScript client for Camoufox. It is a port of the
Python wrapper in [`../pythonlib`](../pythonlib) — it does **not** shell out
to Python.

The two launchers are twins: they read the same `properties.json`, write the
same chunked `CAMOU_CONFIG`, share the same browser install directory, and
ship the same fingerprint presets, font/voice lists, WebGL catalogue, and
GeoIP configuration. They draw the same identities too: fingerprints come from a
TypeScript port of [fpgen](https://github.com/scrapfly/fingerprint-generator)
using the same pinned model, and the per-identity draws (fonts, voices, GPU,
media devices, noise seeds) use a bit-exact port of CPython's `random`, so a
pinned identity presents identically from either language.

## Installation

```bash
npm install @camoufox/camoufox playwright-core
# then download the browser
npx camoufox fetch
```

`playwright-core` is a peer dependency — bring your own version (`<1.63`,
the same ceiling as the Python package). Node 22.15 or newer is required.

## Usage

```javascript
import { Camoufox } from "@camoufox/camoufox";

const browser = await Camoufox({
    // any Camoufox option, plus any Playwright Firefox launch option
    headless: true,
    os: "windows",
    geoip: true,
});

const page = await browser.newPage(); // a Playwright Page
await page.goto("https://example.com");
await browser.close();
```

### Persistent profiles

```javascript
const context = await Camoufox({ user_data_dir: "./profiles/alice" });
const page = await context.newPage();
```

### Per-context identities

`NewContext()` gives each context its own fingerprint — real preset or
fpgen-synthesised — with unique audio/canvas/font-spacing seeds. The
values are applied through `addInitScript`, so the setters self-destruct before
any page script runs.

```javascript
import { Camoufox, NewContext } from "@camoufox/camoufox";

const browser = await Camoufox({ headless: true });
const context = await NewContext(browser, {
    os: "macos",
    proxy: { server: "http://proxy:8080", username: "u", password: "p" },
});
```

When a `proxy` is given and no `webrtc_ip`/`timezoneId` is, both are resolved
from the proxy's exit IP.

### Server mode

```javascript
import { launchServer } from "@camoufox/camoufox";

const server = await launchServer({ headless: true, port: 9222 });
console.log(server.wsEndpoint());
```

Persistent contexts are not servable — Playwright's `launchServer` can only
expose a pre-launched `Browser`.

### Building launch options yourself

```javascript
import { launchOptions } from "@camoufox/camoufox";
import { firefox } from "playwright-core";

const browser = await firefox.launch(await launchOptions({ os: "linux" }));
```

## Options

Every option from the Python `launch_options()` is supported, with the same
snake_case names: `os`, `config`, `block_images`, `block_webrtc`,
`block_webgl`, `disable_coop`, `webgl_config`, `geoip`, `geoip_db`, `humanize`,
`locale`, `addons`, `fonts`, `custom_fonts_only`, `exclude_addons`, `screen`,
`window`, `fingerprint`, `fingerprint_preset`, `ff_version`, `headless`,
`main_world_eval`, `allow_addon_new_tab`, `executable_path`, `browser`,
`firefox_user_prefs`, `proxy`, `enable_cache`, `args`, `env`,
`i_know_what_im_doing`, `debug`, `virtual_display`. Anything else is passed
straight through to Playwright.

Two things differ from the Python signatures, both because of the runtime:

- The returned launch options use Playwright's camelCase keys
  (`executablePath`, `firefoxUserPrefs`) rather than Python's snake_case.
- `headless: "virtual"` is handled by `Camoufox()` / `NewBrowser()` /
  `launchServer()`, not by `launchOptions()` — same as in Python.

## CLI

```
camoufox sync                     # refresh the version catalogue
camoufox fetch [version]          # install the active or a specific version
camoufox set <specifier>          # pin a version, or set a repo/channel
camoufox set --geoip <name>       # choose a GeoIP source
camoufox list [installed|all]     # list versions
camoufox remove [version]         # remove one version, or everything
camoufox remove-geoip             # remove the GeoIP database
camoufox active                   # print the active version
camoufox path                     # print the install directory
camoufox version                  # version / storage info
camoufox test [url]               # open the Playwright inspector
camoufox server                   # launch a Playwright server
```

The Python CLI's `gui` command (a PySide6 desktop app) has no Node equivalent
and is not provided. Where the Python CLI opens an interactive picker, this one
takes the same specifier as an argument instead.

## Development

```bash
pnpm install
pnpm build       # tsc -> dist/, then copy src/data-files
pnpm test        # vitest
pnpm check       # biome lint + format
pnpm typecheck   # tsc --noEmit
```

`src/data-files/` holds copies of the Python package's data, kept
byte-identical by `scripts/sync-identity-data.py` (and `webgl_data.json` is
exported from `webgl_data.db`); a test fails if any copy drifts.

### Parity with pythonlib

The golden tests are what keep the two launchers twins. Scripts under
`scripts/golden/` run the Python code over hundreds of fixed inputs and record
its output in `tests/fixtures/`; the TS tests must reproduce it exactly —
`launch_options()` byte for byte, including the `CAMOU_CONFIG` blob. After a
pythonlib change, mirror it here and regenerate:

```bash
python3 -m venv .venv && .venv/bin/pip install -e pythonlib   # repo root
.venv/bin/python scripts/pin-fpgen-model.py
.venv/bin/python typescript/scripts/golden/identity_golden.py
.venv/bin/python typescript/scripts/golden/launch_golden.py
.venv/bin/python typescript/scripts/golden/fpgen_golden.py
```

The end-to-end suite launches a real browser through both launchers and compares
what a page sees:

```bash
CAMOUFOX_E2E=1 CAMOUFOX_EXECUTABLE=/path/to/camoufox-bin pnpm test tests/e2e.test.ts
```

## Releasing

`.github/workflows/publish-npm.yml` is dispatched by hand, like the PyPI
workflow. It type-checks, lints, tests, builds, runs `scripts/check-pack.mjs`
(the version must equal pythonlib's; every data file must be in the tarball;
the tarball must install and import in an empty project), then publishes with
npm trusted publishing -- no token is stored.
