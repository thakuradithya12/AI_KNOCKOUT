import uuid
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from app.matches.chess import ChessRulesAdapter
from app.matches.controller import (
    MatchClock,
    MatchConfiguration,
    MatchControllerError,
    MatchExecutionBinding,
    MatchMove,
    MatchResult,
    MatchSideIdentity,
    position_identity,
    rehydrate_controller,
)
from app.models.match import MatchFailure, MatchOutcome, MatchSideColor, MatchState
from app.sandbox.contract import AIKTransport

START_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


def adapter(fen=START_FEN):
    return ChessRulesAdapter(fen)


def test_adapter_rejects_an_unpinned_rules_configuration():
    with pytest.raises(RuntimeError, match="CHESS_RULES_CONFIGURATION_VERSION_MISMATCH"):
        ChessRulesAdapter(START_FEN, rules_version="unversioned")


def apply_all(rules, moves):
    position = rules.initial_position
    history = ()
    for move in moves:
        position = rules.apply_legal_move(position, move, history)
        assert position is not None
        history += (move,)
    return position, history


def test_legal_move_and_wrong_turn_or_illegal_move():
    rules = adapter()
    assert rules.apply_legal_move(rules.initial_position, "e2e4", ()) is not None
    assert rules.apply_legal_move(rules.initial_position, "e7e5", ()) is None
    assert rules.apply_legal_move(rules.initial_position, "e2e5", ()) is None


def test_check_detection():
    rules = adapter("4k3/8/8/8/8/8/4R3/4K3 b - - 0 1")
    assert rules.is_check(rules.initial_position)
    assert not adapter().is_check(START_FEN)


def test_checkmate_and_terminal_result():
    rules = adapter("r1bqkb1r/pppp1Qpp/2n2n2/4p3/2B1P3/8/PPPP1PPP/RNB1K1NR b KQkq - 0 4")
    assert rules.is_checkmate(rules.initial_position)
    assert rules.terminal_result(rules.initial_position, ()) == (
        MatchOutcome.WHITE_WIN,
        "CHECKMATE",
    )


def test_stalemate():
    rules = adapter("7k/5Q2/6K1/8/8/8/8/8 b - - 0 1")
    assert rules.is_stalemate(rules.initial_position)
    assert rules.terminal_result(rules.initial_position, ()) == (MatchOutcome.DRAW, "STALEMATE")


def test_castling():
    rules = adapter("r3k2r/8/8/8/8/8/8/R3K2R w KQkq - 0 1")
    result = rules.apply_legal_move(rules.initial_position, "e1g1", ())
    assert result is not None
    assert "R4RK1" in result


def test_en_passant():
    rules = adapter()
    result, history = apply_all(rules, ("e2e4", "a7a6", "e4e5", "d7d5"))
    assert " d6 " in result
    captured = rules.apply_legal_move(result, "e5d6", history)
    assert captured is not None and "p2P4" in captured


def test_promotion():
    rules = adapter("7k/P7/8/8/8/8/8/7K w - - 0 1")
    result = rules.apply_legal_move(rules.initial_position, "a7a8q", ())
    assert result is not None and result.startswith("Q6k/")


def test_threefold_repetition_claim_and_automatic_fivefold_terminal():
    rules = adapter()
    cycle = ("g1f3", "g8f6", "f3g1", "f6g8")
    position, history = apply_all(rules, cycle * 2)
    assert rules.verify_draw_claim(position, history, "threefold_repetition", None)
    position, history = apply_all(rules, cycle * 4)
    assert rules.terminal_result(position, history) == (
        MatchOutcome.DRAW,
        "FIVEFOLD_REPETITION",
    )


def test_fifty_move_claim_and_75_move_terminal():
    claim = adapter("4k3/8/8/8/8/8/8/R3K3 w Q - 100 51")
    assert claim.verify_draw_claim(claim.initial_position, (), "fifty_move_rule", None)
    automatic = adapter("4k3/8/8/8/8/8/8/R3K3 w Q - 150 76")
    assert automatic.terminal_result(automatic.initial_position, ()) == (
        MatchOutcome.DRAW,
        "SEVENTY_FIVE_MOVE_RULE",
    )


def test_insufficient_material():
    rules = adapter("4k3/8/8/8/8/8/8/4K3 w - - 0 1")
    assert rules.is_insufficient_material(rules.initial_position)
    assert rules.terminal_result(rules.initial_position, ()) == (
        MatchOutcome.DRAW,
        "DEAD_POSITION",
    )


def test_initial_fen_controls_side_to_move():
    rules = adapter("4k3/8/8/8/8/8/8/4K3 b - - 0 1")
    assert rules.side_to_move(rules.initial_position) == "BLACK"


def test_position_serialization_and_reconstruction():
    rules = adapter()
    position, history = apply_all(rules, ("e2e4", "c7c5", "g1f3"))
    encoded = rules.serialize_position(position, history)
    assert rules.deserialize_position(encoded, history) == encoded
    assert rules.side_to_move(position, history) == "BLACK"


def recovery_fixture(moves_uci=()):
    match_id = uuid.uuid4()
    white = MatchSideIdentity(uuid.uuid4(), match_id, MatchSideColor.WHITE, "white", uuid.uuid4(),
                              uuid.uuid4(), "a" * 64, "aik-jsonl/1", "runtime-v1")
    black = MatchSideIdentity(uuid.uuid4(), match_id, MatchSideColor.BLACK, "black", uuid.uuid4(),
                              uuid.uuid4(), "b" * 64, "aik-jsonl/1", "runtime-v1")
    sides = {MatchSideColor.WHITE: white, MatchSideColor.BLACK: black}
    bindings = {
        color: MatchExecutionBinding(uuid.uuid4(), match_id, side.side_id, uuid.uuid4(),
                                     side.submission_version_id, side.artifact_digest,
                                     f"worker-{color.value}", uuid.uuid4(), 1, 1)
        for color, side in sides.items()
    }
    rules = adapter()
    config = MatchConfiguration(
        "aik-jsonl/1", "runtime-v1", "policy-v1", rules.rules_version,
        START_FEN, 60_000, 0, 5_000, "c" * 64,
    )
    position = rules.initial_position
    history = ()
    records = []
    color = MatchSideColor.WHITE
    for sequence, uci in enumerate(moves_uci, 1):
        after = rules.apply_legal_move(position, uci, history)
        assert after is not None
        records.append(MatchMove(match_id, sequence, sides[color].side_id, color, uci,
                                 position_identity(position), position_identity(after), position,
                                 after, 0, datetime(2026, 1, 1, tzinfo=timezone.utc),
                                 bindings[color], f"request-{sequence}"))
        position, history = after, (*history, uci)
        color = MatchSideColor.BLACK if color == MatchSideColor.WHITE else MatchSideColor.WHITE
    return match_id, sides, bindings, config, rules, position, history, records, color


def hydrate_fixture(fixture, *, moves=None, state=MatchState.ABORTED, result=None, failure=None):
    match_id, sides, bindings, config, rules, position, history, records, color = fixture
    moves = fixture[7] if moves is None else moves
    if state == MatchState.ABORTED and failure is None:
        failure = MatchFailure.PROCESS_CRASH
    clock = MatchClock(60_000, 60_000, 0,
                       rules.side_to_move(position, history) == "WHITE" and MatchSideColor.WHITE
                       or MatchSideColor.BLACK)
    return rehydrate_controller(
        match_id=match_id, state=state, sides=sides, current_bindings=bindings,
        historical_bindings={binding.binding_id: binding for binding in bindings.values()},
        configuration=config, accepted_moves=moves, persisted_fen=position,
        persisted_ply=len(moves), persisted_side_to_move=color, clock=clock, rules=rules,
        transport=AIKTransport(monotonic=lambda: 0.0), monotonic=lambda: 0.0,
        result=result, failure=failure,
    )


def test_rehydrate_empty_and_multi_move_transcripts():
    empty = recovery_fixture()
    empty = (*empty[:2], {}, *empty[3:])
    queued = hydrate_fixture(empty, state=MatchState.QUEUED, failure=None)
    assert queued.current_ply == 0 and queued.history == ()
    multi = recovery_fixture(("e2e4", "e7e5", "g1f3"))
    replayed = hydrate_fixture(multi)
    assert replayed.current_fen == multi[5]
    assert replayed.history == ("e2e4", "e7e5", "g1f3")
    assert replayed.side_to_move == MatchSideColor.BLACK


def test_rehydrate_terminal_replay_and_reject_post_terminal_move():
    game = recovery_fixture(("f2f3", "e7e5", "g2g4", "d8h4"))
    result = MatchResult(MatchOutcome.BLACK_WIN, "CHECKMATE")
    replayed = hydrate_fixture(game, state=MatchState.COMPLETED, result=result)
    assert replayed.result == result and replayed.state == MatchState.COMPLETED
    extra = replace(game[7][-1], sequence=5, color=MatchSideColor.WHITE,
                    side_id=game[1][MatchSideColor.WHITE].side_id, move_uci="e2e4",
                    execution_binding=game[2][MatchSideColor.WHITE], request_id="request-5",
                    position_before_fen=game[5], position_before_sha256=position_identity(game[5]))
    with pytest.raises(ValueError, match="MATCH_TRANSCRIPT_REPLAY_MISMATCH"):
        hydrate_fixture(game, moves=[*game[7], extra], state=MatchState.ABORTED,
                        failure=MatchFailure.PROCESS_CRASH)


def test_rehydrate_rejects_corruption_wrong_side_and_binding_mismatch():
    game = recovery_fixture(("e2e4", "e7e5"))
    with pytest.raises(ValueError, match="MATCH_TRANSCRIPT_REPLAY_MISMATCH"):
        hydrate_fixture(game, moves=[replace(game[7][0], move_uci="e2e5"), game[7][1]])
    with pytest.raises(ValueError, match="MATCH_TRANSCRIPT_REPLAY_MISMATCH"):
        hydrate_fixture(
            game,
            moves=[replace(game[7][0], position_after_sha256="0" * 64), game[7][1]],
        )
    with pytest.raises(ValueError, match="MATCH_TRANSCRIPT_SEQUENCE_OR_POSITION_INVALID"):
        hydrate_fixture(game, moves=[replace(game[7][0], color=MatchSideColor.BLACK), game[7][1]])
    altered_binding = replace(game[7][0].execution_binding, artifact_digest="d" * 64)
    with pytest.raises(ValueError, match="MATCH_TRANSCRIPT_BINDING_MISMATCH"):
        hydrate_fixture(game, moves=[replace(game[7][0], execution_binding=altered_binding),
                                     game[7][1]])


def test_rehydrate_never_resumes_active_monotonic_clock():
    game = recovery_fixture()
    with pytest.raises(MatchControllerError, match="ACTIVE_MATCH_RESTART_REQUIRES_ABORT"):
        hydrate_fixture(game, state=MatchState.RUNNING, failure=None)
