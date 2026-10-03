"""Typed sandbox boundary and inert fake; this module never launches a process.

The contracts model the future trusted supervisor/launcher boundary. They do not
provide Linux isolation and the fake adapter accepts only inert byte fixtures.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, StrictInt


class SandboxContractError(RuntimeError):
    """Base error for rejected sandbox-boundary operations."""


class InvalidTransitionError(SandboxContractError):
    pass


class IdentityMismatchError(SandboxContractError):
    pass


class StaleFenceError(SandboxContractError):
    pass


class ExpiredLeaseError(SandboxContractError):
    pass


class AIKProtocolError(SandboxContractError):
    pass


class NetworkMode(str, Enum):
    DENY_ALL = "DENY_ALL"


class WritableStoragePolicy(str, Enum):
    PRIVATE_TMPFS = "PRIVATE_TMPFS"


class CleanupResult(str, Enum):
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class ExecutionState(str, Enum):
    CREATED = "CREATED"
    STAGED = "STAGED"
    STARTED = "STARTED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    TIMED_OUT = "TIMED_OUT"
    RESOURCE_EXCEEDED = "RESOURCE_EXCEEDED"
    PROTOCOL_FAILED = "PROTOCOL_FAILED"
    CRASHED = "CRASHED"
    TERMINATED = "TERMINATED"
    CLEANUP_FAILED = "CLEANUP_FAILED"
    CLEANUP_UNKNOWN = "CLEANUP_UNKNOWN"
    DESTROYED = "DESTROYED"


class FailureClass(str, Enum):
    STARTUP_FAILURE = "STARTUP_FAILURE"
    MOVE_TIMEOUT = "MOVE_TIMEOUT"
    RESOURCE_EXHAUSTION = "RESOURCE_EXHAUSTION"
    PROTOCOL_VIOLATION = "PROTOCOL_VIOLATION"
    PARTICIPANT_PROCESS_FAILURE = "PARTICIPANT_PROCESS_FAILURE"
    STALE_EXECUTION = "STALE_EXECUTION"
    INFRASTRUCTURE_FAILURE = "INFRASTRUCTURE_FAILURE"
    EXECUTION_TIMEOUT = "EXECUTION_TIMEOUT"
    SANDBOX_VIOLATION = "SANDBOX_VIOLATION"


_TERMINAL = {
    ExecutionState.COMPLETED,
    ExecutionState.TIMED_OUT,
    ExecutionState.RESOURCE_EXCEEDED,
    ExecutionState.PROTOCOL_FAILED,
    ExecutionState.CRASHED,
    ExecutionState.TERMINATED,
}
_ALLOWED: dict[ExecutionState, set[ExecutionState]] = {
    ExecutionState.CREATED: {ExecutionState.STAGED, ExecutionState.TERMINATED},
    ExecutionState.STAGED: {ExecutionState.STARTED, ExecutionState.TERMINATED},
    ExecutionState.STARTED: {
        ExecutionState.RUNNING,
        ExecutionState.TIMED_OUT,
        ExecutionState.CRASHED,
        ExecutionState.TERMINATED,
    },
    ExecutionState.RUNNING: _TERMINAL,
    **{
        state: {
            ExecutionState.CLEANUP_FAILED,
            ExecutionState.CLEANUP_UNKNOWN,
            ExecutionState.DESTROYED,
        }
        for state in _TERMINAL
    },
    ExecutionState.CLEANUP_FAILED: {ExecutionState.DESTROYED},
    ExecutionState.CLEANUP_UNKNOWN: {ExecutionState.DESTROYED},
    ExecutionState.DESTROYED: set(),
}


@dataclass(frozen=True)
class ExecutionIdentity:
    execution_id: uuid.UUID
    submission_version_id: uuid.UUID
    artifact_digest: str
    policy_profile: str
    runtime_profile: str
    worker_id: str
    lease_id: uuid.UUID
    fencing_token: int
    protocol_version: str
    lease_expires_at: datetime

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", self.artifact_digest):
            raise ValueError("ARTIFACT_DIGEST_INVALID")
        if self.fencing_token < 1:
            raise ValueError("FENCING_TOKEN_INVALID")
        for name in ("policy_profile", "runtime_profile", "worker_id", "protocol_version"):
            value = getattr(self, name)
            if not value or len(value.encode("utf-8")) > 128:
                raise ValueError(f"{name.upper()}_INVALID")
        if self.lease_expires_at.tzinfo is None:
            raise ValueError("LEASE_EXPIRY_MUST_BE_TIMEZONE_AWARE")


class SandboxPolicy(BaseModel):
    """Server-selected bounded limits. Unknown fields, including secrets, fail closed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    cpu_millicores: StrictInt = Field(ge=1, le=1000)
    memory_bytes: StrictInt = Field(ge=1, le=268_435_456)
    pid_limit: StrictInt = Field(ge=1, le=16)
    filesystem_bytes: StrictInt = Field(ge=1, le=33_554_432)
    filesystem_inodes: StrictInt = Field(ge=1, le=128)
    file_descriptors: StrictInt = Field(ge=1, le=64)
    stdout_bytes: StrictInt = Field(ge=1, le=1_048_576)
    stderr_bytes: StrictInt = Field(ge=1, le=262_144)
    startup_timeout_ms: StrictInt = Field(ge=1, le=5000)
    move_timeout_ms: StrictInt = Field(ge=1, le=3_600_000)
    total_timeout_ms: StrictInt = Field(ge=1, le=86_400_000)
    network_mode: NetworkMode
    writable_storage: WritableStoragePolicy


@dataclass(frozen=True)
class LeaseSnapshot:
    worker_id: str
    lease_id: uuid.UUID
    fencing_token: int
    expires_at: datetime


class FenceAuthority(Protocol):
    def assert_current(self, identity: ExecutionIdentity, now: datetime) -> None: ...


class LeaseFenceAuthority:
    """In-memory test authority; production authority must read trusted control state."""

    def __init__(self) -> None:
        self._leases: dict[uuid.UUID, LeaseSnapshot] = {}

    def set_current(self, snapshot: LeaseSnapshot) -> None:
        self._leases[snapshot.lease_id] = snapshot

    def fence(self, lease_id: uuid.UUID, *, next_token: int) -> None:
        old = self._leases[lease_id]
        self._leases[lease_id] = LeaseSnapshot(
            old.worker_id, old.lease_id, next_token, old.expires_at
        )

    def assert_current(self, identity: ExecutionIdentity, now: datetime) -> None:
        lease = self._leases.get(identity.lease_id)
        if lease is None or lease.worker_id != identity.worker_id:
            raise StaleFenceError("LEASE_NOT_CURRENT")
        if lease.fencing_token != identity.fencing_token:
            raise StaleFenceError("FENCING_TOKEN_STALE")
        if lease.expires_at <= now or identity.lease_expires_at <= now:
            raise ExpiredLeaseError("LEASE_EXPIRED")


@dataclass(frozen=True)
class StagedArtifact:
    execution_id: uuid.UUID
    submission_version_id: uuid.UUID
    digest: str
    size: int
    content: bytes = field(repr=False)
    read_only: bool = True


@dataclass(frozen=True)
class AIKMessage:
    message_id: str
    payload: bytes


@dataclass(frozen=True)
class RuntimeObservation:
    kind: str
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    process_count: int = 1
    memory_bytes: int = 0
    filesystem_bytes: int = 0
    filesystem_inodes: int = 0
    file_descriptors: int = 0
    cpu_millicores: int = 0
    network_attempt: bool = False
    exit_code: int | None = None

    def __post_init__(self) -> None:
        counters = (
            self.stdout_bytes,
            self.stderr_bytes,
            self.process_count,
            self.memory_bytes,
            self.filesystem_bytes,
            self.filesystem_inodes,
            self.file_descriptors,
            self.cpu_millicores,
        )
        if any(type(value) is not int or value < 0 for value in counters):
            raise ValueError("RUNTIME_OBSERVATION_COUNTER_INVALID")
        if self.exit_code is not None and (
            type(self.exit_code) is not int or not -255 <= self.exit_code <= 255
        ):
            raise ValueError("RUNTIME_EXIT_CODE_INVALID")
        if type(self.network_attempt) is not bool:
            raise ValueError("RUNTIME_NETWORK_OBSERVATION_INVALID")


@dataclass(frozen=True)
class SandboxSnapshot:
    identity: ExecutionIdentity
    state: ExecutionState
    staged_digest: str | None
    cleanup: CleanupResult | None
    failure_class: FailureClass | None


class SandboxAdapter(Protocol):
    def create_execution(
        self, identity: ExecutionIdentity, policy: SandboxPolicy
    ) -> SandboxSnapshot: ...
    def stage_artifact(
        self,
        execution_id: uuid.UUID,
        artifact_bytes: bytes,
        submission_version_id: uuid.UUID,
        expected_digest: str,
    ) -> StagedArtifact: ...
    def start(self, execution_id: uuid.UUID) -> SandboxSnapshot: ...
    def send_input(self, execution_id: uuid.UUID, message: AIKMessage) -> None: ...
    def read_output(self, execution_id: uuid.UUID) -> AIKMessage | None: ...
    def inspect(self, execution_id: uuid.UUID) -> SandboxSnapshot: ...
    def terminate(self, execution_id: uuid.UUID) -> SandboxSnapshot: ...
    def apply_supervisor_timeout(
        self, execution_id: uuid.UUID, failure_class: FailureClass
    ) -> SandboxSnapshot: ...
    def collect_evidence(self, execution_id: uuid.UUID) -> SandboxEvidence: ...
    def verify_cleanup(self, execution_id: uuid.UUID) -> CleanupResult: ...
    def destroy(self, execution_id: uuid.UUID) -> SandboxSnapshot: ...


@dataclass(frozen=True)
class SandboxEvidence:
    execution_id: uuid.UUID
    artifact_digest: str
    submission_version_id: uuid.UUID
    state: ExecutionState
    failure_class: FailureClass | None
    stdout_bytes: int
    stderr_bytes: int
    process_count: int
    memory_bytes: int
    filesystem_bytes: int
    filesystem_inodes: int
    file_descriptors: int
    cpu_millicores: int
    started_monotonic: float | None
    observed_monotonic: float
    cleanup: CleanupResult | None


class FakeSandboxAdapter:
    """Inert lifecycle simulator. It never imports, extracts, or executes artifact data."""

    def __init__(
        self,
        authority: FenceAuthority,
        *,
        monotonic=time.monotonic,
        wall_clock=lambda: datetime.now(timezone.utc),
        cleanup_result: CleanupResult = CleanupResult.VERIFIED,
    ) -> None:
        self._authority = authority
        self._monotonic = monotonic
        self._wall_clock = wall_clock
        self._cleanup_result = cleanup_result
        self._records: dict[uuid.UUID, dict] = {}

    def _record(self, execution_id: uuid.UUID) -> dict:
        try:
            return self._records[execution_id]
        except KeyError as exc:
            raise IdentityMismatchError("EXECUTION_NOT_FOUND") from exc

    def _guard(self, record: dict) -> None:
        self._authority.assert_current(record["identity"], self._wall_clock())

    @staticmethod
    def _move(record: dict, state: ExecutionState) -> None:
        current = record["state"]
        if state not in _ALLOWED[current]:
            raise InvalidTransitionError(f"{current.value}_TO_{state.value}_INVALID")
        record["state"] = state

    def _snapshot(self, record: dict) -> SandboxSnapshot:
        return SandboxSnapshot(
            record["identity"],
            record["state"],
            record["staged"].digest if record["staged"] else None,
            record["cleanup"],
            record["failure"],
        )

    def create_execution(
        self, identity: ExecutionIdentity, policy: SandboxPolicy
    ) -> SandboxSnapshot:
        self._authority.assert_current(identity, self._wall_clock())
        if identity.execution_id in self._records:
            raise IdentityMismatchError("EXECUTION_ID_ALREADY_EXISTS")
        record = {
            "identity": identity,
            "policy": policy,
            "state": ExecutionState.CREATED,
            "staged": None,
            "cleanup": None,
            "failure": None,
            "output": [],
            "input": [],
            "input_bytes": 0,
            "output_bytes": 0,
            "started": None,
            "observation": RuntimeObservation("running"),
        }
        self._records[identity.execution_id] = record
        return self._snapshot(record)

    def stage_artifact(
        self,
        execution_id: uuid.UUID,
        artifact_bytes: bytes,
        submission_version_id: uuid.UUID,
        expected_digest: str,
    ) -> StagedArtifact:
        record = self._record(execution_id)
        self._guard(record)
        identity: ExecutionIdentity = record["identity"]
        if submission_version_id != identity.submission_version_id:
            raise IdentityMismatchError("SUBMISSION_VERSION_MISMATCH")
        if expected_digest != identity.artifact_digest:
            raise IdentityMismatchError("EXPECTED_DIGEST_MISMATCH")
        if hashlib.sha256(artifact_bytes).hexdigest() != identity.artifact_digest:
            raise IdentityMismatchError("ARTIFACT_DIGEST_MISMATCH")
        if record["state"] != ExecutionState.CREATED:
            raise InvalidTransitionError("ARTIFACT_STAGE_STATE_INVALID")
        staged = StagedArtifact(
            execution_id,
            submission_version_id,
            identity.artifact_digest,
            len(artifact_bytes),
            bytes(artifact_bytes),
        )
        record["staged"] = staged
        self._move(record, ExecutionState.STAGED)
        return staged

    def start(self, execution_id: uuid.UUID) -> SandboxSnapshot:
        record = self._record(execution_id)
        self._guard(record)
        if record["state"] != ExecutionState.STAGED:
            raise InvalidTransitionError("START_REQUIRES_STAGED_STATE")
        if (
            record["staged"] is None
            or record["staged"].digest != record["identity"].artifact_digest
        ):
            raise IdentityMismatchError("STAGED_ARTIFACT_IDENTITY_MISMATCH")
        self._move(record, ExecutionState.STARTED)
        record["started"] = self._monotonic()
        self._move(record, ExecutionState.RUNNING)
        return self._snapshot(record)

    def send_input(self, execution_id: uuid.UUID, message: AIKMessage) -> None:
        record = self._record(execution_id)
        self._guard(record)
        if record["state"] != ExecutionState.RUNNING:
            raise InvalidTransitionError("INPUT_REQUIRES_RUNNING_STATE")
        next_total = record["input_bytes"] + len(message.payload)
        if next_total > record["policy"].stdout_bytes:
            raise AIKProtocolError("STDIN_LIMIT_EXCEEDED")
        if len(record["input"]) >= 4096:
            raise AIKProtocolError("STDIN_MESSAGE_COUNT_EXCEEDED")
        record["input_bytes"] = next_total
        record["input"].append(message)

    def read_output(self, execution_id: uuid.UUID) -> AIKMessage | None:
        record = self._record(execution_id)
        self._guard(record)
        if record["state"] != ExecutionState.RUNNING:
            raise InvalidTransitionError("OUTPUT_REQUIRES_RUNNING_STATE")
        return record["output"].pop(0) if record["output"] else None

    def queue_inert_output(self, execution_id: uuid.UUID, message: AIKMessage) -> None:
        """Test-only injection of fixed bytes; it does not run an AI or parse an artifact."""
        record = self._record(execution_id)
        next_total = record["output_bytes"] + len(message.payload)
        if next_total > record["policy"].stdout_bytes:
            raise AIKProtocolError("STDOUT_LIMIT_EXCEEDED")
        if len(record["output"]) >= 4096:
            raise AIKProtocolError("STDOUT_MESSAGE_COUNT_EXCEEDED")
        record["output_bytes"] = next_total
        record["output"].append(message)

    def inspect(self, execution_id: uuid.UUID) -> SandboxSnapshot:
        record = self._record(execution_id)
        self._guard(record)
        return self._snapshot(record)

    def simulate_observation(
        self, execution_id: uuid.UUID, observation: RuntimeObservation, policy: SandboxPolicy
    ) -> SandboxSnapshot:
        """Inject an inert observation into the fake and classify policy violations."""
        record = self._record(execution_id)
        self._guard(record)
        if record["state"] != ExecutionState.RUNNING:
            raise InvalidTransitionError("OBSERVATION_REQUIRES_RUNNING_STATE")
        record["observation"] = observation
        if observation.kind in {"startup_timeout", "move_timeout", "total_timeout"}:
            record["failure"] = (
                FailureClass.MOVE_TIMEOUT
                if observation.kind == "move_timeout"
                else FailureClass.STARTUP_FAILURE
            )
            self._move(record, ExecutionState.TIMED_OUT)
        elif (
            observation.stdout_bytes > policy.stdout_bytes
            or observation.stderr_bytes > policy.stderr_bytes
            or observation.process_count > policy.pid_limit
            or observation.memory_bytes > policy.memory_bytes
            or observation.filesystem_bytes > policy.filesystem_bytes
            or observation.filesystem_inodes > policy.filesystem_inodes
            or observation.file_descriptors > policy.file_descriptors
            or observation.cpu_millicores > policy.cpu_millicores
            or observation.network_attempt
        ):
            record["failure"] = (
                FailureClass.PROTOCOL_VIOLATION
                if observation.stdout_bytes > policy.stdout_bytes
                or observation.stderr_bytes > policy.stderr_bytes
                else (
                    FailureClass.SANDBOX_VIOLATION
                    if observation.network_attempt
                    else FailureClass.RESOURCE_EXHAUSTION
                )
            )
            self._move(record, ExecutionState.RESOURCE_EXCEEDED)
        elif observation.kind == "malformed_protocol":
            record["failure"] = FailureClass.PROTOCOL_VIOLATION
            self._move(record, ExecutionState.PROTOCOL_FAILED)
        elif observation.kind == "crash":
            record["failure"] = FailureClass.PARTICIPANT_PROCESS_FAILURE
            self._move(record, ExecutionState.CRASHED)
        elif observation.kind == "completed":
            self._move(record, ExecutionState.COMPLETED)
        return self._snapshot(record)

    def terminate(self, execution_id: uuid.UUID) -> SandboxSnapshot:
        record = self._record(execution_id)
        if record["state"] == ExecutionState.TERMINATED:
            return self._snapshot(record)
        if record["state"] in _TERMINAL:
            return self._snapshot(record)
        self._move(record, ExecutionState.TERMINATED)
        return self._snapshot(record)

    def apply_supervisor_timeout(
        self, execution_id: uuid.UUID, failure_class: FailureClass
    ) -> SandboxSnapshot:
        record = self._record(execution_id)
        if record["state"] not in {ExecutionState.STARTED, ExecutionState.RUNNING}:
            raise InvalidTransitionError("TIMEOUT_REQUIRES_ACTIVE_STATE")
        record["failure"] = failure_class
        self._move(record, ExecutionState.TIMED_OUT)
        return self._snapshot(record)

    def collect_evidence(self, execution_id: uuid.UUID) -> SandboxEvidence:
        record = self._record(execution_id)
        observation: RuntimeObservation = record["observation"]
        return SandboxEvidence(
            execution_id=execution_id,
            artifact_digest=record["identity"].artifact_digest,
            submission_version_id=record["identity"].submission_version_id,
            state=record["state"],
            failure_class=record["failure"],
            stdout_bytes=min(observation.stdout_bytes, 2**63 - 1),
            stderr_bytes=min(observation.stderr_bytes, 2**63 - 1),
            process_count=min(observation.process_count, 2**31 - 1),
            memory_bytes=min(observation.memory_bytes, 2**63 - 1),
            filesystem_bytes=min(observation.filesystem_bytes, 2**63 - 1),
            filesystem_inodes=min(observation.filesystem_inodes, 2**63 - 1),
            file_descriptors=min(observation.file_descriptors, 2**31 - 1),
            cpu_millicores=min(observation.cpu_millicores, 2**31 - 1),
            started_monotonic=record["started"],
            observed_monotonic=self._monotonic(),
            cleanup=record["cleanup"],
        )

    def verify_cleanup(self, execution_id: uuid.UUID) -> CleanupResult:
        record = self._record(execution_id)
        if record["state"] not in _TERMINAL | {
            ExecutionState.CLEANUP_FAILED,
            ExecutionState.CLEANUP_UNKNOWN,
        }:
            raise InvalidTransitionError("CLEANUP_REQUIRES_TERMINAL_STATE")
        result = self._cleanup_result
        record["cleanup"] = result
        if result == CleanupResult.FAILED and record["state"] not in {
            ExecutionState.CLEANUP_FAILED,
            ExecutionState.CLEANUP_UNKNOWN,
        }:
            self._move(record, ExecutionState.CLEANUP_FAILED)
        elif result == CleanupResult.UNKNOWN and record["state"] not in {
            ExecutionState.CLEANUP_FAILED,
            ExecutionState.CLEANUP_UNKNOWN,
        }:
            self._move(record, ExecutionState.CLEANUP_UNKNOWN)
        return result

    def destroy(self, execution_id: uuid.UUID) -> SandboxSnapshot:
        record = self._record(execution_id)
        if record["cleanup"] != CleanupResult.VERIFIED:
            raise InvalidTransitionError("CLEANUP_NOT_VERIFIED")
        self._move(record, ExecutionState.DESTROYED)
        record["staged"] = None
        record["output"].clear()
        record["input"].clear()
        return self._snapshot(record)


class MonotonicClock(Protocol):
    def __call__(self) -> float: ...


class SandboxSupervisor:
    """Trusted-side policy coordinator; observations are never participant authority."""

    def __init__(
        self,
        adapter: SandboxAdapter,
        authority: FenceAuthority,
        *,
        monotonic: MonotonicClock = time.monotonic,
    ) -> None:
        self._adapter = adapter
        self._authority = authority
        self._monotonic = monotonic
        self._deadlines: dict[uuid.UUID, dict[str, tuple[float, FailureClass]]] = {}
        self._identities: dict[uuid.UUID, ExecutionIdentity] = {}

    def begin_deadline(self, identity: ExecutionIdentity, policy: SandboxPolicy) -> None:
        """Set the total execution deadline once; callers cannot extend it."""
        self._authority.assert_current(identity, datetime.now(timezone.utc))
        if "total" in self._deadlines.get(identity.execution_id, {}):
            raise ValueError("TOTAL_DEADLINE_ALREADY_SET")
        deadlines = self._deadlines.setdefault(identity.execution_id, {})
        deadlines["total"] = (
            self._monotonic() + policy.total_timeout_ms / 1000,
            FailureClass.EXECUTION_TIMEOUT,
        )
        self._identities[identity.execution_id] = identity

    def arm_startup_deadline(self, identity: ExecutionIdentity, policy: SandboxPolicy) -> None:
        self._arm_deadline(
            identity, "startup", policy.startup_timeout_ms, FailureClass.STARTUP_FAILURE
        )

    def arm_move_deadline(self, identity: ExecutionIdentity, policy: SandboxPolicy) -> None:
        self._arm_deadline(identity, "move", policy.move_timeout_ms, FailureClass.MOVE_TIMEOUT)

    def _arm_deadline(
        self,
        identity: ExecutionIdentity,
        kind: str,
        timeout_ms: int,
        failure_class: FailureClass,
    ) -> None:
        self._authority.assert_current(identity, datetime.now(timezone.utc))
        deadlines = self._deadlines.setdefault(identity.execution_id, {})
        deadlines[kind] = (self._monotonic() + timeout_ms / 1000, failure_class)
        self._identities[identity.execution_id] = identity

    def supervise(self, execution_id: uuid.UUID) -> SandboxSnapshot:
        try:
            identity = self._identities[execution_id]
            self._authority.assert_current(identity, datetime.now(timezone.utc))
        except (StaleFenceError, ExpiredLeaseError):
            return self._adapter.terminate(execution_id)
        snapshot = self._adapter.inspect(execution_id)
        now = self._monotonic()
        expired = [
            deadline
            for deadline in self._deadlines.get(execution_id, {}).values()
            if now >= deadline[0]
        ]
        if expired:
            _, failure_class = min(expired, key=lambda deadline: deadline[0])
            return self._adapter.apply_supervisor_timeout(execution_id, failure_class)
        return snapshot

    @staticmethod
    def classify(observation: RuntimeObservation) -> FailureClass | None:
        if observation.kind in {"startup_timeout", "total_timeout"}:
            return FailureClass.STARTUP_FAILURE
        if observation.kind == "move_timeout":
            return FailureClass.MOVE_TIMEOUT
        if observation.kind == "malformed_protocol":
            return FailureClass.PROTOCOL_VIOLATION
        if observation.kind == "crash":
            return FailureClass.PARTICIPANT_PROCESS_FAILURE
        if observation.network_attempt:
            return FailureClass.SANDBOX_VIOLATION
        if any(
            (
                observation.process_count > 16,
                observation.memory_bytes > 268_435_456,
                observation.filesystem_bytes > 33_554_432,
                observation.filesystem_inodes > 128,
                observation.file_descriptors > 64,
                observation.cpu_millicores > 1000,
                observation.stdout_bytes > 1_048_576,
                observation.stderr_bytes > 262_144,
            )
        ):
            return FailureClass.RESOURCE_EXHAUSTION
        return None


class AIKTransport:
    """Bounded inert JSONL encoder/parser; it owns bytes, never a process or chess state."""

    def __init__(
        self,
        *,
        message_bytes: int = 65_536,
        stdout_bytes: int = 1_048_576,
        stderr_bytes: int = 262_144,
        monotonic=time.monotonic,
    ) -> None:
        if not 1 <= message_bytes <= 65_536 or not 1 <= stdout_bytes <= 1_048_576:
            raise ValueError("AIK_OUTPUT_POLICY_INVALID")
        if not 1 <= stderr_bytes <= 262_144:
            raise ValueError("AIK_STDERR_POLICY_INVALID")
        self.message_bytes = message_bytes
        self.stdout_limit = stdout_bytes
        self.stderr_limit = stderr_bytes
        self._monotonic = monotonic
        self._stdout_total = 0
        self._stderr_total = 0
        self._seen_payloads: set[str] = set()
        self._deadline: float | None = None
        self._stdin_total = 0

    def set_deadline(self, deadline_monotonic: float) -> None:
        self._deadline = deadline_monotonic

    def encode_controller_message(self, message_id: str, payload: dict) -> AIKMessage:
        self._check_deadline()
        if not message_id or len(message_id.encode("utf-8")) > 64:
            raise AIKProtocolError("MESSAGE_ID_INVALID")
        if "request_id" in payload:
            raise AIKProtocolError("MESSAGE_ID_RESERVED")
        if payload.get("protocol_version") != "aik-jsonl/1" or payload.get("type") not in {
            "hello",
            "new_game",
            "position",
            "go",
            "claim_rejected",
            "game_over",
            "shutdown",
        }:
            raise AIKProtocolError("CONTROLLER_MESSAGE_SCHEMA_INVALID")
        try:
            data = (
                json.dumps(
                    {"request_id": message_id, **payload}, separators=(",", ":"), allow_nan=False
                ).encode("utf-8")
                + b"\n"
            )
        except (TypeError, ValueError, UnicodeEncodeError) as exc:
            raise AIKProtocolError("MESSAGE_ENCODING_INVALID") from exc
        if len(data) > self.message_bytes:
            raise AIKProtocolError("MESSAGE_TOO_LARGE")
        self._stdin_total += len(data)
        if self._stdin_total > self.stdout_limit:
            raise AIKProtocolError("STDIN_LIMIT_EXCEEDED")
        return AIKMessage(message_id, data)

    def _check_deadline(self) -> None:
        if self._deadline is not None and self._monotonic() > self._deadline:
            raise AIKProtocolError("LATE_MESSAGE")

    def parse_stdout(self, message: AIKMessage) -> dict:
        self._check_deadline()
        raw = message.payload
        self._stdout_total += len(raw)
        if self._stdout_total > self.stdout_limit:
            raise AIKProtocolError("STDOUT_LIMIT_EXCEEDED")
        if len(raw) > self.message_bytes or not raw.endswith(b"\n") or raw.count(b"\n") != 1:
            raise AIKProtocolError("JSONL_LINE_INVALID")
        try:
            text = raw[:-1].decode("utf-8", errors="strict")

            def pairs(items):
                result = {}
                for key, value in items:
                    if key in result:
                        raise AIKProtocolError("DUPLICATE_JSON_KEY")
                    result[key] = value
                return result

            parsed = json.loads(
                text,
                object_pairs_hook=pairs,
                parse_constant=lambda _: (_ for _ in ()).throw(AIKProtocolError("NONFINITE_JSON")),
            )
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise AIKProtocolError("JSONL_MESSAGE_INVALID") from exc
        if not isinstance(parsed, dict) or parsed.get("request_id") != message.message_id:
            raise AIKProtocolError("MESSAGE_ID_MISMATCH")
        message_type = parsed.get("type")
        fields = {
            "ready": {"protocol_version", "type", "request_id"},
            "ack": {"protocol_version", "type", "request_id"},
            "bestmove": {"protocol_version", "type", "request_id", "move"},
            "claim_draw": {"protocol_version", "type", "request_id", "reason", "intended_move"},
        }
        if message_type not in fields or set(parsed) != fields[message_type]:
            raise AIKProtocolError("PARTICIPANT_MESSAGE_SCHEMA_INVALID")
        if parsed.get("protocol_version") != "aik-jsonl/1":
            raise AIKProtocolError("PROTOCOL_VERSION_INVALID")
        if (
            not isinstance(parsed.get("request_id"), str)
            or not 1 <= len(parsed["request_id"].encode("utf-8")) <= 64
        ):
            raise AIKProtocolError("MESSAGE_ID_INVALID")
        if message_type == "bestmove":
            move = parsed.get("move")
            if not isinstance(move, str) or not re.fullmatch(r"[a-h][1-8][a-h][1-8][qrbn]?", move):
                raise AIKProtocolError("MOVE_PROPOSAL_FORMAT_INVALID")
        if message_type == "claim_draw":
            if parsed.get("reason") not in {"threefold_repetition", "fifty_move_rule"}:
                raise AIKProtocolError("DRAW_CLAIM_REASON_INVALID")
            intended_move = parsed.get("intended_move")
            if intended_move is not None and (
                not isinstance(intended_move, str)
                or not re.fullmatch(r"[a-h][1-8][a-h][1-8][qrbn]?", intended_move)
            ):
                raise AIKProtocolError("DRAW_CLAIM_MOVE_FORMAT_INVALID")
        digest = hashlib.sha256(raw).hexdigest()
        if digest in self._seen_payloads:
            raise AIKProtocolError("DUPLICATE_MESSAGE")
        self._seen_payloads.add(digest)
        return parsed

    def record_stderr(self, data: bytes) -> bytes:
        self._check_deadline()
        self._stderr_total += len(data)
        if self._stderr_total > self.stderr_limit:
            raise AIKProtocolError("STDERR_LIMIT_EXCEEDED")
        return bytes(data)
