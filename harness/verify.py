#!/usr/bin/env python3
"""The proof gate. Nothing else decides whether a run is green.

This exists because "the agent said the build works" is not evidence. Every
claim in the pull request body is computed here, from files the gates wrote,
against a baseline recorded from the last known-good release.

What makes a run fail:

  * A required gate produced no evidence file. Absence is failure, never a
    skip -- otherwise deleting a gate would be the cheapest way to go green.
  * A required gate produced evidence stamped with a different run id, i.e. a
    stale file from an earlier attempt.
  * A test that passed in the baseline does not pass now. That includes tests
    that vanished from the run: deleting a failing test is the second cheapest
    way to go green, so a missing test counts as a regression.
  * A test that is failing and is not covered by a dated, reasoned waiver.
  * An expired waiver, so excuses cannot quietly become permanent.

Run:
    python3 -m harness.verify                 # gate a run, exit non-zero on failure
    python3 -m harness.verify --report-only   # compute and print, always exit 0
    python3 -m harness.verify --update-baseline   # record this run as the new floor
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from . import evidence
from ._util import (
    BASELINE_DIR,
    EVIDENCE_DIR,
    POLICY_PATH,
    REPO_ROOT,
    log,
    read_json,
    summary,
    write_json,
)

BASELINE_PATH = BASELINE_DIR / "current.json"


# ---------------------------------------------------------------------------


@dataclass
class Problem:
    gate: str
    kind: str
    detail: str

    def line(self) -> str:
        return f"  [{self.gate}] {self.kind}: {self.detail}"


@dataclass
class Verdict:
    problems: List[Problem] = field(default_factory=list)
    gate_rows: List[Dict[str, Any]] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def fail(self, gate: str, kind: str, detail: str) -> None:
        self.problems.append(Problem(gate, kind, detail))

    def warn(self, msg: str) -> None:
        log(msg, level="WARN")
        self.warnings.append(msg)


# ---------------------------------------------------------------------------


def load_policy(path: Optional[Path] = None) -> dict:
    import yaml

    with open(path or POLICY_PATH, encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def load_baseline(path: Optional[Path] = None) -> dict:
    path = path or BASELINE_PATH
    if not path.exists():
        return {}
    return read_json(path, default={})


def _waivers(cfg: dict, verdict: Verdict, gate: str) -> Dict[str, dict]:
    """Waivers for a gate, keyed by test id. Expired ones are a failure."""
    out: Dict[str, dict] = {}
    today = date.today()
    for entry in cfg.get("waivers") or []:
        if not isinstance(entry, dict):
            verdict.fail(gate, "malformed waiver", f"expected a mapping, got {entry!r}")
            continue
        tid = str(entry.get("id", "")).strip()
        reason = str(entry.get("reason", "")).strip()
        expires = entry.get("expires")
        if not tid:
            verdict.fail(gate, "malformed waiver", "waiver has no id")
            continue
        if not reason:
            verdict.fail(gate, "unreasoned waiver", f"{tid} has no reason; a waiver needs one")
            continue
        if not expires:
            verdict.fail(gate, "undated waiver", f"{tid} has no expiry; waivers must expire")
            continue
        try:
            when = expires if isinstance(expires, date) else datetime.fromisoformat(str(expires)).date()
        except ValueError:
            verdict.fail(gate, "malformed waiver", f"{tid} has an unparseable expiry {expires!r}")
            continue
        if when < today:
            verdict.fail(
                gate,
                "expired waiver",
                f"{tid} expired on {when}. Re-justify it or fix the underlying failure.",
            )
            continue
        out[tid] = {"reason": reason, "expires": str(when)}
    return out


def _expectations(cfg: dict, verdict: Verdict, gate: str) -> Dict[str, dict]:
    """Known-failing upstream tests, loaded from the file the policy names."""
    rel = cfg.get("expectations")
    if not rel:
        return {}
    import yaml

    # policy.yml states the path relative to the repository root.
    path = (REPO_ROOT / rel).resolve()
    if not path.exists():
        verdict.warn(f"[{gate}] expectations file {rel} does not exist yet")
        return {}
    with open(path, encoding="utf-8") as fh:
        data = yaml.safe_load(fh) or {}
    out: Dict[str, dict] = {}
    for entry in data.get("expected_failures") or []:
        tid = str(entry.get("id", "")).strip()
        reason = str(entry.get("reason", "")).strip()
        if not tid:
            continue
        if not reason:
            verdict.fail(gate, "unreasoned expectation", f"{tid} is expected to fail but says nothing about why")
            continue
        out[tid] = entry
    return out


# ---------------------------------------------------------------------------


def check_gate(
    name: str,
    cfg: dict,
    ev: Optional[dict],
    base_tests: Dict[str, str],
    baseline_metrics: Optional[Dict[str, Any]],
    policy: dict,
    verdict: Verdict,
    *,
    current_run: str,
) -> Dict[str, Any]:
    """Evaluate one gate and append a row for the summary table."""
    required = bool(cfg.get("required", True))
    row: Dict[str, Any] = {
        "gate": name,
        "required": required,
        "status": "missing",
        "detail": "",
        "regressions": 0,
        "new_failures": 0,
        "fixed": 0,
    }

    # --- evidence must exist, and must belong to this run ------------------
    if ev is None:
        row["detail"] = "no evidence file"
        if required:
            verdict.fail(
                name,
                "no evidence",
                "the gate is required but wrote no result. A gate that did not run has not passed.",
            )
        else:
            row["status"] = "skipped"
        return row

    row["status"] = ev.get("status", "error")

    if ev.get("run_id") and current_run and ev["run_id"] != current_run:
        verdict.fail(
            name,
            "stale evidence",
            f"result is stamped run {ev['run_id']}, this is run {current_run}",
        )
        row["detail"] = "stale"
        return row

    reg_cfg = policy.get("regression", {})
    tests: Dict[str, str] = ev.get("tests") or {}
    notes = "; ".join(ev.get("notes", [])[-3:])

    # An error means the gate broke structurally -- the suite would not start,
    # the binary was missing, the credential was refused. Always fatal.
    if required and ev.get("status") == evidence.ERROR:
        verdict.fail(name, "gate errored", notes or "see the gate's log")
        return row

    # Some policy rules are not about the baseline at all: a category that must
    # be clean, a stealth pass rate floor. A gate records those as structured
    # violations so they keep failing the run even when the baseline was just as
    # bad -- "it was already broken" is not a reason to ship it.
    for violation in ev.get("metrics", {}).get("policy_violations") or []:
        verdict.fail(name, "policy violation", str(violation))

    if not tests:
        # No per-test detail, so the gate's own status is all there is. Correct
        # for `build` and `patches_apply`, which are pass-or-fail by nature --
        # and for `sundial`, which publishes a score on purpose.
        row["detail"] = notes or "no per-test detail"

        # A gate that reports a rate instead of identities regresses by getting
        # worse, so compare the number. This is how the stealth check is judged:
        # it deliberately publishes no vector rows to compare.
        max_drop = cfg.get("max_pass_rate_drop")
        current_rate = (ev.get("metrics") or {}).get("pass_rate")
        baseline_rate = ((baseline_metrics or {}).get("pass_rate"))
        if max_drop is not None and current_rate is not None and baseline_rate is not None:
            drop = float(baseline_rate) - float(current_rate)
            row["detail"] = (
                f"{float(current_rate) * 100:.1f}% (baseline {float(baseline_rate) * 100:.1f}%)"
            )
            if drop > float(max_drop):
                verdict.fail(
                    name,
                    "score regressed",
                    f"pass rate fell from {float(baseline_rate) * 100:.1f}% to "
                    f"{float(current_rate) * 100:.1f}% (a drop of {drop * 100:.1f} points; "
                    f"policy allows {float(max_drop) * 100:.1f}). This gate publishes a score "
                    "rather than vector identities, so a drop is all there is to see -- open "
                    "the sealed report locally to find out which checks moved."
                )

        if required and ev.get("status") != evidence.PASS:
            verdict.fail(name, f"gate reported {ev.get('status')}", notes or "see the gate's log")
        return row

    # --- per-test regression detection -------------------------------------
    #
    # Past here the gate's own pass/fail is deliberately NOT the verdict. Every
    # one of these suites has a standing set of known failures -- tests Camoufox
    # cannot pass by design -- and the release that produced the baseline shipped
    # with them. Failing the run because that set is non-empty would mean the
    # harness could never go green at all. What matters is whether the set
    # *changed*, which is what the rest of this function works out.

    waivers = _waivers(cfg, verdict, name)
    expectations = _expectations(cfg, verdict, name)
    excused: Set[str] = set(waivers) | set(expectations)
    bootstrap = bool(cfg.get("bootstrap", False))

    min_collected = int(cfg.get("min_tests_collected", 0) or 0)
    if min_collected and len(tests) < min_collected:
        verdict.fail(
            name,
            "suite did not run properly",
            f"collected {len(tests)} tests, policy expects at least {min_collected}. "
            "A suite that collapses early looks green if you only count failures.",
        )

    regressions: List[str] = []
    new_failures: List[str] = []
    fixed: List[str] = []

    treat_missing = bool(reg_cfg.get("treat_missing_as_regression", True))
    treat_new = bool(reg_cfg.get("treat_new_failure_as_regression", True))

    for tid, was in base_tests.items():
        if was != evidence.PASS:
            continue
        now = tests.get(tid)
        if now == evidence.PASS:
            continue
        if now is None:
            if treat_missing and tid not in excused:
                regressions.append(f"{tid} (passed before, absent now)")
            continue
        if tid not in excused:
            regressions.append(f"{tid} ({was} -> {now})")

    for tid, now in tests.items():
        if now == evidence.PASS:
            if base_tests.get(tid) not in (None, evidence.PASS):
                fixed.append(tid)
            continue
        if now == evidence.SKIP:
            continue
        if tid in base_tests:
            continue  # already handled above
        if tid in excused:
            continue
        new_failures.append(f"{tid} ({now})")

    row["regressions"] = len(regressions)
    row["new_failures"] = len(new_failures)
    row["fixed"] = len(fixed)
    if (
        ev.get("status") != evidence.PASS
        and not regressions
        and not new_failures
        and not (ev.get("metrics", {}).get("policy_violations") or [])
    ):
        # Known failures, unchanged since the baseline. Say so, rather than
        # showing a red cross a human will spend ten minutes chasing.
        row["status"] = "known-failures"

    tally = ev.get("metrics", {}).get("tally", {})
    row["detail"] = (
        f"{tally.get('pass', 0)} passed, {tally.get('fail', 0) + tally.get('error', 0)} failed, "
        f"{tally.get('total', len(tests))} collected"
    )

    if regressions:
        shown = regressions[:25]
        more = f" (+{len(regressions) - 25} more)" if len(regressions) > 25 else ""
        verdict.fail(
            name,
            f"{len(regressions)} regression(s)",
            "tests that passed on the previous release and do not now:\n"
            + "\n".join(f"      - {r}" for r in shown)
            + more,
        )

    max_new = cfg.get("max_new_failures")
    if new_failures:
        if bootstrap:
            verdict.warn(
                f"[{name}] bootstrap mode: {len(new_failures)} unexpected failure(s) recorded, not gated"
            )
        elif treat_new and (max_new is None or len(new_failures) > int(max_new)):
            shown = new_failures[:25]
            more = f" (+{len(new_failures) - 25} more)" if len(new_failures) > 25 else ""
            verdict.fail(
                name,
                f"{len(new_failures)} unexpected failure(s)",
                "not in the baseline and not waived. Either fix them, or add each to the "
                "expectations file with a reason:\n"
                + "\n".join(f"      - {t}" for t in shown)
                + more,
            )

    allow = cfg.get("allow_failures")
    if allow is not None:
        failing = sum(1 for outcome in tests.values() if outcome in (evidence.FAIL, evidence.ERROR))
        if failing > int(allow):
            verdict.fail(
                name, "too many failures", f"{failing} failing, policy allows {allow}"
            )

    return row


# ---------------------------------------------------------------------------


def verify(
    *,
    policy: Optional[dict] = None,
    baseline: Optional[dict] = None,
    evidence_dir: Optional[Path] = None,
) -> Tuple[Verdict, Dict[str, Any]]:
    policy = policy or load_policy()
    baseline = baseline if baseline is not None else load_baseline()

    # The upstream Playwright suite is sharded across runners, arriving as
    # playwright_upstream-3of6 and siblings. Fold them back into one record
    # using the same code the pull-request summary uses, so the harness and CI
    # never disagree about what a suite's result was.
    from ci.summarize import merge_shards

    records = merge_shards(evidence.load_all(evidence_dir or EVIDENCE_DIR))
    verdict = Verdict()

    current_run = evidence.run_id()
    base_gates = baseline.get("gates", {})

    if not baseline:
        verdict.warn(
            "no baseline recorded yet -- regressions cannot be detected on this run. "
            "Record one from a known-good release with --update-baseline."
        )

    for name, cfg in (policy.get("gates") or {}).items():
        cfg = cfg or {}
        base_gate = base_gates.get(name) or {}
        row = check_gate(
            name, cfg, records.get(name), base_gate.get("tests", {}),
            base_gate.get("metrics", {}), policy, verdict,
            current_run=current_run,
        )
        verdict.gate_rows.append(row)

    # An evidence file for a gate the policy does not know about means someone
    # added a check without declaring it -- or renamed one to dodge the gate.
    unknown = set(records) - set(policy.get("gates") or {})
    for name in sorted(unknown):
        verdict.warn(f"evidence for undeclared gate {name!r} -- it is not gated on")

    report = {
        "ok": verdict.ok,
        "run_id": current_run,
        "generated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "baseline": {
            "recorded": baseline.get("recorded"),
            "firefox_version": baseline.get("firefox_version"),
            "camoufox_release": baseline.get("camoufox_release"),
            "playwright_tag": baseline.get("playwright_tag"),
        },
        "gates": verdict.gate_rows,
        "problems": [{"gate": p.gate, "kind": p.kind, "detail": p.detail} for p in verdict.problems],
        "warnings": verdict.warnings,
    }
    return verdict, report


def render_markdown(report: Dict[str, Any], records: Dict[str, Any]) -> str:
    ok = report["ok"]
    head = "## ✅ All gates passed" if ok else "## ❌ Gates failed"
    base = report["baseline"]
    lines = [
        head,
        "",
        (
            f"Baseline: Firefox `{base.get('firefox_version') or '-'}` "
            f"`{base.get('camoufox_release') or '-'}`, Playwright `{base.get('playwright_tag') or '-'}` "
            f"(recorded {base.get('recorded') or 'never'})"
        ),
        "",
        "| Gate | Status | Result | Regressions | New failures | Newly fixed |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for row in report["gates"]:
        icon = {
            "pass": "✅", "known-failures": "➖", "fail": "❌",
            "error": "💥", "missing": "🚫", "skipped": "⏭️",
        }.get(row["status"], "❓")
        req = "" if row["required"] else " _(advisory)_"
        lines.append(
            f"| `{row['gate']}`{req} | {icon} {row['status']} | {row['detail'] or '-'} | "
            f"{row['regressions'] or '-'} | {row['new_failures'] or '-'} | {row['fixed'] or '-'} |"
        )

    if report["problems"]:
        lines += ["", "### What failed", ""]
        for problem in report["problems"]:
            lines.append(f"**`{problem['gate']}` — {problem['kind']}**")
            lines.append("")
            lines.append("```")
            lines.append(problem["detail"])
            lines.append("```")
            lines.append("")

    if report["warnings"]:
        lines += ["", "<details><summary>Warnings</summary>", ""]
        lines += [f"- {w}" for w in report["warnings"]]
        lines += ["", "</details>"]

    lines += [
        "",
        "<sub>Computed by `harness/verify.py` from the evidence bundle, not from anything "
        "the repair agent reported. A required gate that produced no evidence is a failure, "
        "and a test that passed on the previous release and does not now is a regression even "
        "if the totals improved.</sub>",
    ]
    return "\n".join(lines)


def update_baseline(
    *,
    meta: Dict[str, str],
    evidence_dir: Optional[Path] = None,
    path: Optional[Path] = None,
) -> Path:
    """Record the current evidence as the floor future runs must clear."""
    records = evidence.load_all(evidence_dir or EVIDENCE_DIR)
    baseline = {
        "schema": 1,
        "recorded": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        **meta,
        "gates": {
            name: {
                "status": rec.get("status"),
                "tests": rec.get("tests", {}),
                "metrics": rec.get("metrics", {}),
            }
            for name, rec in records.items()
        },
    }
    target = path or BASELINE_PATH
    write_json(target, baseline)
    total = sum(len(g["tests"]) for g in baseline["gates"].values())
    log(f"baseline written to {target}: {len(baseline['gates'])} gates, {total} test identities")
    return target


# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report-only", action="store_true", help="never exit non-zero")
    parser.add_argument("--update-baseline", action="store_true")
    parser.add_argument("--evidence-dir", type=Path)
    parser.add_argument("--out", type=Path, help="write the machine-readable report here")
    parser.add_argument("--markdown", type=Path, help="write the human-readable report here")
    parser.add_argument("--firefox-version", default="")
    parser.add_argument("--camoufox-release", default="")
    parser.add_argument("--playwright-tag", default="")
    args = parser.parse_args(argv)

    evidence_dir = args.evidence_dir or EVIDENCE_DIR
    verdict, report = verify(evidence_dir=evidence_dir)
    records = evidence.load_all(evidence_dir)
    markdown = render_markdown(report, records)

    print()
    print(markdown)
    print()

    if args.out:
        write_json(args.out, report)
    if args.markdown:
        args.markdown.parent.mkdir(parents=True, exist_ok=True)
        args.markdown.write_text(markdown + "\n", encoding="utf-8")
    summary(markdown)

    if args.update_baseline:
        if not verdict.ok:
            log("refusing to record a baseline from a run that did not pass", level="ERROR")
            return 2
        update_baseline(
            meta={
                "firefox_version": args.firefox_version,
                "camoufox_release": args.camoufox_release,
                "playwright_tag": args.playwright_tag,
            },
            evidence_dir=evidence_dir,
        )

    if verdict.ok:
        log("all gates passed")
        return 0

    log(f"{len(verdict.problems)} problem(s):", level="ERROR")
    for problem in verdict.problems:
        log(problem.line(), level="ERROR")
    return 0 if args.report_only else 1


if __name__ == "__main__":
    sys.exit(main())
