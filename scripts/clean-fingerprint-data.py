#!/usr/bin/env python3
"""Drop identities the shipped data files cannot honestly offer.

`camoufox.coherence` checks an identity when one is drawn. This does the same
to the DATA the identities are drawn FROM, so an impossible row never reaches a
build in the first place. Two filters for the same rules: the runtime one has to
produce an identity and so repairs what it can, while this one can simply drop a
row -- there are plenty of others, and dropping loses nothing but a machine that
never existed.

What it covers:

  fingerprint-presets.json, fingerprint-presets-v150.json
      Each preset is converted the way a launch converts it and checked. A row
      that fails is dropped rather than repaired: repairing would write an
      invented value ("what core count does a 2-core Apple M1 really have?")
      into a file whose entire purpose is being real.

  webgl/webgl_data.db
      Each (vendor, renderer) pair carries a probability per OS. A pair the OS
      cannot report has that probability zeroed, which keeps the row for the
      platforms where it IS real -- "Radeon R9 200 Series" is a genuine Linux
      and Windows GPU, it simply never shipped in a Mac.

Usage:
    python3 scripts/clean-fingerprint-data.py            # report only
    python3 scripts/clean-fingerprint-data.py --write    # rewrite the files

`--check` exits non-zero when anything would be dropped; pythonlib's
tests/test_shipped_data.py asserts the same thing, so a data refresh that
reintroduces a bad row fails CI rather than shipping.
"""

import argparse
import json
import sqlite3
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / 'pythonlib'))

from camoufox import coherence  # noqa: E402
from camoufox.fingerprints import from_preset  # noqa: E402

PRESET_FILES = (
    REPO / 'pythonlib' / 'camoufox' / 'fingerprint-presets.json',
    REPO / 'pythonlib' / 'camoufox' / 'fingerprint-presets-v150.json',
)
WEBGL_DB = REPO / 'pythonlib' / 'camoufox' / 'webgl' / 'webgl_data.db'
OS_KEY = {'macos': 'mac', 'windows': 'win', 'linux': 'lin'}
# The Firefox version only decides the UA rewrite, which no rule reads.
FF_VERSION = '152'


def preset_violations(preset, os_name):
    """What this preset breaks, as a launch would see it."""
    try:
        config = from_preset(preset, FF_VERSION)
    except Exception as exc:  # a row too malformed to convert is itself a defect
        return [coherence.Violation('unconvertible', f'{type(exc).__name__}: {exc}')]
    return coherence.validate(config, OS_KEY[os_name])


def clean_presets(path, write):
    data = json.loads(path.read_text())
    dropped = Counter()
    kept_total = dropped_total = 0
    for os_name, presets in data['presets'].items():
        kept = []
        for index, preset in enumerate(presets):
            violations = preset_violations(preset, os_name)
            if violations:
                dropped_total += 1
                for violation in violations:
                    dropped[violation.rule] += 1
                print(f'  drop {path.name} {os_name}[{index}]: '
                      f'{"; ".join(v.detail for v in violations)[:120]}')
            else:
                kept.append(preset)
        kept_total += len(kept)
        data['presets'][os_name] = kept
    if write and dropped_total:
        # Keep the file's own formatting. Rewriting 2-space-indented JSON
        # compactly turns a 38-row deletion into a 22,546-line diff that no
        # reviewer can read, which is how a data change stops being reviewable.
        # ensure_ascii=False as well: the originals hold 'Amélie' as itself,
        # and escaping it would rewrite lines no preset touched.
        # A no-op run must be byte-identical, so the diff of a real run is the
        # dropped rows and nothing else: 2-space indent, non-ASCII kept as
        # itself ('Amélie'), and no trailing newline, which is how these
        # files are written.
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    print(f'{path.name}: {dropped_total} dropped, {kept_total} kept'
          + (f'  {dict(dropped)}' if dropped else ''))
    return dropped_total


def clean_webgl_db(write):
    connection = sqlite3.connect(WEBGL_DB)
    cursor = connection.cursor()
    cursor.execute('SELECT rowid, vendor, renderer, win, mac, lin FROM webgl_fingerprints')
    rows = cursor.fetchall()
    zeroed = 0
    for rowid, vendor, renderer, *weights in rows:
        for column, weight in zip(('win', 'mac', 'lin'), weights):
            if weight and weight > 0 and not coherence.gpu_fits_os(renderer, column):
                zeroed += 1
                print(f'  zero webgl_data.db {column}={weight:.3f} for {renderer[:58]!r}')
                if write:
                    cursor.execute(
                        f'UPDATE webgl_fingerprints SET {column} = 0 WHERE rowid = ?',  # nosec
                        (rowid,),
                    )
    if write and zeroed:
        connection.commit()
    connection.close()
    print(f'webgl_data.db: {zeroed} impossible OS weight(s)'
          + (' zeroed' if write and zeroed else ''))
    return zeroed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--write', action='store_true', help='rewrite the data files')
    parser.add_argument('--check', action='store_true', help='exit non-zero if anything is unclean')
    args = parser.parse_args()

    total = sum(clean_presets(path, args.write) for path in PRESET_FILES)
    total += clean_webgl_db(args.write)

    if not total:
        print('\nEvery shipped identity is coherent.')
        return 0
    if args.write:
        print(f'\n{total} incoherent entr(ies) removed.')
        return 0
    print(f'\n{total} incoherent entr(ies). Run with --write to remove them.')
    return 1 if args.check else 0


if __name__ == '__main__':
    raise SystemExit(main())
