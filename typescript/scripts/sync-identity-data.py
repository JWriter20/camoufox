"""Copy the identity data files from pythonlib into src/data-files.

The TypeScript identity layer reads the SAME files the Python launcher does, so
one seed draws one identity in both. The JSON files are copied byte-for-byte;
webgl_data.db (SQLite) is exported to webgl_data.json, one object per row in
rowid order and parsed with orjson (as sample_webgl parses it) -- the order `SELECT ... FROM webgl_fingerprints` returns and
therefore the order the seeded draw indexes into.

    .venv/bin/python typescript/scripts/sync-identity-data.py

tests/identity-data.test.ts fails when a copy drifts from pythonlib.
"""

import json
import shutil
import sqlite3
from pathlib import Path

# The row data is parsed the way sample_webgl parses it: orjson reads an
# integer beyond the u64 range (18446744073709552000) as a float, where json
# would keep an exact int -- and the config the launcher writes differs.
import orjson

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / 'pythonlib' / 'camoufox'
DST = ROOT / 'typescript' / 'src' / 'data-files'

COPIED = (
    'fonts.json',
    'font-bases.json',
    'font-groups.json',
    'voice-manifests.json',
    'voice-uris.json',
    'media-devices.json',
    'fingerprint-presets.json',
    'fingerprint-presets-v150.json',
)


def export_webgl(db: Path, out: Path) -> None:
    con = sqlite3.connect(db)
    try:
        rows = con.execute(
            'SELECT vendor, renderer, win, mac, lin, data FROM webgl_fingerprints ORDER BY rowid'
        ).fetchall()
    finally:
        con.close()
    records = [
        {'vendor': v, 'renderer': r, 'win': w, 'mac': m, 'lin': l, 'data': orjson.loads(d)}
        for v, r, w, m, l, d in rows
    ]
    out.write_text(json.dumps(records, ensure_ascii=False) + '\n', encoding='utf-8')


def main() -> None:
    for name in COPIED:
        shutil.copyfile(SRC / name, DST / name)
        print('copied', name)
    export_webgl(SRC / 'webgl' / 'webgl_data.db', DST / 'webgl_data.json')
    print('exported webgl_data.json')


if __name__ == '__main__':
    main()
