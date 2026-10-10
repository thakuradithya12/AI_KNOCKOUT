"""Strict worker protocol request/response schemas; unknown fields fail closed."""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StrictStr,
    field_validator,
    model_validator,
)

from app.models.worker import ExecutionFailureClass


class StrictModel(BaseModel):
    # Keep structured UUID/datetime decoding for JSON transport while primitive
    # fields below use StrictStr/StrictInt/StrictBool to reject coercion.
    model_config = ConfigDict(extra="forbid")


class WorkerCapabilities(StrictModel):
    sandbox_backend: Literal["KATA", "MOCK_TEST_ONLY"]
    sandbox_version: StrictStr = Field(min_length=1, max_length=128)
    sandbox_digest: StrictStr = Field(pattern="^[0-9a-f]{64}$")
    protocol_versions: list[StrictStr] = Field(max_length=8)
    execution_modes: list[Literal["MOCK_PROTOCOL_ONLY", "KATA_AI_EXECUTION"]] = Field(
        default_factory=lambda: ["MOCK_PROTOCOL_ONLY"], max_length=2
    )

    @field_validator("protocol_versions")
    @classmethod
    def bound_protocol_versions(cls, values: list[str]) -> list[str]:
        if any(not value or len(value.encode("utf-8")) > 64 for value in values):
            raise ValueError("protocol version entries must be 1-64 UTF-8 bytes")
        encoded = json.dumps(values, ensure_ascii=True, separators=(",", ":")).encode("ascii")
        if len(encoded) > 512:
            raise ValueError("protocol metadata exceeds 512 bytes")
        return values

    @field_validator("execution_modes")
    @classmethod
    def unique_execution_modes(cls, values: list[str]) -> list[str]:
        if not values or len(values) != len(set(values)):
            raise ValueError("execution modes must be non-empty and unique")
        return values


class WorkerEnrollmentIn(StrictModel):
    worker_id: StrictStr = Field(min_length=3, max_length=64)
    enrollment_token: StrictStr = Field(min_length=32, max_length=128)
    capabilities: WorkerCapabilities
    software_version: StrictStr = Field(min_length=1, max_length=128)
    runtime_version: StrictStr = Field(min_length=1, max_length=128)


class WorkerIdentityOut(StrictModel):
    worker_id: str
    status: str
    certificate_fingerprint: str
    expires_at: datetime


class LeaseOperation(StrictModel):
    lease_id: uuid.UUID
    fencing_token: StrictInt = Field(ge=1)
    sequence: StrictInt = Field(ge=1)


class MatchCommandRequest(StrictModel):
    lease_id: uuid.UUID
    fencing_token: StrictInt = Field(ge=1)
    attempt: StrictInt = Field(ge=1, le=2**31 - 1)


class MatchResponseRequest(MatchCommandRequest):
    command_id: StrictStr = Field(min_length=1, max_length=64)
    payload: StrictStr = Field(min_length=1, max_length=65_536)


class LeaseOut(StrictModel):
    lease_id: uuid.UUID
    fencing_token: int
    expires_at: datetime
    next_sequence: int


class ResourcePolicy(StrictModel):
    cpu_millicores: StrictInt = Field(default=1000, ge=1, le=1000)
    memory_bytes: StrictInt = Field(default=268435456, ge=1, le=268435456)
    pid_limit: StrictInt = Field(default=16, ge=1, le=16)
    tmpfs_bytes: StrictInt = Field(default=33554432, ge=1, le=33554432)
    inode_limit: StrictInt = Field(default=128, ge=1, le=128)
    nofile_limit: StrictInt = Field(default=64, ge=1, le=64)
    stdout_line_bytes: StrictInt = Field(default=65536, ge=1, le=65536)
    stdout_total_bytes: StrictInt = Field(default=1048576, ge=1, le=1048576)
    stderr_total_bytes: StrictInt = Field(default=262144, ge=1, le=262144)
    startup_timeout_ms: StrictInt = Field(default=5000, ge=1, le=5000)
    ready_timeout_ms: StrictInt = Field(default=2000, ge=1, le=2000)
    # These values remain required policy inputs until the organizer approves them.
    move_timeout_ms: StrictInt = Field(ge=1, le=3600000)
    process_lifetime_ms: StrictInt = Field(ge=1, le=86400000)
    concurrency_per_worker: StrictInt = Field(ge=1, le=128)
    network_mode: Literal["DENY_ALL"] = "DENY_ALL"


class HeartbeatIn(StrictModel):
    software_version: StrictStr = Field(min_length=1, max_length=128)
    runtime_version: StrictStr = Field(min_length=1, max_length=128)
    capabilities: WorkerCapabilities
    security_state: StrictStr = Field(default="OK", min_length=1, max_length=32)


class JobAcknowledgementIn(LeaseOperation):
    accepted: StrictBool


class ArtifactCapabilityIn(LeaseOperation):
    pass


class LeaseRenewalIn(LeaseOperation):
    pass


class ArtifactCapabilityOut(StrictModel):
    capability: str
    expires_at: datetime
    operation: str = "DOWNLOAD_ARCHIVE"
    max_uses: int = 1


class EvidenceEvent(str, Enum):
    STARTED = "STARTED"
    ARTIFACT_VERIFIED = "ARTIFACT_VERIFIED"
    RESOURCE_LIMIT = "RESOURCE_LIMIT"
    PROTOCOL_VIOLATION = "PROTOCOL_VIOLATION"
    SANDBOX_VIOLATION = "SANDBOX_VIOLATION"
    FAILED = "FAILED"


class EvidenceIn(LeaseOperation):
    event_type: EvidenceEvent
    started_at: datetime | None = None
    ended_at: datetime | None = None
    exit_code: StrictInt | None = Field(default=None, ge=-255, le=255)
    failure_class: ExecutionFailureClass | None = None
    termination_reason: StrictStr | None = Field(default=None, max_length=64)
    resource_usage: dict[str, StrictInt] = Field(default_factory=dict)
    artifact_digest_verified: StrictBool = False
    stdout_bytes: StrictInt = Field(default=0, ge=0, le=1048576)
    stdout_sha256: StrictStr | None = None
    stderr_bytes: StrictInt = Field(default=0, ge=0, le=262144)
    stderr_sha256: StrictStr | None = None
    protocol_violation: StrictStr | None = Field(default=None, max_length=64)
    sandbox_violation: StrictStr | None = Field(default=None, max_length=64)

    @model_validator(mode="after")
    def validate_untrusted_timeline(self):
        # Worker clocks are untrusted. Enforce internal ordering only; recorded_at
        # on the server remains the trusted chronology.
        if any(
            value is not None and value.tzinfo is None
            for value in (self.started_at, self.ended_at)
        ):
            raise ValueError("worker telemetry timestamps must include a timezone")
        if self.started_at is not None and self.ended_at is not None:
            start = self.started_at
            end = self.ended_at
            if end < start:
                raise ValueError("worker telemetry timestamps must be ordered")
        return self

    @field_validator("stdout_sha256", "stderr_sha256")
    @classmethod
    def validate_digest(cls, value: str | None) -> str | None:
        if value is not None and not re.fullmatch(r"[0-9a-f]{64}", value):
            raise ValueError("digest must be lowercase SHA-256 hex")
        return value

    @field_validator("resource_usage")
    @classmethod
    def validate_resource_names(cls, value: dict[str, int]) -> dict[str, int]:
        allowed = {"cpu_usec", "memory_peak_bytes", "pids_peak", "tmpfs_peak_bytes"}
        if set(value) - allowed or any(v < 0 for v in value.values()):
            raise ValueError("unsupported resource usage fields")
        return value


class CleanupIn(LeaseOperation):
    cleanup_verified: StrictBool
    final_status: str = Field(pattern="^(COMPLETED|FAILED)$")
    failure_class: ExecutionFailureClass | None = None


class WorkerOperationOut(StrictModel):
    accepted: bool = True
    execution_id: uuid.UUID | None = None
    sequence: int | None = None
    status: str | None = None


class JobOut(StrictModel):
    execution_id: uuid.UUID
    execution_mode: Literal["MOCK_PROTOCOL_ONLY", "KATA_AI_EXECUTION"]
    execution_kind: Literal["PARTICIPANT", "HOUSE_AI"]
    match_id: uuid.UUID | None
    match_side_id: uuid.UUID | None = None
    match_purpose: Literal["PEER_TEST", "OFFICIAL", "DEVELOPMENT"] | None = None
    worker_id: str
    participant_id: str | None
    submission_id: uuid.UUID | None
    submission_version: int | None
    submission_version_id: uuid.UUID | None = None
    house_ai_id: str | None = None
    house_ai_version: int | None = None
    artifact_sha256: str
    immutable_artifact_ref: str
    runtime_profile: str
    protocol_version: str
    initial_state: dict[str, Any]
    resource_policy: ResourcePolicy
    policy_digest: str | None = None
    attempt_number: int
    created_at: datetime
    execution_expires_at: datetime
    lease: LeaseOut
