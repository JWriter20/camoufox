# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Camoufox is an anti-detect fork of Firefox for web scraping and automation. The distinguishing design choice is that fingerprint spoofing happens at the **C++/Juggler implementation level**, not via injected JavaScript, so it is invisible to page-side inspection.

This is a **monorepo with three top-level parts**. Know which one you are in before you start:

| Directory | What it is |
| --- | --- |
| `browser/` | The browser. **Not the Firefox source** — a *build system* that fetches upstream Firefox, applies a stack of patches + code additions, and produces the hardened binary. Everything below about builds and patches happens here. |
| `python/` | The `camoufox` PyPI package: the Playwright-compatible Python launcher. Sources in `python/src/`. |
| `typescript/` | The `camoufox` npm package: the Playwright-compatible JS/TS launcher. Sources in `typescript/src/`. |

The two launchers are **twins** — same `properties.json`, same chunked `CAMOU_CONFIG`, same `~/.cache/camoufox` install dir and `version.json`, same presets/fonts/voices/WebGL/GeoIP data. **A behaviour change in one belongs in the other.** Module names line up one-to-one (`python/src/pkgman.py` ↔ `typescript/src/pkgman.ts`); the exceptions are `locales.py` + `geolocation.py`, which the TS side merges into `locale.ts`, and `sync_api.py` + `async_api.py`, which merge into `sync_api.ts`.

Both packages keep their sources under `src/`, but the Python **import** name is still `camoufox` — `pyproject.toml` maps the directory at build time (`packages = [{ include = "*", from = "src", to = "camoufox" }]`) and `python/conftest.py` does the same for an uninstalled working tree. So `from camoufox.utils import ...` is correct everywhere; `from src.utils import ...` is never correct.

Root also holds `build-tester/` and `service-tester/` (cross-cutting QA suites, see Testing) and `.github/` (CI: `build.yml` runs in `browser/`, `publish-pypi.yml` in `python/`).

### Inside `browser/`

The actual Firefox tree lives in `browser/camoufox-<version>-<release>/` (e.g. `camoufox-152.0.4-beta.28/`), created by the build. That directory is generated — never edit it directly to make lasting changes; changes there are captured as patches (see "Making patches" below).

`upstream.sh` pins `version` / `release`, and is sourced+exported by the `Makefile`, so those variables flow into every script.

## Build commands

**All of these run from `browser/`.** The build system is designed for **Linux**. Windows and macOS binaries are **cross-compiled from Linux** — they are never built natively. (`scripts/install-deps.sh` covers macOS/Linux host dependencies for local `make dir` + bootstrap experimentation; a full production build path is Linux/Docker.)

```bash
cd browser                     # every command in this section runs here
bash scripts/install-deps.sh   # install host build deps (Python ≥3.11, Rust, aria2, p7zip, go, msitools, wget, sqlite)
make dir                       # fetch Firefox source, extract, copy additions/settings, apply all patches → touches _READY
make bootstrap                 # install system deps (apt/dnf/pacman) + run `mach bootstrap` (one-time)
make build                     # ./mach build in the source dir
make run                       # run the built browser (wipes ~/.camoufox profile)
make run args="--headless https://test.com"
python3 multibuild.py --target linux windows macos --arch x86_64 arm64 i686   # full cross-platform build + package
```

`make dir` is the pipeline that matters: `setup` (fetch tarball via `aria2c` → extract → `copy-additions.sh`) → `python3 scripts/patch.py` (applies every patch, writes `mozconfig`) → `_READY`. `mach` requires **Python ≥ 3.11** (stdlib `tomllib`); older `python3` crashes with `ModuleNotFoundError: No module named 'tomllib'`.

Docker is the portable path (context is `browser/`): `docker build -t camoufox-builder browser/` then `docker run -v "$(pwd)/dist:/app/dist" camoufox-builder --target <os> --arch <arch>`.

Packaging: `make package-linux|package-macos|package-windows arch=<arch>` (wraps `scripts/package.py`). Launcher (Go): `make build-launcher arch=<arch> os=<os>`.

## Working with patches (the core workflow)

All paths in this section are relative to `browser/`. Almost all browser-behavior changes are `patches/*.patch` (~49 patches: `fingerprint-injection.patch`, `webgl-spoofing.patch`, `navigator-spoofing.patch`, `webrtc-ip-spoofing.patch`, the `playwright/` and `librewolf/` and `ghostery/` subdirs, etc.). Do not hand-edit patch files.

Use the developer UI instead:

```bash
make edits          # launches scripts/developer.py — apply/undo/create/manage patches
```

- **New patch:** in the UI "Reset workspace" → edit files in `camoufox-*/` → `make build` / `make run` to test → "Write workspace to patch".
- **Edit existing patch:** "Edit a patch" (resets workspace to that patch's state) → edit → "Write workspace to patch" to overwrite.

Low-level equivalents: `make patch ./patches/x.patch`, `make unpatch ./patches/x.patch`, `make workspace ./patches/x.patch`, `make revert` (reset to `unpatched` tag), `make diff` (diff against `first-checkpoint`). The source dir is a git repo with `unpatched` / `first-checkpoint` / `checkpoint` tags used by these targets.

## Repository layout (the parts that require cross-file understanding)

Paths below are relative to `browser/` unless the entry says otherwise.

- **`patches/`** — the diffs applied to Firefox source. This is where browser behavior is changed.
- **`additions/`** — whole files copied *into* the source tree (not diffs) by `scripts/copy-additions.sh`:
  - `additions/camoucfg/` — the C++ config layer. `MaskConfig.hpp` reads the spoofing config (from `CAMOU_CONFIG` env var / `camoufox.cfg`) that the patches consult at the C++ level; `MouseTrajectories.hpp` is the human-cursor algorithm.
  - `additions/juggler/` — Camoufox's patched **Juggler** (Firefox's Playwright automation protocol, the Firefox analog of CDP). This is where Playwright is made undetectable — the page agent runs in an isolated scope so injected automation JS is not visible to the page.
- **`settings/`** — `camoufox.cfg`, `chrome.css`, `properties.json`, `camoucfg.jvv`, prefs/policies. Copied into the source's `lw/` dir by `copy-additions.sh`. Edit the built config with `make edit-cfg`.
- **`scripts/`** — `patch.py` (the patcher, LibreWolf-derived), `developer.py` (the `make edits` UI), `package.py`, `copy-additions.sh`, `install-deps.sh`.
- **`../python/`** (repo root) — the `camoufox` PyPI package: the Playwright-compatible Python interface that generates + injects fingerprints via BrowserForge and launches the binary. Sources in `python/src/`, imported as `camoufox` (see above). `fingerprint-presets-v150.json` holds real scraped fingerprints. This is the user-facing API; the browser binary is the backend.
- **`../typescript/`** (repo root) — the `camoufox` npm package: a **port** of `python/` (it does not shell out to Python). `src/data-files/` is generated from `python/src/` (`webgl_data.json` is converted from the SQLite `webgl_data.db`); the YAML data files become type-checked modules under `src/mappings/`. `playwright-core` is pinned to the same `<1.61` range as `pyproject.toml` — newer versions send Juggler params this browser rejects.
- **`jsonvv/`** — JSON-with-validation format library used for `camoucfg.jvv` (config schema).
- **`legacy/launcher/`** — Go launcher binary.
- **`assets/`** — `base.mozconfig` and other build inputs.

## Testing

Two suites, **both required for PRs** (they cover different layers):

- **`build-tester/`** — tests the raw binary directly (bypasses the Python package); fingerprints injected via `generate_context_fingerprint` + `addInitScript` and `CAMOU_CONFIG`. Run when changing patches / C++ / JS browser layer:
  ```bash
  cd build-tester && npm install && pip install -r requirements.txt
  python scripts/run_tests.py /path/to/camoufox-binary
  ```
- **`service-tester/`** — tests the Python package / service layer.
- **`browser/tests/`** — Playwright tests, run via `cd browser && make tests` (add `headful=true` for headful): points at `camoufox-*/obj-*/dist/bin/camoufox-bin`.
- **`typescript/tests/`** — vitest unit tests for the TS launcher (no browser needed). Run when changing `typescript/`:
  ```bash
  cd typescript && pnpm install && pnpm test && pnpm check && pnpm typecheck
  ```
- **`python/tests/`** — pytest unit tests for the Python launcher (no browser needed): `cd python && python -m pytest tests`.

`ccache` is enabled in the build config — install it for fast incremental rebuilds (cold ~40 min, incremental ~5 min).

## Constraints when editing this repo

- The `browser/camoufox-*/` source directory is regenerated — persist changes as patches, never as edits committed to that tree.
- Keep the `browser/Makefile` diff clean against `main` unless a change genuinely belongs there — dependency setup lives in `browser/scripts/install-deps.sh`, not the Makefile.
- Never add a bare `/browser/` ignore to the root `.gitignore`. Upstream Firefox trees ignore that path as a build-output dir; here it is our source folder. Firefox-build ignores stay scoped inside `browser/.gitignore`.
- Every PR must be tied to a GitHub issue and pass both test suites (see `CONTRIBUTING.md`).
