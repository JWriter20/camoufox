#!/usr/bin/env python3
"""Stealth gate: drive the private sundial suite and bring back numbers only.

Sundial is a private detection suite. Its value is that the vectors it probes
are not public, so **nothing identifying a vector may ever leave this module**:
not a name, not a description, not a measured value, not the test's source. This
repository is public, and an evidence file or a pull-request comment is
permanent. Everything downstream of `_redact()` is counts and opaque ids.

The opaque id is `HMAC(salt, vector_key)`. That is enough to notice "the vector
that passed last release is failing now", which is the only thing the gate
needs, and it is not enough to learn what the vector was.

Scope: only categories Camoufox actually claims to implement are gated
(`policy.yml: gates.sundial.gated_categories`). Cross-OS rendering parity, for
one, is measured and reported but never fails a build -- Camoufox does not claim
byte-identical emulation of another platform's rasterizer.

How it runs:
  1. Form-login to sundial, keep the `sundial_session` cookie.
  2. Start a loopback collector.
  3. Launch the built binary, seed the cookie, open `?auto=1&post=<collector>`.
     Sundial runs its scan on load and POSTs `window.fullReport` back.
  4. Redact, score, write evidence.

Run:
    python3 -m ci.run_sundial --binary /path/to/camoufox-bin
    python3 -m ci.run_sundial waive --key '<vector key>' --reason '...'
"""

from __future__ import annotations

import argparse
import asyncio
import http.server
import json
import os
import sys
import threading
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import results as evidence
from ._util import CI_DIR, RESULTS_DIR, WORK_DIR, log, opaque_id, run

CONFIG_PATH = CI_DIR / "sundial.yml"
COOKIE_NAME = "sundial_session"
DEFAULT_URL = "https://sundial.daijro.dev"

# Report fields that may describe a private vector. Dropped without exception.
_FORBIDDEN_FIELDS = (
    "name", "brief", "src", "source", "value", "expect", "requires",
    "key", "id", "cat", "elapsedMs", "entropy",
)


# ---------------------------------------------------------------------------
# auth
# ---------------------------------------------------------------------------


def login(base_url: str, username: str, password: str, *, timeout: int = 30) -> str:
    """Form-login and return the session cookie value.

    The endpoint answers 303 + Set-Cookie on success and 401 + the login page on
    failure, so a redirect handler would hide the result; handle it manually.
    """
    body = urllib.parse.urlencode({"username": username, "password": password}).encode()
    req = urllib.request.Request(
        base_url.rstrip("/") + "/__auth/login",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "camoufox-harness",
        },
    )

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *_args, **_kwargs):  # noqa: D102
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    try:
        resp = opener.open(req, timeout=timeout)
        status, headers = resp.status, resp.headers
    except urllib.error.HTTPError as exc:
        status, headers = exc.code, exc.headers
        if status == 401:
            raise RuntimeError(
                "sundial rejected the credentials. Check SUNDIAL_USERNAME and "
                "SUNDIAL_AUTOMATION_KEY."
            ) from None
        if status == 429:
            raise RuntimeError("sundial rate-limited the login; try again in a few minutes.") from None
        if status != 303:
            raise RuntimeError(f"sundial login returned {status}") from None

    for raw in headers.get_all("Set-Cookie") or []:
        if raw.startswith(COOKIE_NAME + "="):
            value = raw.split(";", 1)[0][len(COOKIE_NAME) + 1 :]
            if value:
                log("sundial login OK")
                return value
    raise RuntimeError("sundial login succeeded but returned no session cookie")


# ---------------------------------------------------------------------------
# collector
# ---------------------------------------------------------------------------


class _Collector:
    """Loopback endpoint that receives the one POST sundial makes."""

    def __init__(self) -> None:
        self.payload: Optional[dict] = None
        self._event = threading.Event()
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length", "0"))
                raw = self.rfile.read(length).decode("utf-8", "replace")
                try:
                    outer.payload = json.loads(raw)
                except json.JSONDecodeError:
                    outer.payload = {"_parse_error": raw[:500]}
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                outer._event.set()

            def do_OPTIONS(self) -> None:  # noqa: N802
                self.send_response(204)
                self.send_header("Access-Control-Allow-Origin", "*")
                self.send_header("Access-Control-Allow-Headers", "*")
                self.end_headers()

            def log_message(self, *_args) -> None:  # keep the run log readable
                return

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def __enter__(self) -> "_Collector":
        self._thread.start()
        log(f"collector listening on 127.0.0.1:{self.port}")
        return self

    def __exit__(self, *_exc) -> None:
        self._server.shutdown()
        self._server.server_close()

    def wait(self, timeout: float) -> Optional[dict]:
        self._event.wait(timeout)
        return self.payload


# ---------------------------------------------------------------------------
# redaction
# ---------------------------------------------------------------------------


def _iter_entries(report: dict) -> List[Tuple[str, str, str]]:
    """(vector_key, category, status) for every vector, public and private."""
    out: List[Tuple[str, str, str]] = []
    buckets = ("failures", "succeeded", "pending", "skipped")
    for scope in (report, report.get("private") or {}):
        if not isinstance(scope, dict):
            continue
        for bucket in buckets:
            grouped = scope.get(bucket)
            if not isinstance(grouped, dict):
                continue
            for category, entries in grouped.items():
                for entry in entries or []:
                    if not isinstance(entry, dict):
                        continue
                    key = entry.get("key") or entry.get("id") or ""
                    if not key:
                        continue
                    out.append((str(key), str(category or "Uncategorised"), str(entry.get("status", "pending"))))
    return out


_STATUS_MAP = {
    "pass": evidence.PASS,
    "fail": evidence.FAIL,
    "error": evidence.ERROR,
    "skipped": evidence.SKIP,
    "pending": evidence.SKIP,
}


def grade(pass_rate: float) -> str:
    """A single letter, which is the only stealth number that goes public."""
    for floor, letter in ((0.99, "A+"), (0.97, "A"), (0.94, "B"), (0.90, "C"), (0.80, "D")):
        if pass_rate >= floor:
            return letter
    return "F"


def redact(report: dict, gated: List[str], ungated: List[str]) -> Dict[str, Any]:
    """Turn a full sundial report into a grade and a set of opaque ids.

    This is the trust boundary, and it is deliberately lossy.

    What survives: a letter grade, a pass count, and one HMAC per vector so a
    later run can notice "the vector that passed last time is failing now".

    What does not survive: names, descriptions, measured values, expectations,
    source, and -- since the second pass over this file -- the per-category
    breakdown. A table reading "Graphics 3/17" tells a reader which part of the
    fingerprint is weakest, which is the most useful single fact an adversary
    could take from a public CI log. Categories still decide what is gated;
    that decision happens in here and the answer stays in here.
    """
    entries = _iter_entries(report)
    gated_set = {c.lower() for c in gated}

    gated_tests: Dict[str, str] = {}
    ungated_tests: Dict[str, str] = {}

    for key, category, status in entries:
        outcome = _STATUS_MAP.get(status, evidence.SKIP)
        target = gated_tests if category.lower() in gated_set else ungated_tests
        target[opaque_id(key)] = outcome

    scored = [o for o in gated_tests.values() if o in (evidence.PASS, evidence.FAIL, evidence.ERROR)]
    passed = sum(1 for o in scored if o == evidence.PASS)
    pass_rate = round(passed / len(scored), 4) if scored else 0.0

    identity = report.get("identity") or {}
    return {
        "tests": gated_tests,
        "metrics": {
            # Our own browser's claimed identity. This describes Camoufox, not a
            # sundial vector, so it leaks nothing and makes a bad run diagnosable.
            "identity": {
                k: identity.get(k)
                for k in ("name", "os", "osDetected", "engine", "platform", "lang", "tz",
                          "uaMismatch", "engineMismatch", "osConsistent")
                if k in identity
            },
            "sundial_version": report.get("sundialVersion"),
            "schema_version": report.get("schemaVersion"),
            "grade": grade(pass_rate),
            "gated_total": len(gated_tests),
            "gated_scored": len(scored),
            "gated_passed": passed,
            "pass_rate": pass_rate,
            "ungated_total": len(ungated_tests),
            "ungated_failed": sum(
                1 for o in ungated_tests.values() if o in (evidence.FAIL, evidence.ERROR)
            ),
            "ungated_tests": ungated_tests,
        },
    }


# ---------------------------------------------------------------------------
# the scan
# ---------------------------------------------------------------------------


async def scan(
    *,
    binary: Path,
    base_url: str,
    cookie: str,
    os_name: str,
    headless: bool,
    timeout: float,
) -> dict:
    """Open sundial in the built browser and collect the report it posts back."""
    from camoufox.async_api import AsyncCamoufox

    host = urllib.parse.urlparse(base_url).hostname or "sundial.daijro.dev"

    with _Collector() as collector:
        target = (
            f"{base_url.rstrip('/')}/?auto=1&post="
            + urllib.parse.quote(f"http://127.0.0.1:{collector.port}/collect", safe="")
        )
        async with AsyncCamoufox(
            executable_path=str(binary),
            headless=headless,
            os=os_name,
            i_know_what_im_doing=True,
        ) as browser:
            context = await browser.new_context()
            await context.add_cookies(
                [{
                    "name": COOKIE_NAME,
                    "value": cookie,
                    "domain": host,
                    "path": "/",
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Strict",
                }]
            )
            page = await context.new_page()
            log(f"opening sundial (auto scan) as {os_name}")
            await page.goto(target, wait_until="load", timeout=120_000)

            payload = await asyncio.get_running_loop().run_in_executor(
                None, collector.wait, timeout
            )
            await context.close()

    if payload is None:
        raise TimeoutError(
            f"sundial did not post a report within {timeout:.0f}s. The usual cause is an "
            "expired session cookie (the page silently renders the login form instead of "
            "the suite), or the browser failing to reach the loopback collector."
        )
    if "_parse_error" in payload:
        raise RuntimeError("sundial posted something that was not JSON")
    return payload


def seal(report: dict, out: Path) -> Optional[Path]:
    """Optionally keep an encrypted copy of the *full* report for debugging.

    Only written when SUNDIAL_REPORT_AGE_RECIPIENT names an age public key, and
    only readable by whoever holds the matching private key. Without it the full
    report is discarded, because there is nowhere safe to put it: workflow
    artifacts on a public repository are world-readable.
    """
    recipient = os.environ.get("SUNDIAL_REPORT_AGE_RECIPIENT", "").strip()
    if not recipient:
        log("no SUNDIAL_REPORT_AGE_RECIPIENT set -- discarding the full report unencrypted-on-disk")
        return None
    if not run(["which", "age"]).ok:
        log("age is not installed; cannot seal the full report", level="WARN")
        return None
    out.parent.mkdir(parents=True, exist_ok=True)
    plain = out.with_suffix(".tmp.json")
    plain.write_text(json.dumps(report), encoding="utf-8")
    try:
        res = run(["age", "-r", recipient, "-o", str(out), str(plain)])
    finally:
        plain.unlink(missing_ok=True)
    if not res.ok:
        log(f"sealing failed: {res.combined()[-400:]}", level="WARN")
        return None
    log(f"sealed full report -> {out} (only the age key holder can read it)")
    return out


# ---------------------------------------------------------------------------


def _config() -> dict:
    """Scope and thresholds, from ci/sundial.yml."""
    import yaml

    with open(CONFIG_PATH, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def gate(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--binary", type=Path, help="camoufox-bin under test")
    parser.add_argument("--os", dest="os_name", default="linux", choices=["linux", "macos", "windows"])
    parser.add_argument("--headful", action="store_true")
    parser.add_argument("--timeout", type=float, default=300.0)
    parser.add_argument("--evidence-dir", type=Path, default=RESULTS_DIR)
    args = parser.parse_args(argv)

    cfg = _config()
    base_url = os.environ.get("SUNDIAL_URL") or cfg.get("url") or DEFAULT_URL
    result = evidence.GateResult(gate="sundial")

    username = os.environ.get("SUNDIAL_USERNAME", "").strip()
    password = os.environ.get("SUNDIAL_AUTOMATION_KEY", "").strip()
    if not username or not password:
        result.note(
            "SUNDIAL_USERNAME / SUNDIAL_AUTOMATION_KEY are not both set. The stealth gate is "
            "required by policy, so a missing credential fails the run rather than skipping it."
        )
        result.finish(evidence.ERROR).save(args.evidence_dir)
        return 1

    from ._pytest import require_binary

    try:
        binary = args.binary or require_binary()
        cookie = login(base_url, username, password)
        report = asyncio.run(
            scan(
                binary=binary,
                base_url=base_url,
                cookie=cookie,
                os_name=args.os_name,
                headless=not args.headful,
                timeout=args.timeout,
            )
        )
    except Exception as exc:  # noqa: BLE001 -- any failure here is a gate failure
        result.note(f"{type(exc).__name__}: {exc}")
        result.finish(evidence.ERROR).save(args.evidence_dir)
        return 1

    sealed = seal(report, WORK_DIR / "sundial-full-report.age")
    # The plaintext report is dropped here and never referenced again.
    redacted = redact(report, cfg.get("gated_categories") or [], cfg.get("ungated_categories") or [])
    del report

    result.tests = redacted["tests"]
    result.metrics = redacted["metrics"]
    if sealed:
        result.artifacts.append(sealed.name)

    metrics = result.metrics
    # This string reaches the job summary and the pull request. Grade and counts
    # only -- no category, no vector, no value.
    result.note(
        f"grade {metrics['grade']} -- {metrics['gated_passed']}/{metrics['gated_scored']} "
        f"in-scope checks passed ({metrics['pass_rate'] * 100:.1f}%). "
        f"{metrics['ungated_failed']} out-of-scope check(s) failed; those are measured but "
        "not gated, because Camoufox does not claim them."
    )

    floor = float(cfg.get("min_pass_rate", 0) or 0)
    status = evidence.PASS
    # Absolute rules, independent of the baseline: a run that scores badly fails
    # even if the previous release scored just as badly.
    violations: List[str] = []
    if metrics["gated_scored"] == 0:
        result.note("no gated vectors were scored -- treating as a failure, not a pass")
        violations.append(
            "no gated vectors were scored; the scan produced a report with nothing in scope"
        )
        status = evidence.FAIL
    elif metrics["pass_rate"] < floor:
        result.note(f"pass rate {metrics['pass_rate']:.3f} is below the policy floor {floor}")
        violations.append(
            f"stealth pass rate {metrics['pass_rate']:.3f} is below the policy floor {floor}"
        )
        status = evidence.FAIL
    result.metrics["policy_violations"] = violations

    result.finish(status).save(args.evidence_dir)
    # Per-test regressions are verify.py's job; this only reports the floor.
    return 0 if status == evidence.PASS else 1


def waive(argv: Optional[List[str]] = None) -> int:
    """Print a policy.yml waiver stanza for a vector, without naming it there."""
    parser = argparse.ArgumentParser(description="compute a waiver entry for a sundial vector")
    parser.add_argument("--key", required=True, help="the vector key, as it appears in the report")
    parser.add_argument("--reason", required=True, help="why Camoufox does not claim this")
    parser.add_argument("--days", type=int, default=90, help="waiver lifetime (default 90)")
    args = parser.parse_args(argv)

    print("\nAdd under gates.sundial.waivers in harness/policy.yml:\n")
    print(f"  - id: {opaque_id(args.key)}")
    print(f"    reason: {args.reason}")
    print(f"    expires: {date.today() + timedelta(days=args.days)}")
    print("\n(The key itself is deliberately not written to the file.)\n")
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "waive":
        return waive(argv[1:])
    return gate(argv)


if __name__ == "__main__":
    sys.exit(main())
