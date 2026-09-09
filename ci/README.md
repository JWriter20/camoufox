# The test pipeline

Everything that runs a suite against Camoufox. Driven identically from a pull
request, a push to main, and the Firefox auto-update harness — so there is one
definition of "the tests pass", not two.

```
resolve ──┬─ static ────────── tribal rules, skiplist, self-tests   (seconds)
          ├─ pythonlib ─────── the package's own tests               (a minute)
          └─ build ──┬─ playwright upstream × 6 shards               (conformance)
                     ├─ playwright vendored                          (regression)
                     ├─ native ───────────── leaks, contexts         (ours)
                     ├─ patch guards ─────── one per spoofing patch
                     ├─ build-tester ─────── 8 fingerprint profiles
                     └─ sundial ──────────── stealth grade
                                    │
                                 summary ──► one comment on the PR
```

## Which browser, which suite

`ci/versions.py` answers both, and every entry point uses it:

- **browser** — from `upstream.sh`, or whatever a caller passes in. The harness
  passes the Firefox version it is moving to, which is what lets one pipeline
  test both a pull request and an upgrade.
- **suite** — the newest *released* playwright-python tag whose pinned Firefox is
  not ahead of that browser.

Newest-not-ahead, rather than an exact match, because Playwright trails Firefox
by weeks: requiring a match would leave most of a release cycle with no suite,
and taking a newer one would test against an automation contract that assumes
engine work the build does not have.

```bash
python3 -m ci.versions --json                          # what would run
python3 -m ci.versions --browser-version 153.0.4 --json
```

## The two Playwright suites

`tests/` is a maintained fork of a ~v1.55-era upstream suite carrying roughly
1800 semantic lines of Camoufox adaptations. Being frozen is the point: every
test in it has a known prior outcome, so it is the **regression** check.

The upstream suite is fetched fresh per run at the resolved tag and is the
**conformance** check — it knows about tests written after the fork stopped
tracking upstream. It runs unmodified; `ci/pw_camoufox_plugin.py` adapts the
environment around it rather than editing it, by hooking `BrowserType` at the
`_impl` layer so upstream can refactor its fixtures freely.

Regenerating `tests/` from upstream would discard those 1800 lines. The pipeline
never does.

## Main-world execution, and the skip list

The upstream suite runs with world isolation **off**. It asserts upstream
semantics — tests read globals their own page scripts defined and pass handles
into `evaluate()` — and about 37 of them fail on "X is not defined" otherwise.
Camoufox's actual isolated-world behaviour is covered by
`tests/patches/isolated-evaluate.py`, which must keep passing *without* that
flag. That file is what to check if isolation regresses, not this suite.

With main world on, 1392 of 1592 upstream tests run. The other 200 are
deselected by [`ci/skiplist.yml`](skiplist.yml), which requires a stated reason
per entry — `ci/summarize.py` fails the run on an unreasoned one, because a skip
list that can grow silently is a way to make any failing test disappear. The
current entries are all one of: synthesized input (Camoufox humanizes it),
User-Agent override (resolved from the fingerprint, deliberately not
overridable), or another engine's tests.

## Camoufox's own suite

`native-tests/` covers what neither Playwright suite can ask about:

- **Leaks.** Launch browsers, kill them, prove nothing survived — file
  descriptors, sockets, child processes, X11 lock files. The real assertion is
  that cost does not *scale* with launch count, because that is the shape a leak
  actually has: a scraper that runs fine for six hours and then dies of EMFILE.
  Scope is honest: this measures resources held by our process and its children,
  not Gecko's internal heap.
- **Contexts versus browsers.** Two contexts in one browser must get different
  fingerprints; two pages in one context must get the same one. Get this wrong
  and per-context injection silently degrades to process-global — which passes
  every single-context test there is. It has happened here before (commit
  `d17c887`, "fix screen size leak in contexts").
- **Settled decisions.** `ci/tribal-rules.yml` lists choices this project already
  made, each with the issue or PR that made it, and
  `native-tests/test_tribal_rules.py` asserts them. A comment explaining a
  decision only works on someone who reads it.

## Sundial

The stealth check reports **a letter grade and a count**. Nothing else leaves
`ci/run_sundial.py::redact()` — not a vector name, description, measured value,
source, and not a per-category breakdown either: a table reading "Graphics 3/17"
is the most useful single fact an adversary could take from a public CI log.

Identities in the results file are HMACs, which is enough to notice "the check
that passed last release is failing now" and not enough to learn what it was.
Scope and thresholds live in [`ci/sundial.yml`](sundial.yml); only categories
Camoufox actually claims are gated.

Needs `SUNDIAL_USERNAME` and `SUNDIAL_AUTOMATION_KEY`. Absent — a pull request
from a fork — the job is skipped and the summary says so.

## Running a piece by hand

```bash
python3 -m ci.run_playwright --suite upstream --binary path/to/camoufox-bin
python3 -m ci.run_playwright --suite upstream --shard 3/6
python3 -m ci.run_native     --subset rules            # no browser needed
python3 -m ci.run_native     --subset browser --binary path/to/camoufox-bin
python3 -m ci.run_sundial    --binary path/to/camoufox-bin
python3 -m ci.summarize      --results-dir .ci-work/results
```

Each writes one result file to `.ci-work/results/`. `ci/summarize.py` folds the
shards, decides, and renders the table. A required suite that produced no result
file is a **failure**, never a skip — otherwise deleting a job would be the
cheapest way to a green tick.

## Self-tests

`ci/tests/` asserts the pipeline reports honestly: redaction leaks nothing,
skips carry reasons, shards partition exactly once, version resolution never
picks a suite newer than the browser. These run in the `static` job on every
pull request.
