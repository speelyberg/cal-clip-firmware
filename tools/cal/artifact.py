#!/usr/bin/env python3
"""Inspect a signed MCUboot application and write an OTA artifact manifest."""
import argparse
import hashlib
import json
import struct
from pathlib import Path

MAGIC = 0x96F3B83D
SLOT_SIZE = 0xEA000


def inspect_image(path):
    raw = Path(path).read_bytes()
    if len(raw) < 32 or len(raw) >= SLOT_SIZE - 32:
        raise ValueError('Application image is truncated or does not fit the existing slot')
    magic, _, header, protected, size, flags, major, minor, revision, build, _ = struct.unpack_from('<IIHHIIBBHII', raw)
    if magic != MAGIC or header < 32 or header + size + protected + 4 > len(raw):
        raise ValueError('Invalid MCUboot application header')
    if flags & 0x0C:
        raise ValueError('Encrypted images are not supported by this app-only updater')
    end = header + size + protected
    tlv_magic, tlv_size = struct.unpack_from('<HH', raw, end)
    if tlv_magic != 0x6907 or tlv_size < 4 or end + tlv_size > len(raw):
        raise ValueError('Invalid image TLV table')
    fields = {}
    pos = end + 4
    while pos < end + tlv_size:
        if pos + 4 > end + tlv_size:
            raise ValueError('Truncated TLV header')
        kind, _, length = struct.unpack_from('<BBH', raw, pos)
        pos += 4
        if pos + length > end + tlv_size or kind in fields:
            raise ValueError('Truncated or duplicate image TLV')
        fields[kind] = raw[pos:pos + length]
        pos += length
    digest = hashlib.sha256(raw[:end]).digest()
    if fields.get(0x10) != digest or len(fields.get(0x01, b'')) != 32 or len(fields.get(0x20, b'')) != 256:
        raise ValueError('Image hash or RSA-2048 signature metadata is invalid')
    return {'file': Path(path).name, 'file_sha256': hashlib.sha256(raw).hexdigest(),
            'image_hash': digest.hex(), 'key_hash': fields[0x01].hex(),
            'version': f'{major}.{minor}.{revision}+{build}', 'bytes': len(raw),
            'image': 0, 'slot_bytes': SLOT_SIZE}


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument('image', type=Path)
    ap.add_argument('--source-sha', default='')
    ap.add_argument('--variant', choices=['debug', 'production'], required=True)
    ap.add_argument('--output', type=Path, required=True)
    args = ap.parse_args()
    record = inspect_image(args.image)
    record.update(source_sha=args.source_sha, variant=args.variant, signature_verified_by='MCUboot imgtool in CI')
    args.output.write_text(json.dumps(record, indent=2) + '\n')
    print(json.dumps(record))


if __name__ == '__main__':
    main()
