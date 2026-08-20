#!/usr/bin/env python3
"""
Verify one packaged Camoufox build and publish the result.

Two suites, both scoped to a single (target, arch) package:

  playwright  the upstream Playwright suite from browser/tests, minus the
              cases our patches deliberately break (which are counted, not
              hidden) -- does the build still drive?
  sundial     sundial's /automated scan, once per emulated OS -- what does
              the build leak, by category?

    python run.py --package-dir ./unpacked --target linux --arch x86_64 \
        --version 152.0.4 --release beta.29 --out ./results
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from pathlib import Path
from typing import List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from src.binary import BinaryNotFound, ensure_executable, find_binary  # noqa: E402
from src.playwright_suite import SuiteResult, run_suite  # noqa: E402
from src.report import build_results, overall_ok, write_reports  # noqa: E402
from src.sundial import (  # noqa: E402
    DEFAULT_BASE_URL,
    DEFAULT_SCAN_TIMEOUT_MS,
    ProfileResult,
    SundialError,
    parse_report,
    redact_key,
    redact_network,
    scan_page,
)

DEFAULT_PROFILES = ('windows', 'macos', 'linux')


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--package-dir', required=True, type=Path, help='Directory an extracted release package was unzipped into')
    parser.add_argument('--target', required=True, choices=['linux', 'macos', 'windows'])
    parser.add_argument('--arch', required=True)
    parser.add_argument('--version', default=os.environ.get('CAMOUFOX_VERSION', ''))
    parser.add_argument('--release', default=os.environ.get('CAMOUFOX_RELEASE', ''))
    parser.add_argument('--out', required=True, type=Path, help='Directory to write results.json + summary.md into')
    parser.add_argument('--suite', default='all', choices=['all', 'playwright', 'sundial'])
    parser.add_argument('--tests-dir', type=Path, default=REPO_ROOT / 'browser' / 'tests')
    parser.add_argument('--headful', action='store_true', help='Run the Playwright suite headful (needs a display)')
    parser.add_argument('--suite-timeout', type=int, default=5400, help='Wall-clock cap for the whole Playwright suite, seconds')
    parser.add_argument('--per-test-timeout', type=int, default=300, help='Cap for any single Playwright case, seconds')
    parser.add_argument(
        '--sundial-key',
        default=os.environ.get('SUNDIAL_AUTOMATION_KEY', ''),
        help='sundial AUTOMATION_PRIVATE_KEY or AUTOMATION_GUEST_KEY. Not a username/password: '
        '/automated authenticates on this alone. Skipped when unset.',
    )
    parser.add_argument('--sundial-url', default=os.environ.get('SUNDIAL_URL', DEFAULT_BASE_URL))
    parser.add_argument('--sundial-timeout-ms', type=int, default=DEFAULT_SCAN_TIMEOUT_MS)
    parser.add_argument(
        '--profiles',
        default=','.join(DEFAULT_PROFILES),
        help='Comma-separated OSes to emulate for the sundial scans (default: %(default)s)',
    )
    parser.add_argument('--no-geoip', action='store_true', help="Don't derive timezone/locale from the runner IP")
    parser.add_argument('--keep-network-detail', action='store_true', help='Keep the runner IP and geo-IP fields in the saved report')
    parser.add_argument('--fail-on-leaks', action='store_true', help='Exit non-zero when either suite comes back dirty')
    return parser.parse_args(argv)


def run_sundial_profiles(
    binary: Path,
    args: argparse.Namespace,
) -> tuple[List[ProfileResult], Optional[str], dict]:
    """Launch the build once per emulated OS and scan. Returns (results, error, raw reports)."""
    if not args.sundial_key:
        return [], 'SUNDIAL_AUTOMATION_KEY is not set, so the stealth scan was skipped.', {}

    # Imported here so the Playwright-only path does not require the launcher.
    from camoufox.sync_api import Camoufox

    profiles: List[ProfileResult] = []
    raw_reports = {}
    failures: List[str] = []

    for profile_os in [p.strip() for p in args.profiles.split(',') if p.strip()]:
        print(f'==> sundial scan, emulating {profile_os}', flush=True)
        try:
            with Camoufox(
                executable_path=str(binary),
                os=profile_os,
                headless=True,
                # sundial cross-checks the claimed timezone and locale against
                # the geo-IP of the connecting address. Without this the runner
                # would fail Locale/Network on its own configuration rather
                # than on anything the build did.
                geoip=not args.no_geoip,
            ) as browser:
                page = browser.new_page()
                report = scan_page(
                    page,
                    args.sundial_key,
                    base_url=args.sundial_url,
                    scan_timeout_ms=args.sundial_timeout_ms,
                )
            parsed = parse_report(report, profile_os)
            profiles.append(parsed)
            raw_reports[profile_os] = (
                report if args.keep_network_detail else redact_network(report)
            )
            print(
                f'    {parsed.total_tests} checks, '
                f'{parsed.failed_tests + parsed.error_tests} leaks',
                flush=True,
            )
        except SundialError as exc:
            failures.append(f'{profile_os}: {exc}')
            print(f'    scan failed: {exc}', file=sys.stderr, flush=True)
        except Exception as exc:  # noqa: BLE001 - one bad profile must not lose the rest
            detail = redact_key(f'{type(exc).__name__}: {exc}', args.sundial_key)
            failures.append(f'{profile_os}: {detail}')
            traceback.print_exc()

    error = None
    if failures and not profiles:
        error = 'every sundial scan failed -- ' + '; '.join(failures)
    elif failures:
        error = 'some sundial scans failed -- ' + '; '.join(failures)
    return profiles, error, raw_reports


def main(argv: Optional[List[str]] = None) -> int:
    args = parse_args(argv)

    try:
        binary = ensure_executable(find_binary(args.package_dir, args.target))
    except BinaryNotFound as exc:
        print(f'error: {exc}', file=sys.stderr)
        return 2
    print(f'==> testing {binary}', flush=True)

    suite: Optional[SuiteResult] = None
    if args.suite in ('all', 'playwright'):
        try:
            suite = run_suite(
                args.tests_dir,
                binary,
                args.out / 'junit.xml',
                headful=args.headful,
                timeout_sec=args.suite_timeout,
                per_test_timeout_sec=args.per_test_timeout,
            )
        except Exception as exc:  # noqa: BLE001
            # A harness failure here must not take the stealth scan down with
            # it; the run still has to publish what it managed to measure.
            traceback.print_exc()
            suite = SuiteResult(
                total=1,
                errors=1,
                failures=[{'name': 'run_suite', 'kind': 'error', 'message': f'{type(exc).__name__}: {exc}'}],
            )
        print(
            f'    playwright: {suite.passed}/{suite.executed} passed, '
            f'{suite.failed} failed, {suite.errors} errored, {suite.skipped} skipped',
            flush=True,
        )

    profiles: List[ProfileResult] = []
    sundial_error: Optional[str] = None
    raw_reports: dict = {}
    if args.suite in ('all', 'sundial'):
        profiles, sundial_error, raw_reports = run_sundial_profiles(binary, args)

    results = build_results(
        target=args.target,
        arch=args.arch,
        version=args.version,
        release=args.release,
        suite=suite,
        profiles=profiles,
        sundial_error=sundial_error,
    )
    written = write_reports(results, args.out)

    if raw_reports:
        import json

        raw_dir = args.out / 'sundial-raw'
        raw_dir.mkdir(parents=True, exist_ok=True)
        for profile_os, report in raw_reports.items():
            (raw_dir / f'{profile_os}.json').write_text(
                json.dumps(report, indent=2) + '\n', encoding='utf-8'
            )

    print(f"==> wrote {written['results']} and {written['summary']}", flush=True)

    if args.fail_on_leaks and not overall_ok(results):
        print('error: build did not come back clean', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
