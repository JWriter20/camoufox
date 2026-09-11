"""Self-tests for the harness.

The harness's job is to refuse to ship a regression. That claim is only worth
something if the refusal itself is tested, so these focus on the properties that
would let a broken build through:

  * a gate that did not run must fail, not skip;
  * a test that disappeared must count as a regression;
  * stale evidence must not satisfy a gate;
  * an expired or unreasoned waiver must fail;
  * and a gate-level policy rule stays fatal even when the baseline was as bad.

Redaction, sharding, skiplists and version resolution belong to the repo-wide
pipeline and are tested in `ci/tests/`.

Run:  python3 -m pytest harness/tests -q
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from harness import evidence, verify
from harness._util import bump_release, opaque_id, parse_version, read_upstream_sh, write_upstream_sh


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def policy():
    return {
        "gates": {
            "alpha": {"required": True},
            "beta": {"required": True, "max_new_failures": 0},
            "advisory": {"required": False},
        },
        "regression": {
            "treat_missing_as_regression": True,
            "treat_new_failure_as_regression": True,
        },
    }


def _write(directory, gate, *, status="pass", tests=None, run="R1"):
    result = evidence.GateResult(gate=gate, run_id=run)
    for tid, outcome in (tests or {}).items():
        result.tests[tid] = outcome
    result.finish(status).save(directory)
    return result


@pytest.fixture
def green(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_RUN_ID", "R1")
    directory = tmp_path / "evidence"
    _write(directory, "alpha")
    _write(directory, "beta", tests={"t1": "pass", "t2": "pass", "t3": "pass"})
    _write(directory, "advisory")
    return directory


@pytest.fixture
def baseline(green, tmp_path):
    return {
        "gates": {
            "beta": {"tests": {"t1": "pass", "t2": "pass", "t3": "pass"}},
        }
    }


def _verify(policy, baseline, directory):
    return verify.verify(policy=policy, baseline=baseline, evidence_dir=directory)


# ---------------------------------------------------------------------------
# the gate cannot be talked out of failing
# ---------------------------------------------------------------------------


def test_clean_rerun_passes(policy, baseline, green):
    verdict, _ = _verify(policy, baseline, green)
    assert verdict.ok, [p.line() for p in verdict.problems]


def test_missing_required_gate_fails(policy, baseline, green):
    (green / "alpha.json").unlink()
    verdict, _ = _verify(policy, baseline, green)
    assert not verdict.ok
    assert any(p.kind == "no evidence" for p in verdict.problems)


def test_missing_optional_gate_is_allowed(policy, baseline, green):
    (green / "advisory.json").unlink()
    verdict, _ = _verify(policy, baseline, green)
    assert verdict.ok


def test_deleted_test_counts_as_regression(policy, baseline, green):
    """Dropping a failing test is the cheapest way to make a suite go green."""
    data = json.loads((green / "beta.json").read_text())
    del data["tests"]["t2"]
    (green / "beta.json").write_text(json.dumps(data))
    verdict, _ = _verify(policy, baseline, green)
    assert not verdict.ok
    assert any("absent now" in p.detail for p in verdict.problems)


def test_flipped_test_is_a_regression(policy, baseline, green):
    data = json.loads((green / "beta.json").read_text())
    data["tests"]["t2"] = "fail"
    (green / "beta.json").write_text(json.dumps(data))
    verdict, _ = _verify(policy, baseline, green)
    assert not verdict.ok
    assert any("regression" in p.kind for p in verdict.problems)


def test_totals_improving_does_not_excuse_a_regression(policy, baseline, green):
    """More passes overall, but one identity went backwards. Still a failure."""
    data = json.loads((green / "beta.json").read_text())
    data["tests"]["t2"] = "fail"
    data["tests"].update({f"new{i}": "pass" for i in range(20)})
    (green / "beta.json").write_text(json.dumps(data))
    verdict, _ = _verify(policy, baseline, green)
    assert not verdict.ok


def test_stale_evidence_does_not_satisfy_a_gate(policy, baseline, green):
    data = json.loads((green / "alpha.json").read_text())
    data["run_id"] = "an-older-run"
    (green / "alpha.json").write_text(json.dumps(data))
    verdict, _ = _verify(policy, baseline, green)
    assert not verdict.ok
    assert any(p.kind == "stale evidence" for p in verdict.problems)


def test_new_unexpected_failure_fails(policy, baseline, green):
    data = json.loads((green / "beta.json").read_text())
    data["tests"]["brand_new"] = "fail"
    (green / "beta.json").write_text(json.dumps(data))
    verdict, _ = _verify(policy, baseline, green)
    assert not verdict.ok
    assert any("unexpected failure" in p.kind for p in verdict.problems)


def test_bootstrap_reports_without_gating(policy, baseline, green):
    policy["gates"]["beta"]["bootstrap"] = True
    data = json.loads((green / "beta.json").read_text())
    data["tests"]["brand_new"] = "fail"
    (green / "beta.json").write_text(json.dumps(data))
    verdict, _ = _verify(policy, baseline, green)
    assert verdict.ok
    assert any("bootstrap" in w for w in verdict.warnings)


def test_min_tests_collected_catches_a_collapsed_suite(policy, baseline, green):
    policy["gates"]["beta"]["min_tests_collected"] = 100
    verdict, _ = _verify(policy, baseline, green)
    assert not verdict.ok
    assert any("did not run properly" in p.kind for p in verdict.problems)


def test_baseline_is_not_recorded_from_a_failing_run(policy, baseline, green, tmp_path):
    (green / "alpha.json").unlink()
    verdict, _ = _verify(policy, baseline, green)
    assert not verdict.ok  # update_baseline is guarded on exactly this in main()


# ---------------------------------------------------------------------------
# waivers must stay honest
# ---------------------------------------------------------------------------


def _waived(policy, baseline, green, waiver):
    policy["gates"]["beta"]["waivers"] = [waiver]
    data = json.loads((green / "beta.json").read_text())
    data["tests"]["t2"] = "fail"
    (green / "beta.json").write_text(json.dumps(data))
    return _verify(policy, baseline, green)


def test_valid_waiver_excuses_a_failure(policy, baseline, green):
    verdict, _ = _waived(policy, baseline, green, {
        "id": "t2",
        "reason": "Camoufox does not claim this behaviour.",
        "expires": str(date.today() + timedelta(days=30)),
    })
    assert verdict.ok


def test_expired_waiver_fails(policy, baseline, green):
    verdict, _ = _waived(policy, baseline, green, {
        "id": "t2",
        "reason": "Camoufox does not claim this behaviour.",
        "expires": str(date.today() - timedelta(days=1)),
    })
    assert not verdict.ok
    assert any(p.kind == "expired waiver" for p in verdict.problems)


def test_undated_waiver_fails(policy, baseline, green):
    verdict, _ = _waived(policy, baseline, green, {"id": "t2", "reason": "because"})
    assert not verdict.ok
    assert any(p.kind == "undated waiver" for p in verdict.problems)


def test_unreasoned_waiver_fails(policy, baseline, green):
    verdict, _ = _waived(policy, baseline, green, {
        "id": "t2", "expires": str(date.today() + timedelta(days=30)),
    })
    assert not verdict.ok
    assert any(p.kind == "unreasoned waiver" for p in verdict.problems)


# ---------------------------------------------------------------------------
# path confinement -- the agent must not be able to edit its own examiner
# ---------------------------------------------------------------------------


REPAIR_CFG = {
    "writable_paths": ["patches/", "additions/", "settings/", "upstream.sh", "docs/"],
    "forbidden_paths": [
        "harness/", ".github/workflows/", "tests/", "build-tester/", "service-tester/",
    ],
}


@pytest.fixture
def fake_repo(tmp_path, monkeypatch):
    """A throwaway git repo standing in for the real one."""
    import subprocess

    from harness import repair_patches

    def git(*args):
        subprocess.run(["git", *args], cwd=tmp_path, check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "t")
    for rel in ("patches/a.patch", "harness/policy.yml", "tests/conftest.py", "scripts/patch.py"):
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("original\n")
    git("add", "-A")
    git("commit", "-qm", "init")

    # enforce_paths() resolves everything against the module-level REPO_ROOT,
    # which is imported by name -- so patching it here is enough.
    monkeypatch.setattr(repair_patches, "REPO_ROOT", tmp_path)
    return tmp_path


def _enforce(repo):
    from harness import repair_patches

    return repair_patches.enforce_paths(REPAIR_CFG, source_dir_name="camoufox-153.0.4-beta.32")


def test_edits_inside_the_allowance_are_kept(fake_repo):
    (fake_repo / "patches" / "a.patch").write_text("agent edit\n")
    out = _enforce(fake_repo)
    assert out["forbidden"] == []
    assert out["reverted"] == []
    assert (fake_repo / "patches" / "a.patch").read_text() == "agent edit\n"


def test_edits_outside_the_allowance_are_reverted(fake_repo):
    (fake_repo / "scripts" / "patch.py").write_text("agent edit\n")
    out = _enforce(fake_repo)
    assert "scripts/patch.py" in out["reverted"]
    assert (fake_repo / "scripts" / "patch.py").read_text() == "original\n"


def test_untracked_files_outside_the_allowance_are_removed(fake_repo):
    stray = fake_repo / "scripts" / "sneaky.py"
    stray.write_text("whatever\n")
    out = _enforce(fake_repo)
    assert "scripts/sneaky.py" in out["reverted"]
    assert not stray.exists()


@pytest.mark.parametrize(
    "target",
    ["harness/policy.yml", "tests/conftest.py", "build-tester/x.py", ".github/workflows/ci.yml"],
)
def test_touching_a_gate_taints_the_run(fake_repo, target):
    """Reverting is not enough here: reaching for a gate voids the whole run."""
    path = fake_repo / target
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("agent edit\n")
    out = _enforce(fake_repo)
    assert target in out["forbidden"], out
    # Deliberately *not* reverted -- the run is void, not repaired.
    assert path.read_text() == "agent edit\n"


def test_the_generated_firefox_tree_is_ignored(fake_repo):
    """camoufox-*/ is scratch space; churn there is expected, not a violation."""
    tree = fake_repo / "camoufox-153.0.4-beta.32" / "dom" / "base"
    tree.mkdir(parents=True)
    (tree / "Navigator.cpp").write_text("edited by the agent\n")
    out = _enforce(fake_repo)
    assert out["forbidden"] == []
    assert out["reverted"] == []


# ---------------------------------------------------------------------------
# known failures vs regressions -- the distinction the whole thing turns on
# ---------------------------------------------------------------------------


def test_standing_known_failures_do_not_fail_the_run(policy, green):
    """13 failing Playwright tests is the normal state, not a reason to stop.

    Camoufox cannot pass parts of upstream's suite by design. The release that
    produced the baseline shipped with those failures, so failing the run merely
    because the failing set is non-empty would mean the harness could never go
    green at all. What matters is whether the set changed.
    """
    data = json.loads((green / "beta.json").read_text())
    data["tests"]["t2"] = "fail"
    data["status"] = "fail"
    (green / "beta.json").write_text(json.dumps(data))
    baseline = {"gates": {"beta": {"tests": {"t1": "pass", "t2": "fail", "t3": "pass"}}}}

    verdict, report = _verify(policy, baseline, green)
    assert verdict.ok, [p.line() for p in verdict.problems]
    row = next(r for r in report["gates"] if r["gate"] == "beta")
    assert row["status"] == "known-failures"
    assert row["regressions"] == 0


def test_one_more_failure_than_the_baseline_does_fail(policy, green):
    data = json.loads((green / "beta.json").read_text())
    data["tests"].update({"t2": "fail", "t3": "fail"})
    data["status"] = "fail"
    (green / "beta.json").write_text(json.dumps(data))
    baseline = {"gates": {"beta": {"tests": {"t1": "pass", "t2": "fail", "t3": "pass"}}}}

    verdict, _ = _verify(policy, baseline, green)
    assert not verdict.ok
    assert any("regression" in p.kind for p in verdict.problems)


def test_a_gate_that_errored_is_always_fatal(policy, baseline, green):
    """Structural breakage -- suite would not start, binary missing, auth refused."""
    data = json.loads((green / "beta.json").read_text())
    data["status"] = "error"
    (green / "beta.json").write_text(json.dumps(data))
    verdict, _ = _verify(policy, baseline, green)
    assert not verdict.ok
    assert any(p.kind == "gate errored" for p in verdict.problems)


def test_policy_violations_fail_even_with_a_matching_baseline(policy, green):
    """A stealth score below the floor is fatal even if last release was as bad."""
    data = json.loads((green / "beta.json").read_text())
    data["status"] = "fail"
    data["metrics"]["policy_violations"] = ["stealth pass rate 0.400 is below the policy floor 0.9"]
    (green / "beta.json").write_text(json.dumps(data))
    baseline = {"gates": {"beta": {"tests": {"t1": "pass", "t2": "pass", "t3": "pass"}}}}

    verdict, _ = _verify(policy, baseline, green)
    assert not verdict.ok
    assert any(p.kind == "policy violation" for p in verdict.problems)


