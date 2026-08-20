#!/usr/bin/env python3
"""
Fold the per-target result directories into one document for the release body.

    python combine.py --results-root ./artifacts --out ./RESULTS.md
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from pathlib import Path
from typing import List

sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.report import build_summary, overall_ok  # noqa: E402


def find_results(root: Path) -> List[Path]:
    """Every results.json under `root`, ordered so the document is stable."""
    return sorted(root.rglob('results.json'))


def combine(paths: List[Path]) -> str:
    if not paths:
        return '# Build verification\n\nNo results were produced.\n'

    sections: List[str] = []
    verdicts: List[str] = []
    for path in paths:
        results = json.loads(path.read_text(encoding='utf-8'))
        build = results['build']
        name = f"{build['target']}/{build['arch']}"
        verdicts.append(f"| {name} | {':white_check_mark:' if overall_ok(results) else ':x:'} |")
        sections.append(build_summary(results))

    header = [
        '# Build verification',
        '',
        '| Build | Clean |',
        '| --- | --- |',
        *verdicts,
        '',
        '<sub>Playwright: the upstream suite from `browser/tests`, minus cases our '
        'patches break by design. Stealth: sundial `/automated`, scanned once per '
        'emulated OS. Runner IP and geo-IP fields are redacted from the saved reports.</sub>',
        '',
        '---',
        '',
    ]
    return '\n'.join(header) + '\n---\n\n'.join(sections)


def stage_assets(paths: List[Path], assets_dir: Path) -> List[Path]:
    """
    Copy each build's report out under a name unique to that build.

    A GitHub release's assets are a flat namespace, so attaching three
    directories that each contain `results.json` would have them collide and
    only one survive. Naming them for their build is what makes all three
    attachable.
    """
    assets_dir.mkdir(parents=True, exist_ok=True)
    staged: List[Path] = []
    for path in paths:
        build = json.loads(path.read_text(encoding='utf-8'))['build']
        stem = f"verification-{build['target']}.{build['arch']}"
        target = assets_dir / f'{stem}.json'
        shutil.copyfile(path, target)
        staged.append(target)

        summary = path.with_name('summary.md')
        if summary.is_file():
            target = assets_dir / f'{stem}.md'
            shutil.copyfile(summary, target)
            staged.append(target)
    return staged


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--results-root', required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument(
        '--assets-dir',
        type=Path,
        help='Also stage per-build reports here under collision-free names, for release upload',
    )
    args = parser.parse_args(argv)

    paths = find_results(args.results_root)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(combine(paths), encoding='utf-8')
    print(f'==> combined {len(paths)} result set(s) into {args.out}')

    if args.assets_dir:
        staged = stage_assets(paths, args.assets_dir)
        shutil.copyfile(args.out, args.assets_dir / args.out.name)
        print(f'==> staged {len(staged) + 1} release asset(s) in {args.assets_dir}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
