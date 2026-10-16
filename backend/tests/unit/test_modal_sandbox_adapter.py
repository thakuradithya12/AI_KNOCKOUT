"""Unit and integration tests for ModalSandboxAdapter.

Covers:
1. ModalRuntimeConfig defaults and policy bounds (fail-closed on non-isolated configurations).
2. End-to-end sandbox lifecycle (create, stage, start, exchange, terminate, cleanup, destroy).
3. Digest mismatch and malformed artifact rejection.
4. Move timeout and execution timeout classification.
5. Integration with KataMatchParticipant bridge.
6. Integration with WorkerAgent across KATA_AI_EXECUTION and MODAL_AI_EXECUTION modes.
7. Fail-fast behavior when live cloud sandbox is requested without prerequisites.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.matches.kata_participant import KataMatchParticipant
from app.sandbox.contract import (
    AIKMessage,
    CleanupResult,
    ExecutionIdentity,
    ExecutionState,
    ExecutionSubjectKind,
    FailureClass,
    IdentityMismatchError,
    LeaseFenceAuthority,
    LeaseSnapshot,
    NetworkMode,
    SandboxPolicy,
    WritableStoragePolicy,
)
from app.sandbox.modal import (
    ModalRuntimeConfig,
    ModalRuntimeError,
    ModalSandboxAdapter,
)
from app.workers.agent import WorkerAgent
from app.workers.schemas import JobOut, LeaseOut, ResourcePolicy
from tests.fixtures.trusted_engine.builder import build_trusted_engine_archive


def _sample_policy() -> SandboxPolicy:
    return SandboxPolicy(
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


def _policy_digest(policy: SandboxPolicy) -> str:
    return hashlib.sha256(
        json.dumps(policy.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _sample_identity(
    artifact_digest: str,
    policy: SandboxPolicy,
    execution_id: uuid.UUID | None = None,
    lease_id: uuid.UUID | None = None,
) -> ExecutionIdentity:
    p_digest = _policy_digest(policy)
    return ExecutionIdentity(
        execution_id=execution_id or uuid.uuid4(),
        submission_version_id=uuid.uuid4(),
        artifact_digest=artifact_digest,
        policy_profile="development-v1",
        runtime_profile="python-3.12",
        worker_id="worker-001",
        lease_id=lease_id or uuid.uuid4(),
        fencing_token=1,
        protocol_version="aik-jsonl/1",
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        subject_kind=ExecutionSubjectKind.PARTICIPANT,
        match_id=uuid.uuid4(),
        policy_digest=p_digest,
        sandbox_policy_digest=p_digest,
    )


def test_modal_config_defaults_and_policy_bounds() -> None:
    config = ModalRuntimeConfig()
    assert config.mock_mode is True
    assert config.sandbox_enabled is False
    assert config.execution_mode == "mock"
    assert config.block_network is True

    # Reject non-DENY_ALL network mode
    bad_policy_dict = _sample_policy().model_dump()
    with pytest.raises(Exception):
        # SandboxPolicy pydantic validator enforces NetworkMode.DENY_ALL
        SandboxPolicy(**{**bad_policy_dict, "network_mode": "ALLOW_ALL"})


def test_modal_network_isolation_cannot_be_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValueError, match="MODAL_NETWORK_ISOLATION_REQUIRED"):
        ModalRuntimeConfig(block_network=False)

    monkeypatch.setenv("AIK_MODAL_BLOCK_NETWORK", "false")
    with pytest.raises(ValueError, match="MODAL_NETWORK_ISOLATION_REQUIRED"):
        ModalRuntimeConfig.from_env()


def test_modal_adapter_lifecycle_in_mock_mode() -> None:
    authority = LeaseFenceAuthority()
    policy = _sample_policy()
    archive = build_trusted_engine_archive()
    digest = hashlib.sha256(archive).hexdigest()
    identity = _sample_identity(digest, policy)

    authority.set_current(
        LeaseSnapshot(
            worker_id=identity.worker_id,
            lease_id=identity.lease_id,
            fencing_token=identity.fencing_token,
            expires_at=identity.lease_expires_at,
        )
    )

    adapter = ModalSandboxAdapter(authority, ModalRuntimeConfig(mock_mode=True))

    # 1. Create Execution
    snap1 = adapter.create_execution(identity, policy)
    assert snap1.state == ExecutionState.CREATED
    assert snap1.identity == identity

    # 2. Stage Artifact
    staged = adapter.stage_artifact(
        identity.execution_id, archive, identity.submission_version_id, digest
    )
    assert staged.digest == digest
    snap2 = adapter.inspect(identity.execution_id)
    assert snap2.state == ExecutionState.STAGED

    # 3. Start Execution
    snap3 = adapter.start(identity.execution_id)
    assert snap3.state == ExecutionState.RUNNING

    # 4. Exchange AIK protocol messages
    # Hello handshake
    req_hello = str(uuid.uuid4())
    msg_hello = AIKMessage(
        req_hello,
        json.dumps(
            {"protocol_version": "aik-jsonl/1", "type": "hello", "request_id": req_hello}
        ).encode("utf-8")
        + b"\n",
    )
    adapter.send_input(identity.execution_id, msg_hello)
    resp_hello = adapter.read_output(identity.execution_id)
    assert resp_hello is not None
    assert resp_hello.message_id == req_hello
    parsed_hello = json.loads(resp_hello.payload.decode("utf-8"))
    assert parsed_hello["type"] == "ready"

    # New game handshake
    req_game = str(uuid.uuid4())
    msg_game = AIKMessage(
        req_game,
        json.dumps(
            {"protocol_version": "aik-jsonl/1", "type": "new_game", "request_id": req_game}
        ).encode("utf-8")
        + b"\n",
    )
    adapter.send_input(identity.execution_id, msg_game)
    resp_game = adapter.read_output(identity.execution_id)
    assert resp_game is not None
    parsed_game = json.loads(resp_game.payload.decode("utf-8"))
    assert parsed_game["type"] == "ack"

    # Move request (go)
    req_go = str(uuid.uuid4())
    msg_go = AIKMessage(
        req_go,
        json.dumps(
            {"protocol_version": "aik-jsonl/1", "type": "go", "request_id": req_go}
        ).encode("utf-8")
        + b"\n",
    )
    adapter.send_input(identity.execution_id, msg_go)
    resp_go = adapter.read_output(identity.execution_id)
    assert resp_go is not None
    parsed_go = json.loads(resp_go.payload.decode("utf-8"))
    assert parsed_go["type"] == "bestmove"
    assert parsed_go["move"] == "e2e4"

    # 5. Evidence Collection
    evidence = adapter.collect_evidence(identity.execution_id)
    assert evidence.artifact_digest == digest
    assert evidence.state == ExecutionState.RUNNING
    assert "modal/gvisor" in evidence.runtime_identity
    assert evidence.stdout_bytes > 0

    # 6. Terminate
    snap_term = adapter.terminate(identity.execution_id)
    assert snap_term.state == ExecutionState.TERMINATED

    # 7. Cleanup Verification
    cleanup = adapter.verify_cleanup(identity.execution_id)
    assert cleanup == CleanupResult.VERIFIED

    # 8. Destroy
    snap_destroy = adapter.destroy(identity.execution_id)
    assert snap_destroy.state == ExecutionState.DESTROYED


def test_modal_adapter_rejects_corrupted_digest() -> None:
    authority = LeaseFenceAuthority()
    policy = _sample_policy()
    archive = build_trusted_engine_archive()
    digest = hashlib.sha256(archive).hexdigest()
    identity = _sample_identity(digest, policy)

    authority.set_current(
        LeaseSnapshot(
            worker_id=identity.worker_id,
            lease_id=identity.lease_id,
            fencing_token=identity.fencing_token,
            expires_at=identity.lease_expires_at,
        )
    )

    adapter = ModalSandboxAdapter(authority, ModalRuntimeConfig(mock_mode=True))
    adapter.create_execution(identity, policy)

    # Tampered byte
    corrupted_archive = bytearray(archive)
    corrupted_archive[50] = (corrupted_archive[50] + 1) % 256

    with pytest.raises(IdentityMismatchError, match="ARTIFACT_DIGEST_MISMATCH"):
        adapter.stage_artifact(
            identity.execution_id,
            bytes(corrupted_archive),
            identity.submission_version_id,
            digest,
        )


def test_modal_adapter_rejects_malformed_package() -> None:
    authority = LeaseFenceAuthority()
    policy = _sample_policy()
    fake_bytes = b"not a valid tar archive" * 10
    digest = hashlib.sha256(fake_bytes).hexdigest()
    identity = _sample_identity(digest, policy)

    authority.set_current(
        LeaseSnapshot(
            worker_id=identity.worker_id,
            lease_id=identity.lease_id,
            fencing_token=identity.fencing_token,
            expires_at=identity.lease_expires_at,
        )
    )

    adapter = ModalSandboxAdapter(authority, ModalRuntimeConfig(mock_mode=True))
    adapter.create_execution(identity, policy)

    with pytest.raises(IdentityMismatchError, match="ARTIFACT_PACKAGE_REJECTED"):
        adapter.stage_artifact(
            identity.execution_id, fake_bytes, identity.submission_version_id, digest
        )


def test_modal_adapter_supervisor_timeout() -> None:
    authority = LeaseFenceAuthority()
    policy = _sample_policy()
    archive = build_trusted_engine_archive()
    digest = hashlib.sha256(archive).hexdigest()
    identity = _sample_identity(digest, policy)

    authority.set_current(
        LeaseSnapshot(
            worker_id=identity.worker_id,
            lease_id=identity.lease_id,
            fencing_token=identity.fencing_token,
            expires_at=identity.lease_expires_at,
        )
    )

    adapter = ModalSandboxAdapter(authority, ModalRuntimeConfig(mock_mode=True))
    adapter.create_execution(identity, policy)
    adapter.stage_artifact(identity.execution_id, archive, identity.submission_version_id, digest)
    adapter.start(identity.execution_id)

    # Apply move timeout
    timed_out_snap = adapter.apply_supervisor_timeout(
        identity.execution_id, FailureClass.MOVE_TIMEOUT
    )
    assert timed_out_snap.state == ExecutionState.TIMED_OUT
    assert timed_out_snap.failure_class == FailureClass.MOVE_TIMEOUT

    # Verify cleanup and destroy
    assert adapter.verify_cleanup(identity.execution_id) == CleanupResult.VERIFIED
    assert adapter.destroy(identity.execution_id).state == ExecutionState.DESTROYED


def test_modal_adapter_integration_with_match_participant_bridge() -> None:
    authority = LeaseFenceAuthority()
    policy = _sample_policy()
    archive = build_trusted_engine_archive()
    digest = hashlib.sha256(archive).hexdigest()
    identity = _sample_identity(digest, policy)

    authority.set_current(
        LeaseSnapshot(
            worker_id=identity.worker_id,
            lease_id=identity.lease_id,
            fencing_token=identity.fencing_token,
            expires_at=identity.lease_expires_at,
        )
    )

    adapter = ModalSandboxAdapter(authority, ModalRuntimeConfig(mock_mode=True))
    adapter.create_execution(identity, policy)
    adapter.stage_artifact(identity.execution_id, archive, identity.submission_version_id, digest)
    adapter.start(identity.execution_id)

    bridge = KataMatchParticipant(adapter=adapter, execution_id=identity.execution_id)

    # Bridge exchange for hello
    cmd_id = str(uuid.uuid4())
    payload = (
        json.dumps(
            {"protocol_version": "aik-jsonl/1", "type": "hello", "request_id": cmd_id}
        ).encode("utf-8")
        + b"\n"
    )
    result = bridge.exchange(cmd_id, payload, timeout=2.0)
    assert result.message_id == cmd_id
    parsed = json.loads(result.payload.decode("utf-8"))
    assert parsed["type"] == "ready"

    # Bridge exchange for go
    cmd_go = str(uuid.uuid4())
    payload_go = (
        json.dumps({"protocol_version": "aik-jsonl/1", "type": "go", "request_id": cmd_go}).encode(
            "utf-8"
        )
        + b"\n"
    )
    result_go = bridge.exchange(cmd_go, payload_go, timeout=2.0)
    assert result_go.message_id == cmd_go
    parsed_go = json.loads(result_go.payload.decode("utf-8"))
    assert parsed_go["type"] == "bestmove"
    assert parsed_go["move"] == "e2e4"

    bridge.terminate()
    assert adapter.verify_cleanup(identity.execution_id) == CleanupResult.VERIFIED


def test_modal_adapter_integration_with_worker_agent() -> None:
    archive = build_trusted_engine_archive()
    digest = hashlib.sha256(archive).hexdigest()
    now = datetime.now(timezone.utc)
    policy = _sample_policy()
    p_digest = _policy_digest(policy)

    job = JobOut(
        execution_id=uuid.uuid4(),
        execution_mode="MODAL_AI_EXECUTION",
        execution_kind="PARTICIPANT",
        match_id=uuid.uuid4(),
        match_side_id=uuid.uuid4(),
        match_purpose="OFFICIAL",
        worker_id="worker-001",
        participant_id="part-1",
        submission_id=uuid.uuid4(),
        submission_version=1,
        submission_version_id=uuid.uuid4(),
        artifact_sha256=digest,
        immutable_artifact_ref="quarantine/part-1",
        runtime_profile="python-3.12",
        protocol_version="aik-jsonl/1",
        initial_state={"purpose": "OFFICIAL"},
        resource_policy=ResourcePolicy(
            move_timeout_ms=2000,
            process_lifetime_ms=900000,
            concurrency_per_worker=1,
        ),
        policy_digest=p_digest,
        attempt_number=1,
        created_at=now,
        execution_expires_at=now + timedelta(minutes=5),
        lease=LeaseOut(
            lease_id=uuid.uuid4(),
            fencing_token=1,
            expires_at=now + timedelta(minutes=1),
            next_sequence=1,
        ),
    )

    # Mock gateway client
    mock_client = MagicMock()
    mock_client.worker_id = "worker-001"
    mock_client.claim.return_value = job
    mock_client.operation.return_value = {"status": "OK", "next_sequence": 2}
    mock_client.capability.return_value = "token-" + ("x" * 32)
    mock_client.artifact.return_value = archive
    # Next match commands: first hello, then TERMINAL
    mock_client.next_match_command.side_effect = [
        {
            "status": "ACCEPTED",
            "command_id": "cmd-1",
            "message": json.dumps(
                {"protocol_version": "aik-jsonl/1", "type": "hello", "request_id": "cmd-1"}
            )
            + "\n",
        },
        {"status": "TERMINAL"},
    ]

    adapter_created: list[ModalSandboxAdapter] = []

    def factory(fence):
        ad = ModalSandboxAdapter(fence, config=ModalRuntimeConfig(mock_mode=True))
        adapter_created.append(ad)
        return ad

    agent = WorkerAgent(client=mock_client, adapter_factory=factory)
    executed = agent.run_once()
    assert executed is True
    assert len(adapter_created) == 1
    # Verify cleanup operation was reported to gateway
    assert any(
        call[0][0].endswith("/cleanup") for call in mock_client.operation.call_args_list
    )


def test_modal_adapter_fails_fast_when_live_modal_uninstalled() -> None:
    authority = LeaseFenceAuthority()
    policy = _sample_policy()
    archive = build_trusted_engine_archive()
    digest = hashlib.sha256(archive).hexdigest()
    identity = _sample_identity(digest, policy)

    authority.set_current(
        LeaseSnapshot(
            worker_id=identity.worker_id,
            lease_id=identity.lease_id,
            fencing_token=identity.fencing_token,
            expires_at=identity.lease_expires_at,
        )
    )

    # Live Modal config with sandbox_enabled=True, mock_mode=False
    live_config = ModalRuntimeConfig(
        mock_mode=False, sandbox_enabled=True, execution_mode="modal"
    )
    adapter = ModalSandboxAdapter(authority, live_config)
    adapter.create_execution(identity, policy)
    adapter.stage_artifact(identity.execution_id, archive, identity.submission_version_id, digest)

    # If modal package is not installed, start() must fail fast and NOT convert to move!
    with pytest.raises(ModalRuntimeError):
        adapter.start(identity.execution_id)

    # Check evidence records failure class INFRASTRUCTURE_FAILURE
    evidence = adapter.collect_evidence(identity.execution_id)
    assert evidence.failure_class == FailureClass.INFRASTRUCTURE_FAILURE


def test_modal_runtime_config_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SANDBOX_ENABLED", raising=False)
    monkeypatch.delenv("WORKER_EXECUTION_MODE", raising=False)
    monkeypatch.delenv("AIK_MODAL_MOCK_MODE", raising=False)

    cfg = ModalRuntimeConfig.from_env()
    assert cfg.mock_mode is True
    assert cfg.sandbox_enabled is False
    assert cfg.execution_mode == "mock"
    assert cfg.block_network is True
    assert cfg.app_name == "aik-sandbox-modal"

    monkeypatch.setenv("AIK_MODAL_APP_NAME", "custom-app")
    monkeypatch.setenv("AIK_MODAL_CPU_MILLICORES", "2000")
    monkeypatch.setenv("AIK_MODAL_MEMORY_BYTES", "536870912")
    monkeypatch.setenv("AIK_MODAL_MOCK_MODE", "0")
    cfg2 = ModalRuntimeConfig.from_env()
    assert cfg2.app_name == "custom-app"
    assert cfg2.default_cpu_millicores == 2000
    assert cfg2.default_memory_bytes == 536870912
    assert cfg2.mock_mode is False


def test_worker_client_tls_config_from_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    from app.workers.tls import WorkerClientTLSConfig, WorkerTLSConfigurationError

    monkeypatch.delenv("WORKER_ID", raising=False)
    with pytest.raises(
        WorkerTLSConfigurationError,
        match="required worker client TLS configuration is missing",
    ):
        WorkerClientTLSConfig.from_env()

    ca_file = tmp_path / "ca.crt"
    cert_file = tmp_path / "worker.crt"
    key_file = tmp_path / "worker.key"
    ca_file.touch()
    cert_file.touch()
    key_file.touch()

    monkeypatch.setenv("WORKER_CLIENT_CA_CERT", str(ca_file))
    monkeypatch.setenv("WORKER_CLIENT_CERT", str(cert_file))
    monkeypatch.setenv("WORKER_CLIENT_KEY", str(key_file))
    monkeypatch.setenv("WORKER_ID", "worker-test-01")
    monkeypatch.setenv("WORKER_GATEWAY_SERVER_NAME", "gateway.internal")

    cfg = WorkerClientTLSConfig.from_env()
    assert cfg.worker_id == "worker-test-01"
    assert cfg.server_name == "gateway.internal"
    assert cfg.ca_certificate == ca_file


def test_build_sandbox_adapter_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.workers.agent import WorkerAgentError, build_sandbox_adapter_factory

    # Default mock
    factory_mock = build_sandbox_adapter_factory("mock")
    authority = LeaseFenceAuthority()
    adapter = factory_mock(authority)
    assert isinstance(adapter, ModalSandboxAdapter)

    # Modal
    monkeypatch.setenv("WORKER_SANDBOX_BACKEND", "modal")
    factory_modal = build_sandbox_adapter_factory()
    adapter_modal = factory_modal(authority)
    assert isinstance(adapter_modal, ModalSandboxAdapter)

    # Unknown
    with pytest.raises(WorkerAgentError, match="UNKNOWN_SANDBOX_BACKEND"):
        build_sandbox_adapter_factory("invalid-backend")


def test_run_agent_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    import threading
    from unittest.mock import MagicMock

    from app.workers.agent import run_agent

    mock_client = MagicMock()
    mock_client.worker_id = "worker-001"
    # Return None so run_once returns False
    mock_client.claim.return_value = None

    stop = threading.Event()
    # Trigger stop immediately after 1 cycle
    stop.set()
    processed = run_agent(
        client=mock_client,
        adapter_factory=MagicMock(),
        stop_event=stop,
        poll_interval=0.01,
        max_jobs=1,
    )
    assert processed == 0


def test_modal_adapter_generates_guest_confinement_launcher() -> None:
    """Verify that stage_artifact generates _launcher.py enforcing rlimits inside Modal microVM."""
    authority = LeaseFenceAuthority()
    policy = _sample_policy()
    archive = build_trusted_engine_archive()
    digest = hashlib.sha256(archive).hexdigest()
    identity = _sample_identity(digest, policy)

    authority.set_current(
        LeaseSnapshot(
            worker_id=identity.worker_id,
            lease_id=identity.lease_id,
            fencing_token=identity.fencing_token,
            expires_at=identity.lease_expires_at,
        )
    )

    adapter = ModalSandboxAdapter(authority, ModalRuntimeConfig(mock_mode=True))
    adapter.create_execution(identity, policy)
    adapter.stage_artifact(identity.execution_id, archive, identity.submission_version_id, digest)

    record = adapter._records[identity.execution_id]
    assert record.temp_dir is not None
    launcher_path = record.temp_dir / "_launcher.py"
    assert launcher_path.is_file(), "_launcher.py must be created in staged directory"

    content = launcher_path.read_text(encoding="ascii")
    assert "target.is_relative_to(root)" in content
    assert "resource.setrlimit(resource.RLIMIT_NPROC, (16, 16))" in content
    assert "resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))" in content
    assert "resource.setrlimit(resource.RLIMIT_FSIZE, (33554432, 33554432))" in content
    assert "resource.setrlimit(resource.RLIMIT_CORE, (0, 0))" in content
    assert "resource.setrlimit(resource.RLIMIT_CPU, (900, 900))" in content
    assert "sys.exit(126)" in content
    assert "sys.exit(127)" in content
    assert "os.execv(sys.executable, [sys.executable, str(target)] + sys.argv[2:])" in content


def _execute_launcher_script(
    script_path: Path,
    argv: list[str],
    artifact_root: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock_setrlimit_side_effect=None,
) -> tuple[int, str, list[tuple[str, tuple[int, int]]], list[str] | None]:
    """Helper to run the generated _launcher.py logic in a controlled Python sandbox."""
    import io
    import sys
    from unittest.mock import MagicMock

    calls: list[tuple[str, tuple[int, int]]] = []
    execv_calls: list[list[str]] = []

    mock_resource = MagicMock()
    mock_resource.RLIMIT_CORE = "RLIMIT_CORE"
    mock_resource.RLIMIT_NOFILE = "RLIMIT_NOFILE"
    mock_resource.RLIMIT_FSIZE = "RLIMIT_FSIZE"
    mock_resource.RLIMIT_NPROC = "RLIMIT_NPROC"
    mock_resource.RLIMIT_CPU = "RLIMIT_CPU"

    def _setrlimit(limit, val):
        if mock_setrlimit_side_effect:
            mock_setrlimit_side_effect(limit, val)
        calls.append((limit, val))

    mock_resource.setrlimit.side_effect = _setrlimit

    def _mock_execv(prog, args):
        execv_calls.append(args)
        sys.exit(0)

    monkeypatch.setenv("AIK_ARTIFACT_ROOT", str(artifact_root))
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.setitem(sys.modules, "resource", mock_resource)

    code = script_path.read_text(encoding="ascii")
    stderr_buf = io.StringIO()
    monkeypatch.setattr(sys, "stderr", stderr_buf)

    import os
    monkeypatch.setattr(os, "execv", _mock_execv)

    exit_code = 0
    try:
        exec(code, {"__name__": "__main__"})
    except SystemExit as exc:
        exit_code = exc.code if isinstance(exc.code, int) else 1

    return exit_code, stderr_buf.getvalue(), calls, (execv_calls[0] if execv_calls else None)


def test_modal_launcher_fails_closed_missing_entrypoint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify launcher fails closed with exit code 126 when entrypoint argument is missing."""
    authority = LeaseFenceAuthority()
    policy = _sample_policy()
    archive = build_trusted_engine_archive()
    digest = hashlib.sha256(archive).hexdigest()
    identity = _sample_identity(digest, policy)
    authority.set_current(LeaseSnapshot(identity.worker_id, identity.lease_id, identity.fencing_token, identity.lease_expires_at))

    adapter = ModalSandboxAdapter(authority, ModalRuntimeConfig(mock_mode=True))
    adapter.create_execution(identity, policy)
    adapter.stage_artifact(identity.execution_id, archive, identity.submission_version_id, digest)
    record = adapter._records[identity.execution_id]
    launcher_path = record.temp_dir / "_launcher.py"

    code, err, limits, execv = _execute_launcher_script(
        launcher_path,
        ["_launcher.py"],
        record.temp_dir,
        monkeypatch,
    )
    assert code == 126
    assert "LAUNCHER_ERROR: missing entrypoint argument" in err
    assert len(limits) == 0, "No limits should be set if entrypoint missing"
    assert execv is None, "execv must not be called"


def test_modal_launcher_fails_closed_on_path_traversal(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify launcher fails closed with exit code 126 when entrypoint attempts directory traversal."""
    authority = LeaseFenceAuthority()
    policy = _sample_policy()
    archive = build_trusted_engine_archive()
    digest = hashlib.sha256(archive).hexdigest()
    identity = _sample_identity(digest, policy)
    authority.set_current(LeaseSnapshot(identity.worker_id, identity.lease_id, identity.fencing_token, identity.lease_expires_at))

    adapter = ModalSandboxAdapter(authority, ModalRuntimeConfig(mock_mode=True))
    adapter.create_execution(identity, policy)
    adapter.stage_artifact(identity.execution_id, archive, identity.submission_version_id, digest)
    record = adapter._records[identity.execution_id]
    launcher_path = record.temp_dir / "_launcher.py"

    for hostile_path in ["../../bin/sh", "../_launcher.py", "../../etc/passwd"]:
        code, err, limits, execv = _execute_launcher_script(
            launcher_path,
            ["_launcher.py", hostile_path],
            record.temp_dir,
            monkeypatch,
        )
        assert code == 126
        assert "LAUNCHER_ERROR: invalid entrypoint path" in err
        assert len(limits) == 0, "Limits must not be set on invalid entrypoint"
        assert execv is None, "execv must not be called on invalid entrypoint"


def test_modal_launcher_fails_closed_when_entrypoint_is_directory(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify launcher fails closed when entrypoint resolves to a directory or non-file."""
    authority = LeaseFenceAuthority()
    policy = _sample_policy()
    archive = build_trusted_engine_archive()
    digest = hashlib.sha256(archive).hexdigest()
    identity = _sample_identity(digest, policy)
    authority.set_current(LeaseSnapshot(identity.worker_id, identity.lease_id, identity.fencing_token, identity.lease_expires_at))

    adapter = ModalSandboxAdapter(authority, ModalRuntimeConfig(mock_mode=True))
    adapter.create_execution(identity, policy)
    adapter.stage_artifact(identity.execution_id, archive, identity.submission_version_id, digest)
    record = adapter._records[identity.execution_id]
    launcher_path = record.temp_dir / "_launcher.py"

    code, err, limits, execv = _execute_launcher_script(
        launcher_path,
        ["_launcher.py", "engine"],
        record.temp_dir,
        monkeypatch,
    )
    assert code == 126
    assert "LAUNCHER_ERROR: invalid entrypoint path" in err
    assert execv is None


def test_modal_launcher_fails_closed_on_setrlimit_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify launcher fails closed with exit code 127 if any resource limit fails to apply."""
    authority = LeaseFenceAuthority()
    policy = _sample_policy()
    archive = build_trusted_engine_archive()
    digest = hashlib.sha256(archive).hexdigest()
    identity = _sample_identity(digest, policy)
    authority.set_current(LeaseSnapshot(identity.worker_id, identity.lease_id, identity.fencing_token, identity.lease_expires_at))

    adapter = ModalSandboxAdapter(authority, ModalRuntimeConfig(mock_mode=True))
    adapter.create_execution(identity, policy)
    adapter.stage_artifact(identity.execution_id, archive, identity.submission_version_id, digest)
    record = adapter._records[identity.execution_id]
    launcher_path = record.temp_dir / "_launcher.py"

    def fail_rlimit(limit, val):
        if limit == "RLIMIT_NPROC":
            raise OSError("Operation not permitted (EPERM)")

    code, err, limits, execv = _execute_launcher_script(
        launcher_path,
        ["_launcher.py", "engine/main.py"],
        record.temp_dir,
        monkeypatch,
        mock_setrlimit_side_effect=fail_rlimit,
    )
    assert code == 127
    assert "LAUNCHER_CONFINEMENT_FAILED: Operation not permitted (EPERM)" in err
    assert execv is None, "execv must NEVER be called if resource limits fail"


def test_modal_launcher_applies_all_limits_before_execv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Verify that when limits succeed, all 5 limits are configured before os.execv is invoked."""
    authority = LeaseFenceAuthority()
    policy = _sample_policy()
    archive = build_trusted_engine_archive()
    digest = hashlib.sha256(archive).hexdigest()
    identity = _sample_identity(digest, policy)
    authority.set_current(LeaseSnapshot(identity.worker_id, identity.lease_id, identity.fencing_token, identity.lease_expires_at))

    adapter = ModalSandboxAdapter(authority, ModalRuntimeConfig(mock_mode=True))
    adapter.create_execution(identity, policy)
    adapter.stage_artifact(identity.execution_id, archive, identity.submission_version_id, digest)
    record = adapter._records[identity.execution_id]
    launcher_path = record.temp_dir / "_launcher.py"

    code, err, limits, execv = _execute_launcher_script(
        launcher_path,
        ["_launcher.py", "engine/main.py"],
        record.temp_dir,
        monkeypatch,
    )
    assert code == 0
    assert err == ""
    limit_names = [name for name, _ in limits]
    assert limit_names == ["RLIMIT_CORE", "RLIMIT_NOFILE", "RLIMIT_FSIZE", "RLIMIT_NPROC", "RLIMIT_CPU"]
    assert limits[0] == ("RLIMIT_CORE", (0, 0))
    assert limits[1] == ("RLIMIT_NOFILE", (64, 64))
    assert limits[2] == ("RLIMIT_FSIZE", (33554432, 33554432))
    assert limits[3] == ("RLIMIT_NPROC", (16, 16))
    assert limits[4] == ("RLIMIT_CPU", (900, 900))

    assert execv is not None
    assert str((record.temp_dir / "engine/main.py").resolve()) in execv[1]


def test_modal_evidence_collection_truthfully_reports_unavailable_metrics() -> None:
    """Verify Modal evidence collection truthfully reports unavailable measurements and does not fabricate zeros."""
    authority = LeaseFenceAuthority()
    policy = _sample_policy()
    archive = build_trusted_engine_archive()
    digest = hashlib.sha256(archive).hexdigest()
    identity = _sample_identity(digest, policy)
    authority.set_current(LeaseSnapshot(identity.worker_id, identity.lease_id, identity.fencing_token, identity.lease_expires_at))

    adapter = ModalSandboxAdapter(authority, ModalRuntimeConfig(mock_mode=True))
    adapter.create_execution(identity, policy)
    adapter.stage_artifact(identity.execution_id, archive, identity.submission_version_id, digest)
    adapter.start(identity.execution_id)

    evidence = adapter.collect_evidence(identity.execution_id)
    assert evidence.cpu_millicores is None
    assert evidence.memory_bytes is None
    assert evidence.process_count is None
    assert evidence.filesystem_bytes is None
    assert evidence.filesystem_inodes is None
    assert evidence.file_descriptors is None
    assert evidence.resource_usage == {}
    assert "cpu_millicores" in evidence.unavailable_measurements
    assert "memory_bytes" in evidence.unavailable_measurements
    assert "process_count" in evidence.unavailable_measurements
    assert "filesystem_bytes" in evidence.unavailable_measurements
    assert "filesystem_inodes" in evidence.unavailable_measurements
    assert "file_descriptors" in evidence.unavailable_measurements
    assert evidence.measurement_source == "modal-sdk"
    assert evidence.measurement_scope == "remote-cloud-sandbox"


def test_modal_cleanup_fails_closed_if_termination_fails() -> None:
    """Verify cleanup reports CleanupResult.FAILED if remote sandbox termination throws an error."""
    authority = LeaseFenceAuthority()
    policy = _sample_policy()
    archive = build_trusted_engine_archive()
    digest = hashlib.sha256(archive).hexdigest()
    identity = _sample_identity(digest, policy)
    authority.set_current(LeaseSnapshot(identity.worker_id, identity.lease_id, identity.fencing_token, identity.lease_expires_at))

    adapter = ModalSandboxAdapter(authority, ModalRuntimeConfig(mock_mode=True))
    adapter.create_execution(identity, policy)
    adapter.stage_artifact(identity.execution_id, archive, identity.submission_version_id, digest)
    adapter.start(identity.execution_id)
    adapter.terminate(identity.execution_id)

    record = adapter._records[identity.execution_id]
    mock_sandbox = MagicMock()
    mock_sandbox.terminate.side_effect = RuntimeError("Remote API failure during termination")
    record.sandbox = mock_sandbox

    result = adapter.verify_cleanup(identity.execution_id)
    assert result == CleanupResult.FAILED
    assert record.state == ExecutionState.CLEANUP_FAILED


def test_modal_adapter_concurrency_isolation_between_executions() -> None:
    """Gate F: Verify multiple concurrent executions maintain strict directory, state, and output separation."""
    authority = LeaseFenceAuthority()
    policy = _sample_policy()
    archive = build_trusted_engine_archive()
    digest = hashlib.sha256(archive).hexdigest()

    id_a = _sample_identity(digest, policy, execution_id=uuid.uuid4())
    id_b = _sample_identity(digest, policy, execution_id=uuid.uuid4())

    authority.set_current(LeaseSnapshot(id_a.worker_id, id_a.lease_id, id_a.fencing_token, id_a.lease_expires_at))
    authority.set_current(LeaseSnapshot(id_b.worker_id, id_b.lease_id, id_b.fencing_token, id_b.lease_expires_at))

    adapter = ModalSandboxAdapter(authority, ModalRuntimeConfig(mock_mode=True))

    # 1. Create both executions
    adapter.create_execution(id_a, policy)
    adapter.create_execution(id_b, policy)

    # 2. Stage artifacts
    adapter.stage_artifact(id_a.execution_id, archive, id_a.submission_version_id, digest)
    adapter.stage_artifact(id_b.execution_id, archive, id_b.submission_version_id, digest)

    rec_a = adapter._records[id_a.execution_id]
    rec_b = adapter._records[id_b.execution_id]

    assert rec_a.temp_dir is not None
    assert rec_b.temp_dir is not None
    assert rec_a.temp_dir != rec_b.temp_dir
    assert rec_a.temp_dir.exists()
    assert rec_b.temp_dir.exists()

    # 3. Start both executions
    adapter.start(id_a.execution_id)
    adapter.start(id_b.execution_id)

    # 4. Send command to execution A only
    req_a = str(uuid.uuid4())
    msg_a = AIKMessage(
        req_a,
        json.dumps({"protocol_version": "aik-jsonl/1", "type": "go", "request_id": req_a}).encode("utf-8") + b"\n",
    )
    adapter.send_input(id_a.execution_id, msg_a)

    # Verify execution B has received nothing
    assert rec_b.output.empty()

    # Verify execution A produces bestmove
    resp_a = adapter.read_output(id_a.execution_id)
    assert resp_a is not None
    parsed_a = json.loads(resp_a.payload.decode("utf-8"))
    assert parsed_a["request_id"] == req_a

    # 5. Terminate and cleanup execution A
    adapter.terminate(id_a.execution_id)
    cleanup_a = adapter.verify_cleanup(id_a.execution_id)
    assert cleanup_a == CleanupResult.VERIFIED
    assert not rec_a.temp_dir.exists(), "Execution A temp_dir must be cleanly deleted"

    # Verify execution B is STILL RUNNING and its temp_dir is completely intact
    assert rec_b.temp_dir.exists(), "Execution B temp_dir must NOT be affected by Execution A cleanup"
    assert rec_b.state == ExecutionState.RUNNING

    # 6. Cleanup execution B
    adapter.terminate(id_b.execution_id)
    cleanup_b = adapter.verify_cleanup(id_b.execution_id)
    assert cleanup_b == CleanupResult.VERIFIED
    assert not rec_b.temp_dir.exists(), "Execution B temp_dir must now be cleanly deleted"


def test_modal_adapter_idempotent_cleanup_and_directory_removal() -> None:
    """Gate G: Verify cleanup is idempotent, removes directories, and safely transitions destroyed state."""
    authority = LeaseFenceAuthority()
    policy = _sample_policy()
    archive = build_trusted_engine_archive()
    digest = hashlib.sha256(archive).hexdigest()
    identity = _sample_identity(digest, policy)
    authority.set_current(LeaseSnapshot(identity.worker_id, identity.lease_id, identity.fencing_token, identity.lease_expires_at))

    adapter = ModalSandboxAdapter(authority, ModalRuntimeConfig(mock_mode=True))
    adapter.create_execution(identity, policy)
    adapter.stage_artifact(identity.execution_id, archive, identity.submission_version_id, digest)
    adapter.start(identity.execution_id)

    record = adapter._records[identity.execution_id]
    temp_dir = record.temp_dir
    assert temp_dir is not None and temp_dir.exists()

    adapter.terminate(identity.execution_id)

    # First cleanup call removes temp_dir
    res1 = adapter.verify_cleanup(identity.execution_id)
    assert res1 == CleanupResult.VERIFIED
    assert not temp_dir.exists()

    # Second cleanup call is idempotent and succeeds without error
    res2 = adapter.verify_cleanup(identity.execution_id)
    assert res2 == CleanupResult.VERIFIED

    # Destroy removes execution from adapter registry
    snap_destroy = adapter.destroy(identity.execution_id)
    assert snap_destroy.state == ExecutionState.DESTROYED
    assert identity.execution_id not in adapter._records


def test_modal_adapter_lifetime_timeout_and_wall_clock_enforcement() -> None:
    """Gate A: Verify lifetime wall-clock timeout is enforced and transitions state to TIMED_OUT."""
    import time
    authority = LeaseFenceAuthority()
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
        total_timeout_ms=50,
        network_mode=NetworkMode.DENY_ALL,
        writable_storage=WritableStoragePolicy.PRIVATE_TMPFS,
    )
    archive = build_trusted_engine_archive()
    digest = hashlib.sha256(archive).hexdigest()
    identity = _sample_identity(digest, policy)
    authority.set_current(LeaseSnapshot(identity.worker_id, identity.lease_id, identity.fencing_token, identity.lease_expires_at))

    adapter = ModalSandboxAdapter(authority, ModalRuntimeConfig(mock_mode=True))
    adapter.create_execution(identity, policy)
    adapter.stage_artifact(identity.execution_id, archive, identity.submission_version_id, digest)
    adapter.start(identity.execution_id)

    # Sleep past total_timeout_ms
    time.sleep(0.08)

    # Reading output triggers lifetime timeout check
    out = adapter.read_output(identity.execution_id)
    assert out is None

    snap = adapter.inspect(identity.execution_id)
    assert snap.state == ExecutionState.TIMED_OUT
    assert snap.failure_class == FailureClass.EXECUTION_TIMEOUT

    evidence = adapter.collect_evidence(identity.execution_id)
    assert evidence.state == ExecutionState.TIMED_OUT
    assert evidence.failure_class == FailureClass.EXECUTION_TIMEOUT
    assert evidence.termination_reason == "LIFETIME_TIMEOUT"


