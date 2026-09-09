#!/usr/bin/env python3
"""build-tester gate: the raw binary, 8 fingerprint profiles, graded per check.

Grades per individual check rather than per profile, so the evidence carries
~hundreds of stable identities and verify.py can say "this exact check passed on
the last release and does not now" instead of "the grade dropped from A to B".

Run:
    python3 -m harness.gates.build_tester --binary /path/to/camoufox-bin
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict, List, Optional

from .. import evidence
from .._util import EVIDENCE_DIR, POLICY_PATH, REPO_ROOT, WORK_DIR, log, read_json, run

BUILD_TESTER = REPO_ROOT / "build-tester"

# Cross-profile uniqueness slots. Each is "did N profiles produce N distinct
# values". They draw random fingerprints, so an occasional collision is the
# birthday paradox rather than a leak -- see TRIBAL-KNOWLEDGE.md.
_UNIQUENESS_KEYS = (
    "uniqueAudio", "uniqueCanvas", "uniqueFonts", "uniqueTimezones",
    "uniqueScreens", "uniqueVoices", "uniqueWebGL", "uniquePlatforms",
)


def flatten(full: dict) -> Dict[str, str]:
    """The whole result tree -> {check_id: pass|fail}."""
    tests: Dict[str, str] = {}
    for profile in full.get("profiles") or []:
        meta = profile.get("profile") or {}
        # Identify by os+mode+index, never by the display name, which carries a
        # random letter suffix and would churn every run.
        slot = f"{meta.get('os', '?')}-{meta.get('mode', '?')}-{meta.get('index', profile.get('index', 0))}"
        results = profile.get("results") or {}
        if profile.get("error"):
            tests[f"{slot}/launch"] = evidence.ERROR
            continue
        tests[f"{slot}/launch"] = evidence.PASS

        for section in ("core", "extended", "workers", "selfDestruct"):
            categories = results.get(section) or {}
            if not isinstance(categories, dict):
                continue
            for category, checks in categories.items():
                if not isinstance(checks, dict):
                    continue
                for check, payload in checks.items():
                    if not isinstance(payload, dict) or not isinstance(payload.get("passed"), bool):
                        continue
                    tid = f"{slot}/{section}/{category}/{check}"
                    tests[tid] = evidence.PASS if payload["passed"] else evidence.FAIL

        webrtc = results.get("webrtc") or {}
        if webrtc:
            tests[f"{slot}/webrtc"] = evidence.PASS if webrtc.get("passed") else evidence.FAIL
        stability = results.get("stability") or {}
        if stability:
            tests[f"{slot}/stability"] = evidence.PASS if stability.get("stable") else evidence.FAIL
        for match in profile.get("matchResults") or []:
            name = match.get("name") or match.get("key") or "match"
            tests[f"{slot}/match/{name}"] = evidence.PASS if match.get("passed") else evidence.FAIL
    return tests


def category_failures(full: dict, required: List[str]) -> Dict[str, int]:
    """Failing check counts for the categories policy insists must be clean."""
    wanted = {c.lower() for c in required}
    out: Dict[str, int] = {}
    for profile in full.get("profiles") or []:
        results = profile.get("results") or {}
        for section in ("core", "extended", "workers", "selfDestruct"):
            for category, checks in (results.get(section) or {}).items():
                if category.lower() not in wanted or not isinstance(checks, dict):
                    continue
                for payload in checks.values():
                    if isinstance(payload, dict) and payload.get("passed") is False:
                        out[category] = out.get(category, 0) + 1
    return out


def uniqueness_collisions(full: dict) -> List[str]:
    """Slots where profiles that should have differed did not."""
    collisions: List[str] = []
    for group, stats in (full.get("crossProfile") or {}).items():
        total = stats.get("total") or 0
        if total < 2:
            continue
        for key in _UNIQUENESS_KEYS:
            value = stats.get(key)
            if isinstance(value, int) and value < total:
                collisions.append(f"{group}.{key} ({value}/{total} distinct)")
    return collisions


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path)
    parser.add_argument("--evidence-dir", type=Path, default=EVIDENCE_DIR)
    parser.add_argument("--timeout", type=int, default=3600)
    args = parser.parse_args(argv)

    import yaml

    with open(POLICY_PATH, encoding="utf-8") as fh:
        cfg = (yaml.safe_load(fh).get("gates") or {}).get("build_tester") or {}

    from . import require_binary

    result = evidence.GateResult(gate="build_tester")
    out_json = WORK_DIR / "build-tester-result.json"

    try:
        binary = args.binary or require_binary()
    except FileNotFoundError as exc:
        result.note(str(exc))
        result.finish(evidence.ERROR).save(args.evidence_dir)
        return 1

    proc = run(
        [
            sys.executable, "scripts/run_tests.py", str(binary),
            "--profile-count", str(cfg.get("profile_count", 8)),
            "--json", str(out_json),
            "--no-cert",
        ],
        cwd=BUILD_TESTER,
        timeout=args.timeout,
        tee=True,
        capture=False,
    )

    if not out_json.exists():
        result.note(
            f"build-tester exited {proc.code} without writing {out_json.name}. "
            "It crashed before grading; treat this as a failure, not a flake."
        )
        result.finish(evidence.ERROR).save(args.evidence_dir)
        return 1

    full = read_json(out_json)
    result.tests = flatten(full)
    result.artifacts.append(out_json.name)
    result.metrics.update(
        overall_grade=full.get("overallGrade"),
        total_passed=full.get("totalPassed"),
        total_checks=full.get("totalChecks"),
        cross_profile=full.get("crossProfile"),
        exit_code=proc.code,
    )

    status = evidence.PASS
    # Rules that hold regardless of what the baseline looked like. verify.py
    # fails the run on these even when the previous release was equally dirty.
    violations: List[str] = []

    failures = category_failures(full, cfg.get("required_categories") or [])
    if failures:
        pretty = ", ".join(f"{k}: {v}" for k, v in sorted(failures.items()))
        result.note(f"categories policy requires clean have failing checks -- {pretty}")
        violations.append(f"categories that must be clean have failing checks: {pretty}")
        status = evidence.FAIL

    collisions = uniqueness_collisions(full)
    allowed = int(cfg.get("allow_uniqueness_collisions", 0))
    result.metrics["uniqueness_collisions"] = collisions
    if collisions:
        result.note(
            f"{len(collisions)} cross-profile uniqueness collision(s): {', '.join(collisions)} "
            f"(policy tolerates {allowed}; these draw random fingerprints)"
        )
        if len(collisions) > allowed:
            violations.append(
                f"{len(collisions)} cross-profile uniqueness collisions, policy tolerates {allowed}: "
                + ", ".join(collisions)
            )
            status = evidence.FAIL

    result.metrics["policy_violations"] = violations

    result.note(
        f"grade {full.get('overallGrade')}, {full.get('totalPassed')}/{full.get('totalChecks')} checks, "
        f"{len(result.tests)} identities recorded"
    )
    result.finish(status).save(args.evidence_dir)
    return 0 if status == evidence.PASS else 1


if __name__ == "__main__":
    sys.exit(main())
