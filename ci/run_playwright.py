#!/usr/bin/env python3
"""Run the Playwright suite against a Camoufox build.

One suite: playwright-python's own tests, fetched fresh at the tag
`ci/versions.py` resolved for this browser, run unmodified with main-world
execution and `ci/skiplist.yml` applied, plus the Camoufox-specific modules
`ci/suite.py` overlays from `tests/camoufox/`.

This is the conformance check -- does Camoufox still honour the automation
contract its users hold it to -- and, through the overlay, the regression check
for the behaviours that are ours alone. Shardable.

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

    for index, group in enumerate(GROUPS):
        if not group.sharded and not first_shard:
            continue
        group_env = dict(env)
        if args.shard and group.sharded:
            group_env["CI_SHARD"] = args.shard
        group_junit = WORK_DIR / f"junit{suffix}-{index}.xml"
        log(f"group {index + 1}/{len(GROUPS)}: {', '.join(group.targets)}")
        proc = run_pytest(
            cwd=cwd,
            python=python,
            args=[*base_args, *group.targets],
            junit=group_junit,
            env=group_env,
            timeout=args.timeout,
        )
        part = parse_junit(group_junit)
        if not part:
            result.note(
                f"{', '.join(group.targets)} exited {proc.code} and produced no junit "
                "results. That group did not run; it is a failure, not an empty pass."
            )
            result.finish(results.ERROR).save(args.results_dir)
            return 1
        outcomes.update(part)
        ran.append(group)

    result.metrics["groups"] = len(ran)

    for tid, outcome in outcomes.items():
        result.record(tid, outcome)

    # Re-run only what failed. A test that passes on a retry is flaky, not
    # broken, and the record keeps its best outcome. Retries stay inside their
    # own group, for the same reason the groups exist.
    failing = [t for t, o in outcomes.items() if o in (results.FAIL, results.ERROR)]
    for attempt in range(args.retries):
        if not failing:
            break
        log(f"retry {attempt + 1}: {len(failing)} failing test(s)")
        retried: Dict[str, str] = {}
        for index, group in enumerate(ran):
            retry_junit = WORK_DIR / f"junit{suffix}-{index}-retry{attempt + 1}.xml"
            group_env = dict(env)
            if args.shard and group.sharded:
                group_env["CI_SHARD"] = args.shard
            run_pytest(
                cwd=cwd,
                python=python,
                args=[*base_args, "--last-failed", *group.targets],
                junit=retry_junit,
                env=group_env,
                timeout=args.timeout,
            )
            retried.update(parse_junit(retry_junit))
        recovered = [t for t in failing if retried.get(t) == results.PASS]
        for tid in recovered:
            result.record(tid, results.PASS)
        if recovered:
            result.note(f"{len(recovered)} test(s) passed on retry (flaky, not counted as failures)")
        failing = [t for t in failing if retried.get(t) not in (None, results.PASS)]

    tally = result.tally()
    result.artifacts.extend(
        f"junit{suffix}-{i}.xml" for i in range(len(GROUPS)) if i < len(ran)
    )
    result.metrics["exit_code"] = proc.code
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
