#!/usr/bin/env python3
"""Conformance gate: upstream playwright-python's own suite, unmodified.

Fetched fresh at the tag Playwright shipped for the Firefox being targeted, so
the browser is measured against the automation contract its users will actually
hold it to -- including tests written after the vendored fork stopped tracking
upstream.

Failures here are expected and fine, as long as each one is *named*. Camoufox
deliberately breaks some upstream behaviour (page-world evaluate, User-Agent
override), and those tests belong in the expectations file with a reason.
An unexpected failure is what this gate is for.

Run:
    python3 -m harness.gates.playwright_upstream --tag v1.62.0 --binary /path/to/camoufox-bin
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict, List, Optional

from .. import evidence
from .._util import EVIDENCE_DIR, POLICY_PATH, REPO_ROOT, WORK_DIR, log
from ..sync_playwright_suite import prepare
from . import parse_junit, run_pytest


def seed_expectations(path: Path, failures: Dict[str, str], tag: str) -> None:
    """Write a starter expectations file from an observed run.

    Deliberately writes `reason: TRIAGE` rather than inventing justifications.
    verify.py rejects an expectation with an empty reason, so the file cannot be
    used as a gate until a human has actually looked at each entry.
    """
    lines = [
        "# Upstream Playwright tests that Camoufox is expected to fail.",
        "#",
        "# Seeded automatically on the first run; every entry starts as TRIAGE and",
        "# must be replaced with a real reason before the gate can rely on it (set",
        "# gates.playwright_upstream.bootstrap: false in harness/policy.yml once done).",
        "#",
        "# A legitimate entry names a behaviour Camoufox intentionally does not",
        "# provide -- page-world evaluate, User-Agent override, a Chromium-only API.",
        "# 'It was failing when I got here' is not a reason; that is a regression",
        "# wearing a disguise.",
        "",
        f"seeded_from: {tag}",
        "expected_failures:",
    ]
    for tid in sorted(failures):
        lines.append(f"  - id: {tid!r}")
        lines.append(f"    outcome: {failures[tid]}")
        lines.append("    reason: TRIAGE")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    log(f"seeded {len(failures)} expectation(s) -> {path}")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", required=True, help="playwright-python tag, e.g. v1.62.0")
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--evidence-dir", type=Path, default=EVIDENCE_DIR)
    parser.add_argument("--timeout", type=int, default=10800)
    parser.add_argument("--seed-expectations", action="store_true")
    args = parser.parse_args(argv)

    import yaml

    with open(POLICY_PATH, encoding="utf-8") as fh:
        cfg = (yaml.safe_load(fh).get("gates") or {}).get("playwright_upstream") or {}

    from . import require_binary

    result = evidence.GateResult(gate="playwright_upstream")
    result.metrics["playwright_tag"] = args.tag

    try:
        binary = args.binary or require_binary()
    except FileNotFoundError as exc:
        result.note(str(exc))
        result.finish(evidence.ERROR).save(args.evidence_dir)
        return 1

    manifest = prepare(args.tag)
    checkout = Path(manifest["checkout"])
    junit = WORK_DIR / "junit-upstream.xml"

    proc = run_pytest(
        cwd=checkout,
        python=Path(manifest["python"]),
        args=["-p", "pw_camoufox_plugin", "--browser", "firefox", "tests/async/"],
        junit=junit,
        env={"CAMOUFOX_EXECUTABLE_PATH": str(binary.resolve())},
        timeout=args.timeout,
    )

    outcomes = parse_junit(junit)
    if not outcomes:
        result.note(
            f"pytest exited {proc.code} with no junit output. The upstream suite failed to "
            "start -- most often a playwright/pytest version mismatch in the generated venv."
        )
        result.finish(evidence.ERROR).save(args.evidence_dir)
        return 1

    for tid, outcome in outcomes.items():
        result.record(tid, outcome)

    tally = result.tally()
    result.artifacts.append(junit.name)
    result.metrics.update(exit_code=proc.code, upstream_tag=args.tag)
    result.note(
        f"upstream {args.tag}: {tally.get('pass', 0)} passed, {tally.get('fail', 0)} failed, "
        f"{tally.get('error', 0)} errored, {tally.get('skip', 0)} skipped "
        f"({tally.get('total', 0)} collected)"
    )

    failures = {t: o for t, o in result.tests.items() if o in (evidence.FAIL, evidence.ERROR)}
    rel = cfg.get("expectations")
    if args.seed_expectations and rel:
        # policy.yml states the path relative to the repository root.
        target = Path(rel) if Path(rel).is_absolute() else REPO_ROOT / rel
        seed_expectations(target, failures, args.tag)

    minimum = int(cfg.get("min_tests_collected", 0) or 0)
    status = evidence.PASS
    if minimum and tally.get("total", 0) < minimum:
        result.note(f"only {tally.get('total', 0)} tests collected, policy expects >= {minimum}")
        status = evidence.FAIL
    elif failures and not cfg.get("bootstrap", False):
        # verify.py decides which failures are excused; the gate reports the raw
        # count so a human reading the log sees the same number.
        result.note(f"{len(failures)} failing test(s); verify.py checks them against expectations")

    result.finish(status).save(args.evidence_dir)
    return 0


if __name__ == "__main__":
    sys.exit(main())
