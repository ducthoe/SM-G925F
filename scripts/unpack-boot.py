#!/usr/bin/env python3
"""Inspect and reproducibly split an Android boot image v0."""

import argparse
import hashlib
import json
import struct
import sys
from pathlib import Path


def aligned(value: int, page_size: int) -> int:
    return (value + page_size - 1) // page_size * page_size


def c_string(data: bytes) -> str:
    return data.split(b"\0", 1)[0].decode("ascii", errors="replace")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("image", type=Path)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()

    blob = args.image.read_bytes()
    if len(blob) < 608 or blob[:8] != b"ANDROID!":
        print(f"Not a supported Android boot image v0: {args.image}", file=sys.stderr)
        return 1

    fields = struct.unpack_from("<10I", blob, 8)
    keys = (
        "kernel_size",
        "kernel_addr",
        "ramdisk_size",
        "ramdisk_addr",
        "second_size",
        "second_addr",
        "tags_addr",
        "page_size",
        "dt_size",
        "unused",
    )
    header = dict(zip(keys, fields))
    page_size = header["page_size"]
    if page_size == 0 or page_size & (page_size - 1):
        print(f"Invalid page size: {page_size}", file=sys.stderr)
        return 1

    header["name"] = c_string(blob[48:64])
    header["cmdline"] = c_string(blob[64:576])
    header["id_hex"] = blob[576:608].hex()
    header["image_size"] = len(blob)
    header["image_sha256"] = hashlib.sha256(blob).hexdigest()

    kernel_offset = page_size
    ramdisk_offset = aligned(kernel_offset + header["kernel_size"], page_size)
    second_offset = aligned(ramdisk_offset + header["ramdisk_size"], page_size)
    dt_offset = aligned(second_offset + header["second_size"], page_size)

    components = {
        "kernel": (kernel_offset, header["kernel_size"]),
        "ramdisk": (ramdisk_offset, header["ramdisk_size"]),
        "second": (second_offset, header["second_size"]),
        "dt": (dt_offset, header["dt_size"]),
    }
    header["components"] = {
        name: {"offset": offset, "size": size}
        for name, (offset, size) in components.items()
    }

    furthest = max(offset + size for offset, size in components.values())
    if furthest > len(blob):
        print(
            f"Component table extends beyond image: {furthest} > {len(blob)}",
            file=sys.stderr,
        )
        return 1

    print(json.dumps(header, indent=2))

    if args.output_dir is None:
        return 0

    args.output_dir.mkdir(parents=True, exist_ok=True)
    names = {
        "kernel": "kernel",
        "ramdisk": "ramdisk",
        "second": "second",
        "dt": "dt.img",
    }
    for name, (offset, size) in components.items():
        if size == 0:
            continue
        destination = args.output_dir / names[name]
        if destination.exists():
            print(f"Refusing to overwrite: {destination}", file=sys.stderr)
            return 1
        payload = blob[offset : offset + size]
        destination.write_bytes(payload)
        print(
            f"wrote {destination} ({len(payload)} bytes, "
            f"sha256={hashlib.sha256(payload).hexdigest()})",
            file=sys.stderr,
        )

    metadata = args.output_dir / "header.json"
    if metadata.exists():
        print(f"Refusing to overwrite: {metadata}", file=sys.stderr)
        return 1
    metadata.write_text(json.dumps(header, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
