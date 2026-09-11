"""Harness-specific paths, on top of the repo-wide CI plumbing.

Everything that runs a suite lives in `ci/`; the harness only adds the pieces
that are about *gating* a Firefox bump -- the policy, the baseline, the agent.
Re-exporting rather than duplicating keeps one definition of REPO_ROOT, run(),
and the version helpers.
"""

from __future__ import annotations

from ci._util import (  # noqa: F401  -- re-exported for harness modules
    DEFAULT_ID_SALT,
    EVIDENCE_DIR,
    REPO_ROOT,
    RESULTS_DIR,
    WORK_DIR,
    Result,
    bump_release,
    die,
    digest_files,
    endgroup,
    group,
    http_json,
    http_text,
    log,
    major,
    opaque_id,
    parse_version,
    read_json,
    read_upstream_sh,
    run,
    set_output,
    summary,
    write_json,
    write_upstream_sh,
)

HARNESS_DIR = REPO_ROOT / "harness"
BASELINE_DIR = HARNESS_DIR / "baseline"
POLICY_PATH = HARNESS_DIR / "policy.yml"
