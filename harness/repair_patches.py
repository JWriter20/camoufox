#!/usr/bin/env python3
"""Rebase the patch stack onto a new Firefox, with a model doing the hard part.

The mechanical steps -- bump `upstream.sh`, fetch, extract, copy additions --
are scripted. The step that actually needs judgement is reading a reject hunk,
working out where Firefox moved the code, and porting the change. That is handed
to a coding agent, one patch at a time, with the reject hunks and the repository's
own upgrading guide in the prompt.

The loop is bounded in three ways, because an unbounded agent in CI is a way to
spend money and produce nothing:

  * a turn ceiling (`policy.yml: repair.max_agent_turns`);
  * progress checking -- a turn that does not reduce the reject count twice
    running ends the loop;
  * path confinement -- after every turn, anything the agent wrote outside
    `repair.writable_paths` is reverted, and a write to `repair.forbidden_paths`
    marks the run tainted and stops it.

That last one is the important one. `harness/`, `tests/` and the workflows are
forbidden, so the agent cannot make itself pass by editing a gate, a baseline,
or a test.

Run:
    python3 -m harness.repair_patches --target-version 153.0.4
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path
from typing import Dict, List, Optional

from . import evidence, patchset
from ._util import (
    EVIDENCE_DIR,
    HARNESS_DIR,
    POLICY_PATH,
    REPO_ROOT,
    bump_release,
    die,
    log,
    read_upstream_sh,
    run,
    write_upstream_sh,
)
from .agent import providers

PROMPT_PATH = HARNESS_DIR / "agent" / "prompt.md"


# ---------------------------------------------------------------------------
# path confinement
# ---------------------------------------------------------------------------


def _tracked_changes() -> List[str]:
    proc = run(["git", "status", "--porcelain=v1", "--untracked-files=all"], cwd=REPO_ROOT)
    paths: List[str] = []
    for line in proc.stdout.splitlines():
        if len(line) < 4:
            continue
        path = line[3:].strip()
        # Renames read "old -> new"; the new path is what was written.
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        paths.append(path.strip('"'))
    return paths


def _matches(path: str, prefixes: List[str]) -> bool:
    return any(path == p.rstrip("/") or path.startswith(p.rstrip("/") + "/") for p in prefixes)


def enforce_paths(cfg: dict, *, source_dir_name: str) -> Dict[str, List[str]]:
    """Revert anything the agent wrote outside its allowance.

    Returns {"reverted": [...], "forbidden": [...]}. A non-empty `forbidden`
    means the run is tainted and must not continue: an agent that reached for a
    gate has disqualified its own output, whatever the tree looks like now.
    """
    writable = list(cfg.get("writable_paths") or [])
    forbidden = list(cfg.get("forbidden_paths") or [])

    reverted: List[str] = []
    violations: List[str] = []

    for path in _tracked_changes():
        # The generated Firefox tree is scratch space; it is rebuilt from the
        # tarball every run and is never committed.
        if path.startswith(source_dir_name):
            continue
        if _matches(path, forbidden):
            violations.append(path)
            continue
        if _matches(path, writable):
            continue
        reverted.append(path)

    for path in reverted:
        full = REPO_ROOT / path
        restored = run(["git", "checkout", "--", path], cwd=REPO_ROOT)
        if not restored.ok and full.exists():
            # Untracked file the agent invented outside its allowance.
            full.unlink() if full.is_file() else shutil.rmtree(full, ignore_errors=True)
        log(f"reverted out-of-scope change: {path}", level="WARN")

    return {"reverted": reverted, "forbidden": violations}


# ---------------------------------------------------------------------------
# tree preparation
# ---------------------------------------------------------------------------


# `make setup` git-inits the extracted tree and commits, to give the repair loop
# an `unpatched` tag to reset to. A CI runner has no git identity, so that
# commit fails with "fatal: empty ident name". Supplying it through the
# environment sets it for that one commit without writing to global git config.
_GIT_IDENTITY = {
    "GIT_AUTHOR_NAME": "camoufox-harness",
    "GIT_AUTHOR_EMAIL": "harness@camoufox.invalid",
    "GIT_COMMITTER_NAME": "camoufox-harness",
    "GIT_COMMITTER_EMAIL": "harness@camoufox.invalid",
}


def prepare_tree(version: str, release: str, *, skip_fetch: bool = False) -> Path:
    """Fetch and extract Firefox `version` into a fresh camoufox source tree."""
    tree = REPO_ROOT / f"camoufox-{version}-{release}"
    if not skip_fetch:
        run(["make", "fetch"], cwd=REPO_ROOT, check=True, timeout=3600, tee=True, capture=False)
    if not (tree / "configure.py").exists():
        run(["make", "setup"], cwd=REPO_ROOT, env=_GIT_IDENTITY, check=True,
            timeout=3600, tee=True, capture=False)
    if not (tree / "configure.py").exists():
        die(f"{tree} does not look like a Firefox tree after setup")
    return tree


def build_prompt(report: patchset.ApplyReport, *, version: str, previous: str, turn: int) -> str:
    """The task, plus the rejects, plus the repository's own guidance."""
    template = PROMPT_PATH.read_text(encoding="utf-8")

    blocks: List[str] = []
    for name in sorted(report.failed):
        rejects = report.failed[name]
        blocks.append(f"### patches/{name} — {len(rejects)} reject(s)")
        # Budget the prompt: a handful of hunks in full beats fifty truncated.
        for reject in rejects[:6]:
            blocks.append("```diff\n" + reject.summary() + "\n```")
        if len(rejects) > 6:
            blocks.append(f"_({len(rejects) - 6} further reject file(s) not shown)_")

    return (
        template
        .replace("{{TURN}}", str(turn))
        .replace("{{NEW_VERSION}}", version)
        .replace("{{OLD_VERSION}}", previous)
        .replace("{{FAILED_COUNT}}", str(len(report.failed)))
        .replace("{{FAILED_LIST}}", ", ".join(sorted(report.failed)) or "(none)")
        .replace("{{REJECTS}}", "\n\n".join(blocks) or "(no rejects)")
    )


# ---------------------------------------------------------------------------


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-version", required=True)
    parser.add_argument("--release", help="camoufox release tag; defaults to bumping the current one")
    parser.add_argument("--agent", help="codex | claude | none (default: $HARNESS_AGENT or codex)")
    parser.add_argument("--evidence-dir", type=Path, default=EVIDENCE_DIR)
    parser.add_argument("--turn-timeout", type=int, default=1800)
    parser.add_argument("--skip-fetch", action="store_true")
    parser.add_argument("--no-bump", action="store_true", help="assume upstream.sh is already set")
    args = parser.parse_args(argv)

    import yaml

    with open(POLICY_PATH, encoding="utf-8") as fh:
        policy = yaml.safe_load(fh)
    cfg = policy.get("repair") or {}

    result = evidence.GateResult(gate="patches_apply")

    current = read_upstream_sh()
    previous_version = current["version"]
    release = args.release or bump_release(current["release"])

    if not args.no_bump:
        write_upstream_sh({"version": args.target_version, "release": release})
        log(f"upstream.sh: {previous_version} -> {args.target_version}, release {release}")
    result.metrics.update(
        previous_version=previous_version,
        target_version=args.target_version,
        release=release,
    )

    tree = prepare_tree(args.target_version, release, skip_fetch=args.skip_fetch)
    source_dir_name = tree.name

    provider = providers.get(args.agent)
    unavailable = provider.available()
    result.metrics["agent"] = provider.name
    log(f"agent provider: {provider.name}" + (f" (unavailable: {unavailable})" if unavailable else ""))

    max_turns = int(cfg.get("max_agent_turns", 0) or 0)
    turn = 0
    stalls = 0
    previous_failures = None
    report = patchset.ApplyReport()

    while True:
        patchset.reset_tree(tree, args.target_version, release)
        report = patchset.apply_all(tree)
        log(report.brief())
        result.metrics[f"turn_{turn}_failed_patches"] = sorted(report.failed)

        if report.clean:
            break
        if provider.name == "none" or unavailable:
            result.note(
                f"{len(report.failed)} patch(es) need rebasing and no agent is available"
                + (f" ({unavailable})" if unavailable else "")
                + ". Rebase them by hand -- see docs/patch-upgrading-guide.md."
            )
            break
        if turn >= max_turns:
            result.note(f"hit the turn ceiling ({max_turns}) with {len(report.failed)} patch(es) still rejecting")
            break

        # Stop paying for turns that are not making progress.
        failures = len(report.failed)
        if previous_failures is not None and failures >= previous_failures:
            stalls += 1
            if stalls >= 2:
                result.note(
                    f"two consecutive turns made no progress ({failures} patches still rejecting); "
                    "stopping rather than burning the remaining turn budget"
                )
                break
        else:
            stalls = 0
        previous_failures = failures

        turn += 1
        log(f"--- agent turn {turn}/{max_turns}: {failures} patch(es) rejecting ---")
        outcome = provider.run(
            build_prompt(report, version=args.target_version, previous=previous_version, turn=turn),
            cwd=REPO_ROOT,
            timeout=args.turn_timeout,
        )
        if not outcome.ok:
            result.note(f"agent turn {turn} exited non-zero; continuing to re-check the tree anyway")

        confinement = enforce_paths(cfg, source_dir_name=source_dir_name)
        if confinement["forbidden"]:
            result.note(
                "TAINTED: the agent wrote to paths it must never touch -- "
                + ", ".join(confinement["forbidden"])
                + ". Those paths hold the gates and the baseline, so this run is void."
            )
            result.metrics["tainted"] = True
            result.metrics["forbidden_writes"] = confinement["forbidden"]
            result.finish(evidence.ERROR).save(args.evidence_dir)
            return 2
        if confinement["reverted"]:
            result.note(f"reverted {len(confinement['reverted'])} out-of-scope change(s)")

    result.metrics["agent_turns_used"] = turn

    # The verdict is the repository's own patcher on a clean tree, not our
    # diagnostic applier -- if the two ever disagree, the build is what matters.
    if report.clean:
        log("re-applying with scripts/patch.py for the authoritative check")
        authoritative = patchset.verify_with_repo_patcher(args.target_version, release)
        if authoritative:
            # `make build` re-runs `make dir` -- a full reset and re-patch --
            # unless this marker exists. scripts/patch.py leaves it to the
            # Makefile to create, so create it here or the build pays for the
            # whole patch cycle a second time.
            (tree / "_READY").touch()
            result.note(
                f"all {len(report.applied)} patches apply cleanly to Firefox {args.target_version}"
                + (f" after {turn} agent turn(s)" if turn else " with no repair needed")
            )
            result.finish(evidence.PASS).save(args.evidence_dir)
            return 0
        result.note(
            "the diagnostic applier saw a clean tree but scripts/patch.py did not. "
            "Trust scripts/patch.py: it is what the build runs."
        )
        result.finish(evidence.FAIL).save(args.evidence_dir)
        return 1

    for name in sorted(report.failed):
        result.record(f"patches/{name}", evidence.FAIL)
    for name in report.applied:
        result.record(f"patches/{name}", evidence.PASS)
    result.finish(evidence.FAIL).save(args.evidence_dir)
    return 1


if __name__ == "__main__":
    sys.exit(main())
