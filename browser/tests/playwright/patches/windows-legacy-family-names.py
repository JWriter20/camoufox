"""
A Windows identity's legacy styled-family names resolve as stock Windows's do before its font loader runs.

On Windows, DirectWrite folds "Arial Black", "Calibri Light" and "Franklin
Gothic Medium" into the families Arial, Calibri and Franklin Gothic; the names
are GDI legacy names that stock Firefox learns only from its deferred
font-info loader (60 s after startup, or a moment after the first legacy
lookup). Before that, the first lookup of such a name answers its BASE family
(so Arial Black renders as Arial Regular) and every later lookup of a name of
that base misses, because FontList::FindFamily retries the alias table entry
with the base's display name (SharedFontList.cpp:1257-1258), which never
matches a lowercase key. cfparity fonts.detect on win-i9 (three launches):

    Arial Black             1013.417,1013.417,1017.042
    Calibri Light           951.000,1013.417,1017.042
    Franklin Gothic Medium  973.000,1013.417,1017.042

that is, the monospace column is the base family's regular face and the sans
and serif columns are the generic fallback. Camoufox on Linux has them as
ordinary fontconfig families and drew the real faces in every column
(Arial Black 1120.750,1131.750,1132.542). LEAKS #168.

The win-measure fonts-legacy probe (stock 158.0.1 on win-i9, 2026-10-31) adds
Franklin Gothic Demi and Segoe UI Semibold missing after a sibling of their
base was read, and Rockwell Extra Bold / Rockwell Condensed missing even when
asked first; the aliases landed 1727-1832 ms after the first legacy lookup.

The guard measures the probe's three columns in a fresh Windows identity and
asserts stock's pattern: each width relative to the identity's own sans-serif
base must be stock's relative to win-i9's (within RELATIVE_TOLERANCE_PX), so a
column that is the base family, a generic fallback or a face is the same one
stock's is. Absolute widths are not compared: the probe's text ends in 日本語,
and its fallback glyphs are 216 px here but ~214.25 on win-i9 for every base
family (Arial 7.06, Times New Roman 7.12, Consolas 7.01, Calibri 6.27 and
Segoe UI 5.71 all leave the same residual against their bundled advances),
which is CJK fallback, not legacy names, and not this patch. The bases are
printed as a note.

Then, once fonts:windowsAliasLoadMs has passed, Arial Black must be the real
Black face again (the loaded-alias state) in the same document: stock reflows
when the aliases land, which flushes the font cache that held the early answer.

The bare variable-font families "Segoe UI Variable" and "Sitka" are missing
until the aliases land, then resolve to their default instance (stock probe-Y,
win-i9 25H2 26200.9711, 2026-11-01, 3/3: 887.125,1013.417,1017.042 before;
Segoe UI Variable = Segoe UI Variable Text, Sitka = Sitka Text after). And once
loaded, Calibri Light is its real Light face (944.917 on stock), not Calibri's
Regular (951.000) that the old fonts.conf rewrite drew for good.

The same patch keeps fontconfig's generic aliases out of a Windows identity:
DirectWrite has no family named "sans-serif", "monospace", "cursive",
"fantasy", "system-ui", "sans", "mono" or "sans serif", so stock renders each of
those (quoted, or the last three unquoted) as an unknown family: the default
font, i.e. exactly what an absent family gives. Camoufox's fontconfig answered
Arial / Courier New / Comic Sans MS... (probe-Y E). The GDI FontSubstitutes
names stock resolves (MS Shell Dlg, Helv -> Microsoft Sans Serif; Arial CE,
Arial Baltic -> Arial; Courier New CE -> Courier New) must render as their
targets (bundle/fontconfig/windows/fonts.conf).

    python browser/tests/playwright/patches/windows-legacy-family-names.py
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import resolve_binary  # noqa: E402

LOAD_MS = 1500
# App-unit rounding (1/60 px per glyph) moves a 17-glyph width by up to ~0.2.
RELATIVE_TOLERANCE_PX = 0.3
CONFIG = {"fonts:windowsAliasLoadMs": LOAD_MS}
BASES = {"monospace": "887.125", "sans-serif": "1013.417", "serif": "1017.042"}
STOCK = {
    "Arial": "1013.417,1013.417,1013.417",
    "Arial Black": "1013.417,1013.417,1017.042",
    "Calibri": "951.000,951.000,951.000",
    "Calibri Light": "951.000,1013.417,1017.042",
    "Franklin Gothic Medium": "973.000,1013.417,1017.042",
    # Win-measure fonts-legacy probe (stock 158.0.1, win-i9, 2026-10-31, 3/3
    # launches): a second name of an already-read base misses (Demi after
    # Medium, Semibold after Light), and the Rockwell names miss even first.
    "Franklin Gothic Demi": "887.125,1013.417,1017.042",
    "Segoe UI Light": "1011.250,1013.417,1017.042",
    "Segoe UI Semibold": "887.125,1013.417,1017.042",
    "Rockwell Extra Bold": "887.125,1013.417,1017.042",
    "Rockwell Condensed": "887.125,1013.417,1017.042",
    # probe-Y (2026-11-01, 3/3): the bare variable-font families miss until the aliases land.
    "Segoe UI Variable": "887.125,1013.417,1017.042",
    "Sitka": "887.125,1013.417,1017.042",
}
# After the aliases land: each name must render as the family on the right (stock probe-Y C, 3/3).
LOADED_AS = {"Segoe UI Variable": "Segoe UI Variable Text", "Sitka": "Sitka Text"}
# Stock's loaded Calibri Light is its Light face: narrower than Calibri Regular in every column.
LIGHT_FACES = {"Calibri Light": "Calibri", "Candara Light": "Candara", "Corbel Light": "Corbel",
               "Leelawadee UI Semilight": "Leelawadee UI", "Nirmala UI Semilight": "Nirmala UI",
               "Nirmala Text Semilight": "Nirmala Text"}
# Names DirectWrite has no family for: each must render exactly as an absent family does (probe-Y E/E2).
NOT_FAMILIES = ['"sans-serif"', '"monospace"', '"cursive"', '"fantasy"', '"system-ui"', '"serif"',
                'sans', 'mono', '"sans"', '"mono"', '"sans serif"', '"MONO"']
SUBSTITUTES = {"MS Shell Dlg": "Microsoft Sans Serif", "Helv": "Microsoft Sans Serif", "Arial CE": "Arial",
               "Arial Baltic": "Arial", "Courier New CE": "Courier New"}
GENERIC_PROBE = """(a) => {
  const c = document.createElement('canvas').getContext('2d');
  const S = 'Hamburgefonstiv Wwg 0123456789';
  const w = (f) => { c.font = f; return c.measureText(S).width.toFixed(3); };
  const absent = w('16px "cfp-absent-guard"');
  return {absent, names: Object.fromEntries(a.names.map((n) => [n, w('16px ' + n)])),
          subs: Object.fromEntries(Object.entries(a.subs).map(([n, t]) => [n, [w('16px "' + n + '"'), w('16px "' + t + '"')]]))};
}"""
# cfparity fonts.js fonts.detect, synchronously, in its order.
PROBE = """(families) => {
  const TEST = 'mmMwWLliI0O&1@ẞ∑ж日本語';
  const c = document.createElement('canvas').getContext('2d');
  const bases = {};
  for (const b of ['monospace', 'sans-serif', 'serif']) { c.font = `72px ${b}`; bases[b] = c.measureText(TEST).width.toFixed(3); }
  const widths = {};
  for (const f of families) {
    widths[f] = ['monospace', 'sans-serif', 'serif'].map((b) => {
      c.font = `72px "${f}", ${b}`;
      return c.measureText(TEST).width.toFixed(3);
    }).join(',');
  }
  return {bases, widths};
}"""


def main() -> int:
    from camoufox.sync_api import Camoufox

    failures = []
    with Camoufox(executable_path=str(resolve_binary()), headless=True, os="windows", config=CONFIG,
                  i_know_what_im_doing=True) as browser:
        page = browser.new_page()
        page.set_content("<body></body>")
        first = page.evaluate(PROBE, list(STOCK))
        generic = page.evaluate(GENERIC_PROBE, {"names": NOT_FAMILIES, "subs": SUBSTITUTES})
        time.sleep(LOAD_MS / 1000 + 1)
        later = page.evaluate(PROBE, ["Arial Black", *LOADED_AS, *LOADED_AS.values(), *LIGHT_FACES,
                                      *LIGHT_FACES.values()])
    print(f"  bases {first['bases']}")
    for family, widths in first["widths"].items():
        print(f"  {family:24} {widths}   stock {STOCK[family]}")
    print(f"  after {LOAD_MS} ms: Arial Black {later['widths']['Arial Black']}")

    bases = first["bases"]
    if bases != BASES:
        print(f"  note: generic bases {bases} vs win-i9 {BASES} (the probe text's CJK fallback; see above)")
    base, stock_base = float(bases["sans-serif"]), float(BASES["sans-serif"])
    for family, widths in first["widths"].items():
        have = [float(w) - base for w in widths.split(",")]
        want = [float(w) - stock_base for w in STOCK[family].split(",")]
        if any(abs(h - w) > RELATIVE_TOLERANCE_PX for h, w in zip(have, want)):
            failures.append(f"{family}: {widths} ({', '.join(f'{h:+.3f}' for h in have)} from the sans-serif base), "
                            f"stock Windows {STOCK[family]} ({', '.join(f'{w:+.3f}' for w in want)})")
    loaded = later["widths"]["Arial Black"].split(",")
    arial = first["widths"]["Arial"].split(",")[0]
    if loaded[1] == bases["sans-serif"] or loaded[2] == bases["serif"] or loaded[0] == arial:
        failures.append(f"after the alias load Arial Black is still missing or Arial ({later['widths']['Arial Black']}); "
                        "stock resolves it to the Black face once its loader has run")

    lw = later["widths"]
    for name, as_family in LOADED_AS.items():
        print(f"  after load: {name:18} {lw[name]}   {as_family} {lw[as_family]}")
        if lw[name] != lw[as_family]:
            failures.append(f"after the alias load {name} is {lw[name]}, not {as_family}'s {lw[as_family]} "
                            "as on stock Windows")
    for light, regular in LIGHT_FACES.items():
        print(f"  after load: {light:24} {lw[light]}   {regular} {lw[regular]}")
        if float(lw[light].split(",")[0]) >= float(lw[regular].split(",")[0]):
            failures.append(f"after the alias load {light} is {lw[light]}, not narrower than {regular}'s "
                            f"{lw[regular]}: stock renders the real {light.split()[-1]} face")
    print(f"  absent family: {generic['absent']}")
    for name, width in generic["names"].items():
        if width != generic["absent"]:
            failures.append(f"font-family {name} renders {width}, not the absent-family {generic['absent']}: "
                            "Windows has no family of that name")
    for name, (width, target) in generic["subs"].items():
        if width != target:
            failures.append(f'"{name}" renders {width}, not its FontSubstitutes target {SUBSTITUTES[name]} ({target})')

    for failure in failures:
        print(f"FAIL: {failure}")
    if failures:
        return 1
    print("PASS: legacy styled-family names answer the base family once, then miss, until the aliases load; "
          "generic names are no Windows families; FontSubstitutes names render their targets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
