"""Deterministic Kata adapter tests; these do not claim runtime isolation."""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.sandbox.contract import (
    CleanupResult,
    ExecutionIdentity,
    ExecutionState,
    FailureClass,
    NetworkMode,
    SandboxPolicy,
    WritableStoragePolicy,
)
from app.sandbox.kata import KataRuntimeConfig, KataSandboxAdapter, _Execution


def _policy(**changes) -> SandboxPolicy:
    values = dict(
        cpu_millicores=1000,
        memory_bytes=268_435_456,
        pid_limit=16,
        filesystem_bytes=33_554_432,
        filesystem_inodes=128,
        file_descriptors=64,
        stdout_bytes=1_048_576,
        stderr_bytes=262_144,
        startup_timeout_ms=5000,
        move_timeout_ms=2000,
        total_timeout_ms=900_000,
        network_mode=NetworkMode.DENY_ALL,
        writable_storage=WritableStoragePolicy.PRIVATE_TMPFS,
    )
    values.update(changes)
    return SandboxPolicy(**values)


def _adapter(tmp_path: Path) -> KataSandboxAdapter:
    manifest = "a" * 64
    config = KataRuntimeConfig(
        ctr_binary=tmp_path / "ctr",
        containerd_address=tmp_path / "containerd.sock",
        namespace="kata-test",
        base_image=f"registry.invalid/python@sha256:{'b' * 64}",
        base_manifest_digest=manifest,
        staging_parent=tmp_path,
    )
    return KataSandboxAdapter(authority=object(), config=config)


def _blob(data: bytes) -> tuple[str, dict]:
    digest = hashlib.sha256(data).hexdigest()
    return digest, {
        "mediaType": "application/vnd.oci.image.config.v1+json",
        "digest": f"sha256:{digest}",
        "size": len(data),
    }


def _base_oci(path: Path) -> str:
    config = {
        "architecture": "amd64",
        "os": "linux",
        "config": {"Env": ["SHOULD_NOT_LEAK=value"], "Entrypoint": ["old"]},
        "rootfs": {"type": "layers", "diff_ids": []},
        "history": [],
    }
    config_data = json.dumps(config, separators=(",", ":")).encode()
    config_digest, config_desc = _blob(config_data)
    layer = io.BytesIO()
    with tarfile.open(fileobj=layer, mode="w"):
        pass
    layer_data = layer.getvalue()
    layer_digest = hashlib.sha256(layer_data).hexdigest()
    manifest = {
        "schemaVersion": 2,
        "mediaType": "application/vnd.oci.image.manifest.v1+json",
        "config": config_desc,
        "layers": [
            {
                "mediaType": "application/vnd.oci.image.layer.v1.tar",
                "digest": f"sha256:{layer_digest}",
                "size": len(layer_data),
            }
        ],
    }
    manifest_data = json.dumps(manifest, separators=(",", ":")).encode()
    manifest_digest = hashlib.sha256(manifest_data).hexdigest()
    index = {
        "schemaVersion": 2,
        "manifests": [
            {
                "mediaType": "application/vnd.oci.image.manifest.v1+json",
                "digest": f"sha256:{manifest_digest}",
                "size": len(manifest_data),
                "platform": {"architecture": "amd64", "os": "linux"},
            }
        ],
    }
    blobs = {
        f"blobs/sha256/{config_digest}": config_data,
        f"blobs/sha256/{layer_digest}": layer_data,
        f"blobs/sha256/{manifest_digest}": manifest_data,
    }
    with tarfile.open(path, "w:") as archive:
        for name, data in [
            ("oci-layout", b'{"imageLayoutVersion":"1.0.0"}'),
            ("index.json", json.dumps(index).encode()),
            *blobs.items(),
        ]:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            info.mode = 0o444
            archive.addfile(info, io.BytesIO(data))
    return manifest_digest


def test_locked_policy_is_exact() -> None:
    assert KataSandboxAdapter._locked_policy(_policy())
    assert not KataSandboxAdapter._locked_policy(_policy(move_timeout_ms=2001))


def test_sandbox_uid_uses_full_execution_identity() -> None:
    first = uuid.UUID("00000100-0000-0000-0000-000000000001")
    second = uuid.UUID("000001ff-0000-0000-0000-000000000001")

    assert first.hex[:6] == second.hex[:6]
    assert KataSandboxAdapter._sandbox_uid(first) != KataSandboxAdapter._sandbox_uid(second)


def test_runtime_configuration_requires_digest_pinned_python_image(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="DIGEST_PINNED"):
        KataRuntimeConfig(
            ctr_binary=tmp_path / "ctr",
            containerd_address=tmp_path / "containerd.sock",
            namespace="kata-test",
            base_image="registry.invalid/python:latest",
            base_manifest_digest="a" * 64,
        )


def test_artifact_layer_contains_only_safe_readonly_files(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    layer = adapter._artifact_layer(
        {"manifest.json": b"{}", "engine/main.py": b"print('fixture')\n"},
        "engine/main.py",
    )
    with tarfile.open(fileobj=io.BytesIO(layer), mode="r:") as archive:
        members = {entry.name: entry for entry in archive.getmembers()}
        assert "opt/aik/artifact/engine/main.py" in members
        assert members["opt/aik/artifact/engine/main.py"].mode == 0o444
        assert not any(
            entry.issym() or entry.islnk() or entry.isdev() for entry in members.values()
        )
        assert (
            archive.extractfile("opt/aik/artifact/engine/main.py").read() == b"print('fixture')\n"
        )
        launcher = archive.extractfile("opt/aik/launcher.py").read()
        assert b"RLIMIT_NOFILE,(64,64)" in launcher
        assert b"RLIMIT_NPROC,(16,16)" in launcher


@pytest.mark.parametrize(
    "path", ["../escape.py", "/tmp/escape.py", "engine/../../x.py", "engine\\main.py"]
)
def test_artifact_layer_rejects_noncanonical_paths(tmp_path: Path, path: str) -> None:
    with pytest.raises(Exception, match="ARTIFACT_PATH_REJECTED"):
        _adapter(tmp_path)._artifact_layer({path: b"bad"}, "engine/main.py")


def test_oci_derivation_preserves_base_identity_and_scrubs_base_environment(tmp_path: Path) -> None:
    adapter = _adapter(tmp_path)
    base = tmp_path / "base.tar"
    output = tmp_path / "derived.tar"
    pinned_manifest = _base_oci(base)
    adapter._config = KataRuntimeConfig(
        ctr_binary=tmp_path / "ctr",
        containerd_address=tmp_path / "containerd.sock",
        namespace="kata-test",
        base_image=f"registry.invalid/python@sha256:{pinned_manifest}",
        base_manifest_digest=pinned_manifest,
        staging_parent=tmp_path,
    )
    adapter._build_derived_image(
        base,
        output,
        {"manifest.json": b"{}", "engine/main.py": b"print('fixture')\n"},
        "engine/main.py",
        "localhost/aik-execution:test",
    )
    with tarfile.open(output, "r:") as archive:
        index = json.load(archive.extractfile("index.json"))
        desc = index["manifests"][0]
        manifest_blob = archive.extractfile(f"blobs/sha256/{desc['digest'][7:]}").read()
        manifest = json.loads(manifest_blob)
        cfg_desc = manifest["config"]
        config = json.load(archive.extractfile(f"blobs/sha256/{cfg_desc['digest'][7:]}"))
        assert config["config"]["User"] == "65532:65532"
        assert config["config"]["Env"] == [
            "PATH=/usr/local/bin:/usr/bin:/bin",
            "LANG=C.UTF-8",
            "TZ=UTC",
            "PYTHONHASHSEED=0",
            "PYTHONDONTWRITEBYTECODE=1",
        ]
        assert "SHOULD_NOT_LEAK=value" not in config["config"]["Env"]
        assert config["config"]["Entrypoint"] == ["/usr/local/bin/python3", "/opt/aik/launcher.py"]
        assert len(config["rootfs"]["diff_ids"]) == 1
        assert len(manifest["layers"]) == 2


def test_exit_watcher_does_not_overwrite_supervisor_termination(tmp_path: Path) -> None:
    class ExitedProcess:
        def wait(self):
            return -9

    identity = ExecutionIdentity(
        execution_id=uuid.uuid4(),
        submission_version_id=uuid.uuid4(),
        artifact_digest="a" * 64,
        policy_profile="participant-v1",
        runtime_profile="python-3.12",
        worker_id="worker-test",
        lease_id=uuid.uuid4(),
        fencing_token=1,
        protocol_version="aik-jsonl/1",
        lease_expires_at=datetime.now(timezone.utc),
    )
    record = _Execution(
        identity=identity,
        policy=_policy(),
        state=ExecutionState.TERMINATED,
        cleanup=CleanupResult.VERIFIED,
        failure=FailureClass.STALE_EXECUTION,
        termination_reason="FENCING_TOKEN_STALE",
        process=ExitedProcess(),
        lock=threading.RLock(),
    )
    _adapter(tmp_path)._watch_exit(record)
    assert record.state == ExecutionState.TERMINATED
    assert record.failure == FailureClass.STALE_EXECUTION
    assert record.termination_reason == "FENCING_TOKEN_STALE"
