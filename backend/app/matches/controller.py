"""Authoritative match domain logic over inert AIK messages and a rules port.

This module owns accepted position/clock/result state. It does not launch an AI,
provide a chess implementation, or claim live runtime deadline enforcement.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Protocol

from app.models.match import (
    MatchFailure,
    MatchOutcome,
    MatchSideColor,
    MatchSideKind,
    MatchState,
)
from app.sandbox.contract import AIKMessage, AIKProtocolError, AIKTransport


class MatchControllerError(RuntimeError):
    """Base for rejected or terminal match operations."""


class InvalidMatchTransitionError(MatchControllerError):
    pass


class StaleExecutionReportError(MatchControllerError):
    pass


class DuplicateMoveError(MatchControllerError):
    pass


class OutOfOrderMoveError(MatchControllerError):
    pass


class MatchTranscriptCorruptedError(ValueError):
    """Persisted match evidence failed deterministic replay; never repair it."""


class RulesAdapter(Protocol):
    def side_to_move(self, position: str, history: tuple[str, ...] = ()) -> str: ...

    def apply_legal_move(
        self, position: str, move_uci: str, history: tuple[str, ...]
    ) -> str | None:
        """Return the new position, or None when the proposal is illegal."""

    def terminal_result(
        self, position: str, history: tuple[str, ...]
    ) -> tuple[MatchOutcome, str] | None: ...

    def verify_draw_claim(
        self, position: str, history: tuple[str, ...], reason: str, intended_move: str | None
    ) -> bool: ...


@dataclass(frozen=True)
class MatchConfiguration:
    protocol_version: str
    runtime_profile: str
    policy_version: str
    rules_version: str
    initial_fen: str
    initial_clock_ms: int
    increment_ms: int
    move_timeout_ms: int
    match_seed: str
    policy_id: str | None = None
    policy_digest: str | None = None

    def __post_init__(self) -> None:
        if self.protocol_version != "aik-jsonl/1":
            raise ValueError("MATCH_PROTOCOL_VERSION_UNSUPPORTED")
        for name in ("protocol_version", "runtime_profile", "policy_version", "rules_version"):
            value = getattr(self, name)
            if not value or len(value.encode("utf-8")) > 64:
                raise ValueError(f"MATCH_{name.upper()}_INVALID")
        if not re.fullmatch(r"[0-9a-f]{64}", self.match_seed):
            raise ValueError("MATCH_SEED_INVALID")
        if self.policy_digest is not None and not re.fullmatch(r"[0-9a-f]{64}", self.policy_digest):
            raise ValueError("MATCH_POLICY_DIGEST_INVALID")
        if (self.policy_id is None) != (self.policy_digest is None):
            raise ValueError("MATCH_POLICY_IDENTITY_INVALID")
        if not self.initial_fen or len(self.initial_fen.encode("utf-8")) > 256:
            raise ValueError("MATCH_INITIAL_POSITION_INVALID")
        if not 1 <= self.initial_clock_ms <= 3_600_000:
            raise ValueError("MATCH_INITIAL_CLOCK_INVALID")
        if not 0 <= self.increment_ms <= 60_000:
            raise ValueError("MATCH_CLOCK_INCREMENT_INVALID")
        if not 1 <= self.move_timeout_ms <= 3_600_000:
            raise ValueError("MATCH_MOVE_TIMEOUT_INVALID")


@dataclass(frozen=True)
class MatchSideIdentity:
    side_id: uuid.UUID
    match_id: uuid.UUID
    color: MatchSideColor
    participant_id: str | None
    submission_id: uuid.UUID | None
    submission_version_id: uuid.UUID | None
    artifact_digest: str
    protocol_version: str
    runtime_profile: str
    kind: MatchSideKind = MatchSideKind.PARTICIPANT
    house_ai_id: str | None = None
    house_ai_version: int | None = None

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", self.artifact_digest):
            raise ValueError("MATCH_SIDE_DIGEST_INVALID")
        participant_identity = (
            self.participant_id is not None
            and self.submission_id is not None
            and self.submission_version_id is not None
            and self.house_ai_id is None
            and self.house_ai_version is None
        )
        house_identity = (
            self.participant_id is None
            and self.submission_id is None
            and self.submission_version_id is None
            and bool(self.house_ai_id)
            and self.house_ai_version is not None
            and self.house_ai_version > 0
        )
        if (
            self.kind not in {MatchSideKind.PARTICIPANT, MatchSideKind.HOUSE_AI}
            or (self.kind == MatchSideKind.PARTICIPANT and not participant_identity)
            or (self.kind == MatchSideKind.HOUSE_AI and not house_identity)
            or not self.protocol_version
            or not self.runtime_profile
        ):
            raise ValueError("MATCH_SIDE_IDENTITY_INVALID")

    @property
    def actor_identity(self) -> tuple:
        if self.kind == MatchSideKind.PARTICIPANT:
            return (self.kind, self.participant_id)
        return (self.kind, self.house_ai_id, self.house_ai_version)


@dataclass(frozen=True)
class MatchExecutionBinding:
    binding_id: uuid.UUID
    match_id: uuid.UUID
    side_id: uuid.UUID
    execution_id: uuid.UUID
    submission_version_id: uuid.UUID | None
    artifact_digest: str
    worker_id: str
    lease_id: uuid.UUID
    fencing_token: int
    attempt_number: int
    kind: MatchSideKind = MatchSideKind.PARTICIPANT
    house_ai_id: str | None = None
    house_ai_version: int | None = None

    def __post_init__(self) -> None:
        if not re.fullmatch(r"[0-9a-f]{64}", self.artifact_digest):
            raise ValueError("EXECUTION_BINDING_DIGEST_INVALID")
        if (
            not self.worker_id
            or self.fencing_token < 1
            or self.attempt_number < 1
            or self.kind not in {MatchSideKind.PARTICIPANT, MatchSideKind.HOUSE_AI}
            or (
                self.kind == MatchSideKind.PARTICIPANT
                and (
                    self.submission_version_id is None
                    or self.house_ai_id is not None
                    or self.house_ai_version is not None
                )
            )
            or (
                self.kind == MatchSideKind.HOUSE_AI
                and (
                    self.submission_version_id is not None
                    or not self.house_ai_id
                    or self.house_ai_version is None
                    or self.house_ai_version < 1
                )
            )
        ):
            raise ValueError("EXECUTION_BINDING_IDENTITY_INVALID")

    def matches_side(self, side: MatchSideIdentity) -> bool:
        return (
            self.match_id == side.match_id
            and self.side_id == side.side_id
            and self.kind == side.kind
            and (
                self.submission_version_id == side.submission_version_id
                if side.kind == MatchSideKind.PARTICIPANT
                else self.house_ai_id == side.house_ai_id
                and self.house_ai_version == side.house_ai_version
            )
            and self.artifact_digest == side.artifact_digest
        )


class MatchResultReason(str, Enum):
    CHECKMATE = "CHECKMATE"
    STALEMATE = "STALEMATE"
    DEAD_POSITION = "DEAD_POSITION"
    FIVEFOLD_REPETITION = "FIVEFOLD_REPETITION"
    SEVENTY_FIVE_MOVE_RULE = "SEVENTY_FIVE_MOVE_RULE"
    THREEFOLD_REPETITION = "THREEFOLD_REPETITION"
    FIFTY_MOVE_RULE = "FIFTY_MOVE_RULE"


@dataclass(frozen=True)
class MatchResult:
    outcome: MatchOutcome
    reason: MatchResultReason

    def __post_init__(self) -> None:
        try:
            outcome = MatchOutcome(self.outcome)
        except ValueError as exc:
            raise ValueError("MATCH_RESULT_OUTCOME_INVALID") from exc
        try:
            reason = MatchResultReason(self.reason)
        except ValueError as exc:
            raise ValueError("MATCH_RESULT_REASON_NOT_CONTRACT_DEFINED") from exc
        object.__setattr__(self, "outcome", outcome)
        object.__setattr__(self, "reason", reason)


class MatchEventType(str, Enum):
    MATCH_STARTED = "MATCH_STARTED"
    MOVE_REQUESTED = "MOVE_REQUESTED"
    MOVE_ACCEPTED = "MOVE_ACCEPTED"
    MOVE_REJECTED = "MOVE_REJECTED"
    MATCH_COMPLETED = "MATCH_COMPLETED"
    MATCH_ABORTED = "MATCH_ABORTED"


@dataclass(frozen=True)
class MatchEvent:
    event_type: MatchEventType
    match_id: uuid.UUID
    sequence: int
    recorded_at: datetime
    actor_kind: str
    actor_id: str
    metadata: dict = field(default_factory=dict)


@dataclass(frozen=True)
class MatchClock:
    white_remaining_ms: int
    black_remaining_ms: int
    increment_ms: int
    side_to_move: MatchSideColor
    turn_started_monotonic: float | None = None
    move_deadline_monotonic: float | None = None

    def __post_init__(self) -> None:
        if min(self.white_remaining_ms, self.black_remaining_ms, self.increment_ms) < 0:
            raise ValueError("MATCH_CLOCK_VALUE_INVALID")
        if self.turn_started_monotonic is None and self.move_deadline_monotonic is not None:
            raise ValueError("MATCH_CLOCK_DEADLINE_WITHOUT_START")
        if (
            self.turn_started_monotonic is not None
            and self.move_deadline_monotonic is not None
            and self.move_deadline_monotonic < self.turn_started_monotonic
        ):
            raise ValueError("MATCH_CLOCK_DEADLINE_INVALID")

    def start_turn(self, now: float, move_timeout_ms: int) -> MatchClock:
        if self.remaining(self.side_to_move) <= 0:
            raise ValueError("MATCH_CLOCK_EXHAUSTED")
        deadline = min(now + move_timeout_ms / 1000, now + self.remaining(self.side_to_move) / 1000)
        return MatchClock(
            self.white_remaining_ms,
            self.black_remaining_ms,
            self.increment_ms,
            self.side_to_move,
            now,
            deadline,
        )

    def remaining(self, side: MatchSideColor) -> int:
        return self.white_remaining_ms if side == MatchSideColor.WHITE else self.black_remaining_ms

    def elapsed_ms(self, received_at: float) -> int:
        if self.turn_started_monotonic is None:
            raise ValueError("MATCH_CLOCK_NOT_STARTED")
        return max(0, int((received_at - self.turn_started_monotonic) * 1000))

    def after_move(self, elapsed_ms: int, next_side: MatchSideColor) -> MatchClock:
        if elapsed_ms < 0 or elapsed_ms > self.remaining(self.side_to_move):
            raise ValueError("MATCH_CLOCK_EXHAUSTED")
        remaining = self.remaining(self.side_to_move) - elapsed_ms + self.increment_ms
        if self.side_to_move == MatchSideColor.WHITE:
            white, black = remaining, self.black_remaining_ms
        else:
            white, black = self.white_remaining_ms, remaining
        return MatchClock(white, black, self.increment_ms, next_side)


@dataclass(frozen=True)
class MatchMove:
    match_id: uuid.UUID
    sequence: int
    side_id: uuid.UUID
    color: MatchSideColor
    move_uci: str
    position_before_sha256: str
    position_after_sha256: str
    position_before_fen: str
    position_after_fen: str
    elapsed_ms: int
    recorded_at: datetime
    execution_binding: MatchExecutionBinding
    request_id: str


@dataclass(frozen=True)
class ProposalDecision:
    accepted: bool
    move: MatchMove | None = None
    server_response: AIKMessage | None = None
    result: MatchResult | None = None
    failure: MatchFailure | None = None


def position_identity(fen: str) -> str:
    return hashlib.sha256(fen.encode("utf-8")).hexdigest()


class MatchController:
    """One in-memory authoritative controller instance for a persisted match."""

    def __init__(
        self,
        *,
        match_id: uuid.UUID,
        state: MatchState,
        sides: dict[MatchSideColor, MatchSideIdentity],
        bindings: dict[MatchSideColor, MatchExecutionBinding],
        configuration: MatchConfiguration,
        current_fen: str,
        current_ply: int,
        side_to_move: MatchSideColor,
        clock: MatchClock,
        rules: RulesAdapter | None = None,
        transport: AIKTransport,
        monotonic,
        wall_clock=lambda: datetime.now(timezone.utc),
        history: tuple[str, ...] = (),
        new_game_acked: set[MatchSideColor] | None = None,
        hello_ready: set[MatchSideColor] | None = None,
    ) -> None:
        if set(sides) != {MatchSideColor.WHITE, MatchSideColor.BLACK}:
            raise ValueError("MATCH_REQUIRES_EXACTLY_TWO_SIDES")
        if set(bindings) != set(sides) and not (state == MatchState.QUEUED and not bindings):
            raise ValueError("MATCH_REQUIRES_TWO_EXECUTION_BINDINGS")
        if current_ply < 0:
            raise ValueError("MATCH_PLY_INVALID")
        if (
            sides[MatchSideColor.WHITE].actor_identity == sides[MatchSideColor.BLACK].actor_identity
            and sides[MatchSideColor.WHITE].kind != MatchSideKind.HOUSE_AI
        ):
            raise ValueError("DUPLICATE_MATCH_SIDE_IDENTITY")
        for color, side in sides.items():
            if (
                side.match_id != match_id
                or side.color != color
                or side.protocol_version != configuration.protocol_version
                or side.runtime_profile != configuration.runtime_profile
            ):
                raise ValueError("MATCH_SIDE_CONFIGURATION_MISMATCH")
        for color, binding in bindings.items():
            if not binding.matches_side(sides[color]):
                raise ValueError("EXECUTION_BINDING_SIDE_MISMATCH")
        self.match_id = match_id
        self.state = state
        self.sides = dict(sides)
        self.bindings = dict(bindings)
        self.configuration = configuration
        self.current_fen = current_fen
        self.current_ply = current_ply
        self.side_to_move = side_to_move
        self.clock = clock
        if rules is None:
            from app.matches.chess import ChessRulesAdapter

            rules = ChessRulesAdapter(
                configuration.initial_fen, rules_version=configuration.rules_version
            )
        adapter_initial = getattr(rules, "initial_position", configuration.initial_fen)
        if configuration.initial_fen != adapter_initial:
            raise ValueError("MATCH_INITIAL_POSITION_NOT_CANONICAL")
        if current_ply == 0 and current_fen != adapter_initial:
            raise ValueError("MATCH_INITIAL_POSITION_NOT_CANONICAL")
        self.rules = rules
        self.transport = transport
        self.monotonic = monotonic
        self.wall_clock = wall_clock
        self.history = history
        self.new_game_acked = set(new_game_acked or set())
        self.hello_ready = set(hello_ready or set())
        self.moves: list[MatchMove] = []
        self.events: list[MatchEvent] = []
        self.result: MatchResult | None = None
        self.failure: MatchFailure | None = None
        self.started_at: datetime | None = None
        self.completed_at: datetime | None = None
        self._request_id: str | None = None
        self._awaiting_ack_id: str | None = None
        self._awaiting_ack_kind: str | None = None
        self._position_acked = False
        self._claim_attempted = False
        self._used_request_ids: set[str] = set()
        self.persisted_ply = current_ply
        self.persisted_state = state
        self.persisted_move_count = 0
        self.persisted_event_count = 0

    def start(self, *, ready_execution_ids: set[uuid.UUID]) -> None:
        if self.state != MatchState.STARTING:
            raise InvalidMatchTransitionError("MATCH_NOT_STARTING")
        if set(self.bindings) != set(self.sides):
            raise InvalidMatchTransitionError("BOTH_EXECUTIONS_MUST_BE_BOUND")
        if ready_execution_ids != {binding.execution_id for binding in self.bindings.values()}:
            raise InvalidMatchTransitionError("BOTH_EXECUTIONS_MUST_BE_READY")
        self.state = MatchState.RUNNING
        self.started_at = self.wall_clock()
        side_reader = getattr(self.rules, "side_to_move", None)
        if side_reader is not None:
            self.side_to_move = MatchSideColor(side_reader(self.current_fen, self.history))
        self.clock = MatchClock(
            self.clock.white_remaining_ms,
            self.clock.black_remaining_ms,
            self.clock.increment_ms,
            self.side_to_move,
        )
        self._event("MATCH_STARTED", "controller", {"first_side": self.side_to_move.value})

    def issue_new_game_request(self, *, side: MatchSideColor, request_id: str) -> AIKMessage:
        if self.state != MatchState.RUNNING or side not in self.sides:
            raise InvalidMatchTransitionError("MATCH_NOT_RUNNING")
        if side not in self.hello_ready:
            raise MatchControllerError("HELLO_READY_REQUIRED")
        if side in self.new_game_acked or self._awaiting_ack_id is not None:
            raise MatchControllerError("NEW_GAME_REQUEST_ALREADY_ACTIVE")
        self.transport.clear_deadline()
        self._reserve_request_id(request_id)
        self._awaiting_ack_id = request_id
        self._awaiting_ack_kind = "new_game"
        return self.transport.encode_controller_message(
            request_id,
            {
                "protocol_version": self.configuration.protocol_version,
                "type": "new_game",
                "side": side.value,
                "variant": "standard",
                "start_fen": self.configuration.initial_fen,
                "seed": self.configuration.match_seed,
                "clock": {
                    "initial_ms": self.configuration.initial_clock_ms,
                    "increment_ms": self.configuration.increment_ms,
                },
            },
        )

    def issue_hello_request(self, *, side: MatchSideColor, request_id: str) -> AIKMessage:
        if self.state != MatchState.RUNNING or side not in self.sides:
            raise InvalidMatchTransitionError("MATCH_NOT_RUNNING")
        if side in self.hello_ready or self._awaiting_ack_id is not None:
            raise MatchControllerError("HELLO_REQUEST_ALREADY_ACTIVE")
        self.transport.clear_deadline()
        self._reserve_request_id(request_id)
        self._awaiting_ack_id = request_id
        self._awaiting_ack_kind = "hello"
        return self.transport.encode_controller_message(
            request_id,
            {
                "protocol_version": self.configuration.protocol_version,
                "type": "hello",
                "match_id": str(self.match_id),
                "side": side.value,
                "runtime": self.configuration.runtime_profile,
            },
        )

    def issue_position_request(self, *, side: MatchSideColor, request_id: str) -> AIKMessage:
        self._assert_turn(side)
        if side not in self.new_game_acked:
            raise MatchControllerError("NEW_GAME_ACK_REQUIRED")
        if self._awaiting_ack_id is not None or self._request_id is not None:
            raise MatchControllerError("AIK_REQUEST_ALREADY_ACTIVE")
        self.transport.clear_deadline()
        self._reserve_request_id(request_id)
        self._awaiting_ack_id = request_id
        self._awaiting_ack_kind = "position"
        return self.transport.encode_controller_message(
            request_id,
            {
                "protocol_version": self.configuration.protocol_version,
                "type": "position",
                "fen": self.current_fen,
                "uci_moves": list(self.history),
                "side_to_move": self.side_to_move.value,
                "clocks_ms": {
                    "white": self.clock.white_remaining_ms,
                    "black": self.clock.black_remaining_ms,
                },
            },
        )

    def receive_ack(
        self,
        *,
        side: MatchSideColor,
        binding: MatchExecutionBinding,
        message: AIKMessage,
    ) -> None:
        if self.state != MatchState.RUNNING:
            raise InvalidMatchTransitionError("MATCH_NOT_RUNNING")
        if binding != self.bindings.get(side):
            raise StaleExecutionReportError("EXECUTION_BINDING_STALE_OR_MISMATCHED")
        kind = self._awaiting_ack_kind
        if kind == "position" and side != self.side_to_move:
            raise MatchControllerError("WRONG_SIDE_TO_ACKNOWLEDGE")
        if self._awaiting_ack_id is None or message.message_id != self._awaiting_ack_id:
            raise MatchControllerError("AIK_ACK_ID_MISMATCH")
        self.transport.clear_deadline()
        try:
            parsed = self.transport.parse_stdout(message)
        except AIKProtocolError as exc:
            self._abort(MatchFailure.PROTOCOL_ERROR, str(exc))
            raise MatchControllerError("AIK_ACK_INVALID") from exc
        expected_response = "ready" if kind == "hello" else "ack"
        if parsed["type"] != expected_response:
            self._abort(MatchFailure.PROTOCOL_ERROR, "AIK_ACK_TYPE_INVALID")
            raise MatchControllerError("AIK_ACK_INVALID")
        self._awaiting_ack_id = None
        self._awaiting_ack_kind = None
        if kind == "hello":
            self.hello_ready.add(side)
        elif kind == "new_game":
            self.new_game_acked.add(side)
        elif kind == "position":
            self._position_acked = True
        else:
            raise MatchControllerError("AIK_ACK_STATE_INVALID")

    def issue_move_request(self, *, side: MatchSideColor, request_id: str) -> AIKMessage:
        if self.state != MatchState.RUNNING:
            raise InvalidMatchTransitionError("MATCH_NOT_RUNNING")
        if side != self.side_to_move or self.clock.side_to_move != side:
            raise MatchControllerError("WRONG_SIDE_TO_MOVE")
        if not self._position_acked:
            raise MatchControllerError("POSITION_ACK_REQUIRED")
        if self.clock.turn_started_monotonic is None:
            self.clock = self.clock.start_turn(self.monotonic(), self.configuration.move_timeout_ms)
        if self._request_id is not None:
            raise MatchControllerError("MOVE_REQUEST_ALREADY_ACTIVE")
        self._reserve_request_id(request_id)
        self._request_id = request_id
        self._position_acked = False
        now = self.monotonic()
        remaining = self.clock.remaining(side)
        budget = max(0, int((self.clock.move_deadline_monotonic - now) * 1000))
        self.transport.set_deadline(self.clock.move_deadline_monotonic)
        self._event(
            "MOVE_REQUESTED",
            "controller",
            {
                "sequence": self.current_ply + 1,
                "side": side.value,
                "request_id": request_id,
            },
        )
        return self.transport.encode_controller_message(
            request_id,
            {
                "protocol_version": self.configuration.protocol_version,
                "type": "go",
                "move_budget_ms": min(budget, remaining),
                "clocks_ms": {
                    "white": self.clock.white_remaining_ms,
                    "black": self.clock.black_remaining_ms,
                },
            },
        )

    def expire_move_deadline(self) -> ProposalDecision | None:
        """Let the trusted supervisor expire a silent turn without participant input."""
        deadline = self.clock.move_deadline_monotonic
        if (
            self.state == MatchState.RUNNING
            and self._request_id is not None
            and deadline is not None
            and self.monotonic() > deadline
        ):
            return self._abort(MatchFailure.MOVE_TIMEOUT, "MOVE_DEADLINE_EXCEEDED")
        return None

    def receive_proposal(
        self,
        *,
        side: MatchSideColor,
        binding: MatchExecutionBinding,
        sequence: int,
        message: AIKMessage,
    ) -> ProposalDecision:
        if self.state != MatchState.RUNNING:
            raise InvalidMatchTransitionError("MATCH_NOT_RUNNING")
        if binding != self.bindings.get(side):
            raise StaleExecutionReportError("EXECUTION_BINDING_STALE_OR_MISMATCHED")
        if side != self.side_to_move:
            return self._abort(MatchFailure.ILLEGAL_MOVE, "WRONG_SIDE")
        if sequence <= self.current_ply:
            raise DuplicateMoveError("MOVE_SEQUENCE_ALREADY_ACCEPTED")
        if sequence != self.current_ply + 1:
            raise OutOfOrderMoveError("MOVE_SEQUENCE_GAP")
        if self._request_id is None:
            raise MatchControllerError("MOVE_REQUEST_NOT_ISSUED")
        now = self.monotonic()
        deadline = self.clock.move_deadline_monotonic
        if deadline is None or now > deadline:
            return self._abort(MatchFailure.MOVE_TIMEOUT, "MOVE_DEADLINE_EXCEEDED")
        if message.message_id != self._request_id:
            return self._abort(MatchFailure.PROTOCOL_ERROR, "REQUEST_ID_MISMATCH")
        try:
            parsed = self.transport.parse_stdout(message)
        except AIKProtocolError as exc:
            return self._abort(MatchFailure.PROTOCOL_ERROR, str(exc))
        if parsed["type"] == "claim_draw":
            if self._claim_attempted:
                return self._abort(MatchFailure.PROTOCOL_ERROR, "REPEATED_DRAW_CLAIM")
            self._claim_attempted = True
            claimed = self.rules.verify_draw_claim(
                self.current_fen, self.history, parsed["reason"], parsed["intended_move"]
            )
            if claimed:
                self.result = MatchResult(MatchOutcome.DRAW, parsed["reason"].upper())
                self.state = MatchState.COMPLETED
                self.completed_at = self.wall_clock()
                self._request_id = None
                self.transport.clear_deadline()
                self._event(
                    "MATCH_COMPLETED",
                    "controller",
                    {
                        "outcome": self.result.outcome.value,
                        "reason": self.result.reason.value,
                    },
                )
                return ProposalDecision(True, result=self.result)
            self._event("MOVE_REJECTED", "controller", {"reason": "DRAW_CLAIM_UNAVAILABLE"})
            response = self.transport.encode_controller_message(
                self._request_id,
                {
                    "protocol_version": self.configuration.protocol_version,
                    "type": "claim_rejected",
                    "reason": "claim_not_available",
                },
            )
            return ProposalDecision(False, server_response=response)
        elapsed = self.clock.elapsed_ms(now)
        if elapsed > self.clock.remaining(side):
            return self._abort(MatchFailure.MOVE_TIMEOUT, "CHESS_CLOCK_EXHAUSTED")
        move = parsed["move"]
        before = self.current_fen
        after = self.rules.apply_legal_move(before, move, self.history)
        if after is None:
            return self._abort(MatchFailure.ILLEGAL_MOVE, "ILLEGAL_MOVE")
        next_side = MatchSideColor.BLACK if side == MatchSideColor.WHITE else MatchSideColor.WHITE
        next_history = (*self.history, move)
        side_reader = getattr(self.rules, "side_to_move", None)
        if side_reader is not None:
            actual_next_side = MatchSideColor(side_reader(after, next_history))
            if actual_next_side != next_side:
                return self._abort(MatchFailure.SANDBOX_FAILURE, "RULES_SIDE_TO_MOVE_INCONSISTENT")
            next_side = actual_next_side
        move_record = MatchMove(
            match_id=self.match_id,
            sequence=sequence,
            side_id=self.sides[side].side_id,
            color=side,
            move_uci=move,
            position_before_sha256=position_identity(before),
            position_after_sha256=position_identity(after),
            position_before_fen=before,
            position_after_fen=after,
            elapsed_ms=elapsed,
            recorded_at=self.wall_clock(),
            execution_binding=binding,
            request_id=self._request_id,
        )
        self.moves.append(move_record)
        self.current_ply = sequence
        self.current_fen = after
        self.side_to_move = next_side
        self.history = next_history
        self.clock = self.clock.after_move(elapsed, next_side)
        result = self.rules.terminal_result(after, self.history)
        self._request_id = None
        self.transport.clear_deadline()
        self._event(
            "MOVE_ACCEPTED",
            "controller",
            {
                "sequence": sequence,
                "side": side.value,
                "move": move,
                "position_after_sha256": move_record.position_after_sha256,
            },
        )
        if result is not None:
            self.result = MatchResult(*result)
            self.state = MatchState.COMPLETED
            self.completed_at = self.wall_clock()
            self._event(
                "MATCH_COMPLETED",
                "controller",
                {
                    "outcome": self.result.outcome.value,
                    "reason": self.result.reason.value,
                },
            )
            return ProposalDecision(True, move=move_record, result=self.result)
        return ProposalDecision(True, move=move_record)

    def report_failure(
        self, *, side: MatchSideColor, binding: MatchExecutionBinding, failure: MatchFailure
    ) -> ProposalDecision:
        if binding != self.bindings.get(side):
            raise StaleExecutionReportError("EXECUTION_BINDING_STALE_OR_MISMATCHED")
        if self.state not in {MatchState.STARTING, MatchState.RUNNING}:
            if self.state == MatchState.ABORTED and self.failure == failure:
                return ProposalDecision(False, failure=failure)
            raise InvalidMatchTransitionError("MATCH_FAILURE_NOT_ALLOWED")
        return self._abort(failure, failure.value)

    def abort_for_controller_restart(self) -> ProposalDecision:
        if self.state not in {MatchState.STARTING, MatchState.RUNNING}:
            raise InvalidMatchTransitionError("MATCH_NOT_RECOVERABLE_AS_ACTIVE")
        return self._abort(
            MatchFailure.CONTROLLER_RESTART, "ACTIVE_CLOCK_CANNOT_RESUME_MONOTONICALLY"
        )

    def _abort(self, failure: MatchFailure, reason: str) -> ProposalDecision:
        self.state = MatchState.ABORTED
        self.failure = failure
        self.result = None
        self.completed_at = self.wall_clock()
        self._request_id = None
        self._awaiting_ack_id = None
        self._awaiting_ack_kind = None
        self._position_acked = False
        self.transport.clear_deadline()
        self._event("MATCH_ABORTED", "controller", {"failure": failure.value, "reason": reason})
        return ProposalDecision(False, failure=failure)

    def _event(self, event_type: str, actor_kind: str, metadata: dict) -> None:
        event_kind = MatchEventType(event_type)
        self.events.append(
            MatchEvent(
                event_type=event_kind,
                match_id=self.match_id,
                sequence=len(self.events) + 1,
                recorded_at=self.wall_clock(),
                actor_kind=actor_kind,
                actor_id="match-controller",
                metadata=metadata,
            )
        )

    def _assert_turn(self, side: MatchSideColor) -> None:
        if self.state != MatchState.RUNNING:
            raise InvalidMatchTransitionError("MATCH_NOT_RUNNING")
        if side != self.side_to_move:
            raise MatchControllerError("WRONG_SIDE_TO_MOVE")

    def _reserve_request_id(self, request_id: str) -> None:
        if not request_id or request_id in self._used_request_ids:
            raise MatchControllerError("REQUEST_ID_REUSED_OR_INVALID")
        self._used_request_ids.add(request_id)


class FakeRulesAdapter:
    """Controlled transition table for tests; it is not a chess rules engine."""

    def __init__(
        self,
        transitions: dict[tuple[str, str], str],
        *,
        terminal_positions: dict[str, tuple[MatchOutcome, str]] | None = None,
        valid_draw_claims: set[tuple[str, str]] | None = None,
    ) -> None:
        self.transitions = dict(transitions)
        self.terminal_positions = dict(terminal_positions or {})
        self.valid_draw_claims = set(valid_draw_claims or set())

    def apply_legal_move(
        self, position: str, move_uci: str, history: tuple[str, ...]
    ) -> str | None:
        del history
        return self.transitions.get((position, move_uci))

    def terminal_result(
        self, position: str, history: tuple[str, ...]
    ) -> tuple[MatchOutcome, str] | None:
        del history
        return self.terminal_positions.get(position)

    def verify_draw_claim(
        self, position: str, history: tuple[str, ...], reason: str, intended_move: str | None
    ) -> bool:
        del history, intended_move
        return (position, reason) in self.valid_draw_claims


def reconstruct_position(
    initial_fen: str,
    moves: list[MatchMove],
    rules: RulesAdapter,
    *,
    sides: dict[MatchSideColor, MatchSideIdentity] | None = None,
    bindings: dict[uuid.UUID, MatchExecutionBinding] | None = None,
) -> tuple[str, tuple[str, ...]]:
    adapter_initial = getattr(rules, "initial_position", initial_fen)
    if initial_fen != adapter_initial:
        raise ValueError("MATCH_INITIAL_POSITION_NOT_CANONICAL")
    position = initial_fen
    history: tuple[str, ...] = ()
    side_reader = getattr(rules, "side_to_move", None)
    expected_color = (
        MatchSideColor(side_reader(position, history))
        if side_reader is not None
        else MatchSideColor.WHITE
    )
    seen_requests: set[str] = set()
    for expected_sequence, move in enumerate(moves, start=1):
        if (
            move.sequence != expected_sequence
            or move.color != expected_color
            or move.position_before_fen != position
            or move.position_before_sha256 != position_identity(position)
            or not move.request_id
            or move.request_id in seen_requests
        ):
            raise ValueError("MATCH_TRANSCRIPT_SEQUENCE_OR_POSITION_INVALID")
        if sides is not None or bindings is not None:
            side = sides.get(move.color) if sides is not None else None
            binding = (
                bindings.get(move.execution_binding.binding_id) if bindings is not None else None
            )
            if (
                side is None
                or binding is None
                or move.match_id != side.match_id
                or move.side_id != side.side_id
                or binding != move.execution_binding
                or not binding.matches_side(side)
            ):
                raise ValueError("MATCH_TRANSCRIPT_BINDING_MISMATCH")
        next_position = rules.apply_legal_move(position, move.move_uci, history)
        if (
            next_position is None
            or next_position != move.position_after_fen
            or position_identity(next_position) != move.position_after_sha256
        ):
            raise ValueError("MATCH_TRANSCRIPT_REPLAY_MISMATCH")
        position = next_position
        history = (*history, move.move_uci)
        expected_color = (
            MatchSideColor.BLACK if expected_color == MatchSideColor.WHITE else MatchSideColor.WHITE
        )
        seen_requests.add(move.request_id)
    return position, history


def rehydrate_controller(
    *,
    match_id: uuid.UUID,
    state: MatchState,
    sides: dict[MatchSideColor, MatchSideIdentity],
    current_bindings: dict[MatchSideColor, MatchExecutionBinding],
    historical_bindings: dict[uuid.UUID, MatchExecutionBinding],
    configuration: MatchConfiguration,
    accepted_moves: list[MatchMove],
    persisted_fen: str,
    persisted_ply: int,
    persisted_side_to_move: MatchSideColor,
    clock: MatchClock,
    rules: RulesAdapter,
    transport: AIKTransport,
    monotonic,
    result: MatchResult | None = None,
    failure: MatchFailure | None = None,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    wall_clock=lambda: datetime.now(timezone.utc),
) -> MatchController:
    """Rebuild only a queued or terminal controller from verified durable evidence.

    Active matches are not resumed: a process restart destroys the monotonic clock
    deadline and must be recorded through record_controller_restart().
    """
    if state in {MatchState.STARTING, MatchState.RUNNING}:
        raise MatchControllerError("ACTIVE_MATCH_RESTART_REQUIRES_ABORT")
    if clock.turn_started_monotonic is not None or clock.move_deadline_monotonic is not None:
        raise MatchControllerError("PERSISTED_MONOTONIC_DEADLINE_MUST_NOT_BE_RESTORED")
    try:
        position, history = reconstruct_position(
            configuration.initial_fen,
            accepted_moves,
            rules,
            sides=sides,
            bindings=historical_bindings,
        )
    except (ValueError, MatchControllerError) as exc:
        raise MatchTranscriptCorruptedError(str(exc)) from exc
    if position != persisted_fen or len(history) != persisted_ply:
        raise MatchTranscriptCorruptedError("MATCH_PERSISTED_SNAPSHOT_REPLAY_MISMATCH")
    expected_side = rules.side_to_move(position, history)
    if expected_side != persisted_side_to_move.value:
        raise MatchTranscriptCorruptedError("MATCH_PERSISTED_SIDE_TO_MOVE_MISMATCH")
    if state == MatchState.COMPLETED:
        if result is None or failure is not None:
            raise MatchTranscriptCorruptedError("MATCH_TERMINAL_RESULT_EVIDENCE_INVALID")
        terminal = rules.terminal_result(position, history)
        reason = result.reason.value
        if terminal != (result.outcome, reason):
            claim_reason = {
                MatchResultReason.THREEFOLD_REPETITION: "threefold_repetition",
                MatchResultReason.FIFTY_MOVE_RULE: "fifty_move_rule",
            }.get(result.reason)
            claimed = claim_reason is not None and rules.verify_draw_claim(
                position, history, claim_reason, None
            )
            if not claimed or result.outcome != MatchOutcome.DRAW:
                raise MatchTranscriptCorruptedError("MATCH_TERMINAL_RESULT_REPLAY_MISMATCH")
    elif state == MatchState.ABORTED:
        if failure is None or result is not None:
            raise MatchTranscriptCorruptedError("MATCH_ABORT_EVIDENCE_INVALID")
    elif state == MatchState.QUEUED:
        if result is not None or failure is not None or accepted_moves:
            raise MatchTranscriptCorruptedError("MATCH_QUEUED_EVIDENCE_INVALID")
    else:
        raise ValueError("MATCH_REHYDRATION_STATE_UNSUPPORTED")

    controller = MatchController(
        match_id=match_id,
        state=state,
        sides=sides,
        bindings=current_bindings,
        configuration=configuration,
        current_fen=position,
        current_ply=persisted_ply,
        side_to_move=persisted_side_to_move,
        clock=clock,
        rules=rules,
        transport=transport,
        monotonic=monotonic,
        wall_clock=wall_clock,
        history=history,
    )
    controller.moves = list(accepted_moves)
    controller.result = result
    controller.failure = failure
    controller.started_at = started_at
    controller.completed_at = completed_at
    controller.persisted_ply = persisted_ply
    controller.persisted_state = state
    controller.persisted_move_count = len(accepted_moves)
    return controller
