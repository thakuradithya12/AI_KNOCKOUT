"""Worker agent identity and lease guard tests; no participant code is launched."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from threading import RLock

import pytest

from app.sandbox.contract import (
    ExecutionIdentity,
    ExecutionSubjectKind,
    ExpiredLeaseError,
    StaleFenceError,
)
from app.workers.agent import (
    LEASE_SAFETY_SECONDS,
    RenewedLeaseFenceAuthority,
    WorkerAgentError,
    _LeaseState,
)


def _identity() -> ExecutionIdentity:
    return ExecutionIdentity(
        execution_id=uuid.uuid4(),
        submission_version_id=uuid.uuid4(),
        artifact_digest="a" * 64,
        policy_profile="development-v1",
        runtime_profile="python-3.12",
        worker_id="worker-001",
        lease_id=uuid.uuid4(),
        fencing_token=1,
        protocol_version="aik-jsonl/1",
        lease_expires_at=datetime.now(timezone.utc) + timedelta(seconds=30),
        subject_kind=ExecutionSubjectKind.PARTICIPANT,
        match_id=uuid.uuid4(),
        policy_digest="b" * 64,
    )


def test_fence_authority_fails_closed_when_renewal_is_revoked() -> None:
    identity = _identity()
    state = _LeaseState(expires_monotonic=100, sequence=2, revoked=True)
    authority = RenewedLeaseFenceAuthority(identity, state, RLock(), monotonic=lambda: 10)
    with pytest.raises(StaleFenceError):
        authority.assert_current(identity, datetime.now(timezone.utc))


def test_fence_authority_fails_closed_before_unconfirmed_lease_expiry() -> None:
    identity = _identity()
    state = _LeaseState(expires_monotonic=10 + LEASE_SAFETY_SECONDS, sequence=2)
    authority = RenewedLeaseFenceAuthority(identity, state, RLock(), monotonic=lambda: 10)
    with pytest.raises(ExpiredLeaseError):
        authority.assert_current(identity, datetime.now(timezone.utc))


def test_job_subject_must_have_exactly_one_typed_identity() -> None:
    from app.workers.agent import WorkerAgent

    class Job:
        execution_kind = "PARTICIPANT"
        submission_version_id = None
        participant_id = "P-1"
        submission_id = uuid.uuid4()
        house_ai_id = None
        house_ai_version = None

    with pytest.raises(WorkerAgentError, match="JOB_SUBJECT_IDENTITY_INVALID"):
        WorkerAgent._check_subject(Job())


def test_worker_agent_has_no_match_controller_injection_or_import():
    import inspect

    import app.workers.agent as agent_module
    from app.workers.agent import WorkerAgent

    assert "match_context" not in inspect.signature(WorkerAgent).parameters
    assert "MatchController" not in vars(agent_module)
