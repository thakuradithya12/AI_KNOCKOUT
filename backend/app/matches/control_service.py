"""Database-backed adapter from the private control API to MatchCoordinator."""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.matches.controller import (
    MatchClock,
    MatchConfiguration,
    MatchController,
    MatchSideColor,
    MatchSideIdentity,
)
from app.matches.coordinator import (
    CoordinatorCommand,
    CoordinatorError,
    ExecutionAuthority,
    MatchCoordinator,
)
from app.matches.service import (
    MatchServiceError,
    _binding_value,
    _current_binding,
    begin_initialization,
    bind_execution,
    mark_execution_ready,
    persist_controller_update,
    record_controller_restart,
)
from app.models.competition import CompetitionEvent
from app.models.match import (
    Match,
    MatchPurpose,
    MatchSide,
    MatchState,
)
from app.models.match import (
    MatchExecutionBinding as BindingRow,
)
from app.models.worker import (
    ExecutionJobStatus,
    Worker,
    WorkerExecutionJob,
    WorkerStatus,
)
from app.sandbox.contract import AIKTransport
from app.workers.control_protocol import (
    CommandIssueRequest,
    CommandIssueResponse,
    ControlResponseRequest,
)
from app.workers.tls import GATEWAY_SERVICE_ID, VerifiedServiceCertificate


class DatabaseControlService:
    """Validate gateway assertions against PostgreSQL and route to the existing coordinator."""

    def __init__(self, sessions: async_sessionmaker) -> None:
        self.sessions = sessions
        self.coordinator = MatchCoordinator(self._persist)
        self._locks: dict[uuid.UUID, asyncio.Lock] = {}
        self._registered: set[uuid.UUID] = set()

    async def command_issue(
        self, payload: CommandIssueRequest, gateway: VerifiedServiceCertificate
    ) -> CommandIssueResponse:
        self._require_gateway(gateway)
        try:
            authority, match, binding = await self._resolve(payload)
            if match.state in {MatchState.COMPLETED, MatchState.ABORTED}:
                return self._response(payload, "TERMINAL")
            async with self._lock(payload.match_id):
                if match.id not in self._registered and not await self._register_when_ready(
                    match, authority, binding
                ):
                    return self._response(payload, "IDLE")
                command = await self.coordinator.next_command(authority)
        except CoordinatorError as exc:
            if str(exc) == "MATCH_TERMINAL":
                return self._response(payload, "TERMINAL")
            raise
        except MatchServiceError as exc:
            raise ValueError(exc.code) from exc
        if command is None:
            return self._response(payload, "IDLE")
        return self._command_response(command)

    async def submit_response(
        self, payload: ControlResponseRequest, gateway: VerifiedServiceCertificate
    ) -> dict:
        self._require_gateway(gateway)
        authority, _, _ = await self._resolve(payload)
        decision = await self.coordinator.submit_response(
            authority,
            command_id=payload.command_id,
            payload=payload.payload.encode("utf-8"),
        )
        return {
            "status": "ACCEPTED",
            "accepted": bool(decision is None or decision.accepted),
            "terminal": bool(decision and (decision.result or decision.failure)),
        }

    async def fence_execution(self, payload, gateway):
        self._require_gateway(gateway)
        await self.coordinator.fence(
            match_id=payload.match_id,
            execution_id=payload.execution_id,
            lease_id=payload.lease_id,
            fence=payload.fence_token,
        )
        return {"status": "FENCED"}

    async def abort_match(self, payload, gateway):
        self._require_gateway(gateway)
        return {"status": "ABORTED"}

    async def acknowledge(self, payload, gateway):
        self._require_gateway(gateway)
        return {"status": "ACCEPTED"}

    async def _resolve(self, payload):
        async with self.sessions() as db:
            worker = await db.get(Worker, payload.worker_id)
            job = await db.scalar(
                select(WorkerExecutionJob).where(
                    WorkerExecutionJob.execution_id == payload.execution_id
                )
            )
            match = await db.get(Match, payload.match_id)
            side = await db.get(MatchSide, payload.side_id)
            if (
                worker is None or worker.status != WorkerStatus.ACTIVE
                or worker.revoked_at is not None
                or worker.certificate_fingerprint != payload.worker_certificate_fingerprint
                or job is None or match is None or side is None
                or match.purpose not in {
                    MatchPurpose.DEVELOPMENT,
                    MatchPurpose.OFFICIAL,
                    MatchPurpose.PEER_TEST,
                }
                or match.id != job.match_id or side.match_id != match.id
                or side.color.value != payload.side
                or job.assigned_worker_id != payload.worker_id
                or job.status != ExecutionJobStatus.ACKNOWLEDGED
                or job.lease_id != payload.lease_id
                or job.fencing_token != payload.fence_token
                or job.lease_expires_at is None
                or _utc(job.lease_expires_at) <= _now()
            ):
                raise ValueError("EXECUTION_AUTHORITY_MISMATCH")
            if match.state == MatchState.QUEUED:
                await begin_initialization(db, match.id)
            if match.state not in {
                MatchState.STARTING,
                MatchState.RUNNING,
                MatchState.COMPLETED,
                MatchState.ABORTED,
            }:
                raise ValueError("MATCH_NOT_ACTIVE")
            if match.state == MatchState.RUNNING and match.id not in self._registered:
                await record_controller_restart(db, match.id)
                raise ValueError("ACTIVE_MATCH_CONTROLLER_RESTARTED")
            binding_value = await bind_execution(
                db,
                match_id=match.id,
                side_id=side.id,
                execution_id=job.execution_id,
                worker_id=payload.worker_id,
                lease_id=payload.lease_id,
                fencing_token=payload.fence_token,
            )
            if binding_value.attempt_number != payload.attempt:
                raise ValueError("EXECUTION_ATTEMPT_MISMATCH")
            await db.refresh(job)
            await db.refresh(match)
            return (
                ExecutionAuthority(
                    match_id=match.id,
                    side_id=side.id,
                    execution_id=job.execution_id,
                    worker_id=payload.worker_id,
                    lease_id=payload.lease_id,
                    fence=payload.fence_token,
                    attempt=binding_value.attempt_number,
                    side=MatchSideColor(payload.side),
                ),
                match,
                binding_value,
            )

    async def _register_when_ready(self, match, authority, caller_binding) -> bool:
        if match.id in self._registered:
            return True
        async with self.sessions() as db:
            await self._mark_ready(authority, caller_binding, db=db)
            sides = (await db.execute(
                select(MatchSide).where(MatchSide.match_id == match.id)
            )).scalars().all()
            binding_rows = (await db.execute(
                select(BindingRow).where(BindingRow.match_id == match.id)
                .order_by(BindingRow.attempt_number.desc())
            )).scalars().all()
            latest = {}
            for row in binding_rows:
                latest.setdefault(row.match_side_id, row)
            if len(sides) != 2 or set(latest) != {side.id for side in sides}:
                return False
            ready_rows = (await db.execute(
                select(CompetitionEvent.metadata_json).where(
                    CompetitionEvent.aggregate_type == "match",
                    CompetitionEvent.aggregate_id == match.id,
                    CompetitionEvent.event_type == "EXECUTION_READY",
                )
            )).scalars().all()
            ready_binding_ids = {
                data.get("binding_id") for data in ready_rows if isinstance(data, dict)
            }
            if any(str(row.id) not in ready_binding_ids for row in latest.values()):
                return False
            bindings = {color: _binding_value(latest[side.id]) for side in sides
                        for color in [MatchSideColor(side.color.value)]}
            for value in bindings.values():
                if await _current_binding(db, value) is None:
                    raise ValueError("EXECUTION_BINDING_STALE")
            controller = self._make_controller(match, sides, latest, bindings)
            controller.start(
                ready_execution_ids={value.execution_id for value in bindings.values()}
            )
            await persist_controller_update(db, controller)
            self.coordinator.register(controller)
            self._registered.add(match.id)
            return True

    async def _mark_ready(self, authority, binding, db=None) -> None:
        if db is not None:
            await mark_execution_ready(db, binding=binding)
            return
        async with self.sessions() as session:
            await mark_execution_ready(session, binding=binding)

    @staticmethod
    def _make_controller(match, side_rows, binding_rows, bindings):
        from app.matches.chess import ChessRulesAdapter

        configuration = MatchConfiguration(
            protocol_version=match.protocol_version,
            runtime_profile=match.runtime_profile,
            policy_version=match.policy_version,
            rules_version=match.rules_version,
            initial_fen=match.initial_fen,
            initial_clock_ms=match.initial_clock_ms,
            increment_ms=match.increment_ms,
            move_timeout_ms=match.move_timeout_ms,
            match_seed=match.match_seed,
            policy_id=match.policy_id,
            policy_digest=match.policy_digest,
        )
        sides = {}
        for row in side_rows:
            color = MatchSideColor(row.color.value)
            house = row.house_ai_binding
            sides[color] = MatchSideIdentity(
                side_id=row.id,
                match_id=row.match_id,
                color=color,
                participant_id=row.participant_id,
                submission_id=row.submission_id,
                submission_version_id=row.submission_version_id,
                artifact_digest=row.artifact_sha256,
                protocol_version=row.protocol_version,
                runtime_profile=row.runtime_profile,
                kind=row.kind,
                house_ai_id=house.house_ai_id if house else None,
                house_ai_version=house.house_ai_version if house else None,
            )
        return MatchController(
            match_id=match.id,
            state=MatchState.STARTING,
            sides=sides,
            bindings=bindings,
            configuration=configuration,
            current_fen=match.current_fen,
            current_ply=match.current_ply,
            side_to_move=MatchSideColor(match.side_to_move.value),
            clock=MatchClock(
                match.white_remaining_ms,
                match.black_remaining_ms,
                match.increment_ms,
                MatchSideColor(match.side_to_move.value),
            ),
            rules=ChessRulesAdapter(match.initial_fen, rules_version=match.rules_version),
            transport=AIKTransport(monotonic=time.monotonic),
            monotonic=time.monotonic,
        )

    async def _persist(self, controller: MatchController) -> None:
        async with self.sessions() as db:
            await persist_controller_update(db, controller)

    def _lock(self, match_id):
        return self._locks.setdefault(match_id, asyncio.Lock())

    @staticmethod
    def _require_gateway(gateway: VerifiedServiceCertificate) -> None:
        if gateway.service_name != GATEWAY_SERVICE_ID:
            raise ValueError("GATEWAY_SERVICE_IDENTITY_INVALID")

    @staticmethod
    def _response(payload, status):
        return CommandIssueResponse(
            status=status,
            match_id=payload.match_id,
            execution_id=payload.execution_id,
            side_id=payload.side_id,
            side=payload.side,
            attempt=payload.attempt,
            lease_id=payload.lease_id,
            fence_token=payload.fence_token,
        )

    @classmethod
    def _command_response(cls, command: CoordinatorCommand):
        timeout = 30.0
        now = time.monotonic()
        if command.deadline_monotonic is not None:
            timeout = max(0.1, command.deadline_monotonic - now)
        return CommandIssueResponse(
            status="ACCEPTED",
            match_id=command.match_id,
            execution_id=command.execution_id,
            side_id=command.side_id,
            command_id=command.command_id,
            command_type=command.command_type,
            side=command.side.value,
            attempt=command.attempt,
            lease_id=command.lease_id,
            fence_token=command.fence,
            timeout_seconds=min(300, timeout),
            message=command.message.decode("utf-8", errors="strict"),
        )


def _now():
    return datetime.now(timezone.utc)


def _utc(value):
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
