import json

import pytest

from src.sundial import (
    SundialError,
    automated_url,
    parse_report,
    redact_key,
    redact_network,
    scan_page,
)


def test_automated_url_carries_key_and_raw_mode():
    url = automated_url('abc123', 'https://sundial.example/')
    assert url.startswith('https://sundial.example/automated?')
    assert 'key=abc123' in url
    # Without mode=raw the page renders the virtualized viewer and #report-raw
    # never gets the full document.
    assert 'mode=raw' in url


def test_automated_url_requires_a_key():
    with pytest.raises(SundialError, match='automation key'):
        automated_url('')


def test_redact_key_scrubs_the_secret():
    assert redact_key('failed with key=s3cret', 's3cret') == 'failed with key=***'
    assert redact_key('no key here', '') == 'no key here'


REPORT = {
    'sundialVersion': 'v0.4.2',
    'timestamp': '2026-08-20T00:00:00.000Z',
    'dashboard': {
        'sections': [
            {
                'id': 'identity',
                'label': 'Identity',
                'tally': {'pass': 40, 'fail': 2, 'error': 0, 'pending': 1, 'skipped': 3, 'total': 46},
            },
            {
                'id': 'security',
                'label': 'Security',
                'tally': {'pass': 12, 'fail': 0, 'error': 0, 'pending': 0, 'skipped': 0, 'total': 12},
            },
        ],
        'network': {'ip': '4.3.2.1', 'city': 'Des Moines', 'asnOrganization': 'Microsoft', 'rtt': 12},
    },
    'summary': {
        'totalTests': 58,
        'successfulTests': 52,
        'failedTests': 2,
        'errorTests': 0,
        'pendingTests': 1,
        'skippedTests': 3,
        'privateTests': 9,
    },
    'failures': [
        {
            'test': {
                'name': 'navigator.platform vs UA',
                'description': 'claimed platform disagrees',
                'category': 'Identity',
                'value': 'Linux x86_64',
            },
            'status': 'fail',
        }
    ],
}


def test_parse_report_projects_sections_and_summary():
    result = parse_report(REPORT, 'windows')
    assert result.profile == 'windows'
    assert result.sundial_version == 'v0.4.2'
    assert result.total_tests == 58
    assert result.failed_tests == 2
    assert result.private_tests == 9
    assert [s.label for s in result.sections] == ['Identity', 'Security']
    assert result.failures[0]['name'] == 'navigator.platform vs UA'
    assert not result.clean


def test_section_pct_ignores_pending_and_skipped():
    identity, security = parse_report(REPORT, 'windows').sections
    # 40 passed of 42 decided -- the 1 pending and 3 skipped are not counted
    # against the build.
    assert identity.pct == 95.2
    assert not identity.clean
    assert security.pct == 100.0
    assert security.clean


def test_section_pct_is_100_when_nothing_was_decided():
    report = {'dashboard': {'sections': [
        {'id': 'audio', 'label': 'Audio',
         'tally': {'pass': 0, 'fail': 0, 'error': 0, 'pending': 4, 'skipped': 0, 'total': 4}}
    ]}, 'summary': {}}
    (audio,) = parse_report(report, 'linux').sections
    assert audio.pct == 100.0
    assert audio.clean


def test_parse_report_tolerates_a_minimal_envelope():
    result = parse_report({}, 'linux')
    assert result.sections == []
    assert result.total_tests == 0
    assert result.clean


def test_parse_report_rejects_a_non_object():
    with pytest.raises(SundialError):
        parse_report([], 'linux')  # type: ignore[arg-type]


def test_redact_network_drops_runner_identity_but_keeps_the_rest():
    cleaned = redact_network(REPORT)
    network = cleaned['dashboard']['network']
    assert network['ip'] == '[redacted]'
    assert network['city'] == '[redacted]'
    assert network['asnOrganization'] == '[redacted]'
    assert network['rtt'] == 12
    # The original must not be mutated -- the scan result is reused for parsing.
    assert REPORT['dashboard']['network']['ip'] == '4.3.2.1'
    # Sections are untouched, so the published tallies still mean something.
    assert cleaned['dashboard']['sections'] == REPORT['dashboard']['sections']


class FakePage:
    def __init__(self, *, status=200, state='ready', raw=None, error_text=''):
        self._status = status
        self._state = state
        self._raw = json.dumps(REPORT) if raw is None else raw
        self._error_text = error_text
        self.visited = None

    def goto(self, url, **_):
        self.visited = url
        return type('Response', (), {'status': self._status})()

    def wait_for_selector(self, selector, **_):
        assert '[data-sundial-state="ready"]' in selector
        return object()

    def query_selector(self, selector):
        if selector == '[data-sundial-state="error"]':
            return object() if self._state == 'error' else None
        return object()

    def text_content(self, selector, **_):
        if selector == '[data-sundial-state="error"]':
            return self._error_text
        return self._raw


def test_scan_page_returns_the_parsed_report():
    page = FakePage()
    assert scan_page(page, 'k') == REPORT
    assert 'key=k' in page.visited


def test_scan_page_explains_a_401_as_a_key_problem():
    with pytest.raises(SundialError, match='invalid automation key'):
        scan_page(FakePage(status=401), 'k')


def test_scan_page_reports_a_client_side_scan_error():
    page = FakePage(state='error', error_text='scan_failed: WebGL unavailable')
    with pytest.raises(SundialError, match='WebGL unavailable'):
        scan_page(page, 'k')


def test_scan_page_keeps_the_key_out_of_the_error_text():
    page = FakePage(state='error', error_text='boom while fetching key=s3cret')
    with pytest.raises(SundialError) as excinfo:
        scan_page(page, 's3cret')
    assert 's3cret' not in str(excinfo.value)


def test_scan_page_flags_an_empty_raw_node():
    with pytest.raises(SundialError, match='mode=raw'):
        scan_page(FakePage(raw='   '), 'k')


def test_scan_page_flags_unparseable_json():
    with pytest.raises(SundialError, match='parse'):
        scan_page(FakePage(raw='{not json'), 'k')
