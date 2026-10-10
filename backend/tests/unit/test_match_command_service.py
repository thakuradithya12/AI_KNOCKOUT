import asyncio
import uuid
from datetime import datetime, timezone

import pytest

from app.matches.command_service import (
    CommandAuthority,
    MatchCommandError,
    MatchCommandService,
)
from app.models.match import MatchSideColor


def test_command_service_uses_coordinator_time_and_deadline():
    mono = iter([100.0]).__next__
    wall = iter([datetime(2026, 1, 1, tzinfo=timezone.utc)]).__next__
    ids = iter(["coordinator-command-1"]).__next__
    service = MatchCommandService(monotonic=mono, wall_clock=wall, request_id=ids)
    authority = CommandAuthority(
        match_id=uuid.uuid4(), execution_id=uuid.uuid4(), worker_id="worker-001",
        side=MatchSideColor.WHITE, lease_id=uuid.uuid4(), fence_token=5, attempt=2,
        white_remaining_ms=5000, black_remaining_ms=7000, move_timeout_ms=2000,
    )
    command = asyncio.run(service.issue_go(authority))
    assert command.turn_started_monotonic == 100.0
    assert command.deadline_monotonic == 102.0
    assert command.command_id == "coordinator-command-1"
    assert b'"type":"go"' in command.message


def test_fencing_one_execution_does_not_affect_another_match():
    async def run():
        service = MatchCommandService(request_id=lambda: str(uuid.uuid4()))
        first = CommandAuthority(
            match_id=uuid.uuid4(), execution_id=uuid.uuid4(), worker_id="worker-001",
            side=MatchSideColor.WHITE, lease_id=uuid.uuid4(), fence_token=1, attempt=1,
            white_remaining_ms=10000, black_remaining_ms=10000, move_timeout_ms=5000,
        )
        second = CommandAuthority(
            match_id=uuid.uuid4(), execution_id=uuid.uuid4(), worker_id="worker-002",
            side=MatchSideColor.BLACK, lease_id=uuid.uuid4(), fence_token=2, attempt=1,
            white_remaining_ms=10000, black_remaining_ms=10000, move_timeout_ms=5000,
        )
        await service.issue_go(first)
        await service.issue_go(second)
        await service.fence_execution(
            match_id=first.match_id, execution_id=first.execution_id,
            lease_id=first.lease_id, fence_token=first.fence_token,
        )
        assert service.active_command(first.match_id) is None
        assert service.active_command(second.match_id) is not None
        with pytest.raises(MatchCommandError, match="EXECUTION_FENCED"):
            await service.issue_go(first)

    asyncio.run(run())


def test_different_match_locks_are_distinct():
    async def run():
        locks = MatchCommandService().locks
        first, second = uuid.uuid4(), uuid.uuid4()
        first_lease, second_lease = await locks.hold(first), await locks.hold(second)
        assert first_lease.lock is not second_lease.lock

    asyncio.run(run())
