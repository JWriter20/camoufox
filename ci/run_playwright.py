#!/usr/bin/env python3
"""Run the Playwright suite against a Camoufox build.

One suite: playwright-python's own tests, fetched fresh at the tag
`ci/versions.py` resolved for this browser, run unmodified with
`ci/skiplist.yml` applied, plus the Camoufox-specific modules `ci/suite.py`
overlays from `tests/camoufox/`.

This is the conformance check -- does Camoufox still honour the automation
contract its users hold it to -- and, through the overlay, the regression check
for the behaviours that are ours alone. Shardable.

**Isolated first, main world as a counted fallback.** Each group is run up to
three times, and normally twice:

  1. isolated world -- the configuration Camoufox actually ships. `evaluate()`
     runs in its own compartment, so a test that reads a global its page script
     defined fails here by design. Upstream's own pytest-rerunfailures has
     already retried anything that failed, so what arrives at 2 is settled.
  2. those failures with isolation off. A test that passes now is recorded as a
     **main-world fallback**: it counts as a pass for the run, and is named and
     counted in the result so the size of that set is visible and comparable
     between runs. A change in it means the isolated-world conformance gap
     moved, which is a fact about the browser worth seeing.
  3. only what failed in BOTH worlds, retried once. That set is normally empty,
     so this normally costs nothing.

Running main-world-only (the previous behaviour) hid that number entirely. A
test failing in both worlds and on retry is a plain failure.

Run:
    python3 -m ci.run_playwright --binary path/to/camoufox-bin
    python3 -m ci.run_playwright --binary path/to/camoufox-bin --shard 3/6
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict, List, NamedTuple, Optional, Tuple

from . import results
from ._pytest import parse_junit, require_binary, run_pytest
from ._util import REPO_ROOT, RESULTS_DIR, WORK_DIR, log
from .pw_camoufox_plugin import ISOLATED_WORLD, MAIN_WORLD
from .suite import prepare
from .versions import resolve

# What "the suite" means. Named explicitly rather than pointed at `tests/`,
# because the one thing deliberately left out has to be visible.
#
# This used to be `tests/async/` alone, which excluded 722 tests -- 31% of the
# suite -- with nothing recorded anywhere to say so. That was not a decision:
# the vendored fork in tests/ carried `async/` and `async_imp/` and no sync
# suite, and this runner was pointed at the same shape without checking what
# upstream had. The sync tests were never incompatible; they had simply never
# been run.
class Group(NamedTuple):
    """A set of paths that share one pytest process, and whether it shards."""

    targets: Tuple[str, ...]
    sharded: bool


# Each group gets its OWN pytest process. This is not tidiness: upstream's sync
# suite is a greenlet wrapper and its async suite runs under pytest-asyncio, and
# putting them in one process breaks the loop for whichever runs second --
#
#     RuntimeError: Runner.run() cannot be called from a running event loop
#
# Measured: async alone, 1526 passed / 1 timing flake. async + one sync module,
# 14 failed. Sync first, 46 errors. The damage lands in async fixture setup, so
# it reads as "the fetch tests are flaky" rather than as a harness fault, and the
# retry logic quietly hides it -- 50 tests passed only on retry before this split.
#
# tests/common/ and test_reference_count_async.py each start their own Playwright
# inside the test body, which cannot happen while session fixtures hold a loop.
# They are fine together (6 passed) but not with the suites above.
GROUPS: Tuple[Group, ...] = (
    Group(("tests/async/",), sharded=True),
    # The sync API is a greenlet wrapper over the same Juggler traffic, so much of
    # this duplicates tests/async/ at the protocol level. It is here because
    # pythonlib ships a sync API that users drive, and the wrapper has its own
    # timeout and reentrancy behaviour the async tests cannot reach.
    Group(("tests/sync/",), sharded=True),
    # Six tests. Not sharded: splitting them would hand some shard an empty
    # selection, which pytest exits 5 for. Kept because ProtocolCallback objects
    # accumulate when the browser never replies to a protocol message, and this
    # fork patches Juggler heavily, so that leak can be ours.
    Group(("tests/common/", "tests/test_reference_count_async.py"), sharded=False),
)

TARGETS: Tuple[str, ...] = tuple(t for g in GROUPS for t in g.targets)


# The isolated pass is a classifier: its only question is "does this test pass
# as Camoufox ships?". Both settings below bound what a "no" is allowed to cost,
# and neither applies to the passes that adjudicate afterwards.
#
# Some isolated failures do not fail -- they HANG. `expose_function` installs its
# binding on the isolated world's global, so page script calling `window.fn()`
# looks at the page's own window, does not find it, and the call never reaches
# Python. A test awaiting the future that call was meant to resolve waits
# forever, because that await has no Playwright timeout behind it. Reproduced
# directly against a build: page-script-triggered binding hangs isolated and
# resolves in the main world, while the same binding called from evaluate()
# works in both. That is isolation doing its job -- a page that can see an
# automation binding can detect it -- so these tests cannot be fixed, only
# recognised, which pass 2 does in about half a second each.
#
# 90s, against a measured worst case of 30.4s across all 2295 tests in the
# main-world baseline (only two exceeded 30s, none exceeded 45s) and a 30s
# Playwright action timeout. Three times the slowest thing that legitimately
# happens, and half the default.
ISOLATED_TIMEOUT = 90

# upstream's tests/conftest.py sets `reruns = 3` whenever $CI is set, which is
# the only thing it reads $CI for. Insurance that almost never pays out -- the
# main-world baseline recorded 2 reruns across all 2295 tests -- and under
# isolation it turns every deterministic world difference into four attempts:
# 138 reruns in one shard's isolated pass, recovering nothing, at up to 180s
# each for the ones that hang. A flake missed here is not lost; it fails the
# isolated pass, passes pass 2, and is counted as a fallback.
_NO_UPSTREAM_RERUNS = {"CI": ""}


# Left out on purpose, with the reason, so "not run" is never merely implied.
EXCLUDED = {
    "tests/test_installation.py": (
        "pip-installs playwright into a scratch environment to check packaging. "
        "That exercises Playwright's own release process, not this browser."
    ),
}


def unclaimed(checkout: Path) -> List[str]:
    """Test paths upstream ships that TARGETS neither runs nor EXCLUDED names.

    Upstream is free to add a directory, and the failure mode is silence: the
    suite quietly gets narrower and the total still looks healthy. This is the
    same hole the skiplist had one level down, so it gets the same treatment --
    a new subtree fails the run until somebody decides about it.
    """
    claimed = {t.rstrip("/") for t in TARGETS} | set(EXCLUDED)
    root = checkout / "tests"
    missed: List[str] = []
    for child in sorted(root.iterdir()):
        rel = f"tests/{child.name}"
        if rel in claimed:
            continue
        if child.is_dir():
            # Only directories that actually hold tests; assets/ and golden-*/
            # are fixtures.
            if any(child.glob("test_*.py")):
                missed.append(rel + "/")
        elif child.name.startswith("test_") and child.suffix == ".py":
            missed.append(rel)
    return missed


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--browser-version", help="passed through to ci.versions")
    parser.add_argument("--playwright-tag", help="pin the suite instead of resolving one")
    parser.add_argument("--shard", help="e.g. 3/6")
    parser.add_argument("--results-dir", type=Path, default=RESULTS_DIR)
    parser.add_argument("--name", help="result file name; defaults to playwright[-shard]")
    parser.add_argument("--timeout", type=int, default=10800)
    parser.add_argument("--retries", type=int, default=1, help="rerun failures this many times")
    parser.add_argument("--headful", action="store_true")
    args = parser.parse_args(argv)

    suffix = f"-{args.shard.replace('/', 'of')}" if args.shard else ""
    name = args.name or f"playwright{suffix}"
    result = results.GateResult(gate=name)

    try:
        binary = args.binary or require_binary()
    except FileNotFoundError as exc:
        result.note(str(exc))
        result.finish(results.ERROR).save(args.results_dir)
        return 1

    env = {"CAMOUFOX_EXECUTABLE_PATH": str(binary.resolve())}

    versions = resolve(
        browser_version=args.browser_version, playwright_tag=args.playwright_tag
    )
    tag = versions["playwright_tag"]
    result.metrics.update(
        playwright_tag=tag,
        playwright_firefox=versions["playwright_firefox"],
        browser_version=versions["browser_version"],
    )

    manifest = prepare(tag)
    cwd = Path(manifest["checkout"])
    python = Path(manifest["python"])
    result.metrics["camoufox_tests"] = len(manifest.get("camoufox_tests", []))

    missed = unclaimed(cwd)
    if missed:
        result.note(
            f"{tag} ships test paths this runner neither runs nor excludes: "
            + ", ".join(missed)
            + ". Add them to TARGETS, or to EXCLUDED with a reason. Refusing to report a "
            "pass over a suite that quietly got narrower."
        )
        result.finish(results.ERROR).save(args.results_dir)
        return 1

    base_args = ["-p", "pw_camoufox_plugin", "--browser", "firefox"]
    if args.headful:
        base_args.append("--headed")

    # The plugin reads the skiplist from the repository, not the fetched
    # checkout, so a local edit takes effect without re-preparing.
    env["CI_SKIPLIST"] = str(REPO_ROOT / "ci" / "skiplist.yml")
    if args.shard:
        result.metrics["shard"] = args.shard

    first_shard = not args.shard or args.shard.split("/")[0] == "1"
    outcomes: Dict[str, str] = {}
    ran: List[Group] = []
    # Still failing under isolation once flakes are excluded -- the set handed
    # to the main-world pass.
    isolated_failures: List[str] = []
    # ...and the subset of those that passed with isolation off.
    fallbacks: List[str] = []
    fallback_junits: List[str] = []
    last_code = 0

    for index, group in enumerate(GROUPS):
        if not group.sharded and not first_shard:
            continue
        group_env = dict(env)
        if args.shard and group.sharded:
            group_env["CI_SHARD"] = args.shard

        # One pytest cache per group, never the checkout's shared default.
        # `--last-failed` below reads it, and pytest's `lastfailed` accumulates
        # across every run sharing a cache -- it only drops an entry when that
        # test is collected again and passes. With one cache for the whole
        # checkout, the async group's rerun would be selecting from a set the
        # sync group had also written into: pytest keeps the entries it did not
        # collect, so the selection was either "everything in this group"
        # (when this group had no failures of its own, since pytest declines to
        # filter when nothing selected previously failed) or nothing at all.
        # Per-group, `--last-failed` means exactly what it says.
        cache_dir = WORK_DIR / f"pytest-cache{suffix}" / str(index)
        common = [*base_args, "-o", f"cache_dir={cache_dir}"]
        targets = ", ".join(group.targets)

        # --- 1. isolated world: the browser as it ships --------------------
        log(f"group {index + 1}/{len(GROUPS)}: {targets} [{ISOLATED_WORLD} world]")
        group_junit = WORK_DIR / f"junit{suffix}-{index}.xml"
        proc = run_pytest(
            cwd=cwd,
            python=python,
            args=[*common, *group.targets],
            junit=group_junit,
            env={**group_env, **_NO_UPSTREAM_RERUNS, "CI_WORLD": ISOLATED_WORLD},
            timeout=args.timeout,
            per_test_timeout=ISOLATED_TIMEOUT,
        )
        last_code = proc.code
        part = parse_junit(group_junit)
        if not part:
            result.note(
                f"{targets} exited {proc.code} and produced no junit "
                "results. That group did not run; it is a failure, not an empty pass."
            )
            result.finish(results.ERROR).save(args.results_dir)
            return 1
        for tid, outcome in part.items():
            if outcomes.get(tid) != results.PASS:
                outcomes[tid] = outcome
        ran.append(group)

        failing = {t for t, o in part.items() if o in (results.FAIL, results.ERROR)}
        if not failing:
            # Nothing to re-run, and this guard is load-bearing rather than an
            # optimisation: pytest declines to filter when nothing it collected
            # previously failed, so a `--last-failed` pass with an empty cache
            # runs the ENTIRE group again -- in the main world, silently
            # discarding the isolated result it was meant to refine.
            continue

        # --- 2. main world: what isolation, specifically, costs -----------
        #
        # Straight to the other world, with no same-world retry in between.
        # That retry used to sit here on the theory that a flake must not be
        # mistaken for a world difference, and measured on the first real run it
        # cost 7m50s a shard and recovered nothing at all:
        #
        #   isolated (full)   335s + 331s   35 and 11 failures
        #   isolated retry    205s + 265s   0 recovered
        #   main world         19s +  12s   46 recovered
        #
        # Two reasons it was never going to earn that. These failures are
        # deterministic -- a test reading a global its page script defined does
        # not intermittently see it -- and failing that way is *slow*, because
        # the read returns undefined and the test sits on a Playwright timeout
        # rather than throwing. And upstream's suite already ships
        # pytest-rerunfailures: the "105 rerun" on that first line is every one
        # of those 35 failures having been retried three times before the run
        # even reported them. A flake does not survive that.
        isolated_failures.extend(sorted(failing))
        log(f"  fallback [{MAIN_WORLD} world]: {len(failing)} test(s) that isolation failed")
        fallback_junit = WORK_DIR / f"junit{suffix}-{index}-mainworld.xml"
        run_pytest(
            cwd=cwd,
            python=python,
            args=[*common, "--last-failed", *group.targets],
            junit=fallback_junit,
            env={**group_env, "CI_WORLD": MAIN_WORLD},
            timeout=args.timeout,
        )
        fallback_junits.append(fallback_junit.name)
        recovered_in_main = parse_junit(fallback_junit)
        recovered = {t for t in failing if recovered_in_main.get(t) == results.PASS}
        for tid in recovered:
            outcomes[tid] = results.PASS
        fallbacks.extend(sorted(recovered))
        failing -= recovered

        # --- 3. failed in BOTH worlds: now a retry is worth paying for -----
        #
        # This set is normally empty, which is exactly why the retry belongs
        # here and not one phase earlier: it costs nothing on a healthy run, and
        # on an unhealthy one it answers the only question still open about a
        # test that no world would satisfy -- whether it is broken or merely
        # flaky. Re-run in the main world, the permissive one, so a pass means
        # "not reproducible" rather than "needed isolation off", which is
        # already known by this point.
        for attempt in range(args.retries):
            if not failing:
                break
            log(f"  retry {attempt + 1} [{MAIN_WORLD} world]: {len(failing)} test(s) that failed in both")
            retry_junit = WORK_DIR / f"junit{suffix}-{index}-retry{attempt + 1}.xml"
            run_pytest(
                cwd=cwd,
                python=python,
                args=[*common, "--last-failed", *group.targets],
                junit=retry_junit,
                env={**group_env, "CI_WORLD": MAIN_WORLD},
                timeout=args.timeout,
            )
            retried = parse_junit(retry_junit)
            recovered = {t for t in failing if retried.get(t) == results.PASS}
            for tid in recovered:
                outcomes[tid] = results.PASS
            if recovered:
                result.note(
                    f"{len(recovered)} test(s) that failed in both worlds passed on retry "
                    "(flaky, not counted as failures)"
                )
            failing -= recovered

    result.metrics["groups"] = len(ran)

    for tid, outcome in outcomes.items():
        result.record(tid, outcome)

    # Published on purpose. These tests pass, so they are invisible in the
    # failure count -- but this is the isolated-world conformance gap, and the
    # whole reason for running isolation first is to have a number for it that
    # moves when the browser does.
    result.metrics["isolated_world_failures"] = len(isolated_failures)
    result.metrics["main_world_fallback_count"] = len(fallbacks)
    result.metrics["main_world_fallbacks"] = sorted(fallbacks)
    if fallbacks:
        result.note(
            f"{len(fallbacks)} test(s) failed under world isolation and passed with it off. "
            "They count as passes -- Camoufox honours the contract -- but the set is "
            "recorded so a change in it is visible: "
            + ", ".join(sorted(fallbacks)[:8])
            + (" ..." if len(fallbacks) > 8 else "")
        )
    unexplained = len(isolated_failures) - len(fallbacks)
    if unexplained:
        result.note(
            f"{unexplained} test(s) failed in BOTH worlds; those are real failures, not "
            "an isolation difference."
        )

    tally = result.tally()
    result.artifacts.extend(
        f"junit{suffix}-{i}.xml" for i in range(len(GROUPS)) if i < len(ran)
    )
    result.artifacts.extend(fallback_junits)
    result.metrics["exit_code"] = last_code
    result.note(
        f"{tally.get('pass', 0)} passed, {tally.get('fail', 0)} failed, "
        f"{tally.get('error', 0)} errored, {tally.get('skip', 0)} skipped "
        f"({tally.get('total', 0)} collected)"
    )

    still_failing = tally.get("fail", 0) + tally.get("error", 0)
    status = results.PASS if still_failing == 0 else results.FAIL
    result.finish(status).save(args.results_dir)
    # Exit non-zero so the step goes red in the UI. ci/summarize.py still owns
    # the run's verdict -- it is the only thing that knows what was required --
    # but a green step hiding a failed suite is how a broken pipeline goes
    # unnoticed for a week. Shards are separate jobs with fail-fast disabled, so
    # one going red does not cancel its siblings.
    return 0 if status == results.PASS else 1


if __name__ == "__main__":
    sys.exit(main())
