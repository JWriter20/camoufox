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

## CAPTCHA solving

Camoufox keeps a site from flagging your browser as automated. It does not, by
itself, get you past a challenge that is already on screen. For that there is an
optional integration with
[CaptchaKraken](https://github.com/JWriter20/CaptchaKraken) — OpenCV grid
detection plus a fine-tuned vision model — which drives the visible puzzle to
completion.

```bash
# Python
pip install "camoufox[captcha]"
```

```bash
# JavaScript / TypeScript
npm install captchakraken
```

Pass credentials with the `captcha` launch option. A **token** uses the hosted
service; a **URL** uses your own server. Give it a token and camoufox sends
solves to `https://api.captchakraken.com/v1` by default — you do not also have
to name the endpoint.

```python
from camoufox.sync_api import Camoufox
from camoufox.captcha import solve_captcha

with Camoufox(headless=False, captcha={"token": "ck_live_..."}) as browser:
    page = browser.new_page()
    page.goto("https://example.com/protected")
    print(solve_captcha(page).is_solved)
```

```javascript
import { Camoufox, solveCaptcha } from "camoufox";

const browser = await Camoufox({ headless: false, captcha: "ck_live_..." });
const page = await browser.newPage();
await page.goto("https://example.com/protected");
const result = await solveCaptcha(page);
```

| `captcha=` | Meaning |
| --- | --- |
| `"ck_live_…"` or `{"token": …}` | Hosted service, billed to that key |
| `"http://host:8000/v1"` or `{"url": …}` | Your own vLLM; nothing leaves your network |
| `{"token": …, "url": …}` | Your own endpoint behind an auth proxy |
| `True` | Use `CAPTCHA_KRAKEN_API_KEY` / `VLLM_BASE_URL` already in the environment |

Add `model` to pick which served adapter to ask for, when your server runs more
than one. It defaults to whatever the installed CaptchaKraken release pinned:

```python
Camoufox(captcha={"url": "http://gpu:8000/v1", "model": "captcha-v12"})
```

### Solve them as they appear

`solve_captcha` is a one-shot: it handles whatever is on the page right now.
When you do not know *where* in a script a challenge will interrupt you, install
a watcher instead and let it work underneath your automation.

```python
from camoufox.captcha import watch_captcha

watch_captcha(page).run()          # blocking: hold this page clean

watcher = watch_captcha(page)      # or cooperatively, in your own loop
while working():
    watcher.poll_once()
```

```typescript
import { Camoufox, watchCaptcha } from "camoufox";

const watcher = await watchCaptcha(page);   // returns immediately
await page.goto("https://example.com/protected");
await watcher.stop();
```

Options: `interval_ms` (default 1000), `max_solves`, `error_backoff_ms`,
`on_solved`, `on_error` — camelCased in TypeScript.

**It runs in the isolated world, and injects nothing.** The watcher adds no
script and no binding to the page; it drives CaptchaKraken's own detection from
the driver side on a timer. The DOM reads that probe performs go through
Playwright, which under Camoufox means the sandboxed Juggler world — the same
isolation that already hides Playwright itself, and the reason `main_world_eval`
exists as an opt-*out*. A page can no more see the watcher than it can see
Camoufox. The trade is that a captcha appearing just after a tick waits up to one
`interval_ms`; against a solve measured in seconds, that is not the number that
matters.

The Python watcher blocks and the TypeScript one does not, because a
synchronous Playwright handle is bound to the greenlet that created it and
cannot be driven from a worker thread. `AsyncCamoufox` users cannot use either
solver entry point for the same reason.

An explicit environment variable always wins over the launch option, so a
self-hoster's `VLLM_BASE_URL` is never silently redirected. Check a key before
you rely on it — `verify_credentials()` (`verifyCredentials()` in TS) reports
whether the endpoint answers, the key is accepted, and the account has credit;
a `402` means out of credit.

Handles reCAPTCHA v2 (3×3 dynamic and 4×4 one-shot grids), hCaptcha grids, and
the checkbox / Turnstile flows. Non-grid hCaptcha puzzles are detected and
skipped rather than guessed at. The Python side is synchronous only —
`AsyncCamoufox` users cannot call it, because a sync Playwright handle cannot be
driven from inside an event loop.

**Run it yourself, or don't.** CaptchaKraken is source-available and runs
entirely on your own GPU: point `VLLM_BASE_URL` at your server and no request
leaves your machine. The hosted API is a convenience for people who would rather
not run a 9B vision model; set `CAPTCHA_KRAKEN_API_KEY` to use it.

**Licensing.** CaptchaKraken is source-available under the CaptchaKraken
Source-Available License; the model weights are covered by the same terms. Using
the solver with Camoufox for your own automation is explicitly permitted, free or
commercial. What v1.1 §3(d) restricts is *shipping* it: bundling or advertising
CaptchaKraken as a built-in captcha capability of a stealth/antidetect browser
you distribute to third parties needs a commercial license. This integration is
published by the copyright holder, so Camoufox itself is covered — a fork of
Camoufox that keeps this module and redistributes it is not automatically. See
[LICENSE](https://github.com/JWriter20/CaptchaKraken/blob/main/LICENSE).

**Attribution.** Requests issued through this integration are tagged
`camoufox/<version>` (the `X-CK-Client` header), which is how camoufox-originated
usage is identified and credited. The tag carries no pricing power — the server
derives the billable puzzle class from the request body — and self-hosted users
report nothing to anyone.

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
