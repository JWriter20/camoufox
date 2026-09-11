"""Apply the patch stack and report, in detail, what did not go on.

`scripts/patch.py` is the real patcher and stays authoritative -- the gate is
that script exiting 0 on a clean tree. But it deletes every `.rej` file after
reading it, which is right for a build and useless for a repair loop: the reject
hunks are precisely what an agent needs to see.

So this module applies the same patches in the same order and keeps the rejects.
Diagnosis happens here; the verdict still comes from `scripts/patch.py`.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from ._util import REPO_ROOT, log, run

PATCHES_DIR = REPO_ROOT / "patches"


@dataclass
class Reject:
    patch: str
    target: str
    hunks: str

    def summary(self, max_chars: int = 4000) -> str:
        body = self.hunks if len(self.hunks) <= max_chars else self.hunks[:max_chars] + "\n... (truncated)"
        return f"--- reject: {self.target} (from {self.patch}) ---\n{body}"


@dataclass
class ApplyReport:
    applied: List[str] = field(default_factory=list)
    failed: Dict[str, List[Reject]] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return not self.failed

    def brief(self) -> str:
        if self.clean:
            return f"all {len(self.applied)} patches applied cleanly"
        total = sum(len(r) for r in self.failed.values())
        return (
            f"{len(self.failed)} of {len(self.applied) + len(self.failed)} patches rejected "
            f"({total} reject hunk file(s)): " + ", ".join(sorted(self.failed))
        )


def list_patches() -> List[Path]:
    """Same set and order as scripts/patch.py: roverfox patches go last."""
    every = sorted(PATCHES_DIR.rglob("*.patch"), key=lambda p: p.name)
    roverfox = [p for p in every if "roverfox" in p.parts]
    return [p for p in every if p not in roverfox] + roverfox


def _collect_rejects(tree: Path, since: float, patch_name: str) -> List[Reject]:
    """Find, read and remove the .rej files this patch just produced."""
    out: List[Reject] = []
    for path in tree.rglob("*.rej"):
        try:
            if path.stat().st_mtime < since:
                continue
            out.append(
                Reject(
                    patch=patch_name,
                    target=str(path.relative_to(tree)).removesuffix(".rej"),
                    hunks=path.read_text(encoding="utf-8", errors="replace"),
                )
            )
            # Removed so the next patch's rejects are unambiguous, exactly as
            # scripts/patch.py does.
            path.unlink()
        except OSError:
            continue
    return out


def apply_all(tree: Path, *, stop_after: Optional[int] = None) -> ApplyReport:
    """Apply every patch to `tree`, keeping the rejects. Does not reset first."""
    report = ApplyReport()
    for index, patch in enumerate(list_patches()):
        if stop_after is not None and index >= stop_after:
            break
        started = time.time() - 1  # tolerate coarse mtime granularity
        proc = run(
            ["patch", "-p1", "--forward", "-l", "--binary", "-i", str(patch)],
            cwd=tree,
            timeout=600,
        )
        rejects = _collect_rejects(tree, started, patch.name)
        if rejects:
            report.failed[patch.name] = rejects
            log(f"  ✗ {patch.name}: {len(rejects)} reject(s)", level="WARN")
        else:
            report.applied.append(patch.name)
            if proc.code not in (0, 1):
                log(f"  ? {patch.name}: patch exited {proc.code} with no rejects", level="WARN")
    return report


def reset_tree(tree: Path, version: str, release: str) -> None:
    """Back to pristine extracted Firefox, plus additions and settings.

    Mirrors what `scripts/patch.py` does at the top of a run. Never `git clean`
    here: the tree holds untracked files the build needs (see the upgrading
    guide's "DON'T use git reset or git clean").
    """
    if (tree / ".git").exists():
        run(["git", "reset", "--hard", "unpatched"], cwd=tree, check=True)
    run(["bash", str(REPO_ROOT / "scripts" / "copy-additions.sh"), version, release],
        cwd=tree, check=True)


def verify_with_repo_patcher(version: str, release: str) -> bool:
    """The authoritative check: the repository's own patcher, exit code 0."""
    proc = run(
        ["python3", "scripts/patch.py", version, release],
        cwd=REPO_ROOT,
        timeout=3600,
        tee=True,
        capture=False,
    )
    return proc.ok
