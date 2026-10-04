from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.matches.controller import (
    FakeRulesAdapter,
    MatchClock,
    MatchConfiguration,
    MatchController,
    MatchSideIdentity,
    position_identity,
)
from app.matches.controller import (
    MatchExecutionBinding as MatchExecutionBindingValue,
)
from app.matches.service import (
    MatchServiceError,
    begin_initialization,
    bind_execution,
    create_match,
    mark_execution_ready,
    mark_match_running,
    persist_controller_update,
)
from app.models.competition import ArtifactState, CompetitionEvent
from app.models.match import (
    Match,
    MatchExecutionBinding,
    MatchMove,
    MatchSide,
    MatchSideColor,
    MatchState,
)
from app.models.worker import (
    ExecutionJobStatus,
    Worker,
    WorkerExecutionJob,
    WorkerStatus,
)
from app.sandbox.contract import AIKMessage, AIKTransport
from app.services import quarantine_storage
from app.services.submissions import OrganizerApproval, approve_submission_for_eligibility
from tests.unit.test_submission_validation import _valid_tar


def _configuration():
    return MatchConfiguration(
        "aik-jsonl/1",
        "python-3.12",
        "policy-v1",
        "rules-v1",
        "startpos",
        60_000,
        500,
        5_000,
        "d" * 64,
    )


async def _eligible_version(api_client: httpx.AsyncClient, db_session: AsyncSession, source: bytes):
    registered = await api_client.post(
        "/api/v1/participants/register", json={"name": f"Match_{uuid.uuid4().hex}"}
    )
    assert registered.status_code == 201
    participant_id, access_key = (
        registered.json()["participant_id"],
        registered.json()["access_key"],
    )
    login = await api_client.post(
        "/api/v1/auth/login",
        json={"participant_id": participant_id, "access_key": access_key},
    )
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    submission = await api_client.post("/api/v1/submissions", headers=headers)
    assert submission.status_code == 201
    archive = _valid_tar(source=source)
    upload = await api_client.post(
        f"/api/v1/submissions/{submission.json()['submission_id']}/versions",
        content=archive,
        headers={**headers, "Content-Type": "application/x-tar"},
    )
    assert upload.status_code == 201
    version_id = uuid.UUID(upload.json()["version"]["id"])
    eligible = await approve_submission_for_eligibility(
        db_session,
        submission_version_id=version_id,
        approval=OrganizerApproval(
            actor_id="match-test-organizer",
            reason_code="MANUAL_REVIEW_PASSED",
            approval_reference=f"match-{uuid.uuid4().hex}",
        ),
    )
    assert eligible.state == ArtifactState.ELIGIBLE
    return eligible


@pytest.mark.integration
async def test_match_creation_pins_two_exact_eligible_versions_and_event(
    api_client: httpx.AsyncClient, db_session: AsyncSession
):
    white = await _eligible_version(api_client, db_session, b"# white inert fixture\n")
    black = await _eligible_version(api_client, db_session, b"# black inert fixture\n")
    match, sides = await create_match(
        db_session,
        white_submission_version_id=white.id,
        black_submission_version_id=black.id,
        configuration=_configuration(),
    )
    match_id = match.id
    assert match.state == MatchState.QUEUED
    assert len(sides) == 2
    assert {side.submission_version_id for side in sides} == {white.id, black.id}
    assert {side.artifact_sha256 for side in sides} == {
        white.artifact_sha256,
        black.artifact_sha256,
    }
    event = await db_session.scalar(
        select(CompetitionEvent).where(
            CompetitionEvent.aggregate_type == "match",
            CompetitionEvent.aggregate_id == match.id,
            CompetitionEvent.event_type == "MATCH_CREATED",
        )
    )
    assert event is not None
    assert event.metadata_json["white_artifact_sha256"] == white.artifact_sha256

    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(
                MatchSide(
                    id=uuid.uuid4(),
                    match_id=match.id,
                    color=sides[0].color,
                    participant_id=sides[0].participant_id,
                    submission_id=sides[0].submission_id,
                    submission_version_id=sides[0].submission_version_id,
                    artifact_sha256=sides[0].artifact_sha256,
                    protocol_version=sides[0].protocol_version,
                    runtime_profile=sides[0].runtime_profile,
                    created_at=sides[0].created_at,
                )
            )
            await db_session.flush()

    sides[0].artifact_sha256 = "0" * 64
    with pytest.raises(ValueError, match="MATCH_APPEND_ONLY_RECORD_IMMUTABLE"):
        await db_session.flush()
    await db_session.rollback()

    match = await db_session.get(Match, match_id)
    match.state = MatchState.RUNNING
    with pytest.raises(ValueError, match="INVALID_MATCH_STATE_TRANSITION"):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.integration
async def test_match_creation_rejects_self_match_and_artifact_digest_mismatch(
    api_client: httpx.AsyncClient, db_session: AsyncSession, monkeypatch
):
    version = await _eligible_version(api_client, db_session, b"# one inert fixture\n")
    with pytest.raises(MatchServiceError, match="DUPLICATE_SUBMISSION_VERSION"):
        await create_match(
            db_session,
            white_submission_version_id=version.id,
            black_submission_version_id=version.id,
            configuration=_configuration(),
        )

    monkeypatch.setattr(
        quarantine_storage.quarantine_storage,
        "verify",
        lambda *args, **kwargs: b"tampered inert bytes",
    )
    other = await _eligible_version(api_client, db_session, b"# other inert fixture\n")
    with pytest.raises(MatchServiceError, match="ARTIFACT_INTEGRITY_FAILURE"):
        await create_match(
            db_session,
            white_submission_version_id=version.id,
            black_submission_version_id=other.id,
            configuration=_configuration(),
        )


@pytest.mark.integration
async def test_execution_binding_requires_exact_acknowledged_worker_jobs_and_ready_pair(
    api_client: httpx.AsyncClient, db_session: AsyncSession
):
    white = await _eligible_version(api_client, db_session, b"# white inert fixture\n")
    black = await _eligible_version(api_client, db_session, b"# black inert fixture\n")
    match, sides = await create_match(
        db_session,
        white_submission_version_id=white.id,
        black_submission_version_id=black.id,
        configuration=_configuration(),
    )
    await begin_initialization(db_session, match.id)

    bindings = []
    for side in sides:
        worker_id = f"worker-{uuid.uuid4().hex}"
        worker = Worker(
            worker_id=worker_id,
            status=WorkerStatus.ACTIVE,
            revoked_at=None,
            capabilities={},
            security_state="APPROVED",
        )
        db_session.add(worker)
        execution_id = uuid.uuid4()
        lease_id = uuid.uuid4()
        expires = datetime.now(timezone.utc) + timedelta(minutes=5)
        if not bindings:
            other_side = next(candidate for candidate in sides if candidate.id != side.id)
            wrong_execution_id = uuid.uuid4()
            wrong_lease_id = uuid.uuid4()
            db_session.add(
                WorkerExecutionJob(
                    id=uuid.uuid4(),
                    execution_id=wrong_execution_id,
                    match_id=match.id,
                    participant_id=other_side.participant_id,
                    submission_id=other_side.submission_id,
                    submission_version=1,
                    submission_version_id=other_side.submission_version_id,
                    artifact_sha256=other_side.artifact_sha256,
                    immutable_artifact_ref=other_side.artifact_sha256,
                    runtime_profile=other_side.runtime_profile,
                    protocol_version=other_side.protocol_version,
                    execution_mode="MOCK_PROTOCOL_ONLY",
                    initial_state={},
                    resource_policy={},
                    execution_expires_at=expires,
                    status=ExecutionJobStatus.ACKNOWLEDGED,
                    assigned_worker_id=worker_id,
                    lease_id=wrong_lease_id,
                    fencing_token=6,
                    lease_expires_at=expires,
                    last_sequence=0,
                )
            )
            await db_session.flush()
            with pytest.raises(MatchServiceError, match="WORKER_EXECUTION_BINDING_MISMATCH"):
                await bind_execution(
                    db_session,
                    match_id=match.id,
                    side_id=side.id,
                    execution_id=wrong_execution_id,
                    worker_id=worker_id,
                    lease_id=wrong_lease_id,
                    fencing_token=6,
                )
        db_session.add(
            WorkerExecutionJob(
                id=uuid.uuid4(),
                execution_id=execution_id,
                match_id=match.id,
                participant_id=side.participant_id,
                submission_id=side.submission_id,
                submission_version=1,
                submission_version_id=side.submission_version_id,
                artifact_sha256=side.artifact_sha256,
                immutable_artifact_ref=side.artifact_sha256,
                runtime_profile=side.runtime_profile,
                protocol_version=side.protocol_version,
                execution_mode="MOCK_PROTOCOL_ONLY",
                initial_state={},
                resource_policy={},
                execution_expires_at=expires,
                status=ExecutionJobStatus.ACKNOWLEDGED,
                assigned_worker_id=worker_id,
                lease_id=lease_id,
                fencing_token=7,
                lease_expires_at=expires,
                last_sequence=0,
            )
        )
        await db_session.flush()
        binding = await bind_execution(
            db_session,
            match_id=match.id,
            side_id=side.id,
            execution_id=execution_id,
            worker_id=worker_id,
            lease_id=lease_id,
            fencing_token=7,
        )
        replay = await bind_execution(
            db_session,
            match_id=match.id,
            side_id=side.id,
            execution_id=execution_id,
            worker_id=worker_id,
            lease_id=lease_id,
            fencing_token=7,
        )
        assert replay == binding
        bindings.append(binding)
        if len(bindings) == 1:
            with pytest.raises(MatchServiceError, match="BOTH_EXECUTIONS_MUST_BE_READY"):
                await mark_match_running(db_session, match.id)
        await mark_execution_ready(db_session, binding=binding)
        await mark_execution_ready(db_session, binding=binding)

    started = await mark_match_running(db_session, match.id)
    assert started.state == MatchState.RUNNING
    assert (await mark_match_running(db_session, match.id)).state == MatchState.RUNNING
    assert (
        await db_session.scalar(
            select(MatchExecutionBinding).where(MatchExecutionBinding.match_id == match.id)
        )
        is not None
    )
    assert await db_session.scalar(select(MatchMove).where(MatchMove.match_id == match.id)) is None

    side_identities = {
        side.color: MatchSideIdentity(
            side.id,
            match.id,
            side.color,
            side.participant_id,
            side.submission_id,
            side.submission_version_id,
            side.artifact_sha256,
            side.protocol_version,
            side.runtime_profile,
        )
        for side in sides
    }
    binding_values = {
        side.color: next(
            MatchExecutionBindingValue(
                binding_id=binding.binding_id,
                match_id=binding.match_id,
                side_id=binding.side_id,
                execution_id=binding.execution_id,
                submission_version_id=binding.submission_version_id,
                artifact_digest=binding.artifact_digest,
                worker_id=binding.worker_id,
                lease_id=binding.lease_id,
                fencing_token=binding.fencing_token,
                attempt_number=binding.attempt_number,
            )
            for binding in bindings
            if binding.side_id == side.id
        )
        for side in sides
    }
    configuration = _configuration()
    clock_now = time.monotonic()
    controller = MatchController(
        match_id=match.id,
        state=MatchState.RUNNING,
        sides=side_identities,
        bindings=binding_values,
        configuration=configuration,
        current_fen=match.current_fen,
        current_ply=0,
        side_to_move=MatchSideColor.WHITE,
        clock=MatchClock(
            match.initial_clock_ms,
            match.initial_clock_ms,
            match.increment_ms,
            MatchSideColor.WHITE,
        ).start_turn(clock_now, configuration.move_timeout_ms),
        rules=FakeRulesAdapter({("startpos", "e2e4"): "after-e4"}),
        transport=AIKTransport(),
        monotonic=time.monotonic,
    )
    hello_id = "match-hello"
    controller.issue_hello_request(side=MatchSideColor.WHITE, request_id=hello_id)
    controller.receive_ack(
        side=MatchSideColor.WHITE,
        binding=binding_values[MatchSideColor.WHITE],
        message=AIKMessage(
            hello_id,
            json.dumps(
                {
                    "protocol_version": "aik-jsonl/1",
                    "type": "ready",
                    "request_id": hello_id,
                },
                separators=(",", ":"),
            ).encode()
            + b"\n",
        ),
    )
    new_game_id = "match-new-game"
    controller.issue_new_game_request(side=MatchSideColor.WHITE, request_id=new_game_id)
    controller.receive_ack(
        side=MatchSideColor.WHITE,
        binding=binding_values[MatchSideColor.WHITE],
        message=AIKMessage(
            new_game_id,
            json.dumps(
                {
                    "protocol_version": "aik-jsonl/1",
                    "type": "ack",
                    "request_id": new_game_id,
                },
                separators=(",", ":"),
            ).encode()
            + b"\n",
        ),
    )
    position_id = "match-position-r1"
    controller.issue_position_request(side=MatchSideColor.WHITE, request_id=position_id)
    controller.receive_ack(
        side=MatchSideColor.WHITE,
        binding=binding_values[MatchSideColor.WHITE],
        message=AIKMessage(
            position_id,
            json.dumps(
                {
                    "protocol_version": "aik-jsonl/1",
                    "type": "ack",
                    "request_id": position_id,
                },
                separators=(",", ":"),
            ).encode()
            + b"\n",
        ),
    )
    request = controller.issue_move_request(side=MatchSideColor.WHITE, request_id="match-r1")
    response = AIKMessage(
        request.message_id,
        json.dumps(
            {
                "protocol_version": "aik-jsonl/1",
                "type": "bestmove",
                "request_id": request.message_id,
                "move": "e2e4",
            },
            separators=(",", ":"),
        ).encode()
        + b"\n",
    )
    accepted = controller.receive_proposal(
        side=MatchSideColor.WHITE,
        binding=binding_values[MatchSideColor.WHITE],
        sequence=1,
        message=response,
    )
    assert accepted.accepted
    await persist_controller_update(db_session, controller)
    await db_session.refresh(match)
    assert match.current_ply == 1
    assert match.current_fen == "after-e4"
    assert match.side_to_move == MatchSideColor.BLACK
    move_row = await db_session.scalar(select(MatchMove).where(MatchMove.match_id == match.id))
    assert move_row.position_before_sha256 == position_identity("startpos")
    move_event = await db_session.scalar(
        select(CompetitionEvent).where(
            CompetitionEvent.aggregate_type == "match",
            CompetitionEvent.aggregate_id == match.id,
            CompetitionEvent.event_type == "MOVE_ACCEPTED",
        )
    )
    assert move_event is not None

    first_job = await db_session.scalar(
        select(WorkerExecutionJob).where(
            WorkerExecutionJob.execution_id == bindings[0].execution_id
        )
    )
    first_job.fencing_token += 1
    await db_session.commit()
    with pytest.raises(MatchServiceError, match="EXECUTION_BINDING_STALE"):
        await persist_controller_update(db_session, controller)
