# Camoufox

Camoufox is an anti-detect fork of Firefox for web scraping and automation.
Fingerprint spoofing happens at the **C++/Juggler implementation level**, not
via injected JavaScript, so it is invisible to page-side inspection.

This repository is a monorepo with three parts:

| Directory | What it is |
| --- | --- |
| [`browser/`](browser) | The browser itself: a build system that fetches upstream Firefox, applies a stack of patches + code additions, and produces the hardened binary. Start here to build or to change browser behaviour. |
| [`python/`](python) | The `camoufox` PyPI package — the Playwright-compatible Python launcher. |
| [`typescript/`](typescript) | The `camoufox` npm package — the Playwright-compatible JS/TS launcher. A port of the Python one, not a wrapper around it. |

The two launchers are twins: same `properties.json`, same chunked
`CAMOU_CONFIG`, same `~/.cache/camoufox` install directory, same fingerprint
data. **A behaviour change in one belongs in the other.**

Both keep their sources under `src/`. The Python import name is still
`camoufox` — `pyproject.toml` maps `python/src/` onto it when the wheel is
built, and `python/conftest.py` does the same for an uninstalled checkout, so
`from camoufox.sync_api import Camoufox` is correct in every context.

## Quick start

Using the browser (you do not need to build it — the launchers download a
release binary on first use):

```bash
# Python
pip install camoufox[geoip] && python -m camoufox fetch
```

```bash
# JavaScript / TypeScript
npm install camoufox playwright-core && npx camoufox fetch
```

```python
from camoufox.sync_api import Camoufox

with Camoufox(headless=True) as browser:
    page = browser.new_page()
    page.goto("https://example.com")
```

```javascript
import { Camoufox } from "camoufox";

const browser = await Camoufox({ headless: true });
const page = await browser.newPage();
await page.goto("https://example.com");
```

Building the browser from source, the patch workflow, and the full
fingerprint-property reference all live in
[`browser/README.md`](browser/README.md).

## Testing

| Suite | Covers | Run |
| --- | --- | --- |
| [`build-tester/`](build-tester) | The raw binary, bypassing the launchers | `cd build-tester && python scripts/run_tests.py /path/to/camoufox-bin` |
| [`service-tester/`](service-tester) | The packaged Python wheel end to end | `cd service-tester && bash run_tests.sh` |
| [`browser/tests/`](browser/tests) | Playwright tests against a local build | `cd browser && make tests` |
| [`typescript/tests/`](typescript/tests) | The TS launcher (no browser needed) | `cd typescript && pnpm test` |
| [`python/tests/`](python/tests) | The Python launcher (no browser needed) | `cd python && python -m pytest tests` |

## Contributing

See [`browser/CONTRIBUTING.md`](browser/CONTRIBUTING.md). Every PR must be tied
to a GitHub issue and pass the test suites above.

## License

MPL-2.0. See [`browser/LICENSE`](browser/LICENSE).
