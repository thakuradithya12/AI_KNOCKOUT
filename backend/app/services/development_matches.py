"""Authenticated request and safe inspection services for development matches."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.matches.controller import MatchConfiguration
from app.models.competition import ArtifactState, CompetitionEvent, Submission, SubmissionVersion
from app.models.event import DevelopmentMatchPolicy
from app.models.house_ai import HouseAIVersion
from app.models.match import (
    HouseAIMatchSideBinding,
    Match,
    MatchFailure,
    MatchMove,
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

if TYPE_CHECKING:
    from app.services.trusted_engine_builder import TrustedEngineSession

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


def _request_hash(
    participant_id: str,
    submission_id: uuid.UUID,
    version: int,
    side: str = "WHITE",
    house_ai_id: str | None = None,
) -> str:
    data = {
        "participant_id": participant_id,
        "submission_id": str(submission_id),
        "version": version,
        "side": (side or "WHITE").upper(),
        "house_ai_id": house_ai_id or "",
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
    side: str = "WHITE",
    house_ai_id: str | None = None,
) -> tuple[Match, bool]:
    """Queue an exact typed match; this does not start or simulate either AI."""
    if not idempotency_key or len(idempotency_key.encode("utf-8")) > 128:
        raise DevelopmentMatchError("IDEMPOTENCY_KEY_INVALID", 422)
    if re.search(r"[\x00-\x1f\x7f]", idempotency_key):
        raise DevelopmentMatchError("IDEMPOTENCY_KEY_INVALID", 422)
    await _locked_active_participant(db, participant_id)
    request_digest = _request_hash(
        participant_id, submission_id, version_number, side=side, house_ai_id=house_ai_id
    )

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
    if submission_version.state not in (ArtifactState.APPROVED, ArtifactState.ELIGIBLE):
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

    target_house_ai_id: str
    target_house_ai_version: int
    target_artifact_sha256: str
    target_protocol_version: str
    target_runtime_profile: str
    target_rules_version: str
    target_policy_version: str
    target_policy_digest: str

    if house_ai_id:
        target_version_row = await db.scalar(
            select(HouseAIVersion)
            .where(
                HouseAIVersion.house_ai_id == house_ai_id,
                HouseAIVersion.frozen_at.isnot(None),
            )
            .order_by(HouseAIVersion.version.desc())
            .limit(1)
        )
        if target_version_row is None:
            raise DevelopmentMatchError("HOUSE_AI_CONFIGURATION_INVALID", 404)
        target_house_ai_id = target_version_row.house_ai_id
        target_house_ai_version = target_version_row.version
        target_artifact_sha256 = target_version_row.artifact_sha256
        target_protocol_version = target_version_row.protocol_version
        target_runtime_profile = target_version_row.runtime_profile
        target_rules_version = target_version_row.rules_version
        target_policy_version = target_version_row.policy_version
        target_policy_digest = target_version_row.policy_digest
    else:
        try:
            selection = await get_active_house_ai_selection(db)
        except HouseAIConfigurationError as exc:
            raise DevelopmentMatchError(
                getattr(exc, "code", "HOUSE_AI_CONFIGURATION_INVALID"), 503
            ) from exc
        target_house_ai_id = selection.house_ai_id
        target_house_ai_version = selection.house_ai_version
        target_artifact_sha256 = selection.artifact_sha256
        target_protocol_version = selection.protocol_version
        target_runtime_profile = selection.runtime_profile
        target_rules_version = selection.rules_version
        target_policy_version = selection.policy_version
        target_policy_digest = selection.policy_digest

    policy_row, policy, resource_policy = await _load_policy(db)
    if (
        submission_version.protocol_version != policy["protocol_version"]
        or submission_version.runtime_profile != policy["runtime_profile"]
        or target_protocol_version != policy["protocol_version"]
        or target_runtime_profile != policy["runtime_profile"]
        or target_rules_version != policy["rules_version"]
        or target_policy_version != policy_row.version
        or target_policy_digest != policy_row.policy_digest
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

    participant_color = (
        MatchSideColor.BLACK if (side or "WHITE").upper() == "BLACK" else MatchSideColor.WHITE
    )
    house_color = (
        MatchSideColor.WHITE if participant_color == MatchSideColor.BLACK else MatchSideColor.BLACK
    )

    participant_side = MatchSide(
        id=uuid.uuid4(),
        match_id=match.id,
        color=participant_color,
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
        color=house_color,
        kind=MatchSideKind.HOUSE_AI,
        protocol_version=target_protocol_version,
        runtime_profile=target_runtime_profile,
        created_at=now,
    )
    house_binding = HouseAIMatchSideBinding(
        match_side_id=house_side.id,
        house_ai_id=target_house_ai_id,
        house_ai_version=target_house_ai_version,
        artifact_sha256=target_artifact_sha256,
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
                "participant_color": participant_color.value,
                "house_ai_id": target_house_ai_id,
                "house_ai_version": target_house_ai_version,
                "house_ai_artifact_sha256": target_artifact_sha256,
                "house_ai_color": house_color.value,
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
        .where(
            Match.id == match_id,
            Match.purpose == MatchPurpose.DEVELOPMENT,
            Match.idempotency_participant_id == participant_id,
        )
    )


async def list_participant_development_matches(
    db: AsyncSession, *, participant_id: str, limit: int = 100
) -> list[Match]:
    rows = await db.execute(
        select(Match)
        .where(
            Match.purpose == MatchPurpose.DEVELOPMENT,
            Match.idempotency_participant_id == participant_id,
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

    participant_data = None
    house_ai_data = None
    white_house_data = None
    black_house_data = None

    if participant is not None and house_ai is not None:
        if participant.participant_binding is None or house_ai.house_ai_binding is None:
            raise DevelopmentMatchError("MATCH_IDENTITY_INTEGRITY_FAILURE", 503)
        sub_version = await db.get(
            SubmissionVersion, participant.participant_binding.submission_version_id
        )
        participant_data = {
            "submission_id": participant.participant_binding.submission_id,
            "submission_version": sub_version.version if sub_version else 1,
            "submission_version_id": participant.participant_binding.submission_version_id,
            "artifact_sha256": participant.participant_binding.artifact_sha256,
            "color": participant.color.value,
        }
        house_ai_data = {
            "house_ai_id": house_ai.house_ai_binding.house_ai_id,
            "version": house_ai.house_ai_binding.house_ai_version,
            "artifact_sha256": house_ai.house_ai_binding.artifact_sha256,
            "color": house_ai.color.value,
        }
    elif len(sides) == 2 and all(s.kind == MatchSideKind.HOUSE_AI for s in sides):
        w_side = next((s for s in sides if s.color == MatchSideColor.WHITE), None)
        b_side = next((s for s in sides if s.color == MatchSideColor.BLACK), None)
        if (
            w_side is None
            or b_side is None
            or w_side.house_ai_binding is None
            or b_side.house_ai_binding is None
        ):
            raise DevelopmentMatchError("MATCH_IDENTITY_INTEGRITY_FAILURE", 503)
        white_house_data = {
            "house_ai_id": w_side.house_ai_binding.house_ai_id,
            "version": w_side.house_ai_binding.house_ai_version,
            "artifact_sha256": w_side.house_ai_binding.artifact_sha256,
            "color": "WHITE",
        }
        black_house_data = {
            "house_ai_id": b_side.house_ai_binding.house_ai_id,
            "version": b_side.house_ai_binding.house_ai_version,
            "artifact_sha256": b_side.house_ai_binding.artifact_sha256,
            "color": "BLACK",
        }
        house_ai_data = white_house_data
    else:
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
        "participant": participant_data,
        "house_ai": house_ai_data,
        "white_house_ai": white_house_data,
        "black_house_ai": black_house_data,
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
        "moves": [
            {
                "ply": m.sequence,
                "move_uci": m.move_uci,
                "from_square": m.move_uci[:2] if len(m.move_uci) >= 4 else None,
                "to_square": m.move_uci[2:4] if len(m.move_uci) >= 4 else None,
                "position_before_fen": m.position_before_fen,
                "position_after_fen": m.position_after_fen,
                "elapsed_ms": m.elapsed_ms,
                "recorded_at": m.recorded_at.isoformat() if m.recorded_at else None,
            }
            for m in (
                await db.execute(
                    select(MatchMove).where(MatchMove.match_id == match.id).order_by(MatchMove.sequence.asc())
                )
            ).scalars().all()
        ],
    }


async def request_trusted_house_development_match(
    db: AsyncSession,
    *,
    participant_id: str,
    idempotency_key: str,
    white_house_ai_id: str = "house-ai-benchmark-baseline",
    black_house_ai_id: str = "AIK-HOUSE-001",
) -> tuple[Match, bool]:
    """Queue a trusted House AI vs House AI match for development/testing."""
    if not idempotency_key or len(idempotency_key.encode("utf-8")) > 128:
        raise DevelopmentMatchError("IDEMPOTENCY_KEY_INVALID", 422)
    if re.search(r"[\x00-\x1f\x7f]", idempotency_key):
        raise DevelopmentMatchError("IDEMPOTENCY_KEY_INVALID", 422)
    if white_house_ai_id == black_house_ai_id:
        raise DevelopmentMatchError("DUPLICATE_HOUSE_AI_SIDE", 409)

    await _locked_active_participant(db, participant_id)

    raw_digest = f"{participant_id}:{white_house_ai_id}:{black_house_ai_id}"
    request_digest = hashlib.sha256(raw_digest.encode("utf-8")).hexdigest()

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

    w_version_row = await db.scalar(
        select(HouseAIVersion)
        .where(
            HouseAIVersion.house_ai_id == white_house_ai_id,
            HouseAIVersion.frozen_at.isnot(None),
        )
        .order_by(HouseAIVersion.version.desc())
        .limit(1)
    )
    if w_version_row is None:
        raise DevelopmentMatchError("HOUSE_AI_CONFIGURATION_INVALID", 404)

    b_version_row = await db.scalar(
        select(HouseAIVersion)
        .where(
            HouseAIVersion.house_ai_id == black_house_ai_id,
            HouseAIVersion.frozen_at.isnot(None),
        )
        .order_by(HouseAIVersion.version.desc())
        .limit(1)
    )
    if b_version_row is None:
        raise DevelopmentMatchError("HOUSE_AI_CONFIGURATION_INVALID", 404)

    policy_row, policy, resource_policy = await _load_policy(db)
    for h_ver in (w_version_row, b_version_row):
        if (
            h_ver.protocol_version != policy["protocol_version"]
            or h_ver.runtime_profile != policy["runtime_profile"]
            or h_ver.rules_version != policy["rules_version"]
            or h_ver.policy_version != policy_row.version
            or h_ver.policy_digest != policy_row.policy_digest
        ):
            raise DevelopmentMatchError("MATCH_SUBJECT_POLICY_INCOMPATIBLE", 409)

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

    white_side = MatchSide(
        id=uuid.uuid4(),
        match_id=match.id,
        color=MatchSideColor.WHITE,
        kind=MatchSideKind.HOUSE_AI,
        protocol_version=w_version_row.protocol_version,
        runtime_profile=w_version_row.runtime_profile,
        created_at=now,
    )
    white_binding = HouseAIMatchSideBinding(
        match_side_id=white_side.id,
        house_ai_id=w_version_row.house_ai_id,
        house_ai_version=w_version_row.version,
        artifact_sha256=w_version_row.artifact_sha256,
    )
    white_side.house_ai_binding = white_binding

    black_side = MatchSide(
        id=uuid.uuid4(),
        match_id=match.id,
        color=MatchSideColor.BLACK,
        kind=MatchSideKind.HOUSE_AI,
        protocol_version=b_version_row.protocol_version,
        runtime_profile=b_version_row.runtime_profile,
        created_at=now,
    )
    black_binding = HouseAIMatchSideBinding(
        match_side_id=black_side.id,
        house_ai_id=b_version_row.house_ai_id,
        house_ai_version=b_version_row.version,
        artifact_sha256=b_version_row.artifact_sha256,
    )
    black_side.house_ai_binding = black_binding

    db.add_all([match, white_side, black_side, white_binding, black_binding])
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
                "white_house_ai_id": w_version_row.house_ai_id,
                "white_house_ai_version": w_version_row.version,
                "white_house_ai_artifact_sha256": w_version_row.artifact_sha256,
                "black_house_ai_id": b_version_row.house_ai_id,
                "black_house_ai_version": b_version_row.version,
                "black_house_ai_artifact_sha256": b_version_row.artifact_sha256,
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


async def run_trusted_development_match(
    db: AsyncSession,
    *,
    match_id: uuid.UUID,
    white_engine: TrustedEngineSession | None = None,
    black_engine: TrustedEngineSession | None = None,
    max_plies: int = 200,
) -> Match:
    """Execute a trusted-only House AI match using MatchCoordinator, MatchController, and persist_controller_update."""
    import time
    from app.matches.chess import ChessRulesAdapter
    from app.matches.controller import (
        MatchClock,
        MatchConfiguration,
        MatchController,
        MatchExecutionBinding as MatchExecutionBindingValue,
        MatchSideColor,
        MatchSideIdentity,
    )
    from app.matches.coordinator import CoordinatorError, ExecutionAuthority, MatchCoordinator
    from app.matches.service import (
        begin_initialization,
        bind_execution,
        mark_execution_ready,
        mark_match_running,
        persist_controller_update,
    )
    from app.models.match import MatchExecutionBinding as MatchExecutionBindingRecord
    from app.models.worker import ExecutionJobStatus, Worker, WorkerExecutionJob, WorkerStatus
    from app.sandbox.contract import AIKTransport
    from app.services.trusted_engine_builder import TrustedEngineSession
    from app.workers.service import create_execution_job

    match = await db.get(Match, match_id)
    if match is None:
        raise DevelopmentMatchError("MATCH_NOT_FOUND", 404)
    if match.purpose != MatchPurpose.DEVELOPMENT:
        raise DevelopmentMatchError("DEVELOPMENT_MATCH_REQUIRED", 409)

    sides = (
        await db.execute(
            select(MatchSide)
            .where(MatchSide.match_id == match.id)
            .order_by(MatchSide.color)
        )
    ).scalars().all()
    if len(sides) != 2 or not all(s.kind == MatchSideKind.HOUSE_AI for s in sides):
        raise DevelopmentMatchError("TRUSTED_HOUSE_MATCH_REQUIRED", 409)

    w_side = next(s for s in sides if s.color == MatchSideColor.WHITE)
    b_side = next(s for s in sides if s.color == MatchSideColor.BLACK)

    if match.state == MatchState.QUEUED:
        await begin_initialization(db, match.id)
        await db.refresh(match)

    now_utc = datetime.now(timezone.utc)
    for side in (w_side, b_side):
        worker_id = f"trusted-worker-{side.color.value.lower()}"
        worker = await db.get(Worker, worker_id)
        if worker is None:
            worker = Worker(
                worker_id=worker_id,
                status=WorkerStatus.ACTIVE,
                revoked_at=None,
                capabilities={},
                security_state="APPROVED",
            )
            db.add(worker)
            await db.flush()
        elif worker.status != WorkerStatus.ACTIVE or worker.revoked_at is not None:
            worker.status = WorkerStatus.ACTIVE
            worker.revoked_at = None
            await db.flush()

        job = await db.scalar(
            select(WorkerExecutionJob)
            .where(
                WorkerExecutionJob.match_id == match.id,
                WorkerExecutionJob.house_ai_id == side.house_ai_binding.house_ai_id,
                WorkerExecutionJob.status.in_([
                    ExecutionJobStatus.QUEUED,
                    ExecutionJobStatus.LEASED,
                    ExecutionJobStatus.ACKNOWLEDGED,
                ]),
            )
        )
        if job is not None:
            expires_at = job.execution_expires_at
            if expires_at is not None and expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at is None or expires_at <= now_utc:
                # execution_expires_at is immutable (ORM guard + PostgreSQL trigger), so a
                # stale queued job is retired and replaced instead of being extended.
                job.status = ExecutionJobStatus.EXPIRED
                await db.flush()
                job = None
        if job is None:
            policy_row, policy, resource_policy = await _load_policy(db)
            job = await create_execution_job(
                db,
                house_ai_id=side.house_ai_binding.house_ai_id,
                house_ai_version=side.house_ai_binding.house_ai_version,
                initial_state={
                    "match_id": str(match.id),
                    "purpose": match.purpose.value,
                    "match_seed": match.match_seed,
                    "initial_fen": match.initial_fen,
                    "protocol_version": match.protocol_version,
                    "runtime_profile": match.runtime_profile,
                    "rules_version": match.rules_version,
                    "policy_id": match.policy_id,
                    "policy_version": match.policy_version,
                    "policy_digest": match.policy_digest,
                    "initial_clock_ms": match.initial_clock_ms,
                    "increment_ms": match.increment_ms,
                    "move_timeout_ms": match.move_timeout_ms,
                },
                resource_policy=resource_policy,
                execution_expires_at=now_utc + timedelta(hours=1),
                match_id=match.id,
                execution_mode="KATA_AI_EXECUTION",
                commit=False,
            )

        lease_id = uuid.uuid4()
        fencing_token = (job.fencing_token or 0) + 1
        job.status = ExecutionJobStatus.ACKNOWLEDGED
        job.assigned_worker_id = worker_id
        job.lease_id = lease_id
        job.fencing_token = fencing_token
        job.lease_expires_at = job.execution_expires_at
        await db.flush()

        binding = await bind_execution(
            db,
            match_id=match.id,
            side_id=side.id,
            execution_id=job.execution_id,
            worker_id=worker_id,
            lease_id=lease_id,
            fencing_token=fencing_token,
        )
        await mark_execution_ready(db, binding=binding)

    running_match = await mark_match_running(db, match.id)

    configuration = MatchConfiguration(
        protocol_version=running_match.protocol_version,
        runtime_profile=running_match.runtime_profile,
        policy_version=running_match.policy_version,
        rules_version=running_match.rules_version,
        initial_fen=running_match.initial_fen,
        initial_clock_ms=running_match.initial_clock_ms,
        increment_ms=running_match.increment_ms,
        move_timeout_ms=running_match.move_timeout_ms,
        match_seed=running_match.match_seed,
        policy_id=running_match.policy_id,
        policy_digest=running_match.policy_digest,
    )
    side_identities = {}
    for s in (w_side, b_side):
        color = MatchSideColor(s.color.value)
        house = s.house_ai_binding
        side_identities[color] = MatchSideIdentity(
            side_id=s.id,
            match_id=s.match_id,
            color=color,
            participant_id=None,
            submission_id=None,
            submission_version_id=None,
            artifact_digest=s.artifact_sha256,
            protocol_version=s.protocol_version,
            runtime_profile=s.runtime_profile,
            kind=s.kind,
            house_ai_id=house.house_ai_id if house else None,
            house_ai_version=house.house_ai_version if house else None,
        )

    bindings_rows = (
        await db.execute(
            select(MatchExecutionBindingRecord)
            .where(MatchExecutionBindingRecord.match_id == running_match.id)
        )
    ).scalars().all()

    binding_values = {}
    for brow in bindings_rows:
        s_row = next(s for s in (w_side, b_side) if s.id == brow.match_side_id)
        color = MatchSideColor(s_row.color.value)
        binding_values[color] = MatchExecutionBindingValue(
            binding_id=brow.id,
            match_id=brow.match_id,
            side_id=brow.match_side_id,
            execution_id=brow.execution_id,
            submission_version_id=brow.submission_version_id,
            artifact_digest=brow.artifact_sha256,
            worker_id=brow.worker_id,
            lease_id=brow.lease_id,
            fencing_token=brow.fencing_token,
            attempt_number=brow.attempt_number,
            kind=brow.execution_kind,
            house_ai_id=brow.house_ai_id,
            house_ai_version=brow.house_ai_version,
        )

    controller = MatchController(
        match_id=running_match.id,
        state=MatchState.RUNNING,
        sides=side_identities,
        bindings=binding_values,
        configuration=configuration,
        current_fen=running_match.current_fen,
        current_ply=running_match.current_ply,
        side_to_move=MatchSideColor(running_match.side_to_move.value),
        clock=MatchClock(
            running_match.white_remaining_ms,
            running_match.black_remaining_ms,
            running_match.increment_ms,
            MatchSideColor(running_match.side_to_move.value),
        ),
        rules=ChessRulesAdapter(running_match.initial_fen, rules_version=running_match.rules_version),
        transport=AIKTransport(monotonic=time.monotonic),
        monotonic=time.monotonic,
    )

    async def _persist(ctrl: MatchController) -> None:
        await persist_controller_update(db, ctrl)

    coordinator = MatchCoordinator(_persist)
    coordinator.register(controller)

    authorities = {
        color: ExecutionAuthority(
            match_id=running_match.id,
            side_id=side_identities[color].side_id,
            execution_id=binding_values[color].execution_id,
            worker_id=binding_values[color].worker_id,
            lease_id=binding_values[color].lease_id,
            fence=binding_values[color].fencing_token,
            attempt=binding_values[color].attempt_number,
            side=color,
        )
        for color in (MatchSideColor.WHITE, MatchSideColor.BLACK)
    }

    if white_engine is None:
        white_engine = TrustedEngineSession(w_side.house_ai_binding.house_ai_id)
    if black_engine is None:
        black_engine = TrustedEngineSession(b_side.house_ai_binding.house_ai_id)

    engines = {MatchSideColor.WHITE: white_engine, MatchSideColor.BLACK: black_engine}

    for _ in range(max_plies * 4):
        if controller.state != MatchState.RUNNING or controller.result or controller.failure:
            break
        action_taken = False
        sides_to_check = [
            controller.side_to_move,
            MatchSideColor.BLACK if controller.side_to_move == MatchSideColor.WHITE else MatchSideColor.WHITE,
        ]
        for color in sides_to_check:
            authority = authorities[color]
            try:
                cmd = await coordinator.next_command(authority)
            except CoordinatorError as exc:
                if str(exc) == "MATCH_TERMINAL":
                    break
                raise
            if cmd is not None:
                # Engine search is CPU-bound; run it off the event loop so the API keeps
                # serving other requests while a trusted duel is in progress.
                resp = await asyncio.to_thread(engines[cmd.side].handle_message, cmd.message)
                await coordinator.submit_response(
                    authority, command_id=cmd.command_id, payload=resp
                )
                action_taken = True
                break
        if not action_taken:
            break

    if (
        controller.state == MatchState.RUNNING
        and controller.result is None
        and controller.failure is None
    ):
        # Never leave the match RUNNING when the ply budget runs out: a dangling RUNNING
        # match blocks every later duel for this runner (ACTIVE_DEVELOPMENT_MATCH_EXISTS).
        controller._abort(MatchFailure.EXECUTION_TIMEOUT, "TRUSTED_DUEL_PLY_LIMIT_REACHED")
        await persist_controller_update(db, controller)

    await db.refresh(running_match)
    return running_match

