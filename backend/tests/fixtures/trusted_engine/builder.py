"""Builder for canonical AIK USTAR archives for the trusted benchmark engine."""

from __future__ import annotations

import json
from pathlib import Path

from app.services.submission_validation import canonical_ustar_split


def _octal(value: int, length: int) -> bytes:
    return format(value, f"0{length - 1}o").encode("ascii") + b"\0"


def _header(path: str, size: int) -> bytes:
    prefix, name = canonical_ustar_split(path)
    block = bytearray(512)
    block[: len(name)] = name
    block[100:108] = _octal(0o644, 8)
    block[108:116] = _octal(0, 8)
    block[116:124] = _octal(0, 8)
    block[124:136] = _octal(size, 12)
    block[136:148] = _octal(0, 12)
    block[148:156] = b" " * 8
    block[156:157] = b"0"
    block[257:263] = b"ustar\0"
    block[263:265] = b"00"
    block[345 : 345 + len(prefix)] = prefix
    checksum = sum(block)
    block[148:156] = f"{checksum:06o}".encode("ascii") + b"\0 "
    return bytes(block)


def build_trusted_engine_archive() -> bytes:
    """Build a canonical USTAR archive from the trusted_engine fixture files."""
    fixture_dir = Path(__file__).parent
    manifest_bytes = (fixture_dir / "manifest.json").read_bytes()
    engine_bytes = (fixture_dir / "engine" / "main.py").read_bytes()

    files = [
        ("manifest.json", manifest_bytes),
        ("engine/main.py", engine_bytes),
    ]

    output = bytearray()
    for path, content in sorted(files, key=lambda item: item[0].encode("utf-8")):
        output.extend(_header(path, len(content)))
        output.extend(content)
        output.extend(bytes((-len(content)) % 512))
    output.extend(bytes(1024))
    return bytes(output)
