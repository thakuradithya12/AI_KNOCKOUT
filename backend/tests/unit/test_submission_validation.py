"""Adversarial tests for canonical USTAR paths and manifest intake."""

from __future__ import annotations

import json

import pytest

from app.services.submission_validation import (
    PackageLimits,
    PackageRejected,
    _canonical_path,
    canonical_ustar_split,
    validate_submission_package,
)


def _octal(value: int, width: int) -> bytes:
    return format(value, f"0{width - 1}o").encode("ascii") + b"\0"


def _header(
    path: str,
    size: int,
    *,
    split: tuple[bytes, bytes] | None = None,
    typeflag: bytes = b"0",
    magic: bytes = b"ustar\0",
) -> bytes:
    if split is None:
        prefix, name = canonical_ustar_split(path)
    else:
        prefix, name = split
    block = bytearray(512)
    block[: len(name)] = name
    block[100:108] = _octal(0o644, 8)
    block[108:116] = _octal(0, 8)
    block[116:124] = _octal(0, 8)
    block[124:136] = _octal(size, 12)
    block[136:148] = _octal(0, 12)
    block[148:156] = b" " * 8
    block[156:157] = typeflag
    block[257:263] = magic
    block[263:265] = b"00"
    block[345 : 345 + len(prefix)] = prefix
    checksum = sum(block)
    block[148:156] = f"{checksum:06o}".encode("ascii") + b"\0 "
    return bytes(block)


def _tar(
    members: list[tuple[str, bytes]],
    *,
    overrides: dict[str, tuple[bytes, bytes]] | None = None,
    typeflags: dict[str, bytes] | None = None,
) -> bytes:
    output = bytearray()
    for path, content in sorted(members, key=lambda item: item[0].encode("utf-8")):
        split = (overrides or {}).get(path)
        flag = (typeflags or {}).get(path, b"0")
        output.extend(_header(path, len(content), split=split, typeflag=flag))
        output.extend(content)
        output.extend(bytes((-len(content)) % 512))
    output.extend(bytes(1024))
    return bytes(output)


def _manifest(**changes: str) -> bytes:
    data = {
        "contract_version": "1",
        "protocol_version": "aik-jsonl/1",
        "engine_name": "Test Engine",
        "engine_version": "1.0.0",
        "runtime": "python-3.12",
        "entrypoint": "engine/main.py",
    }
    data.update(changes)
    return json.dumps(data, separators=(",", ":")).encode("utf-8")


def _valid_tar(manifest: bytes | None = None, source: bytes = b"# static only\n") -> bytes:
    members = [
        ("manifest.json", manifest or _manifest()),
        ("engine/main.py", source),
    ]
    members.sort(key=lambda item: item[0].encode("ascii"))
    return _tar(members)


@pytest.mark.parametrize(
    ("path", "prefix", "name"),
    [
        ("a" * 99, b"", b"a" * 99),
        ("a" * 100, b"", b"a" * 100),
        ("a/" + "b" * 99, b"a", b"b" * 99),
        ("p" * 70 + "/" + "n" * 40, b"p" * 70, b"n" * 40),
        # Both slashes can split within USTAR limits; only the rightmost is canonical.
        ("p" * 120 + "/" + "m" * 20 + "/" + "n" * 60,
         b"p" * 120 + b"/" + b"m" * 20, b"n" * 60),
        ("p" * 155 + "/" + "n" * 99, b"p" * 155, b"n" * 99),
        ("p" * 154 + "/" + "n" * 100, b"p" * 154, b"n" * 100),
    ],
)
def test_canonical_ustar_split(path: str, prefix: bytes, name: bytes) -> None:
    assert canonical_ustar_split(path) == (prefix, name)


def test_utf8_lengths_are_bytes_and_existing_ascii_allowlist_rejects_multibyte_path() -> None:
    path = "é" * 51 + "/a"
    prefix, name = canonical_ustar_split(path)
    assert len(path) == 53
    assert len(path.encode("utf-8")) == 104
    assert len(prefix) == 102
    assert name == b"a"
    assert not _canonical_path(path)


@pytest.mark.parametrize(
    "path",
    [
        "x" * 101,  # no separator and an overlong name
        "p" * 156 + "/a",  # no legal prefix
        "a/" + "n" * 101,  # no legal name
        "p" * 256,  # total path byte limit
    ],
)
def test_no_legal_ustar_split_is_rejected(path: str) -> None:
    with pytest.raises(PackageRejected):
        canonical_ustar_split(path)


@pytest.mark.parametrize("path", ["../manifest.json", "/manifest.json", "C:/engine/main.py", "engine\\main.py", "engine//main.py", "engine/./main.py", "engine/../main.py"])
def test_noncanonical_paths_are_rejected(path: str) -> None:
    assert not _canonical_path(path)


def test_noncanonical_alternate_split_is_rejected() -> None:
    path = "engine/" + "p" * 100 + "/" + "m" * 20 + "/" + "n" * 30 + ".py"
    noncanonical = (b"engine/" + b"p" * 100, b"m" * 20 + b"/" + b"n" * 30 + b".py")
    archive = _tar([("manifest.json", _manifest()), (path, b"x"), ("engine/main.py", b"x")], overrides={path: noncanonical})
    with pytest.raises(PackageRejected, match="ARCHIVE_PATH_SPLIT_NONCANONICAL"):
        validate_submission_package(archive)


def test_valid_long_engine_path_uses_rightmost_prefix_name_split() -> None:
    path = "engine/" + "p" * 100 + "/" + "m" * 20 + "/" + "n" * 30 + ".py"
    archive = _tar(
        [
            ("manifest.json", _manifest(entrypoint=path)),
            ("engine/main.py", b"x"),
            (path, b"module = True\n"),
        ]
    )
    assert validate_submission_package(archive).manifest["entrypoint"] == path


def test_member_with_multibyte_utf8_path_is_rejected_not_normalized() -> None:
    unicode_path = "é" * 51 + "/a"
    prefix, name = canonical_ustar_split(unicode_path)
    archive = _tar(
        [("manifest.json", _manifest()), ("engine/main.py", b"x"), (unicode_path, b"x")],
        overrides={unicode_path: (prefix, name)},
    )
    with pytest.raises(PackageRejected, match="ARCHIVE_PATH_ENCODING_INVALID"):
        validate_submission_package(archive)


@pytest.mark.parametrize("extension", [b"L", b"K", b"x", b"g"])
def test_pax_and_gnu_long_name_extensions_are_rejected(extension: bytes) -> None:
    archive = _tar(
        [("manifest.json", _manifest()), ("engine/main.py", b"x")],
        typeflags={"manifest.json": extension},
    )
    with pytest.raises(PackageRejected, match="ARCHIVE_EXTENSION_FORBIDDEN"):
        validate_submission_package(archive)


def test_valid_package_is_parsed_without_execution() -> None:
    result = validate_submission_package(_valid_tar(source=b"raise RuntimeError('never run')\n"))
    assert result.manifest["protocol_version"] == "aik-jsonl/1"
    assert result.files["engine/main.py"].startswith(b"raise RuntimeError")


@pytest.mark.parametrize(
    ("archive", "code"),
    [
        (_tar([("manifest.json", _manifest()), ("engine/main.py", b"x"), ("../escape.py", b"x")]), "ARCHIVE_PATH_INVALID"),
        (_tar([("manifest.json", _manifest()), ("engine/main.py", b"x"), ("/absolute.py", b"x")]), "ARCHIVE_PATH_INVALID"),
        (_tar([("manifest.json", _manifest()), ("engine/main.py", b"x"), ("C:/escape.py", b"x")]), "ARCHIVE_PATH_INVALID"),
        (_tar([("manifest.json", _manifest()), ("engine/main.py", b"x")], typeflags={"engine/main.py": b"2"}), "ARCHIVE_LINK_FORBIDDEN"),
        (_tar([("manifest.json", _manifest()), ("engine/main.py", b"x")], typeflags={"engine/main.py": b"1"}), "ARCHIVE_LINK_FORBIDDEN"),
        (_tar([("manifest.json", _manifest()), ("engine/main.py", b"x")], typeflags={"engine/main.py": b"3"}), "ARCHIVE_SPECIAL_FILE_FORBIDDEN"),
    ],
)
def test_hostile_path_and_member_types_are_rejected(archive: bytes, code: str) -> None:
    with pytest.raises(PackageRejected, match=code):
        validate_submission_package(archive)


def test_duplicate_members_are_rejected() -> None:
    archive = _tar([("manifest.json", _manifest()), ("engine/main.py", b"x"), ("engine/main.py", b"y")])
    with pytest.raises(PackageRejected, match="ARCHIVE_DUPLICATE_MEMBER"):
        validate_submission_package(archive)


def test_case_conflicting_members_are_rejected() -> None:
    archive = _tar([("manifest.json", _manifest()), ("engine/Main.py", b"x"), ("engine/main.py", b"y")])
    with pytest.raises(PackageRejected, match="ARCHIVE_DUPLICATE_MEMBER"):
        validate_submission_package(archive)


def test_conflicting_file_and_parent_path_are_rejected() -> None:
    archive = _tar([
        ("manifest.json", _manifest()),
        ("engine/foo.py", b"x"),
        ("engine/foo.py/bar.py", b"y"),
        ("engine/main.py", b"z"),
    ])
    with pytest.raises(PackageRejected, match="ARCHIVE_PATH_CONFLICT"):
        validate_submission_package(archive)


def test_bad_checksum_malformed_and_truncated_archives_are_rejected() -> None:
    good = _valid_tar()
    bad_checksum = bytearray(good)
    bad_checksum[0] ^= 1
    for candidate in (bytes(bad_checksum), good[:-512], b"not a tar"):
        with pytest.raises(PackageRejected):
            validate_submission_package(candidate)


def test_archive_size_member_count_and_member_size_are_bounded() -> None:
    good = _valid_tar()
    with pytest.raises(PackageRejected, match="PACKAGE_TOO_LARGE"):
        validate_submission_package(good, PackageLimits(max_archive_bytes=len(good) - 1))
    many = _tar([("manifest.json", _manifest()), ("engine/main.py", b"x"), ("engine/a.py", b"x")])
    with pytest.raises(PackageRejected, match="ARCHIVE_TOO_MANY_MEMBERS"):
        validate_submission_package(many, PackageLimits(max_members=2))
    large = _tar([("manifest.json", _manifest()), ("engine/main.py", b"x" * 10)])
    with pytest.raises(PackageRejected, match="ARCHIVE_MEMBER_TOO_LARGE"):
        validate_submission_package(large, PackageLimits(max_member_bytes=5))


@pytest.mark.parametrize(
    ("manifest", "code"),
    [
        (_manifest(contract_version="2"), "UNSUPPORTED_CONTRACT_VERSION"),
        (_manifest(protocol_version="uci"), "UNSUPPORTED_PROTOCOL_VERSION"),
        (_manifest(runtime="python-3.10"), "UNSUPPORTED_RUNTIME"),
        (_manifest(cpu_limit="999"), "MANIFEST_UNKNOWN_FIELD"),
        (b'{"contract_version":"1","contract_version":"1"}', "MANIFEST_DUPLICATE_KEY"),
        (b"{invalid", "MANIFEST_INVALID_JSON"),
        (b"\xef\xbb\xbf" + _manifest(), "MANIFEST_BOM_FORBIDDEN"),
    ],
)
def test_strict_manifest_rejections(manifest: bytes, code: str) -> None:
    with pytest.raises(PackageRejected, match=code):
        validate_submission_package(_valid_tar(manifest=manifest))
