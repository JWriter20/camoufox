# Tribal knowledge

The things that are true about this repository but are not written in the code,
and that cost someone a wasted build to find out. Read this before touching
patches, tests, or the harness — whether you are a person or an agent.

Kept short on purpose. If an entry stops being true, delete it; a stale warning
is worse than none, because people route around the ones they have caught
lying.

---

## The shape of the thing

**This repo is not Firefox.** It is a build system that fetches Firefox, applies
`patches/`, copies `additions/` in whole, and produces a browser. Almost every
behaviour change is a patch.

**`camoufox-<version>-<release>/` is generated.** It is scratch space. Edit it
to try a fix; the fix only survives if you write it back to `patches/`. The
directory is rebuilt from the tarball on every run and is never committed.

**Spoofing lives in C++ and Juggler, not in injected JS.** That is the entire
reason this fork exists. A fix that works by injecting JavaScript into the page
is not a fix, it is a regression with a passing test — it is visible to the page
and therefore detectable.

**`upstream.sh` is the pin.** The `Makefile` sources and exports it, so
`version` and `release` flow into every script. Change it there and nowhere
else.

---

## Patches

**Reject line numbers are always wrong.** They are from the old Firefox. Find
the code by searching for a distinctive symbol from the hunk, never by line.

**Firefox moves code far more often than it deletes it.** Before concluding a
patch is obsolete, search the whole tree for the function. `docs/patch-upgrading-guide.md`
has the reject taxonomy and the `userContextId` extraction patterns.

**Never `git reset` or `git clean` inside `camoufox-*/`.** It deletes untracked
files the build needs. Use `make clean`, or the harness's `patchset.reset_tree`.

**Regenerate a patch with both diffs, in this order.** Staged first, then
unstaged, or new files silently vanish from the patch:

```bash
git add path/to/new/file.cpp
git diff --cached --binary >  /tmp/x.patch
git diff --binary          >> /tmp/x.patch
```

**A patch that applies is not a patch that works.** The two failure modes that
get through the patch gate are a hunk landing in the wrong function and a hunk
becoming a no-op. `tests/patches/*.py` exists to catch exactly this — that is
why the patch-guard gate allows zero failures.

**Never make the stack apply by deleting a patch.** If Firefox genuinely made
one redundant, say so and leave it for a human. Deleting it is indistinguishable
from silently dropping a spoofing behaviour.

**Roverfox patches apply last.** `scripts/patch.py` partitions them out and
applies them after everything else; the harness's applier mirrors that. Order
matters — `1-leak-fixes.patch` undoes parts of `0-playwright.patch`.

---

## Tests

**The two Playwright suites are not the same suite and are not interchangeable.**

- `tests/` is a *maintained fork* of a roughly v1.55-era playwright-python
  suite, carrying ~1800 semantic lines of Camoufox adaptations. Because it is
  frozen, every test in it has a known prior outcome — which is what makes it
  the **regression** gate.
- The **upstream** suite is fetched fresh per run at the tag Playwright shipped
  for the Firefox being targeted. It is the **conformance** gate. Its failures
  are ambiguous by nature (did we break it, or did upstream tighten it?), which
  is why they must be triaged into the expectations file with a reason.

Regenerating `tests/` from upstream would throw away those 1800 lines. Do not.

**Most of the diff between the two suites is formatting, not meaning.** The
vendored copy is formatted at a wider line length than upstream's black profile.
Normalise before comparing or you will conclude the fork is three times more
divergent than it is.

**`.disabled` is the marker for a test Camoufox cannot pass by design** —
page-world `evaluate`, User-Agent override, Chromium/WebKit-only behaviour.
Adding one is a policy decision, not a fix.

**The upstream suite runs with world isolation off.** It asserts upstream
semantics: tests read globals their own page scripts set, and pass handles into
`evaluate()`. Camoufox isolates by default, so ~37 tests fail on "X is not
defined" for a global the page really did set. `harness/pw_camoufox_plugin.py`
disables isolation for that suite alone. Camoufox's actual isolated-world
behaviour is covered by `tests/patches/isolated-evaluate.py`, which must keep
passing *without* that flag — that is the file to check if isolation regresses,
not the conformance suite.

**Patch guards need the Python package on `PYTHONPATH` and fonts staged.**
`make stage-fonts` exists for this; without it they fall back to the system
fontconfig and the font assertions go strange.

**The build-tester uniqueness slot.** Cross-profile uniqueness draws random
fingerprints, so one collision across the eight slots is the birthday paradox,
not a leak. Policy tolerates exactly one. Two in the same run is a real signal.
Historically the colliding slot moves between runs (macOS Screen, Linux Screen,
macOS Canvas) — a slot that collides *repeatedly* is the thing to investigate.

**A suite that collapses looks like a suite that passed** if you only count
failures. `min_tests_collected` in `policy.yml` is there because an import error
early in collection produces a beautifully green run of forty tests.

---

## Sundial is private

**Never write a vector name, description, value, or source anywhere.** Not in an
evidence file, not in a PR comment, not in a commit message, not in a log line.
This repository is public and a workflow artifact is world-readable. Sundial's
worth is that the vectors are not public; one leak is permanent.

`harness/gates/sundial.py::redact()` is the trust boundary. Everything upstream
of it holds the full report; everything downstream holds counts and HMAC'd ids.
Do not move data across it, and do not add a debug print that straddles it.

**Only gate on what Camoufox claims.** `gated_categories` in `policy.yml` is the
list. Cross-OS rendering parity is deliberately excluded: Camoufox does not
claim byte-identical emulation of another platform's rasterizer, so failing a
build over it would be gating on a promise nobody made. Those vectors are still
measured and reported — just not fatal.

**To excuse one vector**, use `python3 -m harness.gates.sundial waive --key ...`.
It prints a stanza with an opaque id, so the waiver can live in a public file
without naming the vector.

**If the session cookie expires, the scan silently "passes" nothing.** Sundial
serves the login page with HTTP 200 for any unauthenticated GET, so a stale
cookie yields a page that never posts a report. The gate treats a missing report
as a failure for exactly this reason. If the stealth gate starts timing out,
check the credentials before you check the browser.

---

## The harness

**Absence is failure, never a skip.** A required gate that produced no evidence
fails the run. This is the property that makes the whole thing worth trusting:
you cannot go green by removing a check.

**Evidence is stamped with the run id.** A leftover file from an earlier attempt
does not satisfy a gate.

**A test that vanished is a regression.** Deleting a failing test is the
cheapest possible way to make a suite go green, so `treat_missing_as_regression`
counts a baseline-passing test that is absent as a failure.

**The agent cannot edit the gates.** `repair.forbidden_paths` covers `harness/`,
`tests/`, `build-tester/`, `service-tester/` and `.github/workflows/`. A write
there voids the run rather than being reverted, because reaching for a gate says
something about the rest of the output.

**Waivers expire.** `verify.py` fails on an expired one. This is deliberate
friction: a permanent excuse is a decision that deserves to be re-made out loud
once a quarter.

**Update the baseline only from a green run**, and only when you have read what
changed. `verify.py --update-baseline` refuses on a failing run, but it cannot
tell you whether the green run was green for the right reasons.

**Bootstrap mode is not a pass.** `playwright_upstream` starts with
`bootstrap: true`, which records and reports without gating. It stays a
non-gate until someone triages the seeded expectations and flips it to `false`.
Leaving it on forever quietly removes the conformance gate.

---

## Builds and CI

**Cold build ≈ 40 minutes, warm ≈ 5.** `ccache` is enabled in the mozconfig; if
it is missing from the runner image, `configure` fails outright with "Cannot
find ccache" rather than degrading.

**`mach` needs Python ≥ 3.11** (stdlib `tomllib`). An older `python3` dies with
`ModuleNotFoundError: No module named 'tomllib'`, which reads like a missing
dependency and is not.

**Linux x86_64 only, for the harness.** Windows and macOS are cross-compiled and
are the release workflow's job. Gating a Firefox bump does not need them, and
building all seven targets would not fit in a GitHub-hosted job.

**Rust parallelism is capped in CI** (`CARGO_BUILD_JOBS: 1`) plus 24 GB of swap.
Without both, the link step is OOM-killed and reports as a mysterious SIGTERM.

**Keep the `Makefile` diff clean against `main`.** Dependency setup belongs in
`scripts/install-deps.sh`.

---

## Releases

**Every PR needs a linked issue and both test suites** (`CONTRIBUTING.md`).
The harness opens a PR and never merges one.

**`PLAYWRIGHT_BROWSER_FLOORS` couples the Python package to browser builds.**
`pythonlib/camoufox/__version__.py` maps a Playwright version to the minimum
browser build that works with it. A floor pointing at a build that is still only
a prerelease resolves to something users cannot install — which is what the
`0.5.6b1` pre-release existed to work around. Bump the floor only once the
browser build it names is a real release.
