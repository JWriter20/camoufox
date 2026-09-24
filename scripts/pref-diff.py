#!/usr/bin/env python3
"""
Audit Camoufox's pref overrides against a stock Firefox release.

settings/camoufox.cfg sets ~375 prefs. A pref that changes page-observable
behaviour is a fingerprint, and one that already matches stock is dead weight.
Nothing until now said which was which.

    python3 scripts/pref-diff.py --stock /path/to/firefox

Reads every pref stock Firefox actually exposes (Marionette, chrome context),
then reports each camoufox.cfg override as:

  REDUNDANT  same value stock already has -- the line can go
  DEVIATION  differs from stock -- the deviation surface, page-observable first
  NEW        stock does not carry this pref at all
  LOCKED     lockPref(), so a caller cannot put it back

Scope: what camoufox.cfg sets. Prefs changed by policies.json, by a patch, or at
launch by pythonlib are not covered -- `--camoufox BIN` adds a live diff of a
built browser when Marionette is reachable on it, which is the wider check.

`--json out.json` writes the full result.
"""

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "tests" / "patches"))
from helpers import Marionette, free_port  # noqa: E402

CFG = REPO_ROOT / "settings" / "camoufox.cfg"

DUMP_JS = """
const out = {};
const p = Services.prefs;
for (const name of p.getChildList('')) {
  let v = null;
  try {
    switch (p.getPrefType(name)) {
      case p.PREF_BOOL:   v = p.getBoolPref(name); break;
      case p.PREF_INT:    v = p.getIntPref(name); break;
      case p.PREF_STRING: v = p.getStringPref(name); break;
      default: v = null;
    }
  } catch (e) { v = '<unreadable>'; }
  out[name] = v;
}
return out;
"""

# Prefixes a page can observe directly, or whose effect a page can measure. A
# deviation here is a leak until argued otherwise; the rest still needs a human,
# it is just lower priority.
PAGE_OBSERVABLE = (
    "dom.", "javascript.", "gfx.", "webgl.", "media.", "network.cookie.",
    "network.http.", "privacy.", "intl.", "font.", "browser.display.",
    "layout.", "ui.", "image.", "canvas.", "apz.", "general.", "widget.",
    "security.", "browser.zoom.", "devtools.", "accessibility.", "fission.",
    "browser.cache.", "browser.sessionhistory.", "print.", "svg.", "mathml.",
)

PREF_CALL = re.compile(
    r'^\s*(?P<kind>default[pP]ref|lock[pP]ref|sticky[pP]ref|clear[pP]ref|pref)\('
    r'\s*"(?P<name>[^"]+)"\s*(?:,\s*(?P<value>.+?))?\s*\)\s*;'
)


def parse_literal(raw: str):
    """A JS literal from camoufox.cfg as a Python value, or the raw text."""
    if raw is None:
        return None
    raw = raw.strip()
    if raw in ("true", "false"):
        return raw == "true"
    if re.fullmatch(r"-?\d+", raw):
        return int(raw)
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in "\"'":
        return raw[1:-1]
    return raw


def cfg_overrides() -> list:
    """Every pref camoufox.cfg sets: {name, value, kind, line}."""
    rows = []
    for n, line in enumerate(CFG.read_text(errors="replace").splitlines(), 1):
        if line.lstrip().startswith("//"):
            continue
        m = PREF_CALL.match(line)
        if m:
            rows.append({
                "pref": m.group("name"),
                "value": parse_literal(m.group("value")),
                "kind": m.group("kind"),
                "line": n,
            })
    return rows


def launcher_overrides() -> list:
    """Every pref the launcher sets for one default identity.

    camoufox.cfg is only half the pref surface a page meets: pythonlib adds its
    own at launch (CAMOU_PREFS plus Playwright's firefoxUserPrefs), and those
    are the ones that carry the identity. Values that come from a draw differ
    per launch -- what this is for is the NAMES, and whether each one deviates
    from stock at all.
    """
    sys.path.insert(0, str(REPO_ROOT / "pythonlib"))
    from camoufox.utils import launch_options  # noqa: PLC0415

    options = launch_options(i_know_what_im_doing=True, headless=True)
    prefs = options.get("firefox_user_prefs") or {}
    return [
        {"pref": name, "value": value, "kind": "launcher", "line": "launcher"}
        for name, value in sorted(prefs.items())
    ]


def dump_prefs(binary: Path, timeout: float = 90) -> dict:
    """Every pref this binary exposes, read in chrome context."""
    port = free_port()
    profile = tempfile.mkdtemp(prefix="prefdiff-")
    Path(profile, "user.js").write_text(
        f'user_pref("marionette.port", {port});\n'
        'user_pref("browser.shell.checkDefaultBrowser", false);\n'
        'user_pref("datareporting.policy.dataSubmissionPolicyBypassNotification", true);\n'
    )
    proc = subprocess.Popen(
        [str(binary), "-headless", "--marionette", "--remote-allow-system-access",
         "--no-remote", "--profile", profile, "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        client = Marionette(port, timeout=timeout)
        try:
            return client.js(DUMP_JS)
        finally:
            client.close()
    finally:
        proc.kill()
        proc.wait(timeout=10)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stock", required=True, type=Path, help="a stock Firefox binary")
    ap.add_argument("--camoufox", type=Path, help="optional: live diff of a built browser")
    ap.add_argument("--json", type=Path)
    ap.add_argument("--show", choices=["deviation", "redundant", "new", "all"],
                    default="deviation")
    ap.add_argument("--launcher", action="store_true",
                    help="also audit the prefs pythonlib sets at launch")
    args = ap.parse_args()

    overrides = cfg_overrides()
    print(f"camoufox.cfg : {len(overrides)} pref calls")
    if args.launcher:
        launcher = launcher_overrides()
        print(f"pythonlib    : {len(launcher)} prefs at launch (one default identity)")
        # The launcher is read last by the browser, so where both set a pref the
        # launcher's value is the one a page meets.
        cfg_only = [r for r in overrides if r["pref"] not in {l["pref"] for l in launcher}]
        overrides = cfg_only + launcher
    print(f"stock        : {args.stock}")
    stock = dump_prefs(args.stock)
    print(f"               {len(stock)} prefs readable\n")

    rows = []
    for row in overrides:
        name, want = row["pref"], row["value"]
        if row["kind"].lower() == "clearpref":
            verdict = "CLEARED"
        elif name not in stock:
            verdict = "NEW"
        elif stock[name] == want:
            verdict = "REDUNDANT"
        else:
            verdict = "DEVIATION"
        rows.append({
            **row,
            "stock": stock.get(name),
            "verdict": verdict,
            "page_observable": name.startswith(PAGE_OBSERVABLE),
            "locked": row["kind"].lower() == "lockpref",
        })

    by = lambda v: [r for r in rows if r["verdict"] == v]  # noqa: E731
    deviations = by("DEVIATION")
    observable = [r for r in deviations if r["page_observable"]]

    print(f"REDUNDANT  {len(by('REDUNDANT')):4}   already stock's value; the line can go")
    print(f"DEVIATION  {len(deviations):4}   differs from stock")
    print(f"  page-observable {len(observable):4} <- triage first")
    print(f"  other           {len(deviations) - len(observable):4}")
    print(f"NEW        {len(by('NEW')):4}   stock does not carry this pref")
    print(f"CLEARED    {len(by('CLEARED')):4}")
    print(f"locked     {sum(1 for r in rows if r['locked']):4}   caller cannot override\n")

    show = {"deviation": deviations, "redundant": by("REDUNDANT"),
            "new": by("NEW"), "all": rows}[args.show]
    if show:
        order = sorted(show, key=lambda r: (not r["page_observable"], r["pref"]))
        print(f"{'':2}{'pref':52} {'stock':>16}  {'camoufox':>16}  cfg")
        for r in order:
            mark = "!" if r["page_observable"] else " "
            where = r["line"] if r["kind"] == "launcher" else f":{r['line']}"
            print(f"{mark} {r['pref']:52} {str(r['stock'])[:16]:>16}  "
                  f"{str(r['value'])[:16]:>16}  {where}")

    if args.camoufox:
        print(f"\ncamoufox     : {args.camoufox}")
        try:
            live = dump_prefs(args.camoufox)
        except Exception as exc:  # noqa: BLE001
            print(f"  live diff unavailable ({type(exc).__name__}); "
                  f"camoufox exposes Marionette only through its own launcher")
            live = None
        if live:
            drift = [n for n in set(stock) & set(live) if stock[n] != live[n]]
            known = {r["pref"] for r in rows}
            print(f"  {len(drift)} live differences, "
                  f"{len([n for n in drift if n not in known])} not from camoufox.cfg")

    if args.json:
        args.json.write_text(json.dumps(rows, indent=1) + "\n")
        print(f"\nfull result -> {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
