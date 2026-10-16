"""Synchronous tests for trusted result-source assignment and request contracts."""

import uuid
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.api.v1.admin import RecordOfficialMatchResultRequest
from app.matches.service import _apply_automated_result
from app.models.match import MatchResultSource
from app.tournaments.engine import compute_leaderboard_standings
from app.tournaments.schemas import TournamentParticipant


def test_automated_source_is_assigned_only_by_server_result_writer() -> None:
    match = SimpleNamespace()
    result = SimpleNamespace(
        outcome=SimpleNamespace(value="WHITE_WIN"),
        reason=SimpleNamespace(value="CHECKMATE"),
    )

    _apply_automated_result(match, result, "completion-time")

    assert match.result_outcome == "WHITE_WIN"
    assert match.result_reason == "CHECKMATE"
    assert match.result_source is MatchResultSource.AUTOMATED
    assert match.completed_at == "completion-time"


def test_manual_result_request_rejects_client_provenance_override() -> None:
    with pytest.raises(ValidationError):
        RecordOfficialMatchResultRequest.model_validate(
            {
                "result_outcome": "WHITE_WIN",
                "result_reason": "CHECKMATE",
                "result_source": "automated",
            }
        )


def test_result_source_does_not_change_leaderboard_scoring() -> None:
    participants = [
        TournamentParticipant(
            participant_id=participant_id,
            name=participant_id,
            submission_version_id=uuid.uuid4(),
            version=1,
            artifact_sha256="a" * 64,
        )
        for participant_id in ("white", "black")
    ]
    match = {
        "match_id": "match-1",
        "white_participant_id": "white",
        "black_participant_id": "black",
        "state": "COMPLETED",
        "result_outcome": "WHITE_WIN",
        "result_reason": "CHECKMATE",
    }

    without_source = compute_leaderboard_standings(participants, [match])
    with_source = compute_leaderboard_standings(
        participants, [{**match, "result_source": "manual"}]
    )

    assert [standing.model_dump() for standing in with_source] == [
        standing.model_dump() for standing in without_source
    ]
