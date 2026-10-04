"""Durable server-authoritative match, side, execution, and move records."""

from __future__ import annotations

import enum
import uuid
from datetime import datetime

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    Uuid,
    event,
)
from sqlalchemy.orm import Mapped, Session, mapped_column

from app.db.base import Base


class MatchState(str, enum.Enum):
    QUEUED = "QUEUED"
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    ABORTED = "ABORTED"


class MatchSideColor(str, enum.Enum):
    WHITE = "WHITE"
    BLACK = "BLACK"


class MatchOutcome(str, enum.Enum):
    WHITE_WIN = "WHITE_WIN"
    BLACK_WIN = "BLACK_WIN"
    DRAW = "DRAW"


class MatchFailure(str, enum.Enum):
    ILLEGAL_MOVE = "ILLEGAL_MOVE"
    MOVE_TIMEOUT = "MOVE_TIMEOUT"
    PROCESS_CRASH = "PROCESS_CRASH"
    PROTOCOL_ERROR = "PROTOCOL_ERROR"
    RESOURCE_EXCEEDED = "RESOURCE_EXCEEDED"
    EXECUTION_TIMEOUT = "EXECUTION_TIMEOUT"
    WORKER_FAILURE = "WORKER_FAILURE"
    SANDBOX_FAILURE = "SANDBOX_FAILURE"
    CLEANUP_FAILURE = "CLEANUP_FAILURE"
    STALE_FENCING = "STALE_FENCING"
    CONTROLLER_RESTART = "CONTROLLER_RESTART"
    TRANSCRIPT_CORRUPTED = "TRANSCRIPT_CORRUPTED"
    ABORTED = "ABORTED"


_MATCH_TRANSITIONS: dict[MatchState, set[MatchState]] = {
    MatchState.QUEUED: {MatchState.STARTING, MatchState.ABORTED},
    MatchState.STARTING: {MatchState.RUNNING, MatchState.ABORTED},
    MatchState.RUNNING: {MatchState.COMPLETED, MatchState.ABORTED},
    MatchState.COMPLETED: set(),
    MatchState.ABORTED: set(),
}


class Match(Base):
    __tablename__ = "matches"
    __table_args__ = (
        CheckConstraint(
            "state IN ('QUEUED','STARTING','RUNNING','COMPLETED','ABORTED')",
            name="ck_matches_state",
        ),
        CheckConstraint("current_ply >= 0", name="ck_matches_ply_nonnegative"),
        CheckConstraint("side_to_move IN ('WHITE','BLACK')", name="ck_matches_side_to_move"),
        CheckConstraint("length(match_seed) = 64", name="ck_matches_seed_length"),
        CheckConstraint("initial_clock_ms > 0", name="ck_matches_initial_clock_positive"),
        CheckConstraint("increment_ms >= 0", name="ck_matches_increment_nonnegative"),
        CheckConstraint("white_remaining_ms >= 0", name="ck_matches_white_clock_nonnegative"),
        CheckConstraint("black_remaining_ms >= 0", name="ck_matches_black_clock_nonnegative"),
        CheckConstraint(
            "result_outcome IS NULL OR result_outcome IN ('WHITE_WIN','BLACK_WIN','DRAW')",
            name="ck_matches_result_outcome",
        ),
        CheckConstraint(
            "result_reason IS NULL OR result_reason IN "
            "('CHECKMATE','STALEMATE','DEAD_POSITION','FIVEFOLD_REPETITION',"
            "'SEVENTY_FIVE_MOVE_RULE','THREEFOLD_REPETITION','FIFTY_MOVE_RULE')",
            name="ck_matches_result_reason",
        ),
        CheckConstraint(
            "(state = 'COMPLETED' AND result_outcome IS NOT NULL AND result_reason IS NOT NULL "
            "AND failure_reason IS NULL) OR "
            "(state = 'ABORTED' AND result_outcome IS NULL AND result_reason IS NULL "
            "AND failure_reason IS NOT NULL) OR "
            "(state NOT IN ('COMPLETED','ABORTED') AND result_outcome IS NULL "
            "AND result_reason IS NULL AND failure_reason IS NULL)",
            name="ck_matches_terminal_authority",
        ),
        CheckConstraint(
            "(state = 'QUEUED' AND started_at IS NULL AND completed_at IS NULL) OR "
            "(state = 'STARTING' AND completed_at IS NULL) OR "
            "(state = 'RUNNING' AND started_at IS NOT NULL AND completed_at IS NULL) OR "
            "(state IN ('COMPLETED','ABORTED') AND completed_at IS NOT NULL)",
            name="ck_matches_lifecycle_timestamps",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    state: Mapped[MatchState] = mapped_column(
        Enum(MatchState, native_enum=False, length=16), nullable=False
    )
    protocol_version: Mapped[str] = mapped_column(String(64), nullable=False)
    runtime_profile: Mapped[str] = mapped_column(String(64), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(64), nullable=False)
    rules_version: Mapped[str] = mapped_column(String(64), nullable=False)
    match_seed: Mapped[str] = mapped_column(String(64), nullable=False)
    initial_fen: Mapped[str] = mapped_column(String(256), nullable=False)
    current_fen: Mapped[str] = mapped_column(String(256), nullable=False)
    side_to_move: Mapped[MatchSideColor] = mapped_column(
        Enum(MatchSideColor, native_enum=False, length=8), nullable=False
    )
    current_ply: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    initial_clock_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    increment_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    move_timeout_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    white_remaining_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    black_remaining_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    result_outcome: Mapped[str | None] = mapped_column(String(16), nullable=True)
    result_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MatchSide(Base):
    __tablename__ = "match_sides"
    __table_args__ = (
        UniqueConstraint("match_id", "color", name="uq_match_sides_color"),
        UniqueConstraint("match_id", "participant_id", name="uq_match_sides_participant"),
        CheckConstraint("color IN ('WHITE','BLACK')", name="ck_match_sides_color"),
        CheckConstraint("length(artifact_sha256) = 64", name="ck_match_side_sha256"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    match_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("matches.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    color: Mapped[MatchSideColor] = mapped_column(
        Enum(MatchSideColor, native_enum=False, length=8), nullable=False
    )
    participant_id: Mapped[str] = mapped_column(
        ForeignKey("participants.participant_id", ondelete="RESTRICT"), nullable=False
    )
    submission_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("submissions.id", ondelete="RESTRICT"), nullable=False
    )
    submission_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("submission_versions.id", ondelete="RESTRICT"), nullable=False
    )
    artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    protocol_version: Mapped[str] = mapped_column(String(64), nullable=False)
    runtime_profile: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MatchExecutionBinding(Base):
    __tablename__ = "match_execution_bindings"
    __table_args__ = (
        UniqueConstraint("execution_id", name="uq_match_execution_id"),
        UniqueConstraint("match_side_id", "attempt_number", name="uq_match_side_attempt"),
        CheckConstraint("attempt_number > 0", name="ck_match_execution_attempt_positive"),
        CheckConstraint("fencing_token > 0", name="ck_match_execution_fence_positive"),
        CheckConstraint("length(artifact_sha256) = 64", name="ck_match_execution_sha256"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    match_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("matches.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    match_side_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("match_sides.id", ondelete="RESTRICT"), nullable=False
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    execution_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    submission_version_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("submission_versions.id", ondelete="RESTRICT"), nullable=False
    )
    artifact_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    worker_id: Mapped[str] = mapped_column(
        ForeignKey("workers.worker_id", ondelete="RESTRICT"), nullable=False
    )
    lease_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False)
    fencing_token: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MatchMove(Base):
    __tablename__ = "match_moves"
    __table_args__ = (
        UniqueConstraint("match_id", "sequence", name="uq_match_move_sequence"),
        UniqueConstraint("match_id", "request_id", name="uq_match_move_request"),
        CheckConstraint("sequence > 0", name="ck_match_move_sequence_positive"),
        CheckConstraint("elapsed_ms >= 0", name="ck_match_move_elapsed_nonnegative"),
        CheckConstraint("length(move_uci) IN (4,5)", name="ck_match_move_uci_length"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True)
    match_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("matches.id", ondelete="RESTRICT"), nullable=False
    )
    match_side_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("match_sides.id", ondelete="RESTRICT"), nullable=False
    )
    execution_binding_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("match_execution_bindings.id", ondelete="RESTRICT"), nullable=False
    )
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    request_id: Mapped[str] = mapped_column(String(64), nullable=False)
    move_uci: Mapped[str] = mapped_column(String(5), nullable=False)
    position_before_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    position_after_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    position_before_fen: Mapped[str] = mapped_column(String(256), nullable=False)
    position_after_fen: Mapped[str] = mapped_column(String(256), nullable=False)
    elapsed_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    recorded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


_IMMUTABLE_MATCH_MODELS = (MatchSide, MatchExecutionBinding, MatchMove)
_MATCH_CONFIG_FIELDS = {
    "id",
    "protocol_version",
    "runtime_profile",
    "policy_version",
    "rules_version",
    "match_seed",
    "initial_fen",
    "initial_clock_ms",
    "increment_ms",
    "move_timeout_ms",
    "created_at",
}
_MATCH_MUTABLE_FIELDS = {
    "state",
    "current_fen",
    "side_to_move",
    "current_ply",
    "white_remaining_ms",
    "black_remaining_ms",
    "result_outcome",
    "result_reason",
    "failure_reason",
    "started_at",
    "completed_at",
}


@event.listens_for(Session, "before_flush")
def enforce_match_immutability(session: Session, _flush_context, _instances) -> None:
    for obj in session.new:
        if isinstance(obj, Match) and obj.state != MatchState.QUEUED:
            raise ValueError("MATCH_MUST_START_QUEUED")
    for obj in session.dirty:
        if isinstance(obj, _IMMUTABLE_MATCH_MODELS) and session.is_modified(
            obj, include_collections=False
        ):
            raise ValueError("MATCH_APPEND_ONLY_RECORD_IMMUTABLE")
        if isinstance(obj, Match) and session.is_modified(obj, include_collections=False):
            from sqlalchemy import inspect

            state = inspect(obj)
            changed = {
                attribute.key for attribute in state.attrs if attribute.history.has_changes()
            }
            for timestamp_field in ("started_at", "completed_at"):
                history = state.attrs[timestamp_field].history
                if history.has_changes() and history.deleted and history.deleted[0] is not None:
                    raise ValueError("MATCH_LIFECYCLE_TIMESTAMP_IMMUTABLE")
            if changed & _MATCH_CONFIG_FIELDS:
                raise ValueError("MATCH_IDENTITY_IMMUTABLE")
            if changed - _MATCH_MUTABLE_FIELDS:
                raise ValueError("MATCH_FIELD_NOT_MUTABLE")
            if "state" in changed:
                old_state = state.attrs.state.history.deleted[0]
                if obj.state not in _MATCH_TRANSITIONS.get(old_state, set()):
                    raise ValueError("INVALID_MATCH_STATE_TRANSITION")
            if state.attrs.state.history.deleted:
                old_state = state.attrs.state.history.deleted[0]
                if old_state in {MatchState.COMPLETED, MatchState.ABORTED}:
                    raise ValueError("TERMINAL_MATCH_IMMUTABLE")
            if obj.state == MatchState.COMPLETED and (
                not obj.result_outcome or not obj.result_reason or obj.failure_reason
            ):
                raise ValueError("COMPLETED_MATCH_REQUIRES_SERVER_RESULT")
            if obj.state == MatchState.ABORTED and (
                not obj.failure_reason or obj.result_outcome or obj.result_reason
            ):
                raise ValueError("ABORTED_MATCH_CANNOT_DECLARE_CHESS_RESULT")
    for obj in session.deleted:
        if isinstance(obj, (*_IMMUTABLE_MATCH_MODELS, Match)):
            raise ValueError("MATCH_RECORD_CANNOT_BE_DELETED")
