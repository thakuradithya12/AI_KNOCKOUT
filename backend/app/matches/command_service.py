"""Single-process authoritative match command and fence state for V1."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from app.matches.controller import MatchSideColor
from app.workers.control_protocol import ControlAckResult


class MatchCommandError(RuntimeError):
    pass


@dataclass(frozen=True)
class CommandAuthority:
    match_id: uuid.UUID
    execution_id: uuid.UUID
    worker_id: str
    side: MatchSideColor
    lease_id: uuid.UUID
    fence_token: int
    attempt: int
    white_remaining_ms: int
    black_remaining_ms: int
    move_timeout_ms: int
    protocol_version: str = "aik-jsonl/1"


@dataclass(frozen=True)
class IssuedCommand:
    match_id: uuid.UUID
    execution_id: uuid.UUID
    worker_id: str
    side: MatchSideColor
    command_id: str
    command_type: str
    attempt: int
    lease_id: uuid.UUID
    fence_token: int
    turn_started_monotonic: float
    deadline_monotonic: float
    turn_started_at: datetime
    deadline_at: datetime
    white_remaining_ms: int
    black_remaining_ms: int
    message: bytes


class PerMatchLocks:
    """Reference-counted independent asyncio locks, one per match UUID."""

    def __init__(self) -> None:
        self._guard = asyncio.Lock()
        self._locks: dict[uuid.UUID, tuple[asyncio.Lock, int]] = {}

    class _Lease:
        def __init__(self, owner: PerMatchLocks, match_id: uuid.UUID, lock: asyncio.Lock):
            self.owner, self.match_id, self.lock = owner, match_id, lock

        async def __aenter__(self):
            await self.lock.acquire()
            return self

        async def __aexit__(self, *_exc):
            self.lock.release()
            async with self.owner._guard:
                current, users = self.owner._locks[self.match_id]
                if current is self.lock and users == 1 and not self.lock.locked():
                    del self.owner._locks[self.match_id]
                else:
                    self.owner._locks[self.match_id] = (current, users - 1)

    async def hold(self, match_id: uuid.UUID) -> PerMatchLocks._Lease:
        async with self._guard:
            lock, users = self._locks.get(match_id, (asyncio.Lock(), 0))
            self._locks[match_id] = (lock, users + 1)
        return self._Lease(self, match_id, lock)


class MatchCommandService:
    """Issue commands from already database-validated authority; no worker clock input."""

    def __init__(
        self,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        request_id: Callable[[], str] = lambda: str(uuid.uuid4()),
    ) -> None:
        self.monotonic = monotonic
        self.wall_clock = wall_clock
        self.request_id = request_id
        self.locks = PerMatchLocks()
        self._active: dict[uuid.UUID, IssuedCommand] = {}
        self._fenced: set[uuid.UUID] = set()
        self._acks: dict[str, ControlAckResult] = {}

    async def issue_go(self, authority: CommandAuthority) -> IssuedCommand:
        lease = await self.locks.hold(authority.match_id)
        async with lease:
            return self.issue_go_locked(authority)

    def issue_go_locked(self, authority: CommandAuthority) -> IssuedCommand:
        """Issue while the caller already holds this match's serialization lock."""
        if authority.execution_id in self._fenced:
            raise MatchCommandError("EXECUTION_FENCED")
        if authority.protocol_version != "aik-jsonl/1":
            raise MatchCommandError("PROTOCOL_VERSION_UNSUPPORTED")
        if min(
            authority.white_remaining_ms,
            authority.black_remaining_ms,
            authority.move_timeout_ms,
        ) < 0 or authority.move_timeout_ms == 0:
            raise MatchCommandError("COMMAND_CLOCK_INVALID")
        previous = self._active.get(authority.match_id)
        if previous is not None:
            raise MatchCommandError("MATCH_COMMAND_ALREADY_ACTIVE")
        remaining = (
            authority.white_remaining_ms
            if authority.side == MatchSideColor.WHITE
            else authority.black_remaining_ms
        )
        if remaining <= 0:
            raise MatchCommandError("MATCH_CLOCK_EXHAUSTED")
        started = self.monotonic()
        duration_ms = min(remaining, authority.move_timeout_ms)
        deadline = started + duration_ms / 1000
        started_at = self.wall_clock()
        command_id = self.request_id()
        message = json.dumps(
            {
                "protocol_version": authority.protocol_version,
                "type": "go",
                "request_id": command_id,
                "move_budget_ms": duration_ms,
                "clocks_ms": {
                    "white": authority.white_remaining_ms,
                    "black": authority.black_remaining_ms,
                },
            },
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8") + b"\n"
        command = IssuedCommand(
            match_id=authority.match_id,
            execution_id=authority.execution_id,
            worker_id=authority.worker_id,
            side=authority.side,
            command_id=command_id,
            command_type="GO",
            attempt=authority.attempt,
            lease_id=authority.lease_id,
            fence_token=authority.fence_token,
            turn_started_monotonic=started,
            deadline_monotonic=deadline,
            turn_started_at=started_at,
            deadline_at=started_at + timedelta(milliseconds=duration_ms),
            white_remaining_ms=authority.white_remaining_ms,
            black_remaining_ms=authority.black_remaining_ms,
            message=message,
        )
        self._active[authority.match_id] = command
        return command

    async def fence_execution(
        self, *, match_id: uuid.UUID, execution_id: uuid.UUID, lease_id: uuid.UUID,
        fence_token: int,
    ) -> IssuedCommand | None:
        lease = await self.locks.hold(match_id)
        async with lease:
            command = self._active.get(match_id)
            if command is not None and (
                command.execution_id != execution_id
                or command.lease_id != lease_id
                or command.fence_token != fence_token
            ):
                raise MatchCommandError("FENCE_AUTHORITY_MISMATCH")
            self._fenced.add(execution_id)
            self._active.pop(match_id, None)
            return command

    async def acknowledge(
        self, *, command_id: str, match_id: uuid.UUID, execution_id: uuid.UUID,
        fence_token: int, result: ControlAckResult,
    ) -> ControlAckResult:
        lease = await self.locks.hold(match_id)
        async with lease:
            command = self._active.get(match_id)
            if command is None or (
                command.command_id != command_id
                or command.execution_id != execution_id
                or command.fence_token != fence_token
            ):
                raise MatchCommandError("CONTROL_ACK_MISMATCH")
            previous = self._acks.get(command_id)
            if previous is not None:
                if previous != result:
                    raise MatchCommandError("CONTROL_ACK_CONFLICT")
                return previous
            self._acks[command_id] = result
            if result in {ControlAckResult.FENCED, ControlAckResult.ABORTED}:
                self._fenced.add(execution_id)
                self._active.pop(match_id, None)
            return result

    def active_command(self, match_id: uuid.UUID) -> IssuedCommand | None:
        return self._active.get(match_id)
