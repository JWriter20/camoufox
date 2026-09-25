#!/usr/bin/env python3
"""typescript gate: the npm package's own checks and test suite.

Two modes, two gates:

  typescript          (default) type check, lint, and the vitest suite. No
                      browser. Includes the golden tests that hold the TS
                      launcher to byte-for-byte parity with pythonlib, so a
                      pythonlib change that is not mirrored in typescript/
                      fails here, in tier 1, rather than after the build.
  typescript_browser  (--browser BINARY) the opt-in end-to-end suite: launches
                      the browser under test through the TS API and compares
                      what a page sees with what pythonlib's launch of the same
                      identity shows.

Run:
    python3 -m ci.run_typescript
    python3 -m ci.run_typescript --browser path/to/camoufox-bin
"""

from __future__ import annotations

import argparse
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional

from . import results as evidence
from ._util import EVIDENCE_DIR, REPO_ROOT, WORK_DIR, run

TYPESCRIPT = REPO_ROOT / "typescript"


def parse_vitest_junit(path: Path) -> Dict[str, str]:
    """vitest junit XML -> {"tests/file.test.ts::suite > test": outcome}.

    vitest puts the test file in `classname` and the describe path in `name`,
    which already make a stable identity; pytest's dotted-module trimming in
    ci/_pytest.py would mangle a `.test.ts` path.
    """
    if not path.exists():
        return {}
    outcomes: Dict[str, str] = {}
    for case in ET.parse(path).getroot().iter("testcase"):
        tid = f"{case.get('classname', '')}::{case.get('name', '')}"
        if case.find("error") is not None:
            outcome = evidence.ERROR
        elif case.find("failure") is not None:
            outcome = evidence.FAIL
        elif case.find("skipped") is not None:
            outcome = evidence.SKIP
        else:
            outcome = evidence.PASS
        if outcomes.get(tid) == evidence.PASS:
            continue
        outcomes[tid] = outcome
    return outcomes


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path, default=EVIDENCE_DIR)
    parser.add_argument("--browser", type=Path, help="camoufox-bin for the end-to-end suite")
    parser.add_argument("--python", type=Path, default=Path(sys.executable),
                        help="interpreter with pythonlib installed, for the e2e comparison")
    parser.add_argument("--timeout", type=int, default=1800)
    args = parser.parse_args(argv)

    gate = "typescript_browser" if args.browser else "typescript"
    result = evidence.GateResult(gate=gate)
    if not (TYPESCRIPT / "package.json").is_file():
        result.note("typescript/package.json does not exist")
        result.finish(evidence.ERROR).save(args.evidence_dir)
        return 1

    env = dict(os.environ)
    if args.browser:
        env.update(
            CAMOUFOX_E2E="1",
            CAMOUFOX_EXECUTABLE=str(args.browser.resolve()),
            CAMOUFOX_E2E_PYTHON=str(args.python.absolute()),
        )

    install = run(["pnpm", "install", "--frozen-lockfile"], cwd=TYPESCRIPT, env=env,
                  timeout=600, tee=True, capture=False)
    if not install.ok:
        result.note(f"pnpm install exited {install.code}")
        result.finish(evidence.ERROR).save(args.evidence_dir)
        return 1

    # Static checks are recorded as tests of their own, so the summary names
    # which one failed instead of reporting a bare non-zero exit.
    if not args.browser:
        for script in ("typecheck", "check"):
            proc = run(["pnpm", script], cwd=TYPESCRIPT, env=env, timeout=600, tee=True, capture=False)
            result.record(f"pnpm {script}", evidence.PASS if proc.ok else evidence.FAIL)

    # The tarball a user would install: builds, ships every data file, installs
    # and imports in an empty project, and its CLI starts. publish-npm.yml runs
    # the same check before uploading; running it here means a packaging mistake
    # is caught on the pull request that makes it, not on release day.
    if not args.browser:
        build = run(["pnpm", "build"], cwd=TYPESCRIPT, env=env, timeout=600, tee=True, capture=False)
        pack = build.ok and run(["node", "scripts/check-pack.mjs"], cwd=TYPESCRIPT, env=env,
                                timeout=900, tee=True, capture=False).ok
        result.record("npm package (scripts/check-pack.mjs)", evidence.PASS if pack else evidence.FAIL)

    junit = WORK_DIR / f"junit-{gate}.xml"
    junit.parent.mkdir(parents=True, exist_ok=True)
    proc = run(
        ["pnpm", "exec", "vitest", "run", "--config", "tests/vitest.config.ts",
         "--reporter=default", "--reporter=junit", f"--outputFile.junit={junit}"],
        cwd=TYPESCRIPT, env=env, timeout=args.timeout, tee=True, capture=False,
    )
    outcomes = parse_vitest_junit(junit)
    if not outcomes:
        result.note(f"vitest exited {proc.code} with no junit output; the suite did not run")
        result.finish(evidence.ERROR).save(args.evidence_dir)
        return 1
    for tid, outcome in outcomes.items():
        result.record(tid, outcome)

    tally = result.tally()
    result.artifacts.append(junit.name)
    result.metrics["exit_code"] = proc.code
    result.note(
        f"{tally.get('pass', 0)} passed, {tally.get('fail', 0)} failed, "
        f"{tally.get('error', 0)} errored, {tally.get('skip', 0)} skipped "
        f"({tally.get('total', 0)} collected)"
    )
    failing = tally.get("fail", 0) + tally.get("error", 0)
    e2e_passed = sum(1 for t, o in result.tests.items() if "e2e" in t and o == evidence.PASS)
    result.metrics["e2e_passed"] = e2e_passed
    if args.browser and e2e_passed == 0:
        # An e2e run where every browser test skipped proved nothing.
        result.note("no end-to-end test ran; CAMOUFOX_E2E did not take effect")
        failing += 1
    status = evidence.PASS if failing == 0 else evidence.FAIL
    result.finish(status).save(args.evidence_dir)
    return 0 if status == evidence.PASS else 1


if __name__ == "__main__":
    sys.exit(main())
