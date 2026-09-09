#!/usr/bin/env python3
"""Regression gate: the maintained suite in tests/.

This is the same suite, unchanged, that passed on the previous release. That is
exactly what makes it the no-regression check: every test identity here has a
known prior outcome, so a test flipping pass -> fail is unambiguously this
Firefox bump's fault rather than upstream having changed the test.

Compare with `playwright_upstream`, which runs a *newer* suite and therefore
cannot distinguish "we broke it" from "upstream tightened it".

Run:
    python3 -m harness.gates.playwright_vendored --binary /path/to/camoufox-bin
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import List, Optional

from .. import evidence
from .._util import EVIDENCE_DIR, REPO_ROOT, WORK_DIR, log, run
from . import parse_junit, run_pytest

TESTS_DIR = REPO_ROOT / "tests"


def ensure_venv() -> Path:
    """tests/setup-venv.sh owns this; just make sure it has been run."""
    python = TESTS_DIR / "venv" / "bin" / "python"
    if not python.exists():
        log("tests/venv missing -- running tests/setup-venv.sh")
        run(["bash", "./setup-venv.sh"], cwd=TESTS_DIR, check=True, timeout=1800, tee=True, capture=False)
    return python


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--evidence-dir", type=Path, default=EVIDENCE_DIR)
    parser.add_argument("--headful", action="store_true")
    parser.add_argument("--timeout", type=int, default=9000)
    parser.add_argument("--retries", type=int, default=1, help="reruns for failures only")
    args = parser.parse_args(argv)

    from . import require_binary

    result = evidence.GateResult(gate="playwright_vendored")
    try:
        binary = args.binary or require_binary()
    except FileNotFoundError as exc:
        result.note(str(exc))
        result.finish(evidence.ERROR).save(args.evidence_dir)
        return 1

    python = ensure_venv()
    junit = WORK_DIR / "junit-vendored.xml"
    env = {"CAMOUFOX_EXECUTABLE_PATH": str(binary.resolve())}
    pytest_args = ["async/"] + ([] if args.headful else ["--headless"])

    proc = run_pytest(
        cwd=TESTS_DIR, python=python, args=pytest_args, junit=junit, env=env, timeout=args.timeout
    )
    outcomes = parse_junit(junit)
    if not outcomes:
        result.note(
            f"pytest exited {proc.code} and produced no junit results. The suite did not run; "
            "that is a failure, not an empty pass."
        )
        result.finish(evidence.ERROR).save(args.evidence_dir)
        return 1

    for tid, outcome in outcomes.items():
        result.record(tid, outcome)

    # Re-run only what failed. A test that passes on a retry is flaky, not
    # broken, and the evidence keeps its best outcome.
    failing = [t for t, o in outcomes.items() if o in (evidence.FAIL, evidence.ERROR)]
    for attempt in range(args.retries):
        if not failing:
            break
        log(f"retry {attempt + 1}: {len(failing)} failing test(s)")
        retry_junit = WORK_DIR / f"junit-vendored-retry{attempt + 1}.xml"
        run_pytest(
            cwd=TESTS_DIR, python=python,
            args=[*(["--headless"] if not args.headful else []), "--last-failed", "async/"],
            junit=retry_junit, env=env, timeout=args.timeout,
        )
        retried = parse_junit(retry_junit)
        recovered = [t for t in failing if retried.get(t) == evidence.PASS]
        for tid in recovered:
            result.record(tid, evidence.PASS)
        if recovered:
            result.note(f"{len(recovered)} test(s) passed on retry (flaky, not counted as failures)")
        failing = [t for t in failing if retried.get(t) not in (None, evidence.PASS)]

    tally = result.tally()
    result.artifacts.append(junit.name)
    result.metrics["exit_code"] = proc.code
    result.note(
        f"{tally.get('pass', 0)} passed, {tally.get('fail', 0)} failed, "
        f"{tally.get('error', 0)} errored, {tally.get('skip', 0)} skipped "
        f"({tally.get('total', 0)} collected)"
    )

    # The gate reports what happened; whether those failures are *regressions*
    # is verify.py's call, because only it holds the baseline.
    still_failing = tally.get("fail", 0) + tally.get("error", 0)
    status = evidence.PASS if still_failing == 0 else evidence.FAIL
    if status == evidence.FAIL:
        result.note(
            f"{still_failing} test(s) failing. verify.py decides which of these are "
            "regressions against the previous release."
        )
    result.finish(status).save(args.evidence_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
