"""Self-tests for the repo-wide CI pipeline.

The pipeline's job is to run the right suite against the right browser and
report honestly. These cover the parts where "honestly" is load-bearing:

  * nothing identifying a sundial vector may survive redaction, and the public
    output is a grade rather than a breakdown of what is weak;
  * a skip must carry a reason, or it is indistinguishable from hiding a test;
  * sharding must be stable, so a flake does not appear to move between runners;
  * version resolution must never pick a suite newer than the browser;
  * test identities must match across the vendored and upstream suite layouts.

Run:  python3 -m pytest ci/tests -q
"""

from __future__ import annotations

import json

import pytest

from ci import results
from ci._pytest import junit_test_id
from ci._util import bump_release, opaque_id, parse_version, read_upstream_sh, write_upstream_sh
from ci.pw_camoufox_plugin import load_skiplist, parse_shard, shard_of, skip_reason
from ci.run_sundial import _iter_entries, grade, redact
from ci.summarize import merge_shards, validate_skiplist
from ci.versions import resolve


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
    from ci.run_sundial import _FORBIDDEN_FIELDS

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
    # The out-of-scope failure is measured and counted...
    assert out["metrics"]["ungated_failed"] == 1
    # ...but is not among the identities a build can be failed on.
    assert opaque_id("canvas.rasterizer.subpixel-drift") not in out["tests"]
    assert opaque_id("identity.nav.oscpu") in out["tests"]


def test_no_per_category_breakdown_is_published():
    """A table reading "Graphics 3/17" is the most useful fact an adversary
    could take from a public CI log: it says which part of the fingerprint is
    weakest. Categories decide what is gated; that decision stays inside
    redact()."""
    out = redact(FULL_REPORT, GATED, UNGATED)
    assert "by_category" not in out["metrics"]
    serialised = json.dumps(out)
    for category in GATED + UNGATED:
        assert category not in serialised, f"published output names the category {category!r}"


@pytest.mark.parametrize(
    "rate,expected",
    [(1.0, "A+"), (0.99, "A+"), (0.975, "A"), (0.95, "B"), (0.91, "C"), (0.85, "D"), (0.5, "F")],
)
def test_grade_boundaries(rate, expected):
    assert grade(rate) == expected


def test_the_public_note_is_a_grade_not_a_breakdown():
    out = redact(FULL_REPORT, GATED, UNGATED)
    metrics = out["metrics"]
    assert set(metrics) == {
        "identity", "sundial_version", "schema_version", "grade", "gated_total",
        "gated_scored", "gated_passed", "pass_rate", "ungated_total",
        "ungated_failed", "ungated_tests",
    }


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
    result = results.GateResult(gate="x")
    result.record("t", "fail")
    result.record("t", "pass")
    result.record("t", "fail")
    assert result.tests["t"] == "pass"




# ---------------------------------------------------------------------------
# skiplist
# ---------------------------------------------------------------------------


SKIPS = [
    {"module": "tests/async/test_click.py", "reason": "humanized input"},
    {"test": "tests/async/test_page.py::test_one", "reason": "specific"},
    {"pattern": "[chromium]", "reason": "not a Chromium fork"},
]


@pytest.mark.parametrize(
    "nodeid,expected",
    [
        ("tests/async/test_click.py::test_anything", "humanized input"),
        ("tests/async/test_clicker.py::test_anything", None),   # prefix must not over-match
        ("tests/async/test_page.py::test_one", "specific"),
        ("tests/async/test_page.py::test_two", None),
        ("tests/async/test_x.py::test_y[chromium]", "not a Chromium fork"),
        ("tests/async/test_x.py::test_y[firefox]", None),
    ],
)
def test_skip_matching(nodeid, expected):
    assert skip_reason(nodeid, SKIPS) == expected


def test_the_shipped_skiplist_is_valid():
    """Every entry names something and says why."""
    assert validate_skiplist() == []
    entries = load_skiplist()
    assert entries, "the shipped skiplist is empty"
    for entry in entries:
        assert str(entry.get("reason", "")).strip()


def test_an_unreasoned_skip_is_rejected(tmp_path):
    path = tmp_path / "skiplist.yml"
    path.write_text("schema: 1\nskip:\n  - module: tests/async/test_x.py\n")
    problems = validate_skiplist(path)
    assert problems and "no reason" in problems[0]


def test_load_skiplist_refuses_an_unreasoned_entry(tmp_path, monkeypatch):
    """The plugin must refuse too -- validating only in the summary would let a
    skip take effect for the whole run before anyone objected."""
    path = tmp_path / "skiplist.yml"
    path.write_text("schema: 1\nskip:\n  - module: tests/async/test_x.py\n")
    monkeypatch.setenv("CI_SKIPLIST", str(path))
    with pytest.raises(RuntimeError, match="no reason"):
        load_skiplist()


# ---------------------------------------------------------------------------
# sharding
# ---------------------------------------------------------------------------


def test_shards_partition_the_suite_exactly_once():
    nodeids = [f"tests/async/test_{i // 20}.py::test_{i}" for i in range(600)]
    seen = {}
    for shard in range(1, 7):
        for nodeid in nodeids:
            if shard_of(nodeid, 6) == shard:
                assert nodeid not in seen, f"{nodeid} landed in two shards"
                seen[nodeid] = shard
    assert len(seen) == len(nodeids), "some tests landed in no shard"


def test_shards_are_roughly_even():
    nodeids = [f"tests/async/test_{i // 20}.py::test_{i}" for i in range(1500)]
    counts = [sum(1 for n in nodeids if shard_of(n, 6) == s) for s in range(1, 7)]
    assert min(counts) > len(nodeids) / 6 * 0.8, counts


def test_shard_membership_is_stable_when_a_test_is_inserted():
    """Hashed, not positional: adding a test in the middle of a file must not
    reshuffle every later test, or a flake looks like it moved runners."""
    before = {n: shard_of(n, 6) for n in ("a::t1", "a::t2", "a::t3")}
    after = {n: shard_of(n, 6) for n in ("a::t1", "a::t_new", "a::t2", "a::t3")}
    for nodeid, shard in before.items():
        assert after[nodeid] == shard


@pytest.mark.parametrize("raw,expected", [("3/6", (3, 6)), ("1/1", (1, 1)), (None, None), ("", None)])
def test_parse_shard(raw, expected):
    assert parse_shard(raw) == expected


@pytest.mark.parametrize("raw", ["0/6", "7/6", "abc", "3/0"])
def test_parse_shard_rejects_nonsense(raw):
    with pytest.raises(RuntimeError):
        parse_shard(raw)


# ---------------------------------------------------------------------------
# version resolution
# ---------------------------------------------------------------------------


PINS = [("v1.62.0", "153.0"), ("v1.61.0", "151.0"), ("v1.60.0", "150.0.2"), ("v1.58.0", "146.0.1")]


@pytest.fixture
def offline_pins(monkeypatch):
    monkeypatch.setattr("ci.versions.pins", lambda limit=10: list(PINS))


def test_never_picks_a_suite_newer_than_the_browser(offline_pins, monkeypatch):
    """A newer suite assumes engine work this build does not have, so every
    failure it reports would be ambiguous."""
    monkeypatch.setattr("ci.versions.read_upstream_sh", lambda: {"version": "152.0.4", "release": "beta.31"})
    out = resolve()
    assert out["playwright_tag"] == "v1.61.0"
    assert parse_version(out["playwright_firefox"]) <= parse_version("152.0.4")


def test_the_harness_can_pass_a_version_in(offline_pins, monkeypatch):
    monkeypatch.setattr("ci.versions.read_upstream_sh", lambda: {"version": "152.0.4", "release": "beta.31"})
    assert resolve(browser_version="153.0.4")["playwright_tag"] == "v1.62.0"
    assert resolve(browser_version="146.0.1")["playwright_tag"] == "v1.58.0"


def test_an_explicit_tag_wins(offline_pins, monkeypatch):
    monkeypatch.setattr("ci.versions.read_upstream_sh", lambda: {"version": "146.0.1", "release": "beta.1"})
    out = resolve(playwright_tag="v1.62.0")
    assert out["playwright_tag"] == "v1.62.0"


def test_a_browser_older_than_every_suite_still_resolves(offline_pins, monkeypatch):
    """An old branch should still get tested, loudly, rather than not at all."""
    monkeypatch.setattr("ci.versions.read_upstream_sh", lambda: {"version": "120.0", "release": "old"})
    out = resolve()
    assert out["playwright_tag"] == "v1.58.0"
    assert "oldest available" in out["note"]


# ---------------------------------------------------------------------------
# shard merging
# ---------------------------------------------------------------------------


def test_shards_merge_into_one_record():
    records = {
        "playwright_upstream-1of3": {
            "gate": "playwright_upstream-1of3", "status": "pass",
            "tests": {"a::t1": "pass"}, "metrics": {"shard": "1/3"}, "notes": ["ok"],
        },
        "playwright_upstream-2of3": {
            "gate": "playwright_upstream-2of3", "status": "fail",
            "tests": {"a::t2": "fail"}, "metrics": {"shard": "2/3"}, "notes": ["one failed"],
        },
        "playwright_upstream-3of3": {
            "gate": "playwright_upstream-3of3", "status": "pass",
            "tests": {"a::t3": "pass"}, "metrics": {"shard": "3/3"}, "notes": ["ok"],
        },
        "build": {"gate": "build", "status": "pass", "tests": {}, "metrics": {}, "notes": []},
    }
    merged = merge_shards(records)
    assert set(merged) == {"playwright_upstream", "build"}
    combined = merged["playwright_upstream"]
    assert combined["tests"] == {"a::t1": "pass", "a::t2": "fail", "a::t3": "pass"}
    assert combined["status"] == "fail", "one failing shard must fail the suite"
    assert combined["metrics"]["shards"] == 3
    assert combined["metrics"]["tally"]["total"] == 3
    assert "shard" not in combined["metrics"]


def test_an_errored_shard_beats_a_failed_one():
    records = {
        "s-1of2": {"gate": "s-1of2", "status": "fail", "tests": {"a::1": "fail"}, "metrics": {}, "notes": []},
        "s-2of2": {"gate": "s-2of2", "status": "error", "tests": {}, "metrics": {}, "notes": []},
    }
    assert merge_shards(records)["s"]["status"] == "error"
