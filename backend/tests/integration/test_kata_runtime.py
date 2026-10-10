"""Opt-in Kata runtime test using only a generated, controlled protocol fixture.

This test never imports .env or reads participant records. To run it, explicitly
provide a locally available digest-pinned Python image and a delegated cgroup-v2
parent on the dedicated Linux worker, then set AIK_KATA_RUNTIME_TEST=1.
"""

from __future__ import annotations

import hashlib
import json
import os
import textwrap
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.sandbox.contract import (
    AIKTransport,
    CleanupResult,
    ExecutionIdentity,
    ExecutionSubjectKind,
    LeaseFenceAuthority,
    LeaseSnapshot,
    NetworkMode,
    SandboxPolicy,
    WritableStoragePolicy,
)
from app.sandbox.kata import KataRuntimeConfig, KataSandboxAdapter
from app.services.submission_validation import canonical_ustar_split


def _octal(value: int, width: int) -> bytes:
    return format(value, f"0{width - 1}o").encode("ascii") + b"\0"


def _header(path: str, data: bytes) -> bytes:
    prefix, name = canonical_ustar_split(path)
    result = bytearray(512)
    result[: len(name)] = name
    result[100:108] = _octal(0o644, 8)
    result[108:116] = _octal(0, 8)
    result[116:124] = _octal(0, 8)
    result[124:136] = _octal(len(data), 12)
    result[136:148] = _octal(0, 12)
    result[148:156] = b" " * 8
    result[156:157] = b"0"
    result[257:263] = b"ustar\0"
    result[263:265] = b"00"
    result[345 : 345 + len(prefix)] = prefix
    result[148:156] = f"{sum(result):06o}".encode("ascii") + b"\0 "
    return bytes(result)


def _fixture_archive() -> bytes:
    manifest = json.dumps(
        {
            "contract_version": "1",
            "protocol_version": "aik-jsonl/1",
            "engine_name": "Kata isolated fixture",
            "engine_version": "1.0.0",
            "runtime": "python-3.12",
            "entrypoint": "engine/main.py",
        },
        separators=(",", ":"),
    ).encode()
    engine = textwrap.dedent(
        """\
        import json, os, socket, sys

        for line in sys.stdin:
            message = json.loads(line)
            if message.get("type") == "hello":
                route = open("/proc/net/route").read()
                status = open("/proc/self/status").read()
                cap_eff = next(
                    row.split()[1] for row in status.splitlines() if row.startswith("CapEff:")
                )
                connection = socket.socket()
                connection.settimeout(0.2)
                network_rc = connection.connect_ex(("1.1.1.1", 53))
                connection.close()
                probe = {
                    "pid": os.getpid(),
                    "route": route,
                    "network_rc": network_rc,
                    "cap_eff": cap_eff,
                    "project": os.path.exists("/mnt/c/TECH_EVENT/AI_KNOCKOUT"),
                    "ctr_socket": os.path.exists("/run/containerd/containerd.sock"),
                    "docker_socket": os.path.exists("/var/run/docker.sock"),
                    "kvm": os.path.exists("/dev/kvm"),
                    "vsock": os.path.exists("/dev/vhost-vsock"),
                }
                print(json.dumps(probe), file=sys.stderr, flush=True)
                response = {
                    "protocol_version": "aik-jsonl/1",
                    "type": "ready",
                    "request_id": message["request_id"],
                }
                print(json.dumps(response, separators=(",", ":")), flush=True)
        """
    ).encode("ascii")
    files = [("manifest.json", manifest), ("engine/main.py", engine)]
    archive = bytearray()
    for path, body in sorted(files, key=lambda item: item[0].encode("ascii")):
        archive.extend(_header(path, body))
        archive.extend(body)
        archive.extend(bytes((-len(body)) % 512))
    archive.extend(bytes(1024))
    return bytes(archive)


@pytest.mark.integration
def test_real_kata_process_and_isolation_controls() -> None:
    if os.environ.get("AIK_KATA_RUNTIME_TEST") != "1":
        pytest.skip("explicit dedicated Linux Kata runtime opt-in required")
    required = {
        key: os.environ.get(key)
        for key in (
            "AIK_KATA_CTR",
            "AIK_KATA_SOCKET",
            "AIK_KATA_NAMESPACE",
            "AIK_KATA_BASE_IMAGE",
            "AIK_KATA_BASE_MANIFEST_DIGEST",
            "AIK_KATA_CGROUP_PARENT",
        )
    }
    if any(not value for value in required.values()):
        pytest.fail("Kata integration opt-in requires all explicit AIK_KATA_* runtime settings")
    policy = SandboxPolicy(
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
    policy_digest = hashlib.sha256(
        json.dumps(policy.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    artifact = _fixture_archive()
    now = datetime.now(timezone.utc)
    identity = ExecutionIdentity(
        execution_id=uuid.uuid4(),
        submission_version_id=uuid.uuid4(),
        artifact_digest=hashlib.sha256(artifact).hexdigest(),
        policy_profile="participant-v1",
        runtime_profile="python-3.12",
        worker_id="kata-integration-worker",
        lease_id=uuid.uuid4(),
        fencing_token=1,
        protocol_version="aik-jsonl/1",
        lease_expires_at=now + timedelta(minutes=20),
        subject_kind=ExecutionSubjectKind.PARTICIPANT,
        match_id=uuid.uuid4(),
        policy_digest=policy_digest,
    )
    authority = LeaseFenceAuthority()
    authority.set_current(
        LeaseSnapshot(
            identity.worker_id, identity.lease_id, identity.fencing_token, identity.lease_expires_at
        )
    )
    config = KataRuntimeConfig(
        ctr_binary=Path(required["AIK_KATA_CTR"]),
        containerd_address=Path(required["AIK_KATA_SOCKET"]),
        namespace=required["AIK_KATA_NAMESPACE"],
        base_image=required["AIK_KATA_BASE_IMAGE"],
        base_manifest_digest=required["AIK_KATA_BASE_MANIFEST_DIGEST"],
        cgroup_parent=Path(required["AIK_KATA_CGROUP_PARENT"]),
    )
    adapter = KataSandboxAdapter(authority, config, eligibility_check=lambda _: True)
    adapter.create_execution(identity, policy)
    staged = False
    try:
        adapter.stage_artifact(
            identity.execution_id,
            artifact,
            identity.submission_version_id,
            identity.artifact_digest,
        )
        staged = True
        adapter.start(identity.execution_id)
        transport = AIKTransport()
        hello = transport.encode_controller_message(
            "kata-real-hello",
            {
                "protocol_version": "aik-jsonl/1",
                "type": "hello",
                "match_id": str(identity.match_id),
                "side": "WHITE",
                "runtime": "python-3.12",
            },
        )
        adapter.send_input(identity.execution_id, hello)
        deadline = time.monotonic() + 5
        output = None
        while time.monotonic() < deadline and output is None:
            output = adapter.read_output(identity.execution_id)
            if output is None:
                time.sleep(0.005)
        assert output is not None
        assert transport.parse_stdout(output)["type"] == "ready"
        adapter.terminate(identity.execution_id)
        assert adapter.verify_cleanup(identity.execution_id) == CleanupResult.VERIFIED
        evidence = adapter.collect_evidence(identity.execution_id)
        probe = json.loads(evidence.stderr_evidence.decode("utf-8").splitlines()[0])
        assert probe["pid"] == 1
        assert probe["network_rc"] != 0
        assert not any(
            row.split()[1] == "00000000"
            for row in probe["route"].splitlines()[1:]
            if len(row.split()) > 1
        )
        assert int(probe["cap_eff"], 16) == 0
        assert probe["project"] is False
        assert probe["ctr_socket"] is False
        assert probe["docker_socket"] is False
        assert probe["kvm"] is False
        assert probe["vsock"] is False
        assert adapter.destroy(identity.execution_id).state.value == "DESTROYED"
    finally:
        if staged:
            current = adapter.inspect(identity.execution_id)
            if current.state.value not in {"DESTROYED", "CLEANUP_FAILED", "CLEANUP_UNKNOWN"}:
                adapter.terminate(identity.execution_id)
                adapter.verify_cleanup(identity.execution_id)
            try:
                adapter.destroy(identity.execution_id)
            except Exception:
                pass
