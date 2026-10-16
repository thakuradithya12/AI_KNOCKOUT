"""Transactional match creation, execution binding, and event persistence."""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.matches.controller import MatchConfiguration, MatchExecutionBinding
from app.models.competition import (
    ArtifactState,
    CompetitionEvent,
    SubmissionVersion,
)
from app.models.match import (
    Match,
    MatchFailure,
    MatchMove,
    MatchPurpose,
    MatchResultSource,
    MatchSide,
    MatchSideColor,
    MatchSideKind,
    MatchState,
    ParticipantMatchSideBinding,
)
from app.models.match import (
    MatchExecutionBinding as MatchExecutionBindingRecord,
)
from app.models.worker import (
    ExecutionJobStatus,
    ExecutionKind,
    Worker,
    WorkerExecutionJob,
    WorkerStatus,
)
from app.services.quarantine_storage import quarantine_storage


class MatchServiceError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime) -> datetime:
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value


def _apply_automated_result(match: Match, result, completed_at: datetime) -> None:
    """Persist result fields only from a trusted MatchController result object."""
    match.result_outcome = result.outcome.value
    match.result_reason = result.reason.value
    match.result_source = MatchResultSource.AUTOMATED
    match.completed_at = completed_at


def _event(
    *,
    match_id: uuid.UUID,
    sequence: int,
    event_type: str,
    actor_id: str,
    metadata: dict,
    recorded_at: datetime,
) -> CompetitionEvent:
    return CompetitionEvent(
        event_type=event_type,
        actor_kind="match_controller",
        actor_id=actor_id,
        severity="warning" if event_type in {"MOVE_REJECTED", "MATCH_ABORTED"} else "info",
        aggregate_type="match",
        aggregate_id=match_id,
        aggregate_sequence=sequence,
        schema_version=1,
        metadata_json=metadata,
        recorded_at=recorded_at,
    )


async def _next_event_sequence(db: AsyncSession, match_id: uuid.UUID) -> int:
    current = await db.scalar(
        select(func.max(CompetitionEvent.aggregate_sequence)).where(
            CompetitionEvent.aggregate_type == "match",
            CompetitionEvent.aggregate_id == match_id,
        )
    )
    return (current or 0) + 1


async def create_match(
    db: AsyncSession,
    *,
    white_submission_version_id: uuid.UUID,
    black_submission_version_id: uuid.UUID,
    configuration: MatchConfiguration,
    purpose: MatchPurpose = MatchPurpose.PEER_TEST,
) -> tuple[Match, tuple[MatchSide, MatchSide]]:
    """Create an immutable, exact-version match. This does not enqueue executions."""
    if white_submission_version_id == black_submission_version_id:
        raise MatchServiceError("DUPLICATE_SUBMISSION_VERSION")
    versions = (
        (
            await db.execute(
                select(SubmissionVersion)
                .where(
                    SubmissionVersion.id.in_(
                        [white_submission_version_id, black_submission_version_id]
                    )
                )
                .order_by(SubmissionVersion.id)
                .with_for_update()
            )
        )
        .scalars()
        .all()
    )
    by_id = {version.id: version for version in versions}
    if len(by_id) != 2:
        raise MatchServiceError("SUBMISSION_VERSION_NOT_FOUND")
    white = by_id[white_submission_version_id]
    black = by_id[black_submission_version_id]
    if white.state != ArtifactState.ELIGIBLE or black.state != ArtifactState.ELIGIBLE:
        raise MatchServiceError("SUBMISSION_VERSION_NOT_ELIGIBLE")
    if white.participant_id == black.participant_id:
        raise MatchServiceError("SELF_MATCH_NOT_ALLOWED")
    for version in (white, black):
        if not re.fullmatch(r"[0-9a-f]{64}", version.artifact_sha256):
            raise MatchServiceError("ARTIFACT_DIGEST_INVALID")
        if version.protocol_version != configuration.protocol_version:
            raise MatchServiceError("PROTOCOL_VERSION_MISMATCH")
        if version.runtime_profile != configuration.runtime_profile:
            raise MatchServiceError("RUNTIME_PROFILE_MISMATCH")
        try:
            archive = await quarantine_storage.verify(
                db, version.storage_key, version.artifact_sha256, version.artifact_size
            )
        except (ValueError, TypeError) as exc:
            raise MatchServiceError("ARTIFACT_INTEGRITY_FAILURE") from exc
        if hashlib.sha256(archive).hexdigest() != version.artifact_sha256:
            raise MatchServiceError("ARTIFACT_INTEGRITY_FAILURE")

    now = _now()
    match = Match(
        id=uuid.uuid4(),
        state=MatchState.QUEUED,
        purpose=purpose,
        protocol_version=configuration.protocol_version,
        runtime_profile=configuration.runtime_profile,
        policy_version=configuration.policy_version,
        policy_id=configuration.policy_id,
        policy_digest=configuration.policy_digest,
        rules_version=configuration.rules_version,
        match_seed=configuration.match_seed,
        initial_fen=configuration.initial_fen,
        current_fen=configuration.initial_fen,
        side_to_move=MatchSideColor.WHITE,
        current_ply=0,
        initial_clock_ms=configuration.initial_clock_ms,
        increment_ms=configuration.increment_ms,
        move_timeout_ms=configuration.move_timeout_ms,
        white_remaining_ms=configuration.initial_clock_ms,
        black_remaining_ms=configuration.initial_clock_ms,
        created_at=now,
    )
    sides = (
        MatchSide(
            id=uuid.uuid4(),
            match_id=match.id,
            color=MatchSideColor.WHITE,
            kind=MatchSideKind.PARTICIPANT,
            protocol_version=configuration.protocol_version,
            runtime_profile=configuration.runtime_profile,
            created_at=now,
        ),
        MatchSide(
            id=uuid.uuid4(),
            match_id=match.id,
            color=MatchSideColor.BLACK,
            kind=MatchSideKind.PARTICIPANT,
            protocol_version=configuration.protocol_version,
            runtime_profile=configuration.runtime_profile,
            created_at=now,
        ),
    )
    side_bindings = (
        ParticipantMatchSideBinding(
            match_side_id=sides[0].id,
            participant_id=white.participant_id,
            submission_id=white.submission_id,
            submission_version_id=white.id,
            artifact_sha256=white.artifact_sha256,
        ),
        ParticipantMatchSideBinding(
            match_side_id=sides[1].id,
            participant_id=black.participant_id,
            submission_id=black.submission_id,
            submission_version_id=black.id,
            artifact_sha256=black.artifact_sha256,
        ),
    )
    sides[0].participant_binding = side_bindings[0]
    sides[1].participant_binding = side_bindings[1]
    db.add(match)
    await db.flush()
    db.add_all(sides)
    db.add_all(side_bindings)
    await db.flush()
    db.add(
        _event(
            match_id=match.id,
            sequence=1,
            event_type="MATCH_CREATED",
            actor_id="control-plane",
            recorded_at=now,
            metadata={
                "protocol_version": configuration.protocol_version,
                "runtime_profile": configuration.runtime_profile,
                "policy_version": configuration.policy_version,
                "rules_version": configuration.rules_version,
                "white_submission_version_id": str(white.id),
                "white_artifact_sha256": white.artifact_sha256,
                "black_submission_version_id": str(black.id),
                "black_artifact_sha256": black.artifact_sha256,
            },
        )
    )
    await db.commit()
    await db.refresh(match)
    return match, sides


async def begin_initialization(db: AsyncSession, match_id: uuid.UUID) -> Match:
    match = await _locked_match(db, match_id)
    if match.state == MatchState.STARTING:
        await db.commit()
        return match
    if match.state != MatchState.QUEUED:
        raise MatchServiceError("MATCH_NOT_QUEUED")
    sides = (
        (await db.execute(select(MatchSide).where(MatchSide.match_id == match.id)))
        .scalars()
        .all()
    )
    kinds = [side.kind for side in sides]
    if len(sides) != 2 or (
        match.purpose in {MatchPurpose.PEER_TEST, MatchPurpose.OFFICIAL}
        and set(kinds) != {MatchSideKind.PARTICIPANT}
    ) or (
        match.purpose == MatchPurpose.DEVELOPMENT
        and sorted(kind.value for kind in kinds) not in (
            sorted([MatchSideKind.PARTICIPANT.value, MatchSideKind.HOUSE_AI.value]),
            [MatchSideKind.HOUSE_AI.value, MatchSideKind.HOUSE_AI.value],
        )
    ):
        raise MatchServiceError("MATCH_PURPOSE_SIDE_COMPOSITION_INVALID")
    match.state = MatchState.STARTING
    now = _now()
    db.add(
        _event(
            match_id=match.id,
            sequence=await _next_event_sequence(db, match.id),
            event_type="MATCH_INITIALIZED",
            actor_id="control-plane",
            recorded_at=now,
            metadata={"state": MatchState.STARTING.value},
        )
    )
    await db.commit()
    return match


async def bind_execution(
    db: AsyncSession,
    *,
    match_id: uuid.UUID,
    side_id: uuid.UUID,
    execution_id: uuid.UUID,
    worker_id: str,
    lease_id: uuid.UUID,
    fencing_token: int,
) -> MatchExecutionBinding:
    match = await _locked_match(db, match_id)
    side = await db.get(MatchSide, side_id)
    if side is None or side.match_id != match.id:
        raise MatchServiceError("MATCH_SIDE_NOT_FOUND")
    existing = await db.scalar(
        select(MatchExecutionBindingRecord).where(
            MatchExecutionBindingRecord.execution_id == execution_id
        )
    )
    if existing is not None:
        if (
            existing.match_id != match.id
            or existing.match_side_id != side.id
            or existing.worker_id != worker_id
            or existing.lease_id != lease_id
            or existing.fencing_token != fencing_token
            or existing.execution_kind != side.kind
            or existing.submission_version_id
            != (side.submission_version_id if side.kind == MatchSideKind.PARTICIPANT else None)
            or existing.house_ai_id
            != (side.house_ai_binding.house_ai_id if side.house_ai_binding else None)
            or existing.house_ai_version
            != (side.house_ai_binding.house_ai_version if side.house_ai_binding else None)
            or existing.artifact_sha256 != side.artifact_sha256
        ):
            raise MatchServiceError("EXECUTION_BINDING_REPLAY_MISMATCH")
        await db.commit()
        return MatchExecutionBinding(
            binding_id=existing.id,
            match_id=existing.match_id,
            side_id=existing.match_side_id,
            execution_id=existing.execution_id,
            submission_version_id=existing.submission_version_id,
            artifact_digest=existing.artifact_sha256,
            worker_id=existing.worker_id,
            lease_id=existing.lease_id,
            fencing_token=existing.fencing_token,
            attempt_number=existing.attempt_number,
            kind=existing.execution_kind,
            house_ai_id=existing.house_ai_id,
            house_ai_version=existing.house_ai_version,
        )
    if match.state != MatchState.STARTING:
        raise MatchServiceError("MATCH_NOT_INITIALIZING")
    worker = (
        await db.execute(select(Worker).where(Worker.worker_id == worker_id).with_for_update())
    ).scalar_one_or_none()
    if worker is None or worker.status != WorkerStatus.ACTIVE or worker.revoked_at is not None:
        raise MatchServiceError("WORKER_NOT_ACTIVE")
    if fencing_token < 1:
        raise MatchServiceError("FENCING_TOKEN_INVALID")
    job = (
        await db.execute(
            select(WorkerExecutionJob)
            .where(WorkerExecutionJob.execution_id == execution_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    subject_matches = False
    if job is not None and side.kind == MatchSideKind.PARTICIPANT:
        subject_matches = (
            job.execution_kind == ExecutionKind.PARTICIPANT
            and job.submission_version_id == side.submission_version_id
            and job.submission_id == side.submission_id
            and job.participant_id == side.participant_id
            and job.artifact_sha256 == side.artifact_sha256
        )
    elif job is not None and side.kind == MatchSideKind.HOUSE_AI and side.house_ai_binding:
        subject_matches = (
            job.execution_kind == ExecutionKind.HOUSE_AI
            and job.house_ai_id == side.house_ai_binding.house_ai_id
            and job.house_ai_version == side.house_ai_binding.house_ai_version
            and job.artifact_sha256 == side.artifact_sha256
        )
    if (
        job is None
        or job.match_id != match.id
        or not subject_matches
        or job.protocol_version != side.protocol_version
        or job.runtime_profile != side.runtime_profile
        or job.assigned_worker_id != worker_id
        or job.lease_id != lease_id
        or job.fencing_token != fencing_token
        or job.status != ExecutionJobStatus.ACKNOWLEDGED
        or job.lease_expires_at is None
        or _utc(job.lease_expires_at) <= _now()
        or _utc(job.execution_expires_at) <= _now()
    ):
        raise MatchServiceError("WORKER_EXECUTION_BINDING_MISMATCH")
    latest = await db.scalar(
        select(func.max(MatchExecutionBindingRecord.attempt_number)).where(
            MatchExecutionBindingRecord.match_side_id == side.id
        )
    )
    row = MatchExecutionBindingRecord(
        id=uuid.uuid4(),
        match_id=match.id,
        match_side_id=side.id,
        attempt_number=(latest or 0) + 1,
        execution_id=execution_id,
        execution_kind=side.kind,
        submission_version_id=(
            side.submission_version_id if side.kind == MatchSideKind.PARTICIPANT else None
        ),
        house_ai_id=side.house_ai_binding.house_ai_id if side.house_ai_binding else None,
        house_ai_version=(
            side.house_ai_binding.house_ai_version if side.house_ai_binding else None
        ),
        artifact_sha256=side.artifact_sha256,
        worker_id=worker_id,
        lease_id=lease_id,
        fencing_token=fencing_token,
        created_at=_now(),
    )
    db.add(row)
    now = _now()
    db.add(
        _event(
            match_id=match.id,
            sequence=await _next_event_sequence(db, match.id),
            event_type="EXECUTION_BOUND",
            actor_id="control-plane",
            recorded_at=now,
            metadata={
                "side_id": str(side.id),
                "execution_id": str(execution_id),
                "attempt_number": row.attempt_number,
                "execution_kind": side.kind.value,
                "submission_version_id": (
                    str(side.submission_version_id)
                    if side.submission_version_id is not None
                    else None
                ),
                "house_ai_id": row.house_ai_id,
                "house_ai_version": row.house_ai_version,
                "artifact_sha256": side.artifact_sha256,
            },
        )
    )
    await db.commit()
    return MatchExecutionBinding(
        binding_id=row.id,
        match_id=row.match_id,
        side_id=row.match_side_id,
        execution_id=row.execution_id,
        submission_version_id=row.submission_version_id,
        artifact_digest=row.artifact_sha256,
        worker_id=row.worker_id,
        lease_id=row.lease_id,
        fencing_token=row.fencing_token,
        attempt_number=row.attempt_number,
        kind=row.execution_kind,
        house_ai_id=row.house_ai_id,
        house_ai_version=row.house_ai_version,
    )


async def mark_execution_ready(db: AsyncSession, *, binding: MatchExecutionBinding) -> None:
    match = await _locked_match(db, binding.match_id)
    if match.state != MatchState.STARTING:
        raise MatchServiceError("MATCH_NOT_INITIALIZING")
    row = await _current_binding(db, binding)
    if row is None:
        raise MatchServiceError("EXECUTION_BINDING_STALE")
    events = (
        (
            await db.execute(
                select(CompetitionEvent).where(
                    CompetitionEvent.aggregate_type == "match",
                    CompetitionEvent.aggregate_id == match.id,
                    CompetitionEvent.event_type == "EXECUTION_READY",
                )
            )
        )
        .scalars()
        .all()
    )
    if any(event.metadata_json.get("binding_id") == str(row.id) for event in events):
        await db.commit()
        return
    now = _now()
    db.add(
        _event(
            match_id=match.id,
            sequence=await _next_event_sequence(db, match.id),
            event_type="EXECUTION_READY",
            actor_id=binding.worker_id,
            recorded_at=now,
            metadata={
                "binding_id": str(row.id),
                "execution_id": str(row.execution_id),
                "side_id": str(row.match_side_id),
            },
        )
    )
    await db.commit()


async def mark_match_running(db: AsyncSession, match_id: uuid.UUID) -> Match:
    match = await _locked_match(db, match_id)
    if match.state == MatchState.RUNNING:
        await db.commit()
        return match
    if match.state != MatchState.STARTING:
        raise MatchServiceError("MATCH_NOT_INITIALIZING")
    bindings = (
        (
            await db.execute(
                select(MatchExecutionBindingRecord)
                .where(MatchExecutionBindingRecord.match_id == match.id)
                .order_by(MatchExecutionBindingRecord.attempt_number.desc())
            )
        )
        .scalars()
        .all()
    )
    latest_by_side: dict[uuid.UUID, MatchExecutionBindingRecord] = {}
    for binding in bindings:
        latest_by_side.setdefault(binding.match_side_id, binding)
    sides = (
        (await db.execute(select(MatchSide).where(MatchSide.match_id == match.id))).scalars().all()
    )
    if len(sides) != 2:
        raise MatchServiceError("TWO_EXECUTIONS_REQUIRED")
    if set(latest_by_side) != {side.id for side in sides}:
        raise MatchServiceError("BOTH_EXECUTIONS_MUST_BE_READY")
    for row in latest_by_side.values():
        if await _current_binding(db, _binding_value(row)) is None:
            raise MatchServiceError("EXECUTION_BINDING_STALE")
    events = (
        (
            await db.execute(
                select(CompetitionEvent).where(
                    CompetitionEvent.aggregate_type == "match",
                    CompetitionEvent.aggregate_id == match.id,
                    CompetitionEvent.event_type == "EXECUTION_READY",
                )
            )
        )
        .scalars()
        .all()
    )
    ready = {event.metadata_json.get("binding_id") for event in events}
    if any(str(latest_by_side[side.id].id) not in ready for side in sides):
        raise MatchServiceError("BOTH_EXECUTIONS_MUST_BE_READY")
    now = _now()
    match.state = MatchState.RUNNING
    match.side_to_move = MatchSideColor.WHITE
    match.started_at = now
    db.add(
        _event(
            match_id=match.id,
            sequence=await _next_event_sequence(db, match.id),
            event_type="MATCH_STARTED",
            actor_id="match-controller",
            recorded_at=now,
            metadata={"first_side": "WHITE"},
        )
    )
    await db.commit()
    return match


async def _locked_match(db: AsyncSession, match_id: uuid.UUID) -> Match:
    match = (
        await db.execute(select(Match).where(Match.id == match_id).with_for_update())
    ).scalar_one_or_none()
    if match is None:
        raise MatchServiceError("MATCH_NOT_FOUND")
    return match


async def _current_binding(
    db: AsyncSession, binding: MatchExecutionBinding
) -> MatchExecutionBindingRecord | None:
    row = await db.get(MatchExecutionBindingRecord, binding.binding_id)
    if row is None:
        return None
    latest_attempt = await db.scalar(
        select(func.max(MatchExecutionBindingRecord.attempt_number)).where(
            MatchExecutionBindingRecord.match_side_id == row.match_side_id
        )
    )
    if row.attempt_number != latest_attempt or (
        row.match_id != binding.match_id
        or row.match_side_id != binding.side_id
        or row.execution_id != binding.execution_id
        or row.execution_kind != binding.kind
        or row.submission_version_id != binding.submission_version_id
        or row.house_ai_id != binding.house_ai_id
        or row.house_ai_version != binding.house_ai_version
        or row.artifact_sha256 != binding.artifact_digest
        or row.worker_id != binding.worker_id
        or row.lease_id != binding.lease_id
        or row.fencing_token != binding.fencing_token
    ):
        return None
    side = await db.get(MatchSide, row.match_side_id)
    worker = (
        await db.execute(select(Worker).where(Worker.worker_id == row.worker_id).with_for_update())
    ).scalar_one_or_none()
    # Match worker revocation's lock order (worker before its active jobs).
    # This avoids a job/worker lock inversion during concurrent revocation.
    job = (
        await db.execute(
            select(WorkerExecutionJob)
            .where(WorkerExecutionJob.execution_id == row.execution_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    subject_matches = False
    if side is not None and side.kind == MatchSideKind.PARTICIPANT:
        subject_matches = (
            job is not None
            and job.execution_kind == ExecutionKind.PARTICIPANT
            and job.participant_id == side.participant_id
            and job.submission_id == side.submission_id
            and job.submission_version_id == row.submission_version_id
            and job.artifact_sha256 == row.artifact_sha256
        )
    elif side is not None and side.kind == MatchSideKind.HOUSE_AI and side.house_ai_binding:
        subject_matches = (
            job is not None
            and job.execution_kind == ExecutionKind.HOUSE_AI
            and job.house_ai_id == side.house_ai_binding.house_ai_id
            and job.house_ai_version == side.house_ai_binding.house_ai_version
            and row.house_ai_id == side.house_ai_binding.house_ai_id
            and row.house_ai_version == side.house_ai_binding.house_ai_version
            and job.artifact_sha256 == row.artifact_sha256
        )
    if (
        job is None
        or side is None
        or worker is None
        or worker.status != WorkerStatus.ACTIVE
        or worker.revoked_at is not None
        or job.status != ExecutionJobStatus.ACKNOWLEDGED
        or job.assigned_worker_id != row.worker_id
        or job.lease_id != row.lease_id
        or job.match_id != row.match_id
        or not subject_matches
        or job.protocol_version != side.protocol_version
        or job.runtime_profile != side.runtime_profile
        or job.fencing_token != row.fencing_token
        or job.lease_expires_at is None
        or _utc(job.lease_expires_at) <= _now()
        or _utc(job.execution_expires_at) <= _now()
    ):
        return None
    return row


def _binding_value(row: MatchExecutionBindingRecord) -> MatchExecutionBinding:
    return MatchExecutionBinding(
        binding_id=row.id,
        match_id=row.match_id,
        side_id=row.match_side_id,
        execution_id=row.execution_id,
        submission_version_id=row.submission_version_id,
        artifact_digest=row.artifact_sha256,
        worker_id=row.worker_id,
        lease_id=row.lease_id,
        fencing_token=row.fencing_token,
        attempt_number=row.attempt_number,
        kind=row.execution_kind,
        house_ai_id=row.house_ai_id,
        house_ai_version=row.house_ai_version,
    )


async def record_controller_restart(db: AsyncSession, match_id: uuid.UUID) -> Match:
    """Fail closed on active-process restart; monotonic deadlines cannot be resumed."""
    match = await _locked_match(db, match_id)
    if (
        match.state == MatchState.ABORTED
        and match.failure_reason == MatchFailure.CONTROLLER_RESTART.value
    ):
        await db.commit()
        return match
    if match.state not in {MatchState.STARTING, MatchState.RUNNING}:
        raise MatchServiceError("MATCH_NOT_ACTIVE_FOR_RECOVERY")
    now = _now()
    match.state = MatchState.ABORTED
    match.failure_reason = MatchFailure.CONTROLLER_RESTART.value
    match.completed_at = now
    db.add(
        _event(
            match_id=match.id,
            sequence=await _next_event_sequence(db, match.id),
            event_type="MATCH_ABORTED",
            actor_id="match-controller",
            recorded_at=now,
            metadata={
                "failure": MatchFailure.CONTROLLER_RESTART.value,
                "reason": "ACTIVE_MONOTONIC_CLOCK_CANNOT_RESUME",
            },
        )
    )
    await db.commit()
    return match


async def record_transcript_corruption(db: AsyncSession, match_id: uuid.UUID) -> Match:
    """Abort an active match after replay evidence fails; never rewrite its transcript."""
    match = await _locked_match(db, match_id)
    if (
        match.state == MatchState.ABORTED
        and match.failure_reason == MatchFailure.TRANSCRIPT_CORRUPTED.value
    ):
        await db.commit()
        return match
    if match.state not in {MatchState.QUEUED, MatchState.STARTING, MatchState.RUNNING}:
        raise MatchServiceError("MATCH_NOT_ACTIVE_FOR_CORRUPTION_ABORT")
    now = _now()
    match.state = MatchState.ABORTED
    match.result_outcome = None
    match.result_reason = None
    match.failure_reason = MatchFailure.TRANSCRIPT_CORRUPTED.value
    match.completed_at = now
    db.add(
        _event(
            match_id=match.id,
            sequence=await _next_event_sequence(db, match.id),
            event_type="MATCH_ABORTED",
            actor_id="match-controller",
            recorded_at=now,
            metadata={
                "failure": MatchFailure.TRANSCRIPT_CORRUPTED.value,
                "reason": "PERSISTED_TRANSCRIPT_REPLAY_FAILED",
            },
        )
    )
    await db.commit()
    await db.refresh(match)
    return match


async def persist_controller_update(db: AsyncSession, controller) -> Match:
    """Atomically persist accepted controller state, move transcript, and outbox events."""
    match = await _locked_match(db, controller.match_id)
    if match.current_ply != controller.persisted_ply or match.state != controller.persisted_state:
        raise MatchServiceError("STALE_MATCH_CONTROLLER_SNAPSHOT")
    for binding in controller.bindings.values():
        if await _current_binding(db, binding) is None:
            raise MatchServiceError("EXECUTION_BINDING_STALE")
    new_moves = controller.moves[controller.persisted_move_count :]
    for move in new_moves:
        binding_row = await db.get(MatchExecutionBindingRecord, move.execution_binding.binding_id)
        if binding_row is None:
            raise MatchServiceError("MOVE_EXECUTION_BINDING_MISSING")
        db.add(
            MatchMove(
                id=uuid.uuid4(),
                match_id=move.match_id,
                match_side_id=move.side_id,
                execution_binding_id=binding_row.id,
                sequence=move.sequence,
                request_id=move.request_id,
                move_uci=move.move_uci,
                position_before_sha256=move.position_before_sha256,
                position_after_sha256=move.position_after_sha256,
                position_before_fen=move.position_before_fen,
                position_after_fen=move.position_after_fen,
                elapsed_ms=move.elapsed_ms,
                recorded_at=move.recorded_at,
            )
        )
    if new_moves:
        await db.flush()
    match.state = controller.state
    match.current_fen = controller.current_fen
    match.side_to_move = controller.side_to_move
    match.current_ply = controller.current_ply
    match.white_remaining_ms = controller.clock.white_remaining_ms
    match.black_remaining_ms = controller.clock.black_remaining_ms
    if match.started_at is None and controller.started_at is not None:
        match.started_at = controller.started_at
    if controller.result is not None:
        _apply_automated_result(match, controller.result, controller.completed_at or _now())
    if controller.failure is not None:
        match.failure_reason = controller.failure.value
        match.completed_at = controller.completed_at or _now()
    await db.flush()
    sequence = await _next_event_sequence(db, match.id)
    for event in controller.events[controller.persisted_event_count :]:
        db.add(
            _event(
                match_id=match.id,
                sequence=sequence,
                event_type=event.event_type,
                actor_id=event.actor_id,
                metadata=event.metadata,
                recorded_at=event.recorded_at,
            )
        )
        sequence += 1
    await db.commit()
    await db.refresh(match)
    controller.persisted_ply = match.current_ply
    controller.persisted_state = match.state
    controller.persisted_move_count = len(controller.moves)
    controller.persisted_event_count = len(controller.events)
    return match
