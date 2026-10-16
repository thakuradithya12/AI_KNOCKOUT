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


def _sample_job(
    *,
    match_purpose: str = "OFFICIAL",
    match_side_id: uuid.UUID | None = None,
    policy_digest: str | None = None,
    worker_id: str = "worker-001",
    execution_mode: str = "KATA_AI_EXECUTION",
    protocol_version: str = "aik-jsonl/1",
    runtime_profile: str = "python-3.12",
):
    from app.workers.schemas import JobOut, LeaseOut, ResourcePolicy

    now = datetime.now(timezone.utc)
    return JobOut(
        execution_id=uuid.uuid4(),
        execution_mode=execution_mode,
        execution_kind="PARTICIPANT",
        match_id=uuid.uuid4(),
        match_side_id=match_side_id if match_side_id is not None else uuid.uuid4(),
        match_purpose=match_purpose,
        worker_id=worker_id,
        participant_id="part-1",
        submission_id=uuid.uuid4(),
        submission_version=1,
        submission_version_id=uuid.uuid4(),
        artifact_sha256="a" * 64,
        immutable_artifact_ref="quarantine/part-1",
        runtime_profile=runtime_profile,
        protocol_version=protocol_version,
        initial_state={"purpose": match_purpose},
        resource_policy=ResourcePolicy(
            move_timeout_ms=2000,
            process_lifetime_ms=900000,
            concurrency_per_worker=1,
        ),
        policy_digest=policy_digest,
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


def test_worker_agent_supported_purposes_accepted():
    from unittest.mock import MagicMock
    from app.models.match import MatchPurpose
    from app.workers.agent import WorkerAgent

    mock_client = MagicMock()
    mock_client.worker_id = "worker-001"
    agent = WorkerAgent(client=mock_client, adapter_factory=MagicMock())

    job_official = _sample_job(match_purpose=MatchPurpose.OFFICIAL.value, policy_digest=None)
    assert job_official.match_purpose == "OFFICIAL"

    job_peer = _sample_job(match_purpose=MatchPurpose.PEER_TEST.value, policy_digest=None)
    assert job_peer.match_purpose == "PEER_TEST"

    job_dev = _sample_job(match_purpose=MatchPurpose.DEVELOPMENT.value, policy_digest="d" * 64)
    assert job_dev.match_purpose == "DEVELOPMENT"


def test_worker_agent_rejects_unsupported_purpose():
    from unittest.mock import MagicMock
    from app.workers.agent import WorkerAgent

    mock_client = MagicMock()
    mock_client.worker_id = "worker-001"
    agent = WorkerAgent(client=mock_client, adapter_factory=MagicMock())

    job = _sample_job(match_purpose="OFFICIAL")
    object.__setattr__(job, "match_purpose", "ARBITRARY_PURPOSE")
    with pytest.raises(WorkerAgentError, match="JOB_PURPOSE_UNSUPPORTED"):
        agent.execute(job)


def test_worker_agent_rejects_missing_match_side_id():
    from unittest.mock import MagicMock
    from app.workers.agent import WorkerAgent

    mock_client = MagicMock()
    mock_client.worker_id = "worker-001"
    agent = WorkerAgent(client=mock_client, adapter_factory=MagicMock())

    job = _sample_job(match_purpose="OFFICIAL")
    object.__setattr__(job, "match_side_id", None)
    with pytest.raises(WorkerAgentError, match="JOB_MATCH_BINDING_INCOMPLETE"):
        agent.execute(job)


def test_worker_agent_development_requires_policy_digest():
    from unittest.mock import MagicMock
    from app.workers.agent import WorkerAgent

    mock_client = MagicMock()
    mock_client.worker_id = "worker-001"
    agent = WorkerAgent(client=mock_client, adapter_factory=MagicMock())

    job = _sample_job(match_purpose="DEVELOPMENT", policy_digest=None)
    with pytest.raises(WorkerAgentError, match="JOB_POLICY_OR_PROTOCOL_INVALID"):
        agent.execute(job)

