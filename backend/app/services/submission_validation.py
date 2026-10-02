"""Bounded, non-executing parser for canonical AI Knockout POSIX USTAR packages."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class PackageLimits:
    max_archive_bytes: int = 16 * 1024 * 1024
    max_members: int = 128
    max_member_bytes: int = 4 * 1024 * 1024
    max_total_bytes: int = 16 * 1024 * 1024
    max_manifest_bytes: int = 16 * 1024


DEFAULT_LIMITS = PackageLimits()
VALIDATOR_VERSION = "aik-intake-validator/1"
POLICY_VERSION = "aik-package-policy/1"


class PackageRejected(Exception):
    def __init__(self, code: str, stage: str = "INSPECTING") -> None:
        self.code = code
        self.stage = stage
        self.manifest: dict | None = None
        super().__init__(code)


@dataclass(frozen=True)
class ValidatedPackage:
    manifest: dict
    files: dict[str, bytes]


def canonical_ustar_split(path: str) -> tuple[bytes, bytes]:
    """Return exact (prefix, name) UTF-8 bytes per the rightmost-slash rule."""
    try:
        encoded = path.encode("utf-8", errors="strict")
    except UnicodeEncodeError as exc:
        raise PackageRejected("ARCHIVE_PATH_ENCODING_INVALID") from exc
    if not encoded or len(encoded) > 255:
        raise PackageRejected("ARCHIVE_PATH_INVALID")
    if len(encoded) <= 100:
        return b"", encoded
    for index in range(len(encoded) - 1, -1, -1):
        if encoded[index] != ord("/"):
            continue
        prefix, name = encoded[:index], encoded[index + 1 :]
        if prefix and len(prefix) <= 155 and name and len(name) <= 100:
            return prefix, name
    raise PackageRejected("ARCHIVE_PATH_SPLIT_INVALID")


def _canonical_path(path: str) -> bool:
    if not path or path.startswith("/") or "\\" in path or re.match(r"^[A-Za-z]:", path):
        return False
    try:
        encoded = path.encode("utf-8", errors="strict")
    except UnicodeEncodeError:
        return False
    if len(encoded) > 255:
        return False
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return False
    # Core Contract V1's existing ASCII member-name allowlist remains in force.
    if any(not re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts):
        return False
    if len(parts[-1].encode("utf-8")) > 100:
        return False
    if len(parts) > 1 and len("/".join(parts[:-1]).encode("utf-8")) > 155:
        return False
    return path == "manifest.json" or path == "README.md" or (
        path.startswith("engine/") and path.endswith(".py")
    )


def _field_text(raw: bytes, code: str) -> str:
    content, separator, suffix = raw.partition(b"\0")
    if separator and any(suffix):
        raise PackageRejected(code)
    try:
        return content.decode("ascii")
    except UnicodeDecodeError as exc:
        raise PackageRejected("ARCHIVE_PATH_ENCODING_INVALID" if code == "ARCHIVE_PATH_INVALID" else code) from exc


def _octal(raw: bytes) -> int:
    if not raw or raw[-1:] != b"\0" or any(byte not in b"01234567" for byte in raw[:-1]):
        raise PackageRejected("ARCHIVE_MALFORMED")
    value = int(raw[:-1], 8)
    if raw[:-1] != format(value, f"0{len(raw) - 1}o").encode("ascii"):
        raise PackageRejected("ARCHIVE_NON_CANONICAL")
    return value


def _header_path(header: bytes) -> str:
    name = _field_text(header[0:100], "ARCHIVE_PATH_INVALID")
    prefix = _field_text(header[345:500], "ARCHIVE_PATH_INVALID")
    if not name:
        raise PackageRejected("ARCHIVE_PATH_INVALID")
    path = f"{prefix}/{name}" if prefix else name
    if not _canonical_path(path):
        raise PackageRejected("ARCHIVE_PATH_INVALID")
    expected_prefix, expected_name = canonical_ustar_split(path)
    actual_prefix = prefix.encode("ascii")
    actual_name = name.encode("ascii")
    if (actual_prefix, actual_name) != (expected_prefix, expected_name):
        raise PackageRejected("ARCHIVE_PATH_SPLIT_NONCANONICAL")
    return path


def _parse_archive(data: bytes, limits: PackageLimits) -> dict[str, bytes]:
    if len(data) > limits.max_archive_bytes:
        raise PackageRejected("PACKAGE_TOO_LARGE")
    if len(data) < 1024 or len(data) % 512:
        raise PackageRejected("ARCHIVE_MALFORMED")

    files: dict[str, bytes] = {}
    folded: set[str] = set()
    pos = 0
    total_size = 0
    last_path: bytes | None = None
    while pos + 512 <= len(data):
        header = data[pos : pos + 512]
        if header == bytes(512):
            if pos + 1024 != len(data) or data[pos + 512 : pos + 1024] != bytes(512):
                raise PackageRejected("ARCHIVE_TRAILING_DATA")
            return files

        if header[257:263] != b"ustar\0" or header[263:265] != b"00":
            raise PackageRejected("ARCHIVE_FORMAT_UNSUPPORTED")
        typeflag = header[156:157]
        if typeflag != b"0":
            if typeflag in (b"1", b"2"):
                raise PackageRejected("ARCHIVE_LINK_FORBIDDEN")
            if typeflag in (b"3", b"4", b"5", b"6"):
                raise PackageRejected("ARCHIVE_SPECIAL_FILE_FORBIDDEN")
            if typeflag in (b"L", b"K", b"x", b"g"):
                raise PackageRejected("ARCHIVE_EXTENSION_FORBIDDEN")
            raise PackageRejected("ARCHIVE_TYPE_FORBIDDEN")
        if any(header[500:512]) or any(header[157:257]) or any(header[329:345]):
            raise PackageRejected("ARCHIVE_NON_CANONICAL")
        if _field_text(header[265:297], "ARCHIVE_NON_CANONICAL") or _field_text(
            header[297:329], "ARCHIVE_NON_CANONICAL"
        ):
            raise PackageRejected("ARCHIVE_NON_CANONICAL")

        checksum_field = header[148:156]
        if checksum_field[6:] != b"\0 " or any(byte not in b"01234567" for byte in checksum_field[:6]):
            raise PackageRejected("ARCHIVE_MALFORMED")
        expected_checksum = int(checksum_field[:6], 8)
        checksum_bytes = bytearray(header)
        checksum_bytes[148:156] = b" " * 8
        if sum(checksum_bytes) != expected_checksum:
            raise PackageRejected("ARCHIVE_CHECKSUM_INVALID")

        mode = _octal(header[100:108])
        uid = _octal(header[108:116])
        gid = _octal(header[116:124])
        size = _octal(header[124:136])
        mtime = _octal(header[136:148])
        if mode != 0o644 or uid or gid or mtime:
            raise PackageRejected("ARCHIVE_NON_CANONICAL")
        path = _header_path(header)
        path_bytes = path.encode("ascii")
        folded_name = path.casefold()
        if folded_name in folded:
            raise PackageRejected("ARCHIVE_DUPLICATE_MEMBER")
        folded.add(folded_name)
        if any(path.startswith(existing + "/") or existing.startswith(path + "/") for existing in files):
            raise PackageRejected("ARCHIVE_PATH_CONFLICT")
        if last_path is not None and path_bytes < last_path:
            raise PackageRejected("ARCHIVE_MEMBER_ORDER_INVALID")
        last_path = path_bytes
        if len(files) >= limits.max_members:
            raise PackageRejected("ARCHIVE_TOO_MANY_MEMBERS")
        if size > limits.max_member_bytes:
            raise PackageRejected("ARCHIVE_MEMBER_TOO_LARGE")
        total_size += size
        if total_size > limits.max_total_bytes:
            raise PackageRejected("ARCHIVE_CONTENT_TOO_LARGE")

        start = pos + 512
        end = start + size
        padded_end = start + ((size + 511) // 512) * 512
        if end > len(data) or padded_end > len(data):
            raise PackageRejected("ARCHIVE_TRUNCATED")
        if any(data[end:padded_end]):
            raise PackageRejected("ARCHIVE_NON_CANONICAL")
        files[path] = data[start:end]
        pos = padded_end

    raise PackageRejected("ARCHIVE_MALFORMED")


def _json_object(pairs: list[tuple[str, object]]) -> dict:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PackageRejected("MANIFEST_DUPLICATE_KEY", "VALIDATING")
        result[key] = value
    return result


def _reject_constant(_: str) -> None:
    raise PackageRejected("MANIFEST_INVALID_JSON", "VALIDATING")


def _manifest(files: dict[str, bytes], limits: PackageLimits) -> dict:
    raw = files.get("manifest.json")
    if raw is None:
        raise PackageRejected("MANIFEST_MISSING", "VALIDATING")
    if len(raw) > limits.max_manifest_bytes:
        raise PackageRejected("MANIFEST_TOO_LARGE", "VALIDATING")
    if raw.startswith(b"\xef\xbb\xbf"):
        raise PackageRejected("MANIFEST_BOM_FORBIDDEN", "VALIDATING")
    try:
        manifest = json.loads(
            raw.decode("utf-8", errors="strict"),
            object_pairs_hook=_json_object,
            parse_constant=_reject_constant,
        )
    except PackageRejected:
        raise
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as exc:
        raise PackageRejected("MANIFEST_INVALID_JSON", "VALIDATING") from exc
    if type(manifest) is not dict:
        raise PackageRejected("MANIFEST_OBJECT_REQUIRED", "VALIDATING")

    fields = {
        "contract_version", "protocol_version", "engine_name", "engine_version",
        "runtime", "entrypoint",
    }
    if set(manifest) - fields:
        raise PackageRejected("MANIFEST_UNKNOWN_FIELD", "VALIDATING")
    if fields - set(manifest):
        raise PackageRejected("MANIFEST_REQUIRED_FIELD_MISSING", "VALIDATING")
    if any(type(manifest[field]) is not str for field in fields):
        raise PackageRejected("MANIFEST_FIELD_TYPE_INVALID", "VALIDATING")
    if manifest["contract_version"] != "1":
        raise PackageRejected("UNSUPPORTED_CONTRACT_VERSION", "VALIDATING")
    if manifest["protocol_version"] != "aik-jsonl/1":
        raise PackageRejected("UNSUPPORTED_PROTOCOL_VERSION", "VALIDATING")
    if manifest["runtime"] != "python-3.12":
        raise PackageRejected("UNSUPPORTED_RUNTIME", "VALIDATING")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}", manifest["engine_name"]):
        raise PackageRejected("MANIFEST_ENGINE_NAME_INVALID", "VALIDATING")
    if len(manifest["engine_version"]) > 64 or not re.fullmatch(
        r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)(?:-[A-Za-z0-9.-]+)?",
        manifest["engine_version"],
    ):
        raise PackageRejected("MANIFEST_ENGINE_VERSION_INVALID", "VALIDATING")
    entrypoint = manifest["entrypoint"]
    if (
        len(entrypoint) < 10 or len(entrypoint) > 255 or not _canonical_path(entrypoint)
        or not entrypoint.startswith("engine/") or not entrypoint.endswith(".py")
        or entrypoint not in files
    ):
        raise PackageRejected("MANIFEST_ENTRYPOINT_INVALID", "VALIDATING")
    return manifest


def validate_submission_package(
    data: bytes, limits: PackageLimits = DEFAULT_LIMITS
) -> ValidatedPackage:
    """Validate the exact uploaded bytes without extraction or code execution."""
    files = _parse_archive(data, limits)
    if "manifest.json" not in files:
        raise PackageRejected("MANIFEST_MISSING", "VALIDATING")
    if "engine/main.py" not in files:
        raise PackageRejected("ENGINE_ENTRYPOINT_MISSING", "VALIDATING")
    for path, body in files.items():
        if path in {"manifest.json", "README.md"} or path.endswith(".py"):
            if body.startswith(b"\xef\xbb\xbf"):
                code = "MANIFEST_BOM_FORBIDDEN" if path == "manifest.json" else "SOURCE_BOM_FORBIDDEN"
                raise PackageRejected(code, "VALIDATING")
            try:
                body.decode("utf-8", errors="strict")
            except UnicodeDecodeError as exc:
                code = "MANIFEST_ENCODING_INVALID" if path == "manifest.json" else "SOURCE_ENCODING_INVALID"
                raise PackageRejected(code, "VALIDATING") from exc
    return ValidatedPackage(manifest=_manifest(files, limits), files=files)
