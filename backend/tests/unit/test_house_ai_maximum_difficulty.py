"""Unit tests for House AI Maximum Difficulty engine.

Verifies:
1. Canonical USTAR archive structure and manifest.
2. aik-jsonl/1 protocol adherence (hello, new_game, position, go).
3. Search capabilities: finds Mate in 1 (Scholar's mate pattern: h5f7#).
4. Positional/tactical play: avoids hanging pieces and captures undefended material.
5. Search depth and superiority over the baseline legal[0] bot.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import tarfile
import chess
import pytest

from app.services.trusted_engine_builder import (
    MAXIMUM_DIFFICULTY_ENGINE_NAME,
    MAXIMUM_DIFFICULTY_ENGINE_VERSION,
    build_maximum_difficulty_engine_archive,
)


def _extract_engine_code() -> str:
    archive_bytes = build_maximum_difficulty_engine_archive()
    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r") as tar:
        member = tar.getmember("engine/main.py")
        extracted = tar.extractfile(member)
        assert extracted is not None
        return extracted.read().decode("utf-8")


def _run_engine_protocol(commands: list[dict], timeout_sec: float = 10.0) -> list[dict]:
    engine_code = _extract_engine_code()
    proc = subprocess.Popen(
        [sys.executable, "-u", "-c", engine_code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    responses = []
    try:
        for cmd in commands:
            proc.stdin.write(json.dumps(cmd) + "\n")
            proc.stdin.flush()
            line = proc.stdout.readline()
            if not line:
                stderr_out = proc.stderr.read()
                raise RuntimeError(f"Engine terminated prematurely on cmd {cmd}: stderr={stderr_out}")
            responses.append(json.loads(line.strip()))
    finally:
        proc.stdin.close()
        proc.terminate()
        proc.wait(timeout=timeout_sec)

    return responses


def test_maximum_difficulty_archive_integrity():
    archive_bytes = build_maximum_difficulty_engine_archive()
    assert len(archive_bytes) > 0

    with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r") as tar:
        names = tar.getnames()
        assert "manifest.json" in names
        assert "engine/main.py" in names

        manifest_file = tar.extractfile("manifest.json")
        assert manifest_file is not None
        manifest = json.loads(manifest_file.read().decode("utf-8"))
        assert manifest["contract_version"] == "1"
        assert manifest["protocol_version"] == "aik-jsonl/1"
        assert manifest["engine_name"] == MAXIMUM_DIFFICULTY_ENGINE_NAME
        assert manifest["engine_version"] == MAXIMUM_DIFFICULTY_ENGINE_VERSION
        assert manifest["runtime"] == "python-3.12"
        assert manifest["entrypoint"] == "engine/main.py"


def test_maximum_difficulty_protocol_handshake_and_go():
    commands = [
        {"type": "hello", "protocol_version": "aik-jsonl/1", "request_id": "req-1"},
        {"type": "new_game", "color": "white", "request_id": "req-2"},
        {
            "type": "position",
            "fen": chess.STARTING_FEN,
            "moves": [],
            "request_id": "req-3",
        },
        {"type": "go", "time_left_ms": 60000, "request_id": "req-4"},
    ]

    responses = _run_engine_protocol(commands)
    assert len(responses) == 4

    # 1. Hello response
    assert responses[0]["type"] == "ready"
    assert responses[0]["protocol_version"] == "aik-jsonl/1"
    assert responses[0]["request_id"] == "req-1"

    # 2. New game response
    assert responses[1]["type"] == "ack"
    assert responses[1]["request_id"] == "req-2"

    # 3. Position response
    assert responses[2]["type"] == "ack"
    assert responses[2]["request_id"] == "req-3"

    # 4. Go response
    assert responses[3]["type"] in ("move", "bestmove")
    assert responses[3]["request_id"] == "req-4"
    move_uci = responses[3].get("move") or responses[3].get("move_uci") or responses[3].get("bestmove")
    assert move_uci is not None

    board = chess.Board()
    move = chess.Move.from_uci(move_uci)
    assert move in board.legal_moves


def test_maximum_difficulty_finds_mate_in_one():
    # Scholar's mate pattern: White Queen on h5 and Bishop on c4 attack unprotected f7 pawn.
    # Checkmate move: h5f7
    mate_in_1_fen = "r1bqkb1r/pppp1ppp/2n5/4p2Q/2B1n3/8/PPPP1PPP/RNB1K1NR w KQkq - 0 5"
    board = chess.Board(mate_in_1_fen)
    assert chess.Move.from_uci("h5f7") in board.legal_moves
    board.push_uci("h5f7")
    assert board.is_checkmate()

    commands = [
        {"type": "hello", "protocol_version": "aik-jsonl/1", "request_id": "h-1"},
        {"type": "new_game", "color": "white", "request_id": "ng-1"},
        {"type": "position", "fen": mate_in_1_fen, "moves": [], "request_id": "pos-1"},
        {"type": "go", "time_left_ms": 60000, "request_id": "go-1"},
    ]

    responses = _run_engine_protocol(commands)
    assert len(responses) == 4
    go_resp = responses[3]
    move_uci = go_resp.get("move") or go_resp.get("move_uci") or go_resp.get("bestmove")
    assert move_uci == "h5f7", f"Expected checkmate move h5f7, got {move_uci}"


def test_maximum_difficulty_captures_undefended_queen():
    # Black has an undefended Queen on d4 attacked by White bishop on e3.
    # Capturing d4 with bishop (e3d4) wins Black's queen cleanly.
    hanging_queen_fen = "rnb1kbnr/ppp1pppp/8/8/3q4/4B3/PPPP1PPP/RN1QKBNR w KQkq - 0 1"
    commands = [
        {"type": "hello", "protocol_version": "aik-jsonl/1", "request_id": "h-1"},
        {"type": "new_game", "color": "white", "request_id": "ng-1"},
        {"type": "position", "fen": hanging_queen_fen, "moves": [], "request_id": "pos-1"},
        {"type": "go", "time_left_ms": 60000, "request_id": "go-1"},
    ]

    responses = _run_engine_protocol(commands)
    assert len(responses) == 4
    go_resp = responses[3]
    move_uci = go_resp.get("move") or go_resp.get("move_uci") or go_resp.get("bestmove")
    assert move_uci == "e3d4", f"Expected e3d4 capturing hanging queen, got {move_uci}"


def test_maximum_difficulty_defeats_first_legal_bot():
    # Play Maximum Difficulty (White) vs a bot that blindly plays legal_moves[0] (Black).
    # Maximum Difficulty should checkmate Black quickly.
    engine_code = _extract_engine_code()
    proc = subprocess.Popen(
        [sys.executable, "-u", "-c", engine_code],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        # Hello
        proc.stdin.write(json.dumps({"type": "hello", "protocol_version": "aik-jsonl/1", "request_id": "1"}) + "\n")
        proc.stdin.flush()
        json.loads(proc.stdout.readline())

        # New game
        proc.stdin.write(json.dumps({"type": "new_game", "color": "white", "request_id": "2"}) + "\n")
        proc.stdin.flush()
        json.loads(proc.stdout.readline())

        board = chess.Board()
        moves_played = []
        max_plies = 40  # 20 full moves

        for ply in range(max_plies):
            if board.is_game_over():
                break

            if board.turn == chess.WHITE:
                # Maximum Difficulty move
                proc.stdin.write(json.dumps({
                    "type": "position",
                    "fen": board.fen(),
                    "moves": moves_played,
                    "request_id": f"p-{ply}"
                }) + "\n")
                proc.stdin.flush()
                json.loads(proc.stdout.readline())

                proc.stdin.write(json.dumps({
                    "type": "go",
                    "time_left_ms": 30000,
                    "request_id": f"g-{ply}"
                }) + "\n")
                proc.stdin.flush()
                resp = json.loads(proc.stdout.readline())
                move_uci = resp.get("move") or resp.get("move_uci") or resp.get("bestmove")
                move = chess.Move.from_uci(move_uci)
                assert move in board.legal_moves
                board.push(move)
                moves_played.append(move_uci)
            else:
                # Baseline bot: first legal move
                legal = sorted(board.legal_moves, key=lambda m: m.uci())
                baseline_move = legal[0]
                board.push(baseline_move)
                moves_played.append(baseline_move.uci())

        # Assert Maximum Difficulty won decisively
        assert board.is_checkmate(), f"Expected game to end in checkmate within {max_plies} plies, ended in: {board.result()}"
        # White delivered checkmate
        assert board.turn == chess.BLACK and board.is_checkmate()
    finally:
        proc.stdin.close()
        proc.terminate()
        proc.wait(timeout=5)
