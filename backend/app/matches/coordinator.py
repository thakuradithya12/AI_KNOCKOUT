"""Trusted, process-local DEVELOPMENT match command coordinator.

Controllers are registered only by the server-side match lifecycle. This module
contains no worker imports and exposes only command/response data to its caller.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app.matches.controller import MatchController, MatchSideColor, ProposalDecision
from app.models.match import MatchFailure, MatchSideKind, MatchState
from app.sandbox.contract import AIKMessage


class CoordinatorError(RuntimeError):
    """Safe coordinator rejection code."""


@dataclass(frozen=True)
class ExecutionAuthority:
    match_id: uuid.UUID
    side_id: uuid.UUID
    execution_id: uuid.UUID
    worker_id: str
    lease_id: uuid.UUID
    fence: int
    attempt: int
    side: MatchSideColor


@dataclass(frozen=True)
class CoordinatorCommand:
    match_id: uuid.UUID
    side_id: uuid.UUID
    execution_id: uuid.UUID
    worker_id: str
    lease_id: uuid.UUID
    fence: int
    attempt: int
    command_id: str
    command_type: str
    side: MatchSideColor
    message: bytes
    deadline_monotonic: float | None


@dataclass
class _MatchSlot:
    controller: MatchController
    lock: asyncio.Lock
    pending: CoordinatorCommand | None = None
    responses: dict[tuple[uuid.UUID, str], tuple[str, ProposalDecision | None]] | None = None
    failed_closed: bool = False


PersistController = Callable[[MatchController], Awaitable[None]]


class MatchCoordinator:
    """Own live controllers and serialize authenticated worker AIK traffic."""

    def __init__(self, persist: PersistController) -> None:
        self._persist = persist
        self._matches: dict[uuid.UUID, _MatchSlot] = {}

    def register(self, controller: MatchController) -> None:
        """Register a controller constructed from trusted server/database state."""
        if controller.state != MatchState.RUNNING or set(controller.bindings) != {
            MatchSideColor.WHITE, MatchSideColor.BLACK
        }:
            raise CoordinatorError("MATCH_CONTROLLER_NOT_READY")
        if {side.kind for side in controller.sides.values()} not in (
            {MatchSideKind.PARTICIPANT, MatchSideKind.HOUSE_AI},
            {MatchSideKind.PARTICIPANT},
            {MatchSideKind.HOUSE_AI},
        ):
            raise CoordinatorError("UNSUPPORTED_MATCH_SIDES")
        if controller.match_id in self._matches:
            raise CoordinatorError("MATCH_CONTROLLER_ALREADY_REGISTERED")
        self._matches[controller.match_id] = _MatchSlot(
            controller=controller,
            lock=asyncio.Lock(),
            responses={},
        )

    async def next_command(self, authority: ExecutionAuthority) -> CoordinatorCommand | None:
        slot = self._slot(authority.match_id)
        async with slot.lock:
            self._validate(slot, authority)
            controller = slot.controller
            if controller.state != MatchState.RUNNING or controller.result or controller.failure:
                raise CoordinatorError("MATCH_TERMINAL")
            if self._deadline_expired(controller):
                decision = controller.expire_move_deadline()
                slot.pending = None
                await self._persist_or_fail(slot)
                if decision is not None:
                    return None

            if slot.pending is not None:
                pending = slot.pending
                if self._same_authority(pending, authority):
                    return pending
                return None

            side = authority.side
            request_id = str(uuid.uuid4())
            if side not in controller.hello_ready:
                command_type = "HELLO"
                message = controller.issue_hello_request(side=side, request_id=request_id)
            elif side not in controller.new_game_acked:
                command_type = "NEW_GAME"
                message = controller.issue_new_game_request(side=side, request_id=request_id)
            elif side == controller.side_to_move:
                if not controller._position_acked:
                    command_type = "POSITION"
                    message = controller.issue_position_request(side=side, request_id=request_id)
                else:
                    command_type = "GO"
                    message = controller.issue_move_request(side=side, request_id=request_id)
            else:
                return None
            slot.pending = CoordinatorCommand(
                match_id=authority.match_id,
                side_id=authority.side_id,
                execution_id=authority.execution_id,
                worker_id=authority.worker_id,
                lease_id=authority.lease_id,
                fence=authority.fence,
                attempt=authority.attempt,
                command_id=message.message_id,
                command_type=command_type,
                side=side,
                message=message.payload,
                deadline_monotonic=controller.clock.move_deadline_monotonic
                if command_type == "GO" else None,
            )
            return slot.pending

    async def submit_response(
        self, authority: ExecutionAuthority, *, command_id: str, payload: bytes
    ) -> ProposalDecision | None:
        slot = self._slot(authority.match_id)
        async with slot.lock:
            self._validate(slot, authority)
            digest = hashlib.sha256(payload).hexdigest()
            key = (authority.execution_id, command_id)
            previous = slot.responses.get(key)
            if previous is not None:
                if previous[0] != digest:
                    raise CoordinatorError("DUPLICATE_RESPONSE_CONFLICT")
                return previous[1]
            command = slot.pending
            if command is None or command.command_id != command_id:
                raise CoordinatorError("COMMAND_UNKNOWN_OR_STALE")
            if not self._same_authority(command, authority):
                raise CoordinatorError("COMMAND_AUTHORITY_MISMATCH")
            controller = slot.controller
            message = AIKMessage(command_id, payload)
            decision = None
            if command.command_type in {"HELLO", "NEW_GAME", "POSITION"}:
                controller.receive_ack(
                    side=authority.side,
                    binding=controller.bindings[authority.side],
                    message=message,
                )
            elif command.command_type == "GO":
                decision = controller.receive_proposal(
                    side=authority.side,
                    binding=controller.bindings[authority.side],
                    sequence=controller.current_ply + 1,
                    message=message,
                )
            else:
                raise CoordinatorError("COMMAND_TYPE_INVALID")
            slot.responses[key] = (digest, decision)
            slot.pending = None
            await self._persist_or_fail(slot)
            return decision

    async def fence(
        self, *, match_id: uuid.UUID, execution_id: uuid.UUID,
        lease_id: uuid.UUID, fence: int,
    ) -> None:
        slot = self._slot(match_id)
        async with slot.lock:
            pending = slot.pending
            if pending is not None and (
                pending.execution_id != execution_id
                or pending.lease_id != lease_id
                or pending.fence != fence
            ):
                raise CoordinatorError("FENCE_AUTHORITY_MISMATCH")
            controller = slot.controller
            side = next(
                (color for color, binding in controller.bindings.items()
                 if binding.execution_id == execution_id),
                None,
            )
            if side is None:
                raise CoordinatorError("EXECUTION_UNKNOWN")
            controller.report_failure(
                side=side,
                binding=controller.bindings[side],
                failure=MatchFailure.STALE_FENCING,
            )
            slot.pending = None
            await self._persist_or_fail(slot)

    def controller_for_testing(self, match_id: uuid.UUID) -> MatchController:
        """Introspection hook for server-side tests; never exposed in transport schemas."""
        return self._slot(match_id).controller

    def _slot(self, match_id: uuid.UUID) -> _MatchSlot:
        slot = self._matches.get(match_id)
        if slot is None or slot.failed_closed:
            raise CoordinatorError("MATCH_COORDINATOR_UNAVAILABLE")
        return slot

    @staticmethod
    def _same_authority(command: CoordinatorCommand, authority: ExecutionAuthority) -> bool:
        return (
            command.match_id == authority.match_id
            and command.side_id == authority.side_id
            and command.execution_id == authority.execution_id
            and command.worker_id == authority.worker_id
            and command.lease_id == authority.lease_id
            and command.fence == authority.fence
            and command.attempt == authority.attempt
            and command.side == authority.side
        )

    @staticmethod
    def _validate(slot: _MatchSlot, authority: ExecutionAuthority) -> None:
        controller = slot.controller
        binding = controller.bindings.get(authority.side)
        side = controller.sides.get(authority.side)
        if (
            controller.match_id != authority.match_id
            or side is None or binding is None
            or side.side_id != authority.side_id
            or binding.side_id != authority.side_id
            or binding.execution_id != authority.execution_id
            or binding.worker_id != authority.worker_id
            or binding.lease_id != authority.lease_id
            or binding.fencing_token != authority.fence
            or binding.attempt_number != authority.attempt
        ):
            raise CoordinatorError("EXECUTION_AUTHORITY_MISMATCH")

    @staticmethod
    def _deadline_expired(controller: MatchController) -> bool:
        deadline = controller.clock.move_deadline_monotonic
        return deadline is not None and controller.monotonic() > deadline

    async def _persist_or_fail(self, slot: _MatchSlot) -> None:
        try:
            await self._persist(slot.controller)
        except Exception as exc:
            # Never use a mutated in-memory controller after persistence failed.
            slot.failed_closed = True
            slot.pending = None
            raise CoordinatorError("AUTHORITATIVE_PERSISTENCE_FAILED") from exc
