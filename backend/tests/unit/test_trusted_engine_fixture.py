"""Unit tests for the trusted benchmark engine fixture and builder."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from app.services.submission_validation import validate_submission_package
from tests.fixtures.trusted_engine.builder import build_trusted_engine_archive


def test_trusted_engine_archive_passes_strict_static_validation():
    archive_bytes = build_trusted_engine_archive()
    assert len(archive_bytes) > 0
    package = validate_submission_package(archive_bytes)
    assert package.manifest["contract_version"] == "1"
    assert package.manifest["protocol_version"] == "aik-jsonl/1"
    assert package.manifest["runtime"] == "python-3.12"
    assert package.manifest["entrypoint"] == "engine/main.py"
    assert "manifest.json" in package.files
    assert "engine/main.py" in package.files


def test_trusted_engine_protocol_interaction():
    fixture_dir = Path(__file__).parent.parent / "fixtures" / "trusted_engine"
    engine_path = fixture_dir / "engine" / "main.py"

    proc = subprocess.Popen(
        [sys.executable, str(engine_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    try:
        # 1. Hello
        proc.stdin.write(json.dumps({"protocol_version": "aik-jsonl/1", "type": "hello", "request_id": "req-1"}) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        resp = json.loads(line)
        assert resp == {"protocol_version": "aik-jsonl/1", "type": "ready", "request_id": "req-1"}

        # 2. New Game
        proc.stdin.write(json.dumps({"protocol_version": "aik-jsonl/1", "type": "new_game", "request_id": "req-2", "side": "white"}) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        resp = json.loads(line)
        assert resp == {"protocol_version": "aik-jsonl/1", "type": "ack", "request_id": "req-2"}

        # 3. Position
        start_fen = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"
        proc.stdin.write(json.dumps({"protocol_version": "aik-jsonl/1", "type": "position", "request_id": "req-3", "fen": start_fen}) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        resp = json.loads(line)
        assert resp == {"protocol_version": "aik-jsonl/1", "type": "ack", "request_id": "req-3"}

        # 4. Go (first move)
        proc.stdin.write(json.dumps({"protocol_version": "aik-jsonl/1", "type": "go", "request_id": "req-4", "move_budget_ms": 2000}) + "\n")
        proc.stdin.flush()
        line = proc.stdout.readline()
        resp = json.loads(line)
        assert resp["protocol_version"] == "aik-jsonl/1"
        assert resp["type"] == "move"
        assert resp["request_id"] == "req-4"
        assert resp["move"] == "a2a3"  # First alphabetical legal move from start position

    finally:
        proc.stdin.close()
        proc.terminate()
        proc.wait(timeout=5)
