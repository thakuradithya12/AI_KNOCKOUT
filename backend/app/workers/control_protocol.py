"""Strict, bounded contracts for the private worker control channel."""

from __future__ import annotations

import json
import uuid
from datetime import datetime
from enum import Enum
from typing import Literal

from pydantic import Field, StrictInt, StrictStr, model_validator

from app.workers.schemas import StrictModel


class ControlOperation(str, Enum):
    COMMAND_ISSUE = "COMMAND_ISSUE"
    FENCE_EXECUTION = "FENCE_EXECUTION"
    ABORT_MATCH = "ABORT_MATCH"
    CONTROL_ACK = "CONTROL_ACK"


class ControlAckResult(str, Enum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    FENCED = "FENCED"
    ABORTED = "ABORTED"


class ControlEnvelope(StrictModel):
    protocol_version: Literal["aik-control/1"] = "aik-control/1"
    operation: ControlOperation


class CommandIssueRequest(ControlEnvelope):
    operation: Literal[ControlOperation.COMMAND_ISSUE] = ControlOperation.COMMAND_ISSUE
    match_id: uuid.UUID
    execution_id: uuid.UUID
    side_id: uuid.UUID
    worker_id: StrictStr = Field(min_length=3, max_length=64)
    worker_certificate_fingerprint: StrictStr = Field(pattern="^[0-9a-f]{64}$")
    side: Literal["WHITE", "BLACK"]
    lease_id: uuid.UUID
    fence_token: StrictInt = Field(ge=1, le=2**63 - 1)
    attempt: StrictInt = Field(ge=1, le=2**31 - 1)


class CommandIssueResponse(ControlEnvelope):
    operation: Literal[ControlOperation.COMMAND_ISSUE] = ControlOperation.COMMAND_ISSUE
    status: Literal["ACCEPTED", "IDLE", "TERMINAL", "REJECTED", "FENCED", "ABORTED"]
    match_id: uuid.UUID
    execution_id: uuid.UUID
    side_id: uuid.UUID
    command_id: StrictStr | None = Field(default=None, min_length=1, max_length=64)
    command_type: Literal["HELLO", "NEW_GAME", "POSITION", "GO"] | None = None
    side: Literal["WHITE", "BLACK"]
    attempt: StrictInt = Field(ge=1)
    lease_id: uuid.UUID
    fence_token: StrictInt = Field(ge=1)
    turn_started_at: datetime | None = None
    deadline_at: datetime | None = None
    clocks_ms: dict[Literal["white", "black"], StrictInt] | None = None
    timeout_seconds: float | None = Field(default=None, gt=0, le=300)
    message: StrictStr | None = Field(default=None, min_length=1, max_length=65_536)

    @model_validator(mode="after")
    def bounded_response(self):
        if self.status == "ACCEPTED" and (
            self.command_id is None or self.command_type is None or self.message is None
        ):
            raise ValueError("CONTROL_COMMAND_INCOMPLETE")
        if self.deadline_at and self.turn_started_at and self.deadline_at < self.turn_started_at:
            raise ValueError("CONTROL_DEADLINE_INVALID")
        if self.clocks_ms is not None and (
            set(self.clocks_ms) != {"white", "black"}
            or any(value < 0 for value in self.clocks_ms.values())
        ):
            raise ValueError("CONTROL_CLOCK_INVALID")
        return self


class ControlResponseRequest(ControlEnvelope):
    operation: Literal[ControlOperation.CONTROL_ACK] = ControlOperation.CONTROL_ACK
    match_id: uuid.UUID
    side_id: uuid.UUID
    execution_id: uuid.UUID
    worker_id: StrictStr = Field(min_length=3, max_length=64)
    worker_certificate_fingerprint: StrictStr = Field(pattern="^[0-9a-f]{64}$")
    side: Literal["WHITE", "BLACK"]
    lease_id: uuid.UUID
    fence_token: StrictInt = Field(ge=1, le=2**63 - 1)
    attempt: StrictInt = Field(ge=1, le=2**31 - 1)
    command_id: StrictStr = Field(min_length=1, max_length=64)
    payload: StrictStr = Field(min_length=1, max_length=65_536)


class FenceExecutionRequest(ControlEnvelope):
    operation: Literal[ControlOperation.FENCE_EXECUTION] = ControlOperation.FENCE_EXECUTION
    match_id: uuid.UUID
    execution_id: uuid.UUID
    lease_id: uuid.UUID
    fence_token: StrictInt = Field(ge=1, le=2**63 - 1)
    reason: StrictStr = Field(min_length=1, max_length=96)


class AbortMatchRequest(ControlEnvelope):
    operation: Literal[ControlOperation.ABORT_MATCH] = ControlOperation.ABORT_MATCH
    match_id: uuid.UUID
    execution_id: uuid.UUID
    reason: StrictStr = Field(min_length=1, max_length=96)


class ControlAckRequest(ControlEnvelope):
    operation: Literal[ControlOperation.CONTROL_ACK] = ControlOperation.CONTROL_ACK
    command_id: StrictStr = Field(min_length=1, max_length=64)
    match_id: uuid.UUID
    execution_id: uuid.UUID
    fence_token: StrictInt = Field(ge=1, le=2**63 - 1)
    result: ControlAckResult


def validate_control_body_size(value: ControlEnvelope, *, limit: int = 16_384) -> None:
    """Enforce a canonical encoded-size limit in addition to field bounds."""
    encoded = json.dumps(value.model_dump(mode="json"), separators=(",", ":")).encode()
    if len(encoded) > limit:
        raise ValueError("CONTROL_MESSAGE_TOO_LARGE")
