import uuid
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.workers.control_protocol import (
    CommandIssueRequest,
    CommandIssueResponse,
    ControlAckRequest,
    ControlAckResult,
    FenceExecutionRequest,
    validate_control_body_size,
)


def _command():
    return CommandIssueRequest(
        match_id=uuid.uuid4(), execution_id=uuid.uuid4(), side_id=uuid.uuid4(),
        worker_id="worker-001",
        side="WHITE", lease_id=uuid.uuid4(), fence_token=1, attempt=1,
        worker_certificate_fingerprint="a" * 64,
    )


def test_control_messages_are_versioned_strict_and_bounded():
    command = _command()
    assert command.protocol_version == "aik-control/1"
    with pytest.raises(ValidationError):
        CommandIssueRequest.model_validate({**command.model_dump(), "worker_id": "x", "extra": 1})
    with pytest.raises(ValueError, match="CONTROL_MESSAGE_TOO_LARGE"):
        validate_control_body_size(command, limit=1)


def test_control_response_rejects_deadline_before_start():
    now = datetime.now(timezone.utc)
    with pytest.raises(ValidationError, match="CONTROL_DEADLINE_INVALID"):
        CommandIssueResponse(
            status="ACCEPTED", match_id=uuid.uuid4(), execution_id=uuid.uuid4(),
            side_id=uuid.uuid4(),
            command_id="command-1", command_type="GO", side="WHITE", attempt=1,
            lease_id=uuid.uuid4(), fence_token=1, turn_started_at=now,
            deadline_at=now - timedelta(seconds=1), clocks_ms={"white": 1, "black": 1},
            message="{}",
        )


def test_fence_and_ack_contracts_are_match_scoped():
    match_id, execution_id, lease_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    fence = FenceExecutionRequest(
        match_id=match_id, execution_id=execution_id, lease_id=lease_id,
        fence_token=4, reason="commit-ambiguous",
    )
    ack = ControlAckRequest(
        command_id="cmd-4", match_id=match_id, execution_id=execution_id,
        fence_token=4, result=ControlAckResult.FENCED,
    )
    assert fence.match_id == ack.match_id
    assert ack.result == ControlAckResult.FENCED
