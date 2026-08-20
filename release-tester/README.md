# Camoufox Release Tester

Verifies one **packaged** build, on the OS it was built for, and publishes what
it found. This is the suite CI runs between `build` and `release`, so every
released binary ships evidence rather than an assumption.

Two questions, two suites:

| Suite | Question | Source |
| --- | --- | --- |
| `playwright` | Does the build still drive? | the upstream Playwright suite in [`browser/tests`](../browser/tests) |
| `sundial` | What does it leak, by category? | [sundial](https://sundial.daijro.dev)'s `/automated` scan |

## How it differs from the other suites

| | `build-tester/` | `service-tester/` | `release-tester/` |
| --- | --- | --- | --- |
| Input | a raw binary | `pip install camoufox` | an extracted release **package** |
| Runs on | your machine | your machine | the OS the package targets |
| Measures | in-house antibot checks | the service layer | Playwright functionality + sundial leaks |
| Output | a score + certificate | pass/fail | `results.json` + `summary.md` |

## Usage

```bash
pip install -r requirements.txt
pip install "../python[geoip]"             # the launcher, for the sundial scan
                                           # (not -e: poetry-core cannot remap
                                           #  src/ -> camoufox in editable mode)
pip install -r ../browser/tests/ci-requirements.txt

python run.py \
    --package-dir ./unpacked \
    --target linux --arch x86_64 \
    --version 152.0.4 --release beta.29 \
    --out ./results
```

`--package-dir` is wherever you unzipped `camoufox-<version>-<release>-<os>.<arch>.zip`.
The binary inside it is located per-platform, one directory level deep if needed.

Useful flags: `--suite playwright|sundial|all`, `--profiles windows,macos,linux`,
`--fail-on-leaks`, `--no-geoip`, `--keep-network-detail`, `--per-test-timeout`.

## The sundial scan

`/automated` is **not** opened with a username and password. sundial matches
that route *before* its cookie session check
(`functions/_middleware.js`), so a login session does not reach it — the route
authenticates on a `key` query parameter alone and answers `401` without one:

```
GET /automated?key=<AUTOMATION_KEY>&mode=raw
```

The key must equal sundial's `AUTOMATION_PRIVATE_KEY` or
`AUTOMATION_GUEST_KEY`. **Use the private key**: the guest role is served a
stubbed `vectors-private.js`, so a guest scan silently runs fewer detection
vectors and reports a cleaner build than you actually have.

Pass it as `--sundial-key` or `SUNDIAL_AUTOMATION_KEY`. Unset, the scan is
skipped and the Playwright half still reports — that is a pipeline gap, not a
failing build, so it does not flip the verdict.

The page then runs the scan client-side and marks its own terminal state, which
is what the driver waits on:

| | |
| --- | --- |
| `data-sundial-state` | `scanning` → `finalizing` → `ready` \| `error` |
| `#report-raw` | the full report JSON, once ready |

`mode=raw` is load-bearing: without it the page boots a virtualized viewer and
`#report-raw` never holds the whole document.

### Why one scan per emulated OS

`--profiles` launches the build once per OS it claims to be. Reading a category
across the columns of the summary table is what separates a leak that is always
present from one that only appears when the build claims to be something it is
not — the cross-OS emulation question.

### geoip is on by default

sundial cross-checks the claimed timezone and locale against the geo-IP of the
connecting address. Without `geoip`, a scan fails Locale and Network on the
runner's own configuration rather than on anything the build did. `--no-geoip`
turns it off when you are offline or deliberately testing that path.

### Runner IP is redacted

A sundial report carries the scanning machine's public IP, city, ASN and geo-IP
derivation. The saved reports drop those fields, because these results get
attached to a public release. `--keep-network-detail` keeps them for local
debugging. Category tallies are untouched either way.

## Output

```
results/
  results.json      everything, machine-readable
  summary.md        the job summary / release body section
  junit.xml         raw pytest output
  sundial-raw/
    windows.json    the full (redacted) sundial report, per profile
    macos.json
    linux.json
```

`combine.py --results-root <dir> --out RESULTS.md` folds several targets'
`results.json` into the single document the release body uses.

## Tests

The parsing, reporting and failure paths are unit-tested and need no browser:

```bash
python -m pytest tests -q
```
