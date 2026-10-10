"""The Kata bridge only transports opaque AIK bytes."""

from __future__ import annotations

import json
import uuid

import pytest

from app.matches.kata_participant import KataMatchParticipant
from app.sandbox.contract import AIKMessage, ExecutionState, SandboxSnapshot
from app.sandbox.kata import KataRuntimeError


class _Adapter:
    def __init__(self, response: AIKMessage | None):
        self.response = response
        self.sent = []

    def send_input(self, execution_id, message):
        self.sent.append((execution_id, message))

    def read_output(self, execution_id):
        response, self.response = self.response, None
        return response

    def inspect(self, execution_id):
        return SandboxSnapshot(None, ExecutionState.RUNNING, "a" * 64, None, None)

    def terminate(self, execution_id):
        pass


def test_bridge_transports_command_and_response_without_controller():
    request = {"protocol_version": "aik-jsonl/1", "type": "hello", "request_id": "cmd-1"}
    response = AIKMessage("cmd-1", json.dumps({
        "protocol_version": "aik-jsonl/1", "type": "ready", "request_id": "cmd-1"
    }).encode() + b"\n")
    adapter = _Adapter(response)
    bridge = KataMatchParticipant(adapter=adapter, execution_id=uuid.uuid4(),
                                  monotonic=lambda: 1.0, sleep=lambda _: None)

    result = bridge.exchange("cmd-1", json.dumps(request).encode() + b"\n", timeout=1)

    assert result == response
    assert adapter.sent[0][1].message_id == "cmd-1"
    assert "controller" not in KataMatchParticipant.__init__.__annotations__


def test_bridge_terminates_on_local_timeout():
    adapter = _Adapter(None)
    ticks = iter([0.0, 0.0, 2.0])
    bridge = KataMatchParticipant(adapter=adapter, execution_id=uuid.uuid4(),
                                  monotonic=lambda: next(ticks), sleep=lambda _: None)
    with pytest.raises(KataRuntimeError, match="PARTICIPANT_RESPONSE_TIMEOUT"):
        bridge.exchange("cmd-1", b"{}\n", timeout=1)
