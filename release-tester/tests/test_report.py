import json

from src.playwright_suite import SuiteResult
from src.report import build_results, build_summary, overall_ok, write_reports
from src.sundial import ProfileResult, SectionResult


def suite(**overrides):
    base = dict(total=100, passed=90, failed=0, errors=0, skipped=10, duration_sec=300.0)
    base.update(overrides)
    return SuiteResult(**base)


def profile(name, failed=0, sections=None, failures=None):
    return ProfileResult(
        profile=name,
        sundial_version='v0.4.2',
        timestamp='2026-08-20T00:00:00Z',
        sections=sections or [SectionResult(id='identity', label='Identity', passed=10, failed=failed, total=10 + failed)],
        failures=failures or [],
        total_tests=10 + failed,
        failed_tests=failed,
    )


def results(**overrides):
    base = dict(
        target='linux', arch='x86_64', version='152.0.4', release='beta.29',
        suite=suite(), profiles=[profile('windows'), profile('macos')],
    )
    base.update(overrides)
    return build_results(**base)


def test_summary_titles_the_build():
    text = build_summary(results())
    assert '## linux/x86_64 -- camoufox 152.0.4-beta.29' in text


def test_summary_scores_playwright_against_executed_not_total():
    # 90 of the 90 that ran; the 10 deliberate skips are named, not scored.
    text = build_summary(results())
    assert '90/90 passed' in text
    assert 'skipped as unsupported by design' in text


def test_summary_puts_each_emulated_os_in_its_own_column():
    text = build_summary(results())
    assert '| Category | windows | macos |' in text
    assert '| Identity |' in text


def test_summary_lists_leaks_per_profile():
    leak = {'name': 'navigator.oscpu', 'category': 'Identity', 'description': 'leaks host', 'status': 'fail'}
    text = build_summary(results(profiles=[profile('windows', failed=1, failures=[leak])]))
    assert 'Leaks detected emulating windows (1)' in text
    assert 'navigator.oscpu' in text
    assert '[Identity] ' in text


def test_summary_reports_a_skipped_scan_rather_than_claiming_success():
    text = build_summary(results(profiles=[], sundial_error='SUNDIAL_AUTOMATION_KEY is not set, so the stealth scan was skipped.'))
    assert 'SUNDIAL_AUTOMATION_KEY is not set' in text
    assert '| Category |' not in text


def test_summary_lists_failing_playwright_cases():
    failing = suite(failed=2, passed=88, failures=[
        {'name': 'async.test_click::test_svg', 'kind': 'failure', 'message': 'expected 1'},
    ])
    text = build_summary(results(suite=failing))
    assert 'async.test_click::test_svg' in text
    assert '88/90 passed' in text


def test_overall_ok_is_true_only_when_everything_measured_was_clean():
    assert overall_ok(results()) is True
    assert overall_ok(results(suite=suite(failed=1, passed=89))) is False
    assert overall_ok(results(profiles=[profile('windows', failed=3)])) is False


def test_a_skipped_scan_does_not_fail_the_build():
    # No key configured is a pipeline gap, not a regression in the binary.
    assert overall_ok(results(profiles=[], sundial_error='no key')) is True


def test_write_reports_emits_both_artifacts(tmp_path):
    written = write_reports(results(), tmp_path)
    assert written['results'].is_file() and written['summary'].is_file()
    parsed = json.loads(written['results'].read_text())
    assert parsed['build']['release'] == 'beta.29'
    assert parsed['playwright']['passed'] == 90
    assert [p['profile'] for p in parsed['sundial']['profiles']] == ['windows', 'macos']
    # summary.md must survive the json round-trip that CI does between steps.
    assert build_summary(parsed) == written['summary'].read_text()


def test_summary_handles_a_playwright_only_run():
    text = build_summary(results(suite=None, profiles=[], sundial_error=None))
    assert 'Not run.' in text


def test_combine_builds_one_document_per_target(tmp_path):
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import combine as combine_mod

    for target, arch, failed in (('linux', 'x86_64', 0), ('windows', 'x86_64', 2)):
        directory = tmp_path / f'{target}-{arch}'
        directory.mkdir()
        write_reports(results(target=target, arch=arch, profiles=[profile('windows', failed=failed)]), directory)

    found = combine_mod.find_results(tmp_path)
    assert len(found) == 2
    text = combine_mod.combine(found)
    assert '| linux/x86_64 | :white_check_mark: |' in text
    assert '| windows/x86_64 | :x: |' in text
    # One `## <target>/<arch>` heading per build (### subheadings also
    # contain '## ', so anchor the count to the line start).
    assert sum(line.startswith('## ') for line in text.splitlines()) == 2


def test_combine_says_so_when_nothing_ran():
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import combine as combine_mod

    assert 'No results were produced' in combine_mod.combine([])


def _pins(text):
    import re

    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        match = re.match(r'^([A-Za-z0-9_.\-]+)\s*(.*)$', line)
        out[match.group(1).lower().replace('-', '_')] = match.group(2)
    return out


def test_ci_requirements_do_not_drift_from_the_developer_set():
    # CI installs the lean file; a contributor bumping a pin in
    # local-requirements.txt without the other would have CI testing against
    # different versions than anyone runs locally.
    from pathlib import Path

    tests_dir = Path(__file__).resolve().parents[2] / 'browser' / 'tests'
    local = _pins((tests_dir / 'local-requirements.txt').read_text())
    ci = _pins((tests_dir / 'ci-requirements.txt').read_text())

    unknown = sorted(name for name in ci if name not in local)
    assert not unknown, f'ci-requirements pins packages local-requirements does not: {unknown}'

    drift = {name: (local[name], spec) for name, spec in ci.items() if local[name] != spec}
    assert not drift, f'version drift between the two requirement files: {drift}'


def test_stage_assets_gives_every_build_a_unique_filename(tmp_path):
    # Release assets are flat: without renaming, three builds' results.json
    # collide and two silently vanish from the release.
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import combine as combine_mod

    root = tmp_path / 'artifacts'
    for target, arch in (('linux', 'x86_64'), ('macos', 'arm64'), ('windows', 'x86_64')):
        directory = root / f'CamoufoxResults-{target}-{arch}'
        directory.mkdir(parents=True)
        write_reports(results(target=target, arch=arch), directory)

    staged = combine_mod.stage_assets(combine_mod.find_results(root), tmp_path / 'assets')
    names = sorted(path.name for path in staged)
    assert names == [
        'verification-linux.x86_64.json',
        'verification-linux.x86_64.md',
        'verification-macos.arm64.json',
        'verification-macos.arm64.md',
        'verification-windows.x86_64.json',
        'verification-windows.x86_64.md',
    ]
    assert len(names) == len(set(names))
    # Content must survive the copy, not just the name.
    assert json.loads((tmp_path / 'assets' / 'verification-macos.arm64.json').read_text())['build']['arch'] == 'arm64'


def test_stage_assets_tolerates_a_results_set_with_no_summary(tmp_path):
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import combine as combine_mod

    directory = tmp_path / 'artifacts' / 'only-json'
    directory.mkdir(parents=True)
    write_reports(results(), directory)
    (directory / 'summary.md').unlink()

    staged = combine_mod.stage_assets(combine_mod.find_results(tmp_path / 'artifacts'), tmp_path / 'assets')
    assert [path.name for path in staged] == ['verification-linux.x86_64.json']
