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

Everything that leaves `redact()` is checked against a **whitelist** at runtime,
not a blacklist — a blacklist only stops the leaks somebody already thought of.
Adding a field without adding it to `_PUBLISHABLE` fails the run:

```json
{ "grade": "A", "checks_total": 412, "checks_passed": 403, "pass_rate": 0.978,
  "out_of_scope_failed": 6, "os": "linux", "sundial_version": "0.3.1",
  "schema_version": 1 }
```

There are deliberately **no per-vector rows**, not even opaque ones. An HMAC
names nothing, but a map of them publishes how many distinct checks fail and
lets a reader follow the same id from release to release.

The cost is real: regression detection drops from per-vector ("the check that
passed last release fails now") to per-score ("we got worse"), covered by
`min_pass_rate` in `ci/sundial.yml` and `max_pass_rate_drop` in
`harness/policy.yml`. To get the per-vector view back for your own debugging,
set `SUNDIAL_REPORT_AGE_RECIPIENT` to an `age` public key — the full report is
then kept encrypted to you and nobody else can open it.

Needs **`SUNDIAL_AUTOMATION_KEY`** only. `SUNDIAL_USERNAME` is optional and
defaults to `guest`, sundial's restricted role — a username names an account,
it is not a secret. Absent the password — a pull request from a fork — the job
is skipped and the summary says so.

## Blocking a merge

Branch protection on `main` requires exactly one check: **`All tests passed`**,
the `gate` job. Pointing at one job instead of a dozen means the required-check
list does not need editing every time a suite is added, renamed, or resharded.

The gate allows exactly three skips, each for a stated reason:

| Skipped | Because |
| --- | --- |
| `build` | the browser was fetched, not compiled |
| `fetch-browser` | the browser was compiled, not fetched |
| `sundial` | a fork pull request has no stealth credentials |

Anything else that is not `success` fails it — **including `skipped`**. A suite
that did not run has not passed, and quietly skipping one is the cheapest route
to a green tick.

The settings live in [`ci/branch-protection.json`](branch-protection.json) so
they are reviewable rather than lore. To apply them (needs admin):

```bash
gh api -X PUT repos/<owner>/<repo>/branches/main/protection \
  --input ci/branch-protection.json
```

Two choices worth knowing about:

- **`enforce_admins: false`** — you can still merge when CI itself is broken.
  Protection should stop mistakes, not lock you out of your own repository.
- **`strict: false`** — a pull request does not have to be rebased onto the
  latest `main` before merging. With `true`, every push to `main` would
  invalidate every open pull request and force another build, and a build here
  is over an hour cold.

Reviews are deliberately not required: a solo maintainer cannot approve their
own pull request, so requiring one would block every merge.

## Cost control

Each tier gates the next, so a two-second lint failure never reaches the build:

```
0  static    lint, self-tests, settled decisions        seconds
1  unit      pythonlib                                  ~1 min
2  browser   build  (patches/additions/settings/assets/upstream.sh/Makefile/scripts changed)
             fetch  (anything else -- driver changes test against the published release)
3a smoke     patch guards, build-tester                 ~15 min
3b full      Playwright x2, leaks, stealth              ~40 min
4  gate      the required check
```

**Driver-only pull requests never build.** There is nothing new to compile, so
`fetch-browser` downloads the published release and the browser suites run
against the build users are actually on — a minute instead of seventy.

**The ccache is kept warm from `main`.** Pushes to `main` populate it and a
twice-weekly schedule keeps it from being evicted (GitHub drops a cache after
seven days unused). Pull requests restore it through `restore-keys`, so a build
in a pull request starts warm even though its own key is new.

A prebuilt image in `ghcr.io` with the object cache baked in would be warmer
still and would not need the eviction guard. It also needs registry credentials
and a rebuild pipeline of its own; this is the version that works with no setup.

> One consequence of `cancel-in-progress`: pushing to a branch cancels its
> running build. That is right while iterating, but a 70-minute build will not
> survive a push made 20 minutes in.

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
