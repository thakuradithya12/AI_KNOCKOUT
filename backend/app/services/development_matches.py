"""Authenticated request and safe inspection services for development matches."""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.matches.controller import MatchConfiguration
from app.models.competition import ArtifactState, CompetitionEvent, Submission, SubmissionVersion
from app.models.event import DevelopmentMatchPolicy
from app.models.match import (
    HouseAIMatchSideBinding,
    Match,
    MatchPurpose,
    MatchSide,
    MatchSideColor,
    MatchSideKind,
    MatchState,
    ParticipantMatchSideBinding,
)
from app.models.participant import Participant, ParticipantStatus
from app.services.house_ai_configuration import (
    DEVELOPMENT_POLICY_ID,
    DEVELOPMENT_POLICY_VERSION,
    HouseAIConfigurationError,
    get_active_house_ai_selection,
)
from app.services.quarantine_storage import quarantine_storage
from app.workers.schemas import ResourcePolicy
from app.workers.service import WorkerProtocolError, dispatch_development_match_jobs

ACTIVE_STATES = (MatchState.QUEUED, MatchState.STARTING, MatchState.RUNNING)
STANDARD_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


class DevelopmentMatchError(RuntimeError):
    def __init__(self, code: str, status_code: int = 409) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(code)


def calculate_policy_digest(document: dict) -> str:
    canonical = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _request_hash(participant_id: str, submission_id: uuid.UUID, version: int) -> str:
    data = {
        "participant_id": participant_id,
        "submission_id": str(submission_id),
        "version": version,
        "purpose": MatchPurpose.DEVELOPMENT.value,
    }
    return calculate_policy_digest(data)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def _locked_active_participant(db: AsyncSession, participant_id: str) -> Participant:
    participant = await db.scalar(
        select(Participant)
        .where(Participant.participant_id == participant_id)
        .with_for_update()
    )
    if participant is None or participant.status != ParticipantStatus.ACTIVE:
        raise DevelopmentMatchError("PARTICIPANT_NOT_ACTIVE", 403)
    return participant


async def _load_policy(db: AsyncSession) -> tuple[DevelopmentMatchPolicy, dict, ResourcePolicy]:
    row = await db.get(DevelopmentMatchPolicy, (DEVELOPMENT_POLICY_ID, DEVELOPMENT_POLICY_VERSION))
    if row is None:
        raise DevelopmentMatchError("DEVELOPMENT_POLICY_UNAVAILABLE", 503)
    document = row.policy_document
    if (
        calculate_policy_digest(document) != row.policy_digest
        or document.get("policy_id") != row.policy_id
        or document.get("version") != row.version
        or document.get("policy_version") != DEVELOPMENT_POLICY_VERSION
    ):
        raise DevelopmentMatchError("DEVELOPMENT_POLICY_INTEGRITY_FAILURE", 503)
    try:
        resource_policy = ResourcePolicy(**document["resources"])
    except (KeyError, TypeError, ValueError) as exc:
        raise DevelopmentMatchError("DEVELOPMENT_POLICY_INVALID", 503) from exc
    if (
        document.get("protocol_version") != "aik-jsonl/1"
        or document.get("runtime_profile") != "python-3.12"
        or document.get("rules_version") != "standard-chess/python-chess-1.11.2"
        or document.get("initial_fen") != STANDARD_FEN
        or document.get("initial_clock_ms") != 300_000
        or document.get("increment_ms") != 0
        or document.get("move_timeout_ms") != 2_000
        or resource_policy.model_dump(mode="json") != document.get("resources")
    ):
        raise DevelopmentMatchError("DEVELOPMENT_POLICY_NOT_APPROVED", 503)
    return row, document, resource_policy


async def request_development_match(
    db: AsyncSession,
    *,
    participant_id: str,
    submission_id: uuid.UUID,
    version_number: int,
    idempotency_key: str,
) -> tuple[Match, bool]:
    """Queue an exact typed match; this does not start or simulate either AI."""
    if not idempotency_key or len(idempotency_key.encode("utf-8")) > 128:
        raise DevelopmentMatchError("IDEMPOTENCY_KEY_INVALID", 422)
    if re.search(r"[\x00-\x1f\x7f]", idempotency_key):
        raise DevelopmentMatchError("IDEMPOTENCY_KEY_INVALID", 422)
    await _locked_active_participant(db, participant_id)
    request_digest = _request_hash(participant_id, submission_id, version_number)

    existing = await db.scalar(
        select(Match).where(
            Match.idempotency_participant_id == participant_id,
            Match.idempotency_key == idempotency_key,
        )
    )
    if existing is not None:
        if existing.request_hash != request_digest:
            raise DevelopmentMatchError("IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_REQUEST")
        if existing.state == MatchState.QUEUED:
            try:
                await dispatch_development_match_jobs(db, match_id=existing.id)
            except WorkerProtocolError as exc:
                await db.rollback()
                raise DevelopmentMatchError(exc.code, exc.status_code) from exc
        else:
            await db.commit()
        return existing, False

    submission_version = await db.scalar(
        select(SubmissionVersion)
        .join(Submission, Submission.id == SubmissionVersion.submission_id)
        .where(
            Submission.participant_id == participant_id,
            SubmissionVersion.participant_id == participant_id,
            SubmissionVersion.submission_id == submission_id,
            SubmissionVersion.version == version_number,
        )
        .with_for_update()
    )
    if submission_version is None:
        raise DevelopmentMatchError("SUBMISSION_VERSION_NOT_FOUND", 404)
    if submission_version.state != ArtifactState.ELIGIBLE:
        raise DevelopmentMatchError("SUBMISSION_VERSION_NOT_ELIGIBLE", 409)

    active = await db.scalar(
        select(Match.id)
        .where(
            Match.idempotency_participant_id == participant_id,
            Match.purpose == MatchPurpose.DEVELOPMENT,
            Match.state.in_(ACTIVE_STATES),
        )
        .order_by(Match.created_at, Match.id)
        .limit(1)
    )
    if active is not None:
        raise DevelopmentMatchError("ACTIVE_DEVELOPMENT_MATCH_EXISTS", 409)

    try:
        selection = await get_active_house_ai_selection(db)
    except HouseAIConfigurationError as exc:
        raise DevelopmentMatchError(
            getattr(exc, "code", "HOUSE_AI_CONFIGURATION_INVALID"), 503
        ) from exc

    policy_row, policy, resource_policy = await _load_policy(db)
    if (
        submission_version.protocol_version != policy["protocol_version"]
        or submission_version.runtime_profile != policy["runtime_profile"]
        or selection.protocol_version != policy["protocol_version"]
        or selection.runtime_profile != policy["runtime_profile"]
        or selection.rules_version != policy["rules_version"]
        or selection.policy_version != policy_row.version
        or selection.policy_digest != policy_row.policy_digest
    ):
        raise DevelopmentMatchError("MATCH_SUBJECT_POLICY_INCOMPATIBLE", 409)

    try:
        participant_archive = await quarantine_storage.verify(
            db,
            submission_version.storage_key,
            submission_version.artifact_sha256,
            submission_version.artifact_size,
        )
    except (ValueError, TypeError) as exc:
        raise DevelopmentMatchError("PARTICIPANT_ARTIFACT_INTEGRITY_FAILURE", 503) from exc
    if hashlib.sha256(participant_archive).hexdigest() != submission_version.artifact_sha256:
        raise DevelopmentMatchError("PARTICIPANT_ARTIFACT_INTEGRITY_FAILURE", 503)
    configuration = MatchConfiguration(
        protocol_version=policy["protocol_version"],
        runtime_profile=policy["runtime_profile"],
        policy_version=policy["policy_version"],
        rules_version=policy["rules_version"],
        initial_fen=policy["initial_fen"],
        initial_clock_ms=policy["initial_clock_ms"],
        increment_ms=policy["increment_ms"],
        move_timeout_ms=policy["move_timeout_ms"],
        match_seed=secrets.token_hex(32),
        policy_id=policy_row.policy_id,
        policy_digest=policy_row.policy_digest,
    )
    now = _now()
    match = Match(
        id=uuid.uuid4(),
        state=MatchState.QUEUED,
        purpose=MatchPurpose.DEVELOPMENT,
        idempotency_participant_id=participant_id,
        idempotency_key=idempotency_key,
        request_hash=request_digest,
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
    participant_side = MatchSide(
        id=uuid.uuid4(),
        match_id=match.id,
        color=MatchSideColor.WHITE,
        kind=MatchSideKind.PARTICIPANT,
        protocol_version=configuration.protocol_version,
        runtime_profile=configuration.runtime_profile,
        created_at=now,
    )
    participant_binding = ParticipantMatchSideBinding(
        match_side_id=participant_side.id,
        participant_id=participant_id,
        submission_id=submission_id,
        submission_version_id=submission_version.id,
        artifact_sha256=submission_version.artifact_sha256,
    )
    participant_side.participant_binding = participant_binding
    house_side = MatchSide(
        id=uuid.uuid4(),
        match_id=match.id,
        color=MatchSideColor.BLACK,
        kind=MatchSideKind.HOUSE_AI,
        protocol_version=selection.protocol_version,
        runtime_profile=selection.runtime_profile,
        created_at=now,
    )
    house_binding = HouseAIMatchSideBinding(
        match_side_id=house_side.id,
        house_ai_id=selection.house_ai_id,
        house_ai_version=selection.house_ai_version,
        artifact_sha256=selection.artifact_sha256,
    )
    house_side.house_ai_binding = house_binding
    db.add_all(
        [match, participant_side, house_side, participant_binding, house_binding]
    )
    db.add(
        CompetitionEvent(
            event_type="DEVELOPMENT_MATCH_QUEUED",
            actor_kind="participant",
            actor_id=participant_id,
            severity="info",
            aggregate_type="match",
            aggregate_id=match.id,
            aggregate_sequence=1,
            schema_version=1,
            metadata_json={
                "purpose": MatchPurpose.DEVELOPMENT.value,
                "participant_submission_version_id": str(submission_version.id),
                "participant_artifact_sha256": submission_version.artifact_sha256,
                "house_ai_id": selection.house_ai_id,
                "house_ai_version": selection.house_ai_version,
                "house_ai_artifact_sha256": selection.artifact_sha256,
                "policy_id": policy_row.policy_id,
                "policy_version": policy_row.version,
                "policy_digest": policy_row.policy_digest,
                "match_seed": configuration.match_seed,
            },
            recorded_at=now,
        )
    )
    try:
        await db.flush()
        await dispatch_development_match_jobs(db, match_id=match.id)
    except WorkerProtocolError as exc:
        await db.rollback()
        raise DevelopmentMatchError(exc.code, exc.status_code) from exc
    except Exception:
        await db.rollback()
        raise
    await db.refresh(match)
    return match, True


async def get_participant_development_match(
    db: AsyncSession, *, participant_id: str, match_id: uuid.UUID
) -> Match | None:
    return await db.scalar(
        select(Match)
        .join(MatchSide, MatchSide.match_id == Match.id)
        .join(ParticipantMatchSideBinding,
              ParticipantMatchSideBinding.match_side_id == MatchSide.id)
        .where(
            Match.id == match_id,
            Match.purpose == MatchPurpose.DEVELOPMENT,
            Match.idempotency_participant_id == participant_id,
            ParticipantMatchSideBinding.participant_id == participant_id,
        )
    )


async def list_participant_development_matches(
    db: AsyncSession, *, participant_id: str, limit: int = 100
) -> list[Match]:
    rows = await db.execute(
        select(Match)
        .join(MatchSide, MatchSide.match_id == Match.id)
        .join(ParticipantMatchSideBinding,
              ParticipantMatchSideBinding.match_side_id == MatchSide.id)
        .where(
            Match.purpose == MatchPurpose.DEVELOPMENT,
            Match.idempotency_participant_id == participant_id,
            ParticipantMatchSideBinding.participant_id == participant_id,
        )
        .order_by(Match.created_at.desc(), Match.id.desc())
        .limit(min(max(limit, 1), 100))
    )
    return list(rows.scalars().unique().all())


async def serialize_participant_development_match(
    db: AsyncSession, match: Match
) -> dict:
    sides = (
        await db.execute(
            select(MatchSide).where(MatchSide.match_id == match.id).order_by(MatchSide.color)
        )
    ).scalars().all()
    participant = next(
        (side for side in sides if side.kind == MatchSideKind.PARTICIPANT), None
    )
    house_ai = next((side for side in sides if side.kind == MatchSideKind.HOUSE_AI), None)
    if participant is None or house_ai is None or participant.participant_binding is None or (
        house_ai.house_ai_binding is None
    ):
        raise DevelopmentMatchError("MATCH_IDENTITY_INTEGRITY_FAILURE", 503)
    failure = match.failure_reason
    if failure in {"WORKER_FAILURE", "SANDBOX_FAILURE", "CLEANUP_FAILURE", "STALE_FENCING"}:
        failure = "EXECUTION_FAILED"
    return {
        "match_id": match.id,
        "purpose": match.purpose.value,
        "state": match.state.value,
        "created_at": match.created_at,
        "started_at": match.started_at,
        "completed_at": match.completed_at,
        "participant": {
            "submission_id": participant.participant_binding.submission_id,
            "submission_version": (
                await db.get(
                    SubmissionVersion, participant.participant_binding.submission_version_id
                )
            ).version,
            "submission_version_id": participant.participant_binding.submission_version_id,
            "artifact_sha256": participant.participant_binding.artifact_sha256,
            "color": participant.color.value,
        },
        "house_ai": {
            "house_ai_id": house_ai.house_ai_binding.house_ai_id,
            "version": house_ai.house_ai_binding.house_ai_version,
            "artifact_sha256": house_ai.house_ai_binding.artifact_sha256,
            "color": house_ai.color.value,
        },
        "policy": {
            "id": match.policy_id,
            "version": match.policy_version,
            "digest": match.policy_digest,
        },
        "protocol_version": match.protocol_version,
        "runtime_profile": match.runtime_profile,
        "rules_version": match.rules_version,
        "match_seed": match.match_seed,
        "initial_fen": match.initial_fen,
        "current_fen": match.current_fen,
        "side_to_move": match.side_to_move.value,
        "current_ply": match.current_ply,
        "initial_clock_ms": match.initial_clock_ms,
        "increment_ms": match.increment_ms,
        "move_timeout_ms": match.move_timeout_ms,
        "white_remaining_ms": match.white_remaining_ms,
        "black_remaining_ms": match.black_remaining_ms,
        "result": {
            "outcome": match.result_outcome,
            "reason": match.result_reason,
            "failure": failure,
        },
    }
