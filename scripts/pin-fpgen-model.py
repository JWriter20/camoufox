#!/usr/bin/env python3
"""Install a pinned fpgen model, instead of letting fpgen fetch one itself.

WHY THIS EXISTS
---------------
`fpgen` (scrapfly/fingerprint-generator) is where Camoufox's synthetic
fingerprints come from. It does not ship its model; it downloads one the first
time it is imported, and again whenever the files on disk are over five weeks
old. That fetch has four problems, all of them in fpgen/pkgman.py:

  1. TLS verification is DISABLED on both the API call and the download
     (`httpx.get(..., verify=False)`, `httpx.stream(..., verify=False)`), so
     anyone on the network path can serve the model.
  2. The archive is never checksummed -- it goes straight into
     `zipfile.ZipFile(...).extractall(DATA_DIR)`, which is also not guarded
     against path traversal.
  3. The GitHub API is called unauthenticated, sharing a 60-request/hour limit
     with every other job on the runner's IP.
  4. It takes the FIRST release the API lists and that release's first `.zip`.
     GitHub sorts releases by `created_at`, and `model-4/2025` and
     `model-2/2026` carry an identical `created_at` (2025-03-22T11:41:12Z,
     inherited from the tag's commit), so the tie breaks toward the lower id
     and fpgen always lands on the 2025 model. The 2026 model is unreachable
     through that path.

So an unpinned Camoufox generates from an April-2025 corpus -- newest Firefox
137, newest GPU an RTX 40 -- and cannot be talked into anything newer.

This script puts the model named by scripts/data/fpgen-model.json where fpgen
looks, with TLS verification on and the sha256 checked, before anything imports
fpgen. fpgen then finds recent files and never calls the network.

Locating the data directory must NOT import fpgen: importing is what triggers
the download this script exists to prevent. importlib.util.find_spec() reads the
module's origin without executing it.

Usage:
    python3 scripts/pin-fpgen-model.py           # install if not already pinned
    python3 scripts/pin-fpgen-model.py --check    # verify only; non-zero if not pinned
    python3 scripts/pin-fpgen-model.py --force    # re-download and reinstall
"""

import argparse
import hashlib
import importlib.util
import json
import os
import ssl
import sys
import tempfile
import urllib.request
import zipfile

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = os.path.join(REPO, 'scripts', 'data', 'fpgen-model.json')
STAMP = '.pinned-model'


def load_spec():
    with open(SPEC, encoding='utf-8') as fh:
        return json.load(fh)


def data_dir():
    """fpgen's data directory, found WITHOUT importing fpgen."""
    spec = importlib.util.find_spec('fpgen')
    if spec is None or not spec.origin:
        sys.exit('fpgen is not installed; `pip install -e pythonlib` first')
    return os.path.join(os.path.dirname(spec.origin), 'data')


def digest(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, 'rb') as fh:
        for block in iter(lambda: fh.read(chunk), b''):
            h.update(block)
    return h.hexdigest()


def is_pinned(spec, d):
    """True when the pinned model is already in place."""
    try:
        with open(os.path.join(d, STAMP), encoding='utf-8') as fh:
            if fh.read().strip() != spec['sha256']:
                return False
    except OSError:
        return False
    return all(os.path.exists(os.path.join(d, f)) for f in spec['files'])


def download(spec, dest):
    # Verification ON, unlike fpgen's own fetch. create_default_context()
    # verifies the chain and the hostname.
    ctx = ssl.create_default_context()
    print(f'fetching {spec["url"]}', file=sys.stderr)
    req = urllib.request.Request(spec['url'], headers={'User-Agent': 'camoufox-build'})
    with urllib.request.urlopen(req, context=ctx, timeout=60) as r, open(dest, 'wb') as fh:
        while chunk := r.read(1 << 20):
            fh.write(chunk)
    size = os.path.getsize(dest)
    if spec.get('size') and size != spec['size']:
        sys.exit(f'size mismatch: got {size}, expected {spec["size"]}')
    got = digest(dest)
    if got != spec['sha256']:
        sys.exit(f'sha256 mismatch:\n  got      {got}\n  expected {spec["sha256"]}')


def install(spec, archive, d):
    os.makedirs(d, exist_ok=True)
    with zipfile.ZipFile(archive) as z:
        # fpgen extracts without checking; do not copy that. A member escaping
        # the data directory would write anywhere the build user can.
        for name in z.namelist():
            target = os.path.realpath(os.path.join(d, name))
            if not target.startswith(os.path.realpath(d) + os.sep):
                sys.exit(f'refusing to extract outside the data dir: {name}')
        z.extractall(d)
    # Written last: an interrupted extract leaves no stamp, so the next run
    # reinstalls rather than trusting a partial model.
    with open(os.path.join(d, STAMP), 'w', encoding='utf-8') as fh:
        fh.write(spec['sha256'] + '\n')


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--check', action='store_true', help='verify only; non-zero if not pinned')
    ap.add_argument('--force', action='store_true', help='reinstall even if already pinned')
    args = ap.parse_args()

    spec = load_spec()
    d = data_dir()

    if args.check:
        if is_pinned(spec, d):
            print(f'OK: fpgen model pinned to {spec["tag"]}')
            return 0
        print(f'fpgen model is NOT pinned to {spec["tag"]}; run scripts/pin-fpgen-model.py',
              file=sys.stderr)
        return 1

    if is_pinned(spec, d) and not args.force:
        print(f'OK: fpgen model already pinned to {spec["tag"]}', file=sys.stderr)
        return 0

    with tempfile.TemporaryDirectory() as tmp:
        archive = os.path.join(tmp, 'model-release.zip')
        download(spec, archive)
        install(spec, archive, d)
    print(f'OK: pinned fpgen model {spec["tag"]} -> {d}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
