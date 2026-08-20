import sys

import pytest

from src.playwright_suite import build_command, parse_junit

JUNIT = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="pytest" errors="1" failures="2" skipped="5" tests="20" time="123.5">
    <testcase classname="async.test_click" name="test_click_button" time="0.4"/>
    <testcase classname="async.test_click" name="test_click_svg" time="0.2">
      <failure message="expected 1, got 0">assert 1 == 0</failure>
    </testcase>
    <testcase classname="async.test_input" name="test_type" time="0.1">
      <failure message="timeout">Timeout 30000ms exceeded</failure>
    </testcase>
    <testcase classname="async.test_frames" name="test_frame_nav" time="0.0">
      <error message="browser closed">browser closed unexpectedly</error>
    </testcase>
    <testcase classname="async.test_input" name="test_paste" time="0.0">
      <skipped message="Not supported by Camoufox"/>
    </testcase>
  </testsuite>
</testsuites>
"""


@pytest.fixture
def junit_file(tmp_path):
    path = tmp_path / 'junit.xml'
    path.write_text(JUNIT, encoding='utf-8')
    return path


def test_parse_junit_reads_the_suite_totals(junit_file):
    result = parse_junit(junit_file)
    assert result.total == 20
    assert result.failed == 2
    assert result.errors == 1
    assert result.skipped == 5
    # 20 total, minus 2 failed, 1 errored, 5 skipped.
    assert result.passed == 12
    assert result.duration_sec == 123.5
    assert not result.clean


def test_executed_excludes_the_deliberate_skips(junit_file):
    # The skips are the cases our patches break on purpose; scoring against
    # them would make the suite look worse the more we harden the browser.
    assert parse_junit(junit_file).executed == 15


def test_parse_junit_separates_failures_from_errors(junit_file):
    failures = parse_junit(junit_file).failures
    kinds = {failure['name']: failure['kind'] for failure in failures}
    assert kinds['async.test_click::test_click_svg'] == 'failure'
    assert kinds['async.test_frames::test_frame_nav'] == 'error'
    assert len(failures) == 3


def test_parse_junit_handles_a_bare_testsuite_root(tmp_path):
    path = tmp_path / 'junit.xml'
    path.write_text(
        '<testsuite tests="3" failures="0" errors="0" skipped="1" time="1.0">'
        '<testcase classname="a" name="b"/></testsuite>',
        encoding='utf-8',
    )
    result = parse_junit(path)
    assert result.total == 3
    assert result.passed == 2
    assert result.clean


def test_build_command_runs_headless_by_default(tmp_path):
    command = build_command(tmp_path, tmp_path / 'j.xml')
    assert command[0] == sys.executable
    assert '--headless' in command
    assert f"--junitxml={tmp_path / 'j.xml'}" in command


def test_build_command_drops_headless_when_headful(tmp_path):
    assert '--headless' not in build_command(tmp_path, tmp_path / 'j.xml', headful=True)


def test_a_missing_junit_becomes_a_reported_error_not_an_exception(tmp_path, monkeypatch):
    # Publishing numbers is the job; a suite that never started must still
    # produce a result object so the stealth scan and the report survive.
    import subprocess as sp

    from src import playwright_suite

    monkeypatch.setattr(
        playwright_suite.subprocess, 'run',
        lambda *a, **k: sp.CompletedProcess(a[0] if a else [], 4),
    )
    result = playwright_suite.run_suite(tmp_path, tmp_path / 'bin', tmp_path / 'junit.xml')
    assert result.errors == 1
    assert result.exit_code == 4
    assert not result.clean
    assert 'did not start' in result.failures[0]['message']


def test_a_suite_timeout_is_recorded_against_partial_results(tmp_path, monkeypatch):
    import subprocess as sp

    from src import playwright_suite

    junit = tmp_path / 'junit.xml'

    def fake_run(*args, **kwargs):
        junit.write_text(JUNIT, encoding='utf-8')
        raise sp.TimeoutExpired(cmd='pytest', timeout=kwargs.get('timeout', 1))

    monkeypatch.setattr(playwright_suite.subprocess, 'run', fake_run)
    result = playwright_suite.run_suite(tmp_path, tmp_path / 'bin', junit, timeout_sec=7)
    # The XML pytest flushed is kept, with the timeout added on top.
    assert result.total == 21
    assert result.errors == 2
    assert any('budget' in f['message'] for f in result.failures)


def test_a_timeout_with_no_xml_at_all_still_returns_a_result(tmp_path, monkeypatch):
    import subprocess as sp

    from src import playwright_suite

    monkeypatch.setattr(
        playwright_suite.subprocess, 'run',
        lambda *a, **k: (_ for _ in ()).throw(sp.TimeoutExpired(cmd='pytest', timeout=5)),
    )
    result = playwright_suite.run_suite(tmp_path, tmp_path / 'bin', tmp_path / 'junit.xml')
    assert result.errors == 1
    assert 'budget' in result.failures[0]['message']


def test_run_suite_points_the_browser_at_the_binary(tmp_path, monkeypatch):
    import subprocess as sp

    from src import playwright_suite

    captured = {}

    def fake_run(command, **kwargs):
        captured.update(kwargs)
        (tmp_path / 'junit.xml').write_text(JUNIT, encoding='utf-8')
        return sp.CompletedProcess(command, 0)

    monkeypatch.setattr(playwright_suite.subprocess, 'run', fake_run)
    playwright_suite.run_suite(tmp_path, tmp_path / 'camoufox-bin', tmp_path / 'junit.xml')
    assert captured['env']['CAMOUFOX_EXECUTABLE_PATH'] == str(tmp_path / 'camoufox-bin')
    assert captured['cwd'] == str(tmp_path)
