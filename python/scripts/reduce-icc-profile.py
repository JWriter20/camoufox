#!/usr/bin/env python3
"""Reduce a display ICC profile to the tags Firefox's output profile reads.

Firefox loads the display profile with qcms_profile_from_memory_curves_only
(gfxPlatform::InitializeCMS), which reads the header (size, class, rendering
intent, colour space, PCS, the reserved version bytes) and only these tags
(gfx/qcms/src/iccread.rs Profile::new_from_slice with curves_only):
chad, rXYZ/gXYZ/bXYZ and rTRC/gTRC/bTRC for RGB, kTRC for grey. Everything
else (desc, cprt, wtpt, lumi, vendor blobs such as Dell's 6 KB DVB1, A2B0/B2A0
LUTs) never reaches a conversion, so display-profiles.json bundles the reduced
profile: same conversions, a fraction of the bytes, and no vendor text.

Tag data shared between tags (rTRC = gTRC = bTRC is common) stays shared.
The header's profile ID (MD5, bytes 84-99) is zeroed because it no longer
matches the content; qcms does not read it.

Usage: reduce-icc-profile.py IN.icc OUT.icc   (prints the base64 with --b64)
"""

import argparse
import base64
import struct
import sys

KEPT_TAGS = (b'chad', b'rXYZ', b'gXYZ', b'bXYZ', b'rTRC', b'gTRC', b'bTRC', b'kTRC')


def reduce_icc(data: bytes) -> bytes:
    if len(data) < 132 or data[36:40] != b'acsp':
        raise ValueError('not an ICC profile')
    size = struct.unpack('>I', data[:4])[0]
    if size > len(data):
        raise ValueError('ICC profile is truncated')
    data = data[:size]
    count = struct.unpack('>I', data[128:132])[0]
    if 132 + 12 * count > size:
        raise ValueError('ICC tag table runs past the profile')
    tags = []
    for i in range(count):
        sig, off, length = struct.unpack('>4sII', data[132 + 12 * i:144 + 12 * i])
        if sig in KEPT_TAGS:
            if off + length > size:
                raise ValueError(f'tag {sig!r} runs past the profile')
            tags.append((sig, off, length))
    if not tags:
        raise ValueError('ICC profile has none of the tags qcms reads')

    # Tag data starts after the new table, 4-byte aligned; each distinct
    # (offset, length) block is copied once.
    pos = 132 + 12 * len(tags)
    pos += -pos % 4
    blocks = {}
    body = bytearray()
    table = []
    for sig, off, length in tags:
        key = (off, length)
        if key not in blocks:
            blocks[key] = pos + len(body)
            body += data[off:off + length]
            body += b'\0' * (-len(body) % 4)
        table.append(struct.pack('>4sII', sig, blocks[key], length))

    header = bytearray(data[:128])
    header[84:100] = b'\0' * 16
    out = bytearray(header)
    out += struct.pack('>I', len(tags))
    out += b''.join(table)
    out += b'\0' * (pos - len(out))
    out += body
    out[0:4] = struct.pack('>I', len(out))
    return bytes(out)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument('src')
    ap.add_argument('dst', nargs='?')
    ap.add_argument('--b64', action='store_true', help='print the reduced profile as base64')
    args = ap.parse_args(argv)
    with open(args.src, 'rb') as f:
        reduced = reduce_icc(f.read())
    if args.dst:
        with open(args.dst, 'wb') as f:
            f.write(reduced)
    if args.b64 or not args.dst:
        print(base64.b64encode(reduced).decode())
    print(f'{len(reduced)} bytes', file=sys.stderr)
    return 0


if __name__ == '__main__':
    sys.exit(main())
