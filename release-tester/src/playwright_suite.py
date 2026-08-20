"""
Run the upstream Playwright suite (browser/tests) against a built binary.

browser/tests is a fork of playwright-python's own async tests with the cases
our patches deliberately break marked `@pytest.mark.skip(reason="Not supported
by Camoufox")` or renamed to `*.py.disabled`. Those skips are the "explicitly
should break" set -- they are counted and reported, not hidden, so a skip that
starts covering something new is visible in the published results.

`browser/tests/run-tests.sh` is not used here: it hardcodes `venv/bin/pytest`,
which does not exist on a Windows runner (`venv/Scripts/`), and it cannot emit
JUnit XML. pytest is invoked directly instead, on the interpreter already
running this process.
"""

from __future__ import annotations

import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class SuiteResult:
    total: int = 0
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0
    duration_sec: float = 0.0
    failures: List[Dict[str, str]] = field(default_factory=list)
    exit_code: Optional[int] = None

    @property
    def clean(self) -> bool:
        return self.failed == 0 and self.errors == 0

    @property
    def executed(self) -> int:
        """Cases that actually ran -- the denominator that means something."""
        return self.total - self.skipped


def build_command(
    tests_dir: Path,
    junit_path: Path,
    *,
    headful: bool = False,
    per_test_timeout_sec: int = 300,
) -> List[str]:
    command = [
        sys.executable,
        '-m',
        'pytest',
        'async/',
        f'--junitxml={junit_path}',
        # The suite's own pyproject sets -vv -s; keep output attributable per
        # case but drop the live stream, which interleaves unreadably on CI.
        '-p',
        'no:cacheprovider',
        f'--timeout={per_test_timeout_sec}',
    ]
    if not headful:
        command.append('--headless')
    return command


def run_suite(
    tests_dir: Path,
    binary: Path,
    junit_path: Path,
    *,
    headful: bool = False,
    timeout_sec: int = 5400,
    per_test_timeout_sec: int = 300,
    extra_env: Optional[Dict[str, str]] = None,
) -> SuiteResult:
    env = dict(os.environ)
    env['CAMOUFOX_EXECUTABLE_PATH'] = str(binary)
    if extra_env:
        env.update(extra_env)

    junit_path.parent.mkdir(parents=True, exist_ok=True)
    command = build_command(
        tests_dir, junit_path, headful=headful, per_test_timeout_sec=per_test_timeout_sec
    )

    print(f'==> {" ".join(command)}', flush=True)
    timed_out = False
    returncode: Optional[int] = None
    try:
        completed = subprocess.run(
            command,
            cwd=str(tests_dir),
            env=env,
            timeout=timeout_sec,
            check=False,
        )
        returncode = completed.returncode
    except subprocess.TimeoutExpired:
        # The whole point of this run is to publish numbers. Blowing up here
        # would lose the stealth scan too, so record the timeout as an error
        # and let the caller carry on with whatever XML pytest managed to
        # flush.
        timed_out = True
        print(
            f'error: the Playwright suite exceeded {timeout_sec}s and was killed',
            file=sys.stderr,
            flush=True,
        )

    # pytest exit code 1 just means "tests failed" -- the XML is still the
    # source of truth. A missing XML means pytest never got far enough to write
    # one (2=interrupted, 3=internal, 4=usage, or the timeout above).
    if not junit_path.is_file():
        result = SuiteResult(exit_code=returncode)
        result.errors = 1
        result.total = 1
        result.failures = [
            {
                'name': 'pytest',
                'kind': 'error',
                'message': (
                    f'suite exceeded its {timeout_sec}s budget and produced no report'
                    if timed_out
                    else f'pytest wrote no JUnit XML (exit code {returncode}); the suite did not start'
                ),
            }
        ]
        return result

    result = parse_junit(junit_path)
    result.exit_code = returncode
    if timed_out:
        result.errors += 1
        result.total += 1
        result.failures.append(
            {
                'name': 'pytest',
                'kind': 'error',
                'message': f'suite exceeded its {timeout_sec}s budget; results are partial',
            }
        )
    return result


def parse_junit(path: Path) -> SuiteResult:
    """
    Read pytest's JUnit XML.

    `errors` and `failures` are kept apart: a failure is an assertion the build
    broke, an error is the harness falling over (a browser that would not
    launch, a fixture raising), and telling them apart is the difference
    between "this patch regressed" and "the runner is broken".
    """
    root = ET.parse(path).getroot()
    suites = [root] if root.tag == 'testsuite' else list(root.iter('testsuite'))

    result = SuiteResult()
    for suite in suites:
        result.total += int(suite.get('tests') or 0)
        result.failed += int(suite.get('failures') or 0)
        result.errors += int(suite.get('errors') or 0)
        result.skipped += int(suite.get('skipped') or 0)
        result.duration_sec += float(suite.get('time') or 0.0)

    result.passed = max(0, result.total - result.failed - result.errors - result.skipped)
    result.duration_sec = round(result.duration_sec, 1)

    for case in root.iter('testcase'):
        for kind in ('failure', 'error'):
            node = case.find(kind)
            if node is None:
                continue
            classname = case.get('classname') or ''
            name = case.get('name') or ''
            result.failures.append(
                {
                    'name': f'{classname}::{name}' if classname else name,
                    'kind': kind,
                    'message': (node.get('message') or '').strip()[:500],
                }
            )
    return result
