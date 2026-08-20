"""
Merge the two suites into the artifacts a release publishes:
`results.json` (machine-readable) and `summary.md` (job summary + release).
"""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from .playwright_suite import SuiteResult
from .sundial import ProfileResult

PASS = ':white_check_mark:'
FAIL = ':x:'
SKIP = ':fast_forward:'


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def build_results(
    *,
    target: str,
    arch: str,
    version: str,
    release: str,
    suite: Optional[SuiteResult],
    profiles: List[ProfileResult],
    sundial_error: Optional[str] = None,
    generated_at: Optional[str] = None,
) -> Dict[str, Any]:
    return {
        'build': {
            'version': version,
            'release': release,
            'target': target,
            'arch': arch,
        },
        'generatedAt': generated_at or _now(),
        'playwright': None if suite is None else asdict(suite),
        'sundial': {
            'error': sundial_error,
            'profiles': [asdict(profile) for profile in profiles],
        },
    }


def _verdict(ok: bool) -> str:
    return PASS if ok else FAIL


def _playwright_section(suite: Optional[SuiteResult]) -> List[str]:
    if suite is None:
        return ['### Playwright functionality', '', f'{SKIP} Not run.', '']

    lines = [
        '### Playwright functionality',
        '',
        f'{_verdict(suite.clean)} **{suite.passed}/{suite.executed} passed** '
        f'({suite.failed} failed, {suite.errors} errored, {suite.skipped} skipped '
        f'as unsupported by design) in {suite.duration_sec:.0f}s',
        '',
    ]
    if suite.failures:
        lines += ['<details><summary>Failing cases</summary>', '']
        for failure in suite.failures[:50]:
            message = failure['message'].splitlines()[0] if failure['message'] else ''
            lines.append(f"- `{failure['name']}` ({failure['kind']}) {message}")
        if len(suite.failures) > 50:
            lines.append(f'- ...and {len(suite.failures) - 50} more')
        lines += ['', '</details>', '']
    return lines


def _sundial_section(
    profiles: List[ProfileResult], sundial_error: Optional[str]
) -> List[str]:
    lines = ['### Stealth (sundial)', '']

    if sundial_error:
        lines += [f'{SKIP} {sundial_error}', '']
        return lines
    if not profiles:
        lines += [f'{SKIP} No scans ran.', '']
        return lines

    # One column per emulated OS: reading across a row shows whether a category
    # leaks only when the build claims to be something it is not, which is
    # exactly the cross-OS emulation question.
    labels: List[str] = []
    for profile in profiles:
        for section in profile.sections:
            if section.label not in labels:
                labels.append(section.label)

    header = '| Category | ' + ' | '.join(p.profile for p in profiles) + ' |'
    divider = '| --- | ' + ' | '.join('---' for _ in profiles) + ' |'
    lines += [header, divider]

    for label in labels:
        cells = []
        for profile in profiles:
            match = next((s for s in profile.sections if s.label == label), None)
            if match is None:
                cells.append('-')
            else:
                mark = PASS if match.clean else FAIL
                cells.append(f'{mark} {match.passed}/{match.passed + match.failed + match.errored}')
        lines.append(f'| {label} | ' + ' | '.join(cells) + ' |')

    totals = []
    for profile in profiles:
        mark = PASS if profile.clean else FAIL
        totals.append(f'{mark} {profile.failed_tests + profile.error_tests} leaks')
    lines.append('| **Total** | ' + ' | '.join(totals) + ' |')
    lines.append('')

    for profile in profiles:
        if not profile.failures:
            continue
        lines += [
            f'<details><summary>Leaks detected emulating {profile.profile} '
            f'({len(profile.failures)})</summary>',
            '',
        ]
        for failure in profile.failures[:60]:
            category = f"[{failure['category']}] " if failure['category'] else ''
            description = f" -- {failure['description']}" if failure['description'] else ''
            lines.append(f"- {category}**{failure['name']}**{description}")
        if len(profile.failures) > 60:
            lines.append(f'- ...and {len(profile.failures) - 60} more')
        lines += ['', '</details>', '']

    versions = {p.sundial_version for p in profiles if p.sundial_version}
    if versions:
        lines += [f"<sub>sundial {', '.join(sorted(versions))}</sub>", '']
    return lines


def build_summary(results: Dict[str, Any]) -> str:
    build = results['build']
    suite = results['playwright']
    suite_obj = SuiteResult(**suite) if suite else None
    profiles = [ProfileResult(**profile) for profile in results['sundial']['profiles']]
    # asdict() flattened the nested dataclasses; rebuild only what the summary reads.
    for profile, raw in zip(profiles, results['sundial']['profiles']):
        from .sundial import SectionResult

        profile.sections = [SectionResult(**section) for section in raw['sections']]

    title = (
        f"## {build['target']}/{build['arch']} "
        f"-- camoufox {build['version']}-{build['release']}"
    )
    lines = [title, '']
    lines += _playwright_section(suite_obj)
    lines += _sundial_section(profiles, results['sundial'].get('error'))
    return '\n'.join(lines).rstrip() + '\n'


def write_reports(results: Dict[str, Any], out_dir: Path) -> Dict[str, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    results_path = out_dir / 'results.json'
    summary_path = out_dir / 'summary.md'
    results_path.write_text(json.dumps(results, indent=2) + '\n', encoding='utf-8')
    summary_path.write_text(build_summary(results), encoding='utf-8')
    return {'results': results_path, 'summary': summary_path}


def overall_ok(results: Dict[str, Any]) -> bool:
    """
    Did everything that ran come back clean?

    A sundial scan that could not run at all (no key configured) is not a
    failure of the build -- `sundial.error` is reported and the verdict rests
    on what was actually measured.
    """
    suite = results.get('playwright')
    if suite and (suite['failed'] or suite['errors']):
        return False
    for profile in results['sundial']['profiles']:
        if profile['failed_tests'] or profile['error_tests']:
            return False
    return True
