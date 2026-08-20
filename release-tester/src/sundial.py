"""
Drive sundial's `/automated` export route and return its report.

Contract (sundial >= v0.4.2, functions/_middleware.js + src/lib/export-page.js):

  GET /automated?key=<AUTOMATION_KEY>&mode=raw

`/automated` is matched *before* the cookie session check, so a username and
password will not open it -- the route authenticates on the `key` query
parameter alone and answers 401 without one. The key must equal either
AUTOMATION_GUEST_KEY or AUTOMATION_PRIVATE_KEY; the private role additionally
serves `vectors-private.js`, so a guest key silently yields fewer vectors.

The page then runs the scan client-side and marks its own terminal state:

  data-sundial-state  scanning -> finalizing -> ready | error
  #report-raw         full JSON.stringify(report, null, 2) once ready

`mode=raw` is what puts the JSON in `#report-raw`; without it the page boots
the collapsible viewer instead and the text is virtualized (and so truncated).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
from urllib.parse import urlencode

DEFAULT_BASE_URL = 'https://sundial.daijro.dev'
AUTOMATED_PATH = '/automated'

READY_SELECTOR = '[data-sundial-state="ready"]'
ERROR_SELECTOR = '[data-sundial-state="error"]'
RAW_SELECTOR = '#report-raw'

# The scan budget is sundial's own: testTimeoutMs is 60s per vector group and
# the extension probe runs 128-wide over a few thousand ids. Five minutes is
# comfortably above what a healthy run takes and still fails a hung one inside
# a job timeout.
DEFAULT_SCAN_TIMEOUT_MS = 300_000
DEFAULT_NAV_TIMEOUT_MS = 60_000

_STATUS_HINTS = {
    401: 'invalid automation key (does SUNDIAL_AUTOMATION_KEY match AUTOMATION_PRIVATE_KEY or AUTOMATION_GUEST_KEY?)',
    405: 'method not allowed -- /automated only answers GET',
    503: 'sundial has no automation keys configured',
}


class SundialError(RuntimeError):
    """The scan could not be completed. Distinct from a scan that found leaks."""


@dataclass
class SectionResult:
    """One sundial category (Identity, Security, Graphics, ...)."""

    id: str
    label: str
    passed: int = 0
    failed: int = 0
    errored: int = 0
    pending: int = 0
    skipped: int = 0
    total: int = 0

    @property
    def clean(self) -> bool:
        return self.failed == 0 and self.errored == 0

    @property
    def pct(self) -> float:
        """Share of *decided* checks that passed. Pending/skipped do not count."""
        decided = self.passed + self.failed + self.errored
        return 100.0 if decided == 0 else round(100.0 * self.passed / decided, 1)


@dataclass
class ProfileResult:
    """A single scan: one browser launch, one fingerprint, one sundial report."""

    profile: str
    sundial_version: str = ''
    timestamp: str = ''
    sections: List[SectionResult] = field(default_factory=list)
    failures: List[Dict[str, str]] = field(default_factory=list)
    total_tests: int = 0
    failed_tests: int = 0
    error_tests: int = 0
    private_tests: int = 0

    @property
    def clean(self) -> bool:
        return self.failed_tests == 0 and self.error_tests == 0


def automated_url(key: str, base_url: str = DEFAULT_BASE_URL) -> str:
    """
    Build the scan URL.

    `download` is deliberately left off: it fires a Blob anchor click, which
    only helps a driver that is capturing a download. Reading `#report-raw`
    gets the same bytes without a filesystem round-trip.
    """
    if not key:
        raise SundialError('an automation key is required; /automated 401s without one')
    return f"{base_url.rstrip('/')}{AUTOMATED_PATH}?{urlencode({'key': key, 'mode': 'raw'})}"


def redact_key(text: str, key: Optional[str]) -> str:
    """Keep the automation key out of logs, error text and published reports."""
    if not key:
        return text
    return text.replace(key, '***')


def parse_report(report: Dict[str, Any], profile: str) -> ProfileResult:
    """
    Project sundial's report envelope onto the per-category shape we publish.

    Envelope (src/lib/report-export.js buildReportEnvelope):
      {sundialVersion, timestamp, dashboard: {sections: [...]}, summary,
       failures, successes, pending, skipped}
    """
    if not isinstance(report, dict):
        raise SundialError(f'expected a JSON object report, got {type(report).__name__}')

    summary = report.get('summary') or {}
    result = ProfileResult(
        profile=profile,
        sundial_version=str(report.get('sundialVersion') or ''),
        timestamp=str(report.get('timestamp') or ''),
        total_tests=int(summary.get('totalTests') or 0),
        failed_tests=int(summary.get('failedTests') or 0),
        error_tests=int(summary.get('errorTests') or 0),
        private_tests=int(summary.get('privateTests') or 0),
    )

    for section in (report.get('dashboard') or {}).get('sections') or []:
        tally = section.get('tally') or {}
        result.sections.append(
            SectionResult(
                id=str(section.get('id') or ''),
                label=str(section.get('label') or section.get('id') or ''),
                passed=int(tally.get('pass') or 0),
                failed=int(tally.get('fail') or 0),
                errored=int(tally.get('error') or 0),
                pending=int(tally.get('pending') or 0),
                skipped=int(tally.get('skipped') or 0),
                total=int(tally.get('total') or 0),
            )
        )

    for entry in report.get('failures') or []:
        test = entry.get('test') or {}
        result.failures.append(
            {
                'name': str(test.get('name') or ''),
                'category': str(test.get('category') or ''),
                'description': str(test.get('description') or ''),
                'status': str(entry.get('status') or 'fail'),
            }
        )

    return result


# Fields under dashboard.network / dashboard.browserInfo that carry the
# *runner's* public IP and its geo-IP derivation. The scan has to see them --
# that is how sundial checks WebRTC and timezone coherence -- but a report
# published to a release page should not carry the address of the machine that
# produced it, so they are dropped on the way out unless asked for.
NETWORK_REDACTIONS = (
    'ip',
    'clientIp',
    'city',
    'region',
    'country',
    'postal',
    'latitude',
    'longitude',
    'asn',
    'asnOrganization',
    'organization',
)


def redact_network(report: Dict[str, Any]) -> Dict[str, Any]:
    """Strip runner-identifying network detail from a report about to be published."""

    def scrub(node: Any) -> Any:
        if isinstance(node, dict):
            return {
                key: ('[redacted]' if key in NETWORK_REDACTIONS and node[key] else scrub(value))
                for key, value in node.items()
            }
        if isinstance(node, list):
            return [scrub(item) for item in node]
        return node

    cleaned = dict(report)
    dashboard = cleaned.get('dashboard')
    if isinstance(dashboard, dict):
        cleaned['dashboard'] = {
            key: (scrub(value) if key in ('network', 'browserInfo', 'regional') else value)
            for key, value in dashboard.items()
        }
    return cleaned


def scan_page(
    page,
    key: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
    scan_timeout_ms: int = DEFAULT_SCAN_TIMEOUT_MS,
    nav_timeout_ms: int = DEFAULT_NAV_TIMEOUT_MS,
) -> Dict[str, Any]:
    """
    Run one scan on an already-open Playwright page and return the raw report.

    Takes a page rather than launching one so the caller owns the browser
    configuration -- which fingerprint, which emulated OS -- and so this stays
    testable against a fake page.
    """
    url = automated_url(key, base_url)

    response = page.goto(url, wait_until='domcontentloaded', timeout=nav_timeout_ms)
    status = getattr(response, 'status', None)
    if callable(status):  # sync API exposes .status as a property; be tolerant
        status = status()
    if status is not None and status >= 400:
        hint = _STATUS_HINTS.get(status, 'unexpected status')
        raise SundialError(f'sundial returned HTTP {status}: {hint}')

    # Either terminal state resolves the wait; distinguishing them afterwards
    # gives a real message instead of a bare selector timeout.
    page.wait_for_selector(f'{READY_SELECTOR}, {ERROR_SELECTOR}', timeout=scan_timeout_ms)

    if page.query_selector(ERROR_SELECTOR) is not None:
        detail = (page.text_content(ERROR_SELECTOR) or '').strip()
        raise SundialError(
            redact_key(f'sundial reported a scan error: {detail[:400]}', key)
        )

    raw = page.text_content(RAW_SELECTOR, timeout=nav_timeout_ms)
    if not raw or not raw.strip():
        raise SundialError(
            f'{RAW_SELECTOR} was empty -- was the URL built without mode=raw?'
        )

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SundialError(f'could not parse the sundial report as JSON: {exc}') from exc
