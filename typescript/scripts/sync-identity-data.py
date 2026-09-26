"""Copy the identity data files from pythonlib into src/data-files.

The TypeScript identity layer reads the SAME files the Python launcher does, so
one seed draws one identity in both. The files are copied byte-for-byte.

    .venv/bin/python typescript/scripts/sync-identity-data.py

tests/fingerprints-data.test.ts fails when a copy drifts from pythonlib.
"""

import shutil
from pathlib import Path

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


def main() -> None:
    for name in COPIED:
        shutil.copyfile(SRC / name, DST / name)
        print('copied', name)


if __name__ == '__main__':
    main()
