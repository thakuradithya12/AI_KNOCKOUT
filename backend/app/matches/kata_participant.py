"""Transport AIK bytes between a Kata process and the trusted control path.

This adapter has no chess or match authority. Commands and responses are
opaque protocol bytes; validation belongs to MatchCoordinator/MatchController.
"""

from __future__ import annotations

import time
import uuid

from app.sandbox.contract import AIKMessage, ExecutionState
from app.sandbox.kata import KataRuntimeError, KataSandboxAdapter


class KataMatchParticipant:
    """Send one coordinator command to an AI process and collect its response."""

    def __init__(self, *, adapter: KataSandboxAdapter, execution_id: uuid.UUID,
                 monotonic=time.monotonic, sleep=time.sleep) -> None:
        self.adapter = adapter
        self.execution_id = execution_id
        self._monotonic = monotonic
        self._sleep = sleep

    def exchange(self, command_id: str, message: bytes, *, timeout: float) -> AIKMessage:
        if not command_id or len(command_id) > 64 or not 0 < timeout <= 300:
            raise KataRuntimeError("CONTROL_COMMAND_INVALID")
        self.adapter.send_input(self.execution_id, AIKMessage(command_id, message))
        deadline = self._monotonic() + timeout
        while self._monotonic() < deadline:
            snapshot = self.adapter.inspect(self.execution_id)
            if snapshot.state in {
                ExecutionState.CRASHED, ExecutionState.TIMED_OUT,
                ExecutionState.RESOURCE_EXCEEDED, ExecutionState.PROTOCOL_FAILED,
                ExecutionState.CLEANUP_FAILED, ExecutionState.CLEANUP_UNKNOWN,
            }:
                raise KataRuntimeError("PARTICIPANT_EXECUTION_FAILED")
            output = self.adapter.read_output(self.execution_id)
            if output is not None:
                return output
            self._sleep(0.005)
        self.adapter.terminate(self.execution_id)
        raise KataRuntimeError("PARTICIPANT_RESPONSE_TIMEOUT")

    def terminate(self) -> None:
        self.adapter.terminate(self.execution_id)
