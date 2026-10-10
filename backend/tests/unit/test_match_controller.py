from __future__ import annotations

import asyncio
import json
import uuid
from dataclasses import replace
from datetime import datetime, timezone

import pytest

from app.matches.controller import (
    DuplicateMoveError,
    FakeRulesAdapter,
    InvalidMatchTransitionError,
    MatchClock,
    MatchConfiguration,
    MatchController,
    MatchExecutionBinding,
    MatchResult,
    MatchSideIdentity,
    OutOfOrderMoveError,
    StaleExecutionReportError,
    position_identity,
    reconstruct_position,
)
from app.matches.coordinator import CoordinatorError, ExecutionAuthority, MatchCoordinator
from app.models.match import MatchFailure, MatchOutcome, MatchSideColor, MatchSideKind, MatchState
from app.sandbox.contract import AIKMessage, AIKProtocolError, AIKTransport


class FakeTime:
    def __init__(self):
        self.monotonic_value = 10.0
        self.wall_value = datetime(2026, 1, 1, tzinfo=timezone.utc)

    def monotonic(self):
        return self.monotonic_value

    def wall(self):
        return self.wall_value

    def advance(self, seconds):
        self.monotonic_value += seconds


def make_controller(
    *, transitions=None, terminal_positions=None, valid_draw_claims=None, clock_ms=10_000,
    use_real_rules=False,
):
    timer = FakeTime()
    match_id = uuid.uuid4()
    white_side = MatchSideIdentity(
        uuid.uuid4(),
        match_id,
        MatchSideColor.WHITE,
        "participant-white",
        uuid.uuid4(),
        uuid.uuid4(),
        "a" * 64,
        "aik-jsonl/1",
        "runtime-v1",
    )
    black_side = MatchSideIdentity(
        uuid.uuid4(),
        match_id,
        MatchSideColor.BLACK,
        "participant-black",
        uuid.uuid4(),
        uuid.uuid4(),
        "b" * 64,
        "aik-jsonl/1",
        "runtime-v1",
    )
    bindings = {
        MatchSideColor.WHITE: MatchExecutionBinding(
            uuid.uuid4(),
            match_id,
            white_side.side_id,
            uuid.uuid4(),
            white_side.submission_version_id,
            white_side.artifact_digest,
            "worker-white",
            uuid.uuid4(),
            1,
            1,
        ),
        MatchSideColor.BLACK: MatchExecutionBinding(
            uuid.uuid4(),
            match_id,
            black_side.side_id,
            uuid.uuid4(),
            black_side.submission_version_id,
            black_side.artifact_digest,
            "worker-black",
            uuid.uuid4(),
            1,
            1,
        ),
    }
    config = MatchConfiguration(
        "aik-jsonl/1",
        "runtime-v1",
        "policy-v1",
        "standard-chess/python-chess-1.11.2" if use_real_rules else "rules-v1",
        "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
        if use_real_rules else "initial-position",
        clock_ms,
        100,
        1_000,
        "c" * 64,
    )
    rules = None if use_real_rules else FakeRulesAdapter(
        transitions
        or {("initial-position", "e2e4"): "position-1", ("position-1", "e7e5"): "position-2"},
        terminal_positions=terminal_positions,
        valid_draw_claims=valid_draw_claims,
    )
    controller = MatchController(
        match_id=match_id,
        state=MatchState.STARTING,
        sides={MatchSideColor.WHITE: white_side, MatchSideColor.BLACK: black_side},
        bindings=bindings,
        configuration=config,
        current_fen=config.initial_fen,
        current_ply=0,
        side_to_move=MatchSideColor.WHITE,
        clock=MatchClock(clock_ms, clock_ms, config.increment_ms, MatchSideColor.WHITE),
        rules=rules,
        transport=AIKTransport(monotonic=timer.monotonic),
        monotonic=timer.monotonic,
        wall_clock=timer.wall,
    )
    controller.start(ready_execution_ids={b.execution_id for b in bindings.values()})
    return controller, timer


def proposal(request_id: str, move: str) -> AIKMessage:
    return AIKMessage(
        request_id,
        json.dumps(
            {
                "protocol_version": "aik-jsonl/1",
                "type": "bestmove",
                "request_id": request_id,
                "move": move,
            },
            separators=(",", ":"),
        ).encode()
        + b"\n",
    )


def test_server_coordinator_owns_controller_and_rejects_wrong_worker():
    async def run():
        controller, _ = make_controller()
        black = controller.sides[MatchSideColor.BLACK]
        controller.sides[MatchSideColor.BLACK] = replace(
            black,
            kind=MatchSideKind.HOUSE_AI,
            participant_id=None,
            submission_id=None,
            submission_version_id=None,
            house_ai_id="house-fixed",
            house_ai_version=1,
        )
        binding = controller.bindings[MatchSideColor.BLACK]
        controller.bindings[MatchSideColor.BLACK] = replace(
            binding,
            kind=MatchSideKind.HOUSE_AI,
            submission_version_id=None,
            house_ai_id="house-fixed",
            house_ai_version=1,
        )
        persisted = []

        async def persist(value):
            persisted.append(value)

        coordinator = MatchCoordinator(persist)
        coordinator.register(controller)
        authority = ExecutionAuthority(
            match_id=controller.match_id,
            side_id=controller.sides[MatchSideColor.WHITE].side_id,
            execution_id=controller.bindings[MatchSideColor.WHITE].execution_id,
            worker_id="wrong-worker",
            lease_id=controller.bindings[MatchSideColor.WHITE].lease_id,
            fence=1,
            attempt=1,
            side=MatchSideColor.WHITE,
        )
        with pytest.raises(CoordinatorError, match="EXECUTION_AUTHORITY_MISMATCH"):
            await coordinator.next_command(authority)

        good = replace(authority, worker_id="worker-white")
        command = await coordinator.next_command(good)
        assert command is not None and command.command_type == "HELLO"
        response = (
            '{"protocol_version":"aik-jsonl/1","type":"ready",'
            f'"request_id":"{command.command_id}"}}\n'
        ).encode()
        await coordinator.submit_response(good, command_id=command.command_id, payload=response)
        assert persisted == [controller]
        assert controller in [coordinator.controller_for_testing(controller.match_id)]
        assert controller.hello_ready == {MatchSideColor.WHITE}

    asyncio.run(run())


def ack(request_id: str, response_type: str = "ack") -> AIKMessage:
    return AIKMessage(
        request_id,
        json.dumps(
            {
                "protocol_version": "aik-jsonl/1",
                "type": response_type,
                "request_id": request_id,
            },
            separators=(",", ":"),
        ).encode()
        + b"\n",
    )


def issue(controller, color, request_id):
    binding = controller.bindings[color]
    if color not in controller.hello_ready:
        hello_id = f"hello-{color.value.lower()}"
        hello = controller.issue_hello_request(side=color, request_id=hello_id)
        assert json.loads(hello.payload)["match_id"] == str(controller.match_id)
        controller.receive_ack(side=color, binding=binding, message=ack(hello_id, "ready"))
    if color not in controller.new_game_acked:
        new_game_id = f"new-game-{color.value.lower()}"
        new_game = controller.issue_new_game_request(side=color, request_id=new_game_id)
        assert json.loads(new_game.payload)["seed"] == controller.configuration.match_seed
        controller.receive_ack(side=color, binding=binding, message=ack(new_game_id))
    position_id = f"position-{request_id}"
    position = controller.issue_position_request(side=color, request_id=position_id)
    position_payload = json.loads(position.payload)
    assert position_payload["fen"] == controller.current_fen
    assert position_payload["uci_moves"] == list(controller.history)
    controller.receive_ack(side=color, binding=binding, message=ack(position_id))
    return controller.issue_move_request(side=color, request_id=request_id)


def test_start_requires_both_exact_ready_execution_ids():
    controller, _ = make_controller()
    assert controller.state == MatchState.RUNNING
    with pytest.raises(InvalidMatchTransitionError, match="MATCH_NOT_STARTING"):
        controller.start(ready_execution_ids=set())


def test_accepted_move_is_server_authoritative_and_clocked():
    controller, timer = make_controller()
    issue(controller, MatchSideColor.WHITE, "request-1")
    timer.advance(0.125)
    decision = controller.receive_proposal(
        side=MatchSideColor.WHITE,
        binding=controller.bindings[MatchSideColor.WHITE],
        sequence=1,
        message=proposal("request-1", "e2e4"),
    )
    assert decision.accepted
    assert decision.move.position_before_sha256 == position_identity("initial-position")
    assert controller.current_fen == "position-1"
    assert controller.current_ply == 1
    assert controller.side_to_move == MatchSideColor.BLACK
    assert controller.clock.white_remaining_ms == 9_975
    assert controller.events[-1].event_type == "MOVE_ACCEPTED"


def test_production_controller_defaults_to_real_adapter_and_rejects_illegal_move():
    from app.matches.chess import ChessRulesAdapter

    controller, _ = make_controller(use_real_rules=True)
    assert isinstance(controller.rules, ChessRulesAdapter)
    issue(controller, MatchSideColor.WHITE, "request-real")
    rejected = controller.receive_proposal(
        side=MatchSideColor.WHITE,
        binding=controller.bindings[MatchSideColor.WHITE],
        sequence=1,
        message=proposal("request-real", "e2e5"),
    )
    assert not rejected.accepted
    assert controller.current_ply == 0
    assert controller.current_fen == controller.configuration.initial_fen
    assert controller.failure == MatchFailure.ILLEGAL_MOVE


def test_illegal_move_aborts_without_inventing_chess_result():
    controller, _ = make_controller()
    issue(controller, MatchSideColor.WHITE, "request-1")
    result = controller.receive_proposal(
        side=MatchSideColor.WHITE,
        binding=controller.bindings[MatchSideColor.WHITE],
        sequence=1,
        message=proposal("request-1", "a2a3"),
    )
    assert result.failure == MatchFailure.ILLEGAL_MOVE
    assert controller.state == MatchState.ABORTED
    assert controller.result is None


def test_wrong_side_and_stale_execution_reports_are_rejected():
    controller, _ = make_controller()
    with pytest.raises(StaleExecutionReportError):
        controller.receive_proposal(
            side=MatchSideColor.WHITE,
            binding=controller.bindings[MatchSideColor.BLACK],
            sequence=1,
            message=proposal("request-1", "e2e4"),
        )
    issue(controller, MatchSideColor.WHITE, "request-1")
    decision = controller.receive_proposal(
        side=MatchSideColor.BLACK,
        binding=controller.bindings[MatchSideColor.BLACK],
        sequence=1,
        message=proposal("request-1", "e2e4"),
    )
    assert decision.failure == MatchFailure.ILLEGAL_MOVE
    assert controller.state == MatchState.ABORTED


def test_duplicate_and_out_of_order_move_sequences_are_rejected():
    controller, _ = make_controller()
    issue(controller, MatchSideColor.WHITE, "request-1")
    with pytest.raises(OutOfOrderMoveError):
        controller.receive_proposal(
            side=MatchSideColor.WHITE,
            binding=controller.bindings[MatchSideColor.WHITE],
            sequence=2,
            message=proposal("request-1", "e2e4"),
        )
    controller.receive_proposal(
        side=MatchSideColor.WHITE,
        binding=controller.bindings[MatchSideColor.WHITE],
        sequence=1,
        message=proposal("request-1", "e2e4"),
    )
    with pytest.raises(DuplicateMoveError):
        controller.receive_proposal(
            side=MatchSideColor.BLACK,
            binding=controller.bindings[MatchSideColor.BLACK],
            sequence=1,
            message=proposal("request-1", "e7e5"),
        )


def test_late_and_malformed_messages_fail_closed():
    controller, timer = make_controller()
    issue(controller, MatchSideColor.WHITE, "request-1")
    timer.advance(1.001)
    expired = controller.expire_move_deadline()
    assert expired is not None and expired.failure == MatchFailure.MOVE_TIMEOUT

    controller, timer = make_controller()
    issue(controller, MatchSideColor.WHITE, "request-1")
    timer.advance(1.001)
    late = controller.receive_proposal(
        side=MatchSideColor.WHITE,
        binding=controller.bindings[MatchSideColor.WHITE],
        sequence=1,
        message=proposal("request-1", "e2e4"),
    )
    assert late.failure == MatchFailure.MOVE_TIMEOUT

    controller, _ = make_controller()
    issue(controller, MatchSideColor.WHITE, "request-1")
    malformed = AIKMessage("request-1", b"not-json\n")
    failed = controller.receive_proposal(
        side=MatchSideColor.WHITE,
        binding=controller.bindings[MatchSideColor.WHITE],
        sequence=1,
        message=malformed,
    )
    assert failed.failure == MatchFailure.PROTOCOL_ERROR


def test_duplicate_response_terminal_state_and_participant_result_are_rejected():
    controller, _ = make_controller(
        terminal_positions={"position-1": (MatchOutcome.WHITE_WIN, "CHECKMATE")}
    )
    issue(controller, MatchSideColor.WHITE, "request-1")
    accepted = controller.receive_proposal(
        side=MatchSideColor.WHITE,
        binding=controller.bindings[MatchSideColor.WHITE],
        sequence=1,
        message=proposal("request-1", "e2e4"),
    )
    assert accepted.result == MatchResult(MatchOutcome.WHITE_WIN, "CHECKMATE")
    assert controller.state == MatchState.COMPLETED
    assert controller.events[-1].event_type == "MATCH_COMPLETED"
    with pytest.raises(InvalidMatchTransitionError, match="MATCH_NOT_RUNNING"):
        controller.receive_proposal(
            side=MatchSideColor.BLACK,
            binding=controller.bindings[MatchSideColor.BLACK],
            sequence=2,
            message=proposal("request-1", "e7e5"),
        )
    with pytest.raises(AIKProtocolError, match="PARTICIPANT_MESSAGE_SCHEMA_INVALID"):
        controller.transport.parse_stdout(
            AIKMessage(
                "claim-result",
                b'{"protocol_version":"aik-jsonl/1","type":"game_over",'
                b'"request_id":"claim-result","outcome":"WHITE_WIN"}\n',
            )
        )


def test_server_verifies_draw_claim_and_rejects_repeated_claims():
    controller, _ = make_controller(
        valid_draw_claims={("initial-position", "threefold_repetition")}
    )
    issue(controller, MatchSideColor.WHITE, "request-1")
    valid_claim = AIKMessage(
        "request-1",
        b'{"protocol_version":"aik-jsonl/1","type":"claim_draw",'
        b'"request_id":"request-1","reason":"threefold_repetition",'
        b'"intended_move":null}\n',
    )
    decision = controller.receive_proposal(
        side=MatchSideColor.WHITE,
        binding=controller.bindings[MatchSideColor.WHITE],
        sequence=1,
        message=valid_claim,
    )
    assert decision.result == MatchResult(MatchOutcome.DRAW, "THREEFOLD_REPETITION")
    assert controller.state == MatchState.COMPLETED

    controller, _ = make_controller()
    issue(controller, MatchSideColor.WHITE, "request-1")
    invalid_claim = AIKMessage(
        "request-1",
        b'{"protocol_version":"aik-jsonl/1","type":"claim_draw",'
        b'"request_id":"request-1","reason":"threefold_repetition",'
        b'"intended_move":null}\n',
    )
    rejected = controller.receive_proposal(
        side=MatchSideColor.WHITE,
        binding=controller.bindings[MatchSideColor.WHITE],
        sequence=1,
        message=invalid_claim,
    )
    assert not rejected.accepted
    assert json.loads(rejected.server_response.payload)["type"] == "claim_rejected"
    repeated_claim = AIKMessage(
        "request-1",
        b'{"protocol_version":"aik-jsonl/1","type":"claim_draw",'
        b'"request_id":"request-1","reason":"threefold_repetition",'
        b'"intended_move":"e2e4"}\n',
    )
    repeated = controller.receive_proposal(
        side=MatchSideColor.WHITE,
        binding=controller.bindings[MatchSideColor.WHITE],
        sequence=1,
        message=repeated_claim,
    )
    assert repeated.failure == MatchFailure.PROTOCOL_ERROR


def test_failure_and_controller_restart_are_idempotent_or_terminal():
    controller, _ = make_controller()
    binding = controller.bindings[MatchSideColor.WHITE]
    first = controller.report_failure(
        side=MatchSideColor.WHITE, binding=binding, failure=MatchFailure.PROCESS_CRASH
    )
    retry = controller.report_failure(
        side=MatchSideColor.WHITE, binding=binding, failure=MatchFailure.PROCESS_CRASH
    )
    assert first.failure == retry.failure == MatchFailure.PROCESS_CRASH
    assert controller.result is None
    controller, _ = make_controller()
    assert controller.abort_for_controller_restart().failure == MatchFailure.CONTROLLER_RESTART


def test_result_reasons_are_contract_bounded():
    with pytest.raises(ValueError, match="MATCH_RESULT_REASON_NOT_CONTRACT_DEFINED"):
        MatchResult(MatchOutcome.WHITE_WIN, "PARTICIPANT_SAYS_WIN")


def test_transcript_reconstruction_checks_hashes_order_and_alternating_sides():
    controller, _ = make_controller()
    issue(controller, MatchSideColor.WHITE, "request-1")
    controller.receive_proposal(
        side=MatchSideColor.WHITE,
        binding=controller.bindings[MatchSideColor.WHITE],
        sequence=1,
        message=proposal("request-1", "e2e4"),
    )
    issue(controller, MatchSideColor.BLACK, "request-2")
    controller.receive_proposal(
        side=MatchSideColor.BLACK,
        binding=controller.bindings[MatchSideColor.BLACK],
        sequence=2,
        message=proposal("request-2", "e7e5"),
    )
    position, history = reconstruct_position("initial-position", controller.moves, controller.rules)
    assert (position, history) == ("position-2", ("e2e4", "e7e5"))
    corrupted = [
        controller.moves[0],
        controller.moves[1].__class__(
            **{**controller.moves[1].__dict__, "position_before_sha256": "0" * 64}
        ),
    ]
    with pytest.raises(ValueError, match="MATCH_TRANSCRIPT_SEQUENCE_OR_POSITION_INVALID"):
        reconstruct_position("initial-position", corrupted, controller.rules)
    corrupted_after_fen = replace(
        controller.moves[0], position_after_fen="position-tampered"
    )
    with pytest.raises(ValueError, match="MATCH_TRANSCRIPT_REPLAY_MISMATCH"):
        reconstruct_position("initial-position", [corrupted_after_fen], controller.rules)
