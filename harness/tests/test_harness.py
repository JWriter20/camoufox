"""Self-tests for the harness.

The harness's job is to refuse to ship a regression. That claim is only worth
something if the refusal itself is tested, so these focus on the properties that
would let a broken build through:

  * a gate that did not run must fail, not skip;
  * a test that disappeared must count as a regression;
  * stale evidence must not satisfy a gate;
  * an expired or unreasoned waiver must fail;
  * and nothing that identifies a sundial vector may survive redaction.

Run:  python3 -m pytest harness/tests -q
"""

from __future__ import annotations

import json
from datetime import date, timedelta

import pytest

from harness import evidence, verify
from harness._util import bump_release, opaque_id, parse_version, read_upstream_sh, write_upstream_sh
from harness.gates import junit_test_id
from harness.gates.sundial import _iter_entries, redact


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
# sundial redaction -- the one that must never regress
# ---------------------------------------------------------------------------


SECRETS = [
    "canvas.rasterizer.subpixel-drift",
    "A private vector name nobody may publish",
    "checks whether the FMA3 path is present",
    "return Math.fround(x) !== y",
    "3.14159265358979",
]

FULL_REPORT = {
    "schemaVersion": 1,
    "sundialVersion": "0.3.1",
    "identity": {"name": "Firefox", "os": "linux", "engine": "SpiderMonkey", "tz": "UTC"},
    "summary": {"total": 3, "pass": 1, "fail": 2},
    "failures": {
        "Graphics": [{
            "key": SECRETS[0], "id": "gfx-1", "name": SECRETS[1], "brief": SECRETS[2],
            "src": SECRETS[3], "source": SECRETS[3], "value": SECRETS[4],
            "expect": SECRETS[4], "category": "Graphics", "status": "fail",
        }],
        "Identity": [{
            "key": "identity.nav.oscpu", "id": "id-9", "name": SECRETS[1],
            "value": SECRETS[4], "category": "Identity", "status": "fail",
        }],
    },
    "succeeded": {
        "Identity": [{
            "key": "identity.nav.platform", "id": "id-3", "name": SECRETS[1],
            "value": SECRETS[4], "category": "Identity", "status": "pass",
        }],
    },
    "private": {
        "failures": {
            "Locale": [{
                "key": "pv-secret-vector", "id": "pv-1", "name": SECRETS[1],
                "src": SECRETS[3], "category": "Locale", "status": "fail",
            }],
        },
    },
}

GATED = ["Identity", "Security", "JS Engine", "Display", "Locale", "Network"]
UNGATED = ["Graphics", "Audio", "CPU"]


def test_redaction_leaks_nothing_identifying():
    blob = json.dumps(redact(FULL_REPORT, GATED, UNGATED))
    for secret in SECRETS:
        assert secret not in blob, f"redaction leaked: {secret!r}"
    # Raw vector keys must not survive either.
    for key in ("canvas.rasterizer.subpixel-drift", "identity.nav.oscpu", "pv-secret-vector"):
        assert key not in blob, f"redaction leaked the vector key {key!r}"


def test_redaction_drops_every_forbidden_field():
    """No per-vector field survives.

    `metrics.identity` is deliberately exempt and checked separately: it
    describes the browser under test -- our own claimed platform, engine and
    timezone -- not a sundial vector, so keeping it leaks nothing and makes a
    failed run diagnosable.
    """
    from harness.gates.sundial import _FORBIDDEN_FIELDS

    out = redact(FULL_REPORT, GATED, UNGATED)
    identity = out["metrics"].pop("identity")
    serialised = json.dumps(out)
    for field in _FORBIDDEN_FIELDS:
        assert f'"{field}"' not in serialised, f"redacted output still carries a {field!r} field"

    # And the exemption is a closed whitelist, not an open door.
    assert set(identity) <= {
        "name", "os", "osDetected", "engine", "platform", "lang", "tz",
        "uaMismatch", "engineMismatch", "osConsistent",
    }


def test_redaction_output_is_only_ids_counts_and_our_own_identity():
    """Structural check: every leaf under `tests` is an opaque id -> outcome."""
    out = redact(FULL_REPORT, GATED, UNGATED)
    for tid, outcome in out["tests"].items():
        assert len(tid) == 20 and all(c in "0123456789abcdef" for c in tid), tid
        assert outcome in {"pass", "fail", "error", "skip"}
    for tid in out["metrics"]["ungated_tests"]:
        assert len(tid) == 20 and all(c in "0123456789abcdef" for c in tid), tid


def test_only_gated_categories_can_fail_the_run():
    out = redact(FULL_REPORT, GATED, UNGATED)
    # Graphics is measured...
    assert out["metrics"]["by_category"]["Graphics"]["fail"] == 1
    assert out["metrics"]["ungated_failed"] == 1
    # ...but is not among the identities verify.py gates on.
    assert opaque_id("canvas.rasterizer.subpixel-drift") not in out["tests"]
    assert opaque_id("identity.nav.oscpu") in out["tests"]


def test_private_vectors_are_gated_but_still_opaque():
    out = redact(FULL_REPORT, GATED, UNGATED)
    assert out["tests"][opaque_id("pv-secret-vector")] == "fail"


def test_opaque_ids_are_stable_and_not_the_input():
    assert opaque_id("x") == opaque_id("x")
    assert opaque_id("x") != opaque_id("y")
    assert "x" not in opaque_id("x")


def test_pass_rate_counts_only_gated_scored_vectors():
    out = redact(FULL_REPORT, GATED, UNGATED)["metrics"]
    # Identity: one pass, one fail. Locale (private): one fail. -> 1/3
    assert out["gated_scored"] == 3
    assert out["gated_passed"] == 1
    assert out["pass_rate"] == pytest.approx(1 / 3, abs=1e-4)


def test_iter_entries_finds_public_and_private():
    keys = {k for k, _, _ in _iter_entries(FULL_REPORT)}
    assert "pv-secret-vector" in keys
    assert "identity.nav.platform" in keys


# ---------------------------------------------------------------------------
# small pieces
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "classname,name,expected",
    [
        ("tests.async.test_page", "test_foo", "async/test_page.py::test_foo"),
        ("async.test_page", "test_foo", "async/test_page.py::test_foo"),
    ],
)
def test_junit_ids_match_across_vendored_and_upstream_layouts(classname, name, expected):
    """The two suites report different dotted paths for the same test file."""
    assert junit_test_id(classname, name) == expected


@pytest.mark.parametrize(
    "given,expected",
    [("beta.31", "beta.32"), ("beta.9", "beta.10"), ("alpha", "alpha.1")],
)
def test_release_bump(given, expected):
    assert bump_release(given) == expected


def test_version_parsing():
    assert parse_version("153.0.4") == (153, 0, 4)
    assert parse_version("155.0") == (155, 0, 0)


def test_upstream_sh_roundtrip_preserves_comments(tmp_path):
    path = tmp_path / "upstream.sh"
    path.write_text("# a comment\nversion=152.0.4\nrelease=beta.31\nclosedsrc_rev=1.0.0\n")
    write_upstream_sh({"version": "153.0.4", "release": "beta.32"}, path)
    text = path.read_text()
    assert "# a comment" in text
    assert "closedsrc_rev=1.0.0" in text
    assert read_upstream_sh(path)["version"] == "153.0.4"


def test_gate_result_keeps_the_best_outcome_across_retries():
    result = evidence.GateResult(gate="x")
    result.record("t", "fail")
    result.record("t", "pass")
    result.record("t", "fail")
    assert result.tests["t"] == "pass"


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


def test_build_tester_flags_a_dirty_required_category():
    from harness.gates.build_tester import category_failures, flatten, uniqueness_collisions

    full = {
        "profiles": [{
            "profile": {"os": "linux", "mode": "per-context", "index": 0},
            "results": {"core": {"Automation Detection": {"webdriver": {"passed": False}}}},
            "matchResults": [],
        }],
        "crossProfile": {"macPerContext": {"total": 3, "uniqueCanvas": 2, "uniqueAudio": 3}},
    }
    assert category_failures(full, ["Automation Detection"]) == {"Automation Detection": 1}
    assert flatten(full)["linux-per-context-0/core/Automation Detection/webdriver"] == "fail"
    assert uniqueness_collisions(full) == ["macPerContext.uniqueCanvas (2/3 distinct)"]
