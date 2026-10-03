"""Windows-safe sandbox contract tests; the fake never executes artifact bytes."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.sandbox.contract import (
    AIKMessage,
    AIKProtocolError,
    AIKTransport,
    CleanupResult,
    ExecutionIdentity,
    ExecutionState,
    ExpiredLeaseError,
    FailureClass,
    FakeSandboxAdapter,
    IdentityMismatchError,
    InvalidTransitionError,
    LeaseFenceAuthority,
    LeaseSnapshot,
    NetworkMode,
    RuntimeObservation,
    SandboxPolicy,
    SandboxSupervisor,
    StaleFenceError,
    WritableStoragePolicy,
)


class FakeClock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


def make_policy(**changes) -> SandboxPolicy:
    values = {
        "cpu_millicores": 1000,
        "memory_bytes": 268_435_456,
        "pid_limit": 16,
        "filesystem_bytes": 33_554_432,
        "filesystem_inodes": 128,
        "file_descriptors": 64,
        "stdout_bytes": 1_048_576,
        "stderr_bytes": 262_144,
        "startup_timeout_ms": 5000,
        "move_timeout_ms": 2000,
        "total_timeout_ms": 900_000,
        "network_mode": NetworkMode.DENY_ALL,
        "writable_storage": WritableStoragePolicy.PRIVATE_TMPFS,
    }
    values.update(changes)
    return SandboxPolicy(**values)


def make_harness(*, cleanup=CleanupResult.VERIFIED, lease_seconds=3600):
    now = datetime.now(timezone.utc)
    clock = FakeClock()
    authority = LeaseFenceAuthority()
    identity = ExecutionIdentity(
        execution_id=uuid.uuid4(),
        submission_version_id=uuid.uuid4(),
        artifact_digest=hashlib.sha256(b"inert-fixture").hexdigest(),
        policy_profile="policy-test-v1",
        runtime_profile="python-3.12-test",
        worker_id="worker-test",
        lease_id=uuid.uuid4(),
        fencing_token=1,
        protocol_version="aik-jsonl/1",
        lease_expires_at=now + timedelta(seconds=lease_seconds),
    )
    authority.set_current(
        LeaseSnapshot(
            identity.worker_id,
            identity.lease_id,
            identity.fencing_token,
            identity.lease_expires_at,
        )
    )
    adapter = FakeSandboxAdapter(
        authority, monotonic=clock, wall_clock=lambda: now, cleanup_result=cleanup
    )
    adapter.create_execution(identity, make_policy())
    return identity, authority, adapter, clock


def stage_and_start(identity, adapter):
    adapter.stage_artifact(
        identity.execution_id,
        b"inert-fixture",
        identity.submission_version_id,
        identity.artifact_digest,
    )
    return adapter.start(identity.execution_id)


def test_create_stage_start_inspect_terminate_cleanup_destroy_lifecycle():
    identity, _, adapter, _ = make_harness()
    assert adapter.inspect(identity.execution_id).state == ExecutionState.CREATED
    staged = adapter.stage_artifact(
        identity.execution_id,
        b"inert-fixture",
        identity.submission_version_id,
        identity.artifact_digest,
    )
    assert staged.read_only and staged.content == b"inert-fixture"
    assert adapter.start(identity.execution_id).state == ExecutionState.RUNNING
    assert adapter.terminate(identity.execution_id).state == ExecutionState.TERMINATED
    assert adapter.verify_cleanup(identity.execution_id) == CleanupResult.VERIFIED
    assert adapter.destroy(identity.execution_id).state == ExecutionState.DESTROYED


def test_invalid_state_transitions_rejected():
    identity, _, adapter, _ = make_harness()
    with pytest.raises(InvalidTransitionError):
        adapter.start(identity.execution_id)
    with pytest.raises(InvalidTransitionError):
        adapter.verify_cleanup(identity.execution_id)


def test_execution_identity_is_immutable():
    identity, _, _, _ = make_harness()
    with pytest.raises((FrozenInstanceError, AttributeError)):
        identity.fencing_token = 9


def test_digest_mismatch_rejected():
    identity, _, adapter, _ = make_harness()
    with pytest.raises(IdentityMismatchError, match="ARTIFACT_DIGEST_MISMATCH"):
        adapter.stage_artifact(
            identity.execution_id,
            b"changed",
            identity.submission_version_id,
            identity.artifact_digest,
        )


def test_submission_version_mismatch_rejected():
    identity, _, adapter, _ = make_harness()
    with pytest.raises(IdentityMismatchError, match="SUBMISSION_VERSION_MISMATCH"):
        adapter.stage_artifact(
            identity.execution_id, b"inert-fixture", uuid.uuid4(), identity.artifact_digest
        )


def test_stale_fencing_token_rejected_for_worker_operation():
    identity, authority, adapter, _ = make_harness()
    authority.fence(identity.lease_id, next_token=2)
    with pytest.raises(StaleFenceError):
        adapter.stage_artifact(
            identity.execution_id,
            b"inert-fixture",
            identity.submission_version_id,
            identity.artifact_digest,
        )


def test_expired_lease_rejected():
    identity, authority, adapter, _ = make_harness()
    authority.set_current(
        LeaseSnapshot(
            identity.worker_id,
            identity.lease_id,
            identity.fencing_token,
            datetime.now(timezone.utc) - timedelta(seconds=1),
        )
    )
    with pytest.raises(ExpiredLeaseError):
        adapter.inspect(identity.execution_id)


@pytest.mark.parametrize(
    "field,value",
    [
        ("cpu_millicores", 1001),
        ("memory_bytes", 268_435_457),
        ("pid_limit", 17),
        ("filesystem_bytes", 33_554_433),
        ("filesystem_inodes", 129),
        ("file_descriptors", 65),
        ("stdout_bytes", 1_048_577),
        ("stderr_bytes", 262_145),
        ("startup_timeout_ms", 5001),
        ("move_timeout_ms", 3_600_001),
        ("total_timeout_ms", 86_400_001),
    ],
)
def test_resource_policy_upper_bounds(field, value):
    with pytest.raises(ValidationError):
        make_policy(**{field: value})


def test_resource_policy_forbids_secrets_and_network_modes():
    with pytest.raises(ValidationError):
        make_policy(database_url="postgres://secret")
    with pytest.raises(ValidationError):
        make_policy(network_mode="ALLOW_ALL")


@pytest.mark.parametrize(
    ("kind", "state", "failure"),
    [
        ("startup_timeout", ExecutionState.TIMED_OUT, FailureClass.STARTUP_FAILURE),
        ("move_timeout", ExecutionState.TIMED_OUT, FailureClass.MOVE_TIMEOUT),
    ],
)
def test_startup_and_move_timeout_simulation(kind, state, failure):
    identity, _, adapter, _ = make_harness()
    stage_and_start(identity, adapter)
    result = adapter.simulate_observation(
        identity.execution_id, RuntimeObservation(kind), make_policy()
    )
    assert result.state == state and result.failure_class == failure


@pytest.mark.parametrize(
    "observation",
    [
        RuntimeObservation("running", stdout_bytes=1_048_577),
        RuntimeObservation("running", stderr_bytes=262_145),
        RuntimeObservation("running", process_count=17),
        RuntimeObservation("running", memory_bytes=268_435_457),
        RuntimeObservation("running", filesystem_bytes=33_554_433),
        RuntimeObservation("running", filesystem_inodes=129),
        RuntimeObservation("running", file_descriptors=65),
        RuntimeObservation("running", cpu_millicores=1001),
        RuntimeObservation("network_attempt", network_attempt=True),
    ],
)
def test_resource_output_and_network_violations_fail_closed(observation):
    identity, _, adapter, _ = make_harness()
    stage_and_start(identity, adapter)
    result = adapter.simulate_observation(identity.execution_id, observation, make_policy())
    assert result.state == ExecutionState.RESOURCE_EXCEEDED
    assert result.failure_class is not None


def test_crash_simulation():
    identity, _, adapter, _ = make_harness()
    stage_and_start(identity, adapter)
    result = adapter.simulate_observation(
        identity.execution_id, RuntimeObservation("crash", exit_code=1), make_policy()
    )
    assert result.state == ExecutionState.CRASHED
    assert result.failure_class == FailureClass.PARTICIPANT_PROCESS_FAILURE


@pytest.mark.parametrize(
    ("cleanup", "state"),
    [
        (CleanupResult.FAILED, ExecutionState.CLEANUP_FAILED),
        (CleanupResult.UNKNOWN, ExecutionState.CLEANUP_UNKNOWN),
    ],
)
def test_cleanup_failure_or_unknown_cannot_become_success(cleanup, state):
    identity, _, adapter, _ = make_harness(cleanup=cleanup)
    adapter.terminate(identity.execution_id)
    assert adapter.verify_cleanup(identity.execution_id) == cleanup
    assert adapter.inspect(identity.execution_id).state == state
    with pytest.raises(InvalidTransitionError, match="CLEANUP_NOT_VERIFIED"):
        adapter.destroy(identity.execution_id)


def test_repeated_termination_and_cleanup_verification_are_idempotent():
    identity, _, adapter, _ = make_harness()
    first = adapter.terminate(identity.execution_id)
    second = adapter.terminate(identity.execution_id)
    assert first.state == second.state == ExecutionState.TERMINATED
    assert adapter.verify_cleanup(identity.execution_id) == CleanupResult.VERIFIED
    assert adapter.verify_cleanup(identity.execution_id) == CleanupResult.VERIFIED


def test_supervisor_requests_termination_after_fencing():
    identity, authority, adapter, clock = make_harness()
    stage_and_start(identity, adapter)
    supervisor = SandboxSupervisor(adapter, authority, monotonic=clock)
    supervisor.begin_deadline(identity, make_policy())
    authority.fence(identity.lease_id, next_token=2)
    assert supervisor.supervise(identity.execution_id).state == ExecutionState.TERMINATED


def test_supervisor_enforces_monotonic_total_deadline():
    identity, authority, adapter, clock = make_harness()
    stage_and_start(identity, adapter)
    supervisor = SandboxSupervisor(adapter, authority, monotonic=clock)
    supervisor.begin_deadline(identity, make_policy(total_timeout_ms=10))
    clock.value += 0.011
    assert supervisor.supervise(identity.execution_id).state == ExecutionState.TIMED_OUT
    assert (
        adapter.collect_evidence(identity.execution_id).failure_class
        == FailureClass.EXECUTION_TIMEOUT
    )


@pytest.mark.parametrize(
    ("deadline_kind", "timeout_ms", "failure"),
    [
        ("startup", 5, FailureClass.STARTUP_FAILURE),
        ("move", 5, FailureClass.MOVE_TIMEOUT),
    ],
)
def test_supervisor_enforces_startup_and_move_deadlines(deadline_kind, timeout_ms, failure):
    identity, authority, adapter, clock = make_harness()
    stage_and_start(identity, adapter)
    supervisor = SandboxSupervisor(adapter, authority, monotonic=clock)
    supervisor.begin_deadline(identity, make_policy())
    if deadline_kind == "startup":
        supervisor.arm_startup_deadline(identity, make_policy(startup_timeout_ms=timeout_ms))
    else:
        supervisor.arm_move_deadline(identity, make_policy(move_timeout_ms=timeout_ms))
    clock.value += 0.006
    assert supervisor.supervise(identity.execution_id).state == ExecutionState.TIMED_OUT
    assert adapter.collect_evidence(identity.execution_id).failure_class == failure


def test_total_deadline_cannot_be_extended():
    identity, authority, adapter, clock = make_harness()
    supervisor = SandboxSupervisor(adapter, authority, monotonic=clock)
    supervisor.begin_deadline(identity, make_policy(total_timeout_ms=10))
    clock.value += 0.005
    with pytest.raises(ValueError, match="TOTAL_DEADLINE_ALREADY_SET"):
        supervisor.begin_deadline(identity, make_policy(total_timeout_ms=900_000))


def test_fake_output_fixture_is_bounded():
    identity, _, adapter, _ = make_harness()
    adapter.stage_artifact(
        identity.execution_id,
        b"inert-fixture",
        identity.submission_version_id,
        identity.artifact_digest,
    )
    adapter.start(identity.execution_id)
    with pytest.raises(AIKProtocolError, match="STDOUT_LIMIT_EXCEEDED"):
        adapter.queue_inert_output(identity.execution_id, AIKMessage("x", b"x" * 1_048_577))


def test_artifact_mutation_is_detected_and_retry_uses_new_fenced_identity():
    identity, authority, adapter, _ = make_harness()
    with pytest.raises(IdentityMismatchError):
        adapter.stage_artifact(
            identity.execution_id,
            b"inert-fixturE",
            identity.submission_version_id,
            identity.artifact_digest,
        )
    new_identity = ExecutionIdentity(
        execution_id=uuid.uuid4(),
        submission_version_id=identity.submission_version_id,
        artifact_digest=identity.artifact_digest,
        policy_profile=identity.policy_profile,
        runtime_profile=identity.runtime_profile,
        worker_id=identity.worker_id,
        lease_id=identity.lease_id,
        fencing_token=2,
        protocol_version=identity.protocol_version,
        lease_expires_at=identity.lease_expires_at,
    )
    authority.fence(identity.lease_id, next_token=2)
    assert adapter.create_execution(new_identity, make_policy()).state == ExecutionState.CREATED
    assert new_identity.execution_id != identity.execution_id


def test_fake_transport_only_moves_bytes_and_detects_duplicate_late_malformed_output():
    now = FakeClock()
    transport = AIKTransport(message_bytes=128, stdout_bytes=256, stderr_bytes=32, monotonic=now)
    outgoing = transport.encode_controller_message(
        "req-1", {"protocol_version": "aik-jsonl/1", "type": "hello"}
    )
    assert outgoing.payload.endswith(b"\n")
    response = AIKMessage(
        "req-1", b'{"protocol_version":"aik-jsonl/1","type":"ready","request_id":"req-1"}\n'
    )
    assert transport.parse_stdout(response)["type"] == "ready"
    with pytest.raises(AIKProtocolError, match="DUPLICATE_MESSAGE"):
        transport.parse_stdout(response)
    with pytest.raises(AIKProtocolError, match="JSONL_LINE_INVALID"):
        transport.parse_stdout(AIKMessage("bad", b"not-json"))
    with pytest.raises(AIKProtocolError, match="STDERR_LIMIT_EXCEEDED"):
        transport.record_stderr(b"x" * 33)
    transport.set_deadline(now.value)
    now.value += 1
    with pytest.raises(AIKProtocolError, match="LATE_MESSAGE"):
        transport.parse_stdout(
            AIKMessage(
                "late", b'{"protocol_version":"aik-jsonl/1","type":"ready","request_id":"late"}\n'
            )
        )


def test_transport_rejects_duplicate_json_keys_and_output_flood():
    transport = AIKTransport(message_bytes=256, stdout_bytes=256)
    with pytest.raises(AIKProtocolError, match="DUPLICATE_JSON_KEY"):
        transport.parse_stdout(
            AIKMessage(
                "x",
                b'{"protocol_version":"aik-jsonl/1","type":"ack","request_id":"x","request_id":"x"}\n',
            )
        )
    transport = AIKTransport(message_bytes=64, stdout_bytes=8)
    with pytest.raises(AIKProtocolError, match="STDOUT_LIMIT_EXCEEDED"):
        transport.parse_stdout(
            AIKMessage("x", b'{"protocol_version":"aik-jsonl/1","type":"ready","request_id":"x"}\n')
        )


def test_controller_cannot_override_message_identity_or_exceed_stdin_budget():
    transport = AIKTransport(message_bytes=128, stdout_bytes=130)
    with pytest.raises(AIKProtocolError, match="MESSAGE_ID_RESERVED"):
        transport.encode_controller_message("x", {"request_id": "forged"})
    message = {"protocol_version": "aik-jsonl/1", "type": "hello"}
    transport.encode_controller_message("a", message)
    with pytest.raises(AIKProtocolError, match="STDIN_LIMIT_EXCEEDED"):
        transport.encode_controller_message("b", message)


def test_transport_refuses_participant_result_claims_and_malformed_moves():
    transport = AIKTransport()
    with pytest.raises(AIKProtocolError, match="PARTICIPANT_MESSAGE_SCHEMA_INVALID"):
        transport.parse_stdout(
            AIKMessage(
                "r1",
                b'{"protocol_version":"aik-jsonl/1","type":"game_over",'
                b'"request_id":"r1","outcome":"WIN"}\n',
            )
        )
    with pytest.raises(AIKProtocolError, match="MOVE_PROPOSAL_FORMAT_INVALID"):
        transport.parse_stdout(
            AIKMessage(
                "r2",
                b'{"protocol_version":"aik-jsonl/1","type":"bestmove",'
                b'"request_id":"r2","move":"I win"}\n',
            )
        )
