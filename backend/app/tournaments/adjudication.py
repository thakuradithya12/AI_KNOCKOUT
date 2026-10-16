"""Authoritative match adjudication rules and scoring policies.

Defines explicit treatment for:
- Standard chess outcomes (Win=1, Draw=0.5, Loss=0)
- Participant faults: Timeouts, illegal moves, process crashes, resource exceeded, protocol errors
- Forfeits and disqualifications
- Infrastructure failures: Worker crashes, sandbox failures, controller restarts (void / no contest)
"""

from __future__ import annotations

import enum
from dataclasses import dataclass
from typing import Any, Optional

from app.models.match import MatchFailure, MatchOutcome, MatchState


class AdjudicationCategory(str, enum.Enum):
    STANDARD = "STANDARD"
    PARTICIPANT_FAULT = "PARTICIPANT_FAULT"
    FORFEIT = "FORFEIT"
    DISQUALIFICATION = "DISQUALIFICATION"
    INFRASTRUCTURE_FAILURE = "INFRASTRUCTURE_FAILURE"


# Failures attributable to the participant engine currently on turn
PARTICIPANT_GAMEPLAY_FAULTS = {
    MatchFailure.MOVE_TIMEOUT,
    MatchFailure.ILLEGAL_MOVE,
}

PARTICIPANT_RUNTIME_FAULTS = {
    MatchFailure.PROCESS_CRASH,
    MatchFailure.RESOURCE_EXCEEDED,
    MatchFailure.PROTOCOL_ERROR,
}

# Platform or cluster faults NOT attributable to participant code
INFRASTRUCTURE_FAULTS = {
    MatchFailure.WORKER_FAILURE,
    MatchFailure.SANDBOX_FAILURE,
    MatchFailure.CLEANUP_FAILURE,
    MatchFailure.CONTROLLER_RESTART,
    MatchFailure.STALE_FENCING,
    MatchFailure.TRANSCRIPT_CORRUPTED,
    MatchFailure.EXECUTION_TIMEOUT,
}


@dataclass(frozen=True)
class MatchAdjudication:
    category: AdjudicationCategory
    result_outcome: Optional[str]  # "WHITE_WIN", "BLACK_WIN", "DRAW", or None
    result_reason: Optional[str]
    white_points: float
    black_points: float
    winner_participant_id: Optional[str]
    loser_participant_id: Optional[str]
    faulted_side: Optional[str]  # "WHITE", "BLACK", or None
    is_infrastructure_failure: bool
    is_replayable: bool
    summary: str


def adjudicate_match_record(
    match: dict[str, Any],
) -> MatchAdjudication:
    """Explicitly adjudicate an official match based on its recorded state, outcome, and failure reason.
    
    Adjudication Rules:
    1. COMPLETED matches:
       - WHITE_WIN: White +1.0, Black +0.0
       - BLACK_WIN: Black +1.0, White +0.0
       - DRAW: White +0.5, Black +0.5
    2. ABORTED matches caused by Participant Fault:
       - If White faulted (illegal move, move timeout, process crash, quota exceeded):
         Black is awarded 1.0 point (adjudicated win), White receives 0.0 points.
       - If Black faulted (illegal move, move timeout, process crash, quota exceeded):
         White is awarded 1.0 point (adjudicated win), Black receives 0.0 points.
    3. ABORTED matches caused by Infrastructure Failure:
       - Worker crashes, sandbox failures, controller restarts:
         Neither participant is penalized or granted an unearned win.
         Both receive 0.0 points (NO CONTEST), match is eligible for replay.
    """
    state = match.get("state")
    outcome = match.get("result_outcome")
    reason = match.get("result_reason")
    failure = match.get("failure_reason")
    side_to_move = match.get("side_to_move")
    white_id = match.get("white_participant_id")
    black_id = match.get("black_participant_id")

    # Handle standard completed matches
    if state == "COMPLETED" or outcome in {"WHITE_WIN", "BLACK_WIN", "DRAW"}:
        if outcome == "WHITE_WIN":
            return MatchAdjudication(
                category=AdjudicationCategory.STANDARD,
                result_outcome="WHITE_WIN",
                result_reason=reason or "CHECKMATE",
                white_points=1.0,
                black_points=0.0,
                winner_participant_id=white_id,
                loser_participant_id=black_id,
                faulted_side=None,
                is_infrastructure_failure=False,
                is_replayable=False,
                summary=f"White won by {reason or 'checkmate'}",
            )
        elif outcome == "BLACK_WIN":
            return MatchAdjudication(
                category=AdjudicationCategory.STANDARD,
                result_outcome="BLACK_WIN",
                result_reason=reason or "CHECKMATE",
                white_points=0.0,
                black_points=1.0,
                winner_participant_id=black_id,
                loser_participant_id=white_id,
                faulted_side=None,
                is_infrastructure_failure=False,
                is_replayable=False,
                summary=f"Black won by {reason or 'checkmate'}",
            )
        elif outcome == "DRAW":
            return MatchAdjudication(
                category=AdjudicationCategory.STANDARD,
                result_outcome="DRAW",
                result_reason=reason or "STALEMATE",
                white_points=0.5,
                black_points=0.5,
                winner_participant_id=None,
                loser_participant_id=None,
                faulted_side=None,
                is_infrastructure_failure=False,
                is_replayable=False,
                summary=f"Match drawn by {reason or 'stalemate'}",
            )

    # Convert failure string to MatchFailure enum if possible
    fail_enum = None
    if failure:
        if isinstance(failure, MatchFailure):
            fail_enum = failure
        else:
            try:
                fail_enum = MatchFailure(str(failure))
            except ValueError:
                fail_enum = None

    # Handle participant faults (timeout, illegal move, crash, resource exceeded)
    if fail_enum in PARTICIPANT_GAMEPLAY_FAULTS or fail_enum in PARTICIPANT_RUNTIME_FAULTS:
        # The participant whose turn it was when failure occurred is the faulted side
        faulted = "WHITE" if side_to_move == "WHITE" else "BLACK"
        if faulted == "WHITE":
            return MatchAdjudication(
                category=AdjudicationCategory.PARTICIPANT_FAULT,
                result_outcome="BLACK_WIN",
                result_reason=f"FORFEIT_{fail_enum.value}",
                white_points=0.0,
                black_points=1.0,
                winner_participant_id=black_id,
                loser_participant_id=white_id,
                faulted_side="WHITE",
                is_infrastructure_failure=False,
                is_replayable=False,
                summary=f"White forfeited due to participant fault: {fail_enum.value}",
            )
        else:
            return MatchAdjudication(
                category=AdjudicationCategory.PARTICIPANT_FAULT,
                result_outcome="WHITE_WIN",
                result_reason=f"FORFEIT_{fail_enum.value}",
                white_points=1.0,
                black_points=0.0,
                winner_participant_id=white_id,
                loser_participant_id=black_id,
                faulted_side="BLACK",
                is_infrastructure_failure=False,
                is_replayable=False,
                summary=f"Black forfeited due to participant fault: {fail_enum.value}",
            )

    # Handle infrastructure failures
    if fail_enum in INFRASTRUCTURE_FAULTS or failure in {f.value for f in INFRASTRUCTURE_FAULTS}:
        return MatchAdjudication(
            category=AdjudicationCategory.INFRASTRUCTURE_FAILURE,
            result_outcome=None,
            result_reason=f"INFRASTRUCTURE_{fail_enum.value if fail_enum else failure}",
            white_points=0.0,
            black_points=0.0,
            winner_participant_id=None,
            loser_participant_id=None,
            faulted_side=None,
            is_infrastructure_failure=True,
            is_replayable=True,
            summary=f"Match void due to infrastructure failure: {failure}. Replay permitted.",
        )

    # Uncompleted / pending / unknown state
    return MatchAdjudication(
        category=AdjudicationCategory.STANDARD,
        result_outcome=None,
        result_reason=None,
        white_points=0.0,
        black_points=0.0,
        winner_participant_id=None,
        loser_participant_id=None,
        faulted_side=None,
        is_infrastructure_failure=False,
        is_replayable=False,
        summary="Match in progress or pending",
    )
