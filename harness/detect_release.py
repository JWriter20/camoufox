#!/usr/bin/env python3
"""Decide whether a new Firefox is worth chasing, and which Playwright suite proves it.

Two questions have to answer "yes" together before the harness spends an hour
building anything:

  1. Is there a Firefox release newer than the one `upstream.sh` pins?
  2. Has Playwright shipped a release that actually targets that Firefox, so
     there is a conformance suite to test the result against?

Question 2 is the one that is easy to get wrong. Playwright trails Firefox by a
few weeks, so "latest Firefox" and "Firefox that Playwright knows about" are
usually different numbers. Building against the former means the upstream suite
is testing a browser generation it has never seen, and every failure it reports
is ambiguous. The default `paired` mode therefore targets the newest Firefox
major that a released Playwright tag actually pins.

Run:
    python3 -m harness.detect_release [--mode paired|latest] [--force-version X]
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

from ._util import (
    REPO_ROOT,
    die,
    endgroup,
    group,
    http_json,
    log,
    major,
    parse_version,
    read_upstream_sh,
    set_output,
)

FIREFOX_ARCHIVE = "https://archive.mozilla.org/pub/firefox/releases/{v}/source/firefox-{v}.source.tar.xz"
BROWSERS_JSON = "https://raw.githubusercontent.com/{repo}/{ref}/packages/playwright-core/browsers.json"
TAGS_API = "https://api.github.com/repos/{repo}/tags?per_page=100"


def _gh_headers() -> Dict[str, str]:
    import os

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    return {"Authorization": f"Bearer {token}"} if token else {}


def playwright_tags(repo: str, limit: int = 40) -> List[str]:
    """Released playwright-python tags, newest first. Pre-releases dropped."""
    tags = http_json(TAGS_API.format(repo=repo), headers=_gh_headers())
    out: List[Tuple[Tuple[int, int, int], str]] = []
    for tag in tags:
        name = tag.get("name", "")
        if not name.startswith("v"):
            continue
        # A suite that upstream has not called final is not evidence.
        if any(m in name for m in ("alpha", "beta", "rc", "next", "-")):
            continue
        try:
            out.append((parse_version(name[1:]), name))
        except ValueError:
            continue
    out.sort(reverse=True)
    return [name for _, name in out[:limit]]


def firefox_pinned_by(tag: str, repo: str) -> Optional[str]:
    """The Firefox browserVersion a given Playwright tag ships against."""
    try:
        data = http_json(BROWSERS_JSON.format(repo=repo, ref=tag))
    except urllib.error.HTTPError:
        return None
    for browser in data.get("browsers", []):
        if browser.get("name") == "firefox":
            return browser.get("browserVersion")
    return None


def firefox_releases(url: str) -> Dict[str, dict]:
    """product-details firefox.json -> {version: {date, category, ...}}."""
    data = http_json(url)
    releases = data.get("releases", data)
    out: Dict[str, dict] = {}
    for key, meta in releases.items():
        version = meta.get("version") or key.replace("firefox-", "")
        cat = (meta.get("category") or "").lower()
        # Only shipped desktop releases. esr/dev/nightly are different trees.
        if cat and cat not in ("major", "stability", "dev"):
            continue
        if cat == "dev":
            continue
        out[version] = meta
    return out


def latest_point_release(releases: Dict[str, dict], want_major: int) -> Optional[str]:
    """Newest x.y.z within a Firefox major that Mozilla has actually shipped."""
    candidates = []
    for version in releases:
        try:
            parsed = parse_version(version)
        except ValueError:
            continue
        if parsed[0] == want_major:
            candidates.append((parsed, version))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def release_age_hours(releases: Dict[str, dict], version: str) -> Optional[float]:
    meta = releases.get(version) or {}
    date = meta.get("date")
    if not date:
        return None
    try:
        published = datetime.fromisoformat(date).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return (datetime.now(timezone.utc) - published).total_seconds() / 3600.0


def source_tarball_exists(version: str) -> bool:
    """The whole pipeline starts with `make fetch`; check the URL is really there."""
    import urllib.request

    url = FIREFOX_ARCHIVE.format(v=version)
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "camoufox-harness"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
            return resp.status == 200
    except Exception:
        return False


def decide(policy: dict, *, mode: Optional[str] = None, force_version: Optional[str] = None) -> dict:
    det = policy["release_detection"]
    mode = mode or det.get("mode", "paired")

    current = read_upstream_sh()
    current_version = current.get("version", "0")
    current_major = major(current_version)
    log(f"camoufox currently pins Firefox {current_version} (major {current_major})")

    group("upstream feeds")
    versions = http_json(det["firefox_versions_url"])
    ff_latest = versions.get("LATEST_FIREFOX_VERSION", "")
    log(f"mozilla LATEST_FIREFOX_VERSION = {ff_latest}")

    releases = firefox_releases(det["firefox_releases_url"])
    log(f"product-details knows {len(releases)} shipped releases")

    tags = playwright_tags(det["playwright_python_repo"])
    log(f"newest playwright-python tags: {', '.join(tags[:6])}")

    # Map each recent Playwright tag to the Firefox it pins. Walk newest-first
    # and stop early -- these are one HTTP request each.
    pinned: List[Tuple[str, str]] = []
    for tag in tags[:12]:
        ff = firefox_pinned_by(tag, det["playwright_repo"])
        if ff:
            pinned.append((tag, ff))
            log(f"  {tag} pins firefox {ff}")
    endgroup()

    if not pinned:
        return {"should_update": False, "reason": "could not resolve any Playwright -> Firefox pin"}

    result: dict = {
        "current_version": current_version,
        "current_release": current.get("release", ""),
        "firefox_latest": ff_latest,
        "mode": mode,
    }

    if force_version:
        target_version = force_version
        # Best suite for a forced target: newest tag pinning <= that major.
        suite = next(
            ((t, f) for t, f in pinned if major(f) <= major(target_version)), pinned[0]
        )
        result.update(playwright_tag=suite[0], playwright_firefox=suite[1])
        log(f"forced target {target_version}, suite {suite[0]}")
    elif mode == "latest":
        target_version = ff_latest
        result.update(playwright_tag=pinned[0][0], playwright_firefox=pinned[0][1])
    else:  # paired
        # Newest Playwright-known Firefox major that is ahead of what we ship
        # and not ahead of what Mozilla has actually released.
        best: Optional[Tuple[str, str]] = None
        for tag, ff in pinned:
            ff_major = major(ff)
            if ff_major <= current_major:
                continue
            if ff_latest and ff_major > major(ff_latest):
                continue
            if best is None or ff_major > major(best[1]):
                best = (tag, ff)
        if best is None:
            result.update(
                should_update=False,
                reason=(
                    f"no Playwright release pins a Firefox newer than {current_major}. "
                    f"Newest pin is {pinned[0][1]} from {pinned[0][0]}."
                ),
            )
            return result
        tag, ff = best
        resolved = latest_point_release(releases, major(ff))
        if not resolved:
            result.update(
                should_update=False,
                reason=f"Playwright {tag} pins Firefox {ff} but Mozilla has not shipped that major yet",
            )
            return result
        target_version = resolved
        result.update(playwright_tag=tag, playwright_firefox=ff)

    result["target_version"] = target_version

    # --- guards ------------------------------------------------------------
    jump = major(target_version) - current_major
    if jump <= 0 and not force_version:
        result.update(should_update=False, reason=f"already on Firefox {current_major}")
        return result

    max_jump = int(det.get("max_major_jump", 1))
    if jump > max_jump and not force_version:
        result.update(
            should_update=False,
            reason=(
                f"target Firefox {target_version} is {jump} majors ahead of {current_version}; "
                f"policy allows {max_jump}. Bump one major at a time, or dispatch with force_version."
            ),
        )
        return result

    min_age = float(det.get("min_release_age_hours", 0))
    age = release_age_hours(releases, target_version)
    if age is not None and age < min_age and not force_version:
        result.update(
            should_update=False,
            reason=f"Firefox {target_version} is only {age:.1f}h old; policy waits {min_age}h",
        )
        return result

    if not source_tarball_exists(target_version):
        result.update(
            should_update=False,
            reason=f"no source tarball published for Firefox {target_version} yet",
        )
        return result

    result.update(
        should_update=True,
        reason=(
            f"Firefox {target_version} is available and Playwright "
            f"{result['playwright_tag']} targets Firefox {result['playwright_firefox']}"
        ),
    )
    return result


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["paired", "latest"])
    parser.add_argument("--force-version", help="skip detection and target this Firefox version")
    parser.add_argument("--json", action="store_true", help="print the decision as JSON")
    args = parser.parse_args(argv)

    import yaml

    with open(REPO_ROOT / "harness" / "policy.yml", encoding="utf-8") as fh:
        policy = yaml.safe_load(fh)

    decision = decide(policy, mode=args.mode, force_version=args.force_version)

    if args.json:
        print(json.dumps(decision, indent=2, sort_keys=True))

    log(("UPDATE: " if decision.get("should_update") else "no update: ") + decision["reason"])

    for key in (
        "should_update",
        "target_version",
        "current_version",
        "current_release",
        "playwright_tag",
        "playwright_firefox",
        "reason",
    ):
        value = decision.get(key, "")
        set_output(key, str(value).lower() if isinstance(value, bool) else str(value))

    return 0


if __name__ == "__main__":
    sys.exit(main())
