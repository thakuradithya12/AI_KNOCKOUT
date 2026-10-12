"""Dedicated worker gateway. This router is not mounted on the participant API."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
import uuid

from fastapi import APIRouter, Depends, Header, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from app.models.match import (
    HouseAIMatchSideBinding,
    Match,
    MatchExecutionBinding,
    MatchSide,
    MatchSideKind,
    ParticipantMatchSideBinding,
)
from app.models.worker import ExecutionJobStatus, Worker, WorkerExecutionJob
from app.workers.schemas import (
    ArtifactCapabilityIn,
    ArtifactCapabilityOut,
    CleanupIn,
    EvidenceIn,
    HeartbeatIn,
    JobAcknowledgementIn,
    JobOut,
    LeaseOut,
    LeaseRenewalIn,
    MatchCommandRequest,
    MatchResponseRequest,
    WorkerEnrollmentIn,
    WorkerIdentityOut,
    WorkerOperationOut,
)
from app.workers.service import (
    WorkerProtocolError,
    _lease_valid,
    acknowledge_job,
    authenticate_worker,
    claim_job,
    download_authorized_artifact,
    enroll_worker,
    heartbeat,
    issue_artifact_capability,
    renew_lease,
    submit_cleanup,
    submit_evidence,
)
from app.workers.tls import (
    GatewayServiceClientTLSConfig,
    WorkerGatewayTLSConfig,
    verified_peer_from_scope,
)

router = APIRouter(prefix="/worker/v1", tags=["worker-gateway"])
MAX_WORKER_REQUEST_BYTES = 16 * 1024
MAX_MATCH_RESPONSE_BYTES = 400 * 1024


class WorkerRequestSizeLimit:
    """Bound complete ASGI request bodies before protocol parsing."""

    def __init__(self, app, *, max_bytes: int = MAX_WORKER_REQUEST_BYTES):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith("/worker/"):
            await self.app(scope, receive, send)
            return
        request_limit = (
            MAX_MATCH_RESPONSE_BYTES
            if scope.get("path", "").endswith("/match/response")
            else self.max_bytes
        )
        length = next(
            (value for key, value in scope.get("headers", []) if key.lower() == b"content-length"),
            None,
        )
        if length is not None:
            try:
                too_large = int(length) > request_limit
            except ValueError:
                too_large = True
            if too_large:
                await self._reject(send)
                return

        chunks = []
        total = 0
        more = True
        while more:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            chunk = message.get("body", b"")
            total += len(chunk)
            if total > request_limit:
                await self._reject(send)
                return
            chunks.append(chunk)
            more = message.get("more_body", False)

        body = b"".join(chunks)
        delivered = False

        async def replay_body():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            return {"type": "http.request", "body": b"", "more_body": False}

        await self.app(scope, replay_body, send)

    @staticmethod
    async def _reject(send):
        body = b'{"error":"REQUEST_TOO_LARGE"}'
        await send({
            "type": "http.response.start", "status": 413,
            "headers": [(b"content-type", b"application/json"),
                        (b"content-length", str(len(body)).encode("ascii"))],
        })
        await send({"type": "http.response.body", "body": body})


async def get_worker_db(request: Request):
    """Worker gateway uses its own session factory and never the participant API dependency."""
    factory = getattr(request.app.state, "worker_session_factory", None)
    if factory is None:
        raise WorkerProtocolError(503, "WORKER_GATEWAY_DATABASE_UNAVAILABLE")
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def require_worker(
    request: Request, db: AsyncSession = Depends(get_worker_db)
) -> Worker:
    if request.headers.get("authorization") is not None:
        raise WorkerProtocolError(401, "WORKER_BEARER_AUTH_FORBIDDEN")
    peer = verified_peer_from_scope(
        request.scope, getattr(request.app.state, "worker_tls_ca_certificates", None)
    )
    return await authenticate_worker(db, peer)


@router.post("/enrollment", response_model=WorkerIdentityOut, status_code=status.HTTP_201_CREATED)
async def worker_enrollment(
    payload: WorkerEnrollmentIn,
    request: Request,
    db: AsyncSession = Depends(get_worker_db),
) -> WorkerIdentityOut:
    # The server TLS listener requires a valid private-CA client certificate when
    # one is presented. This bootstrap route additionally consumes an operator-
    # approved, one-time token; normal worker operations require an ACTIVE record.
    peer = verified_peer_from_scope(
        request.scope, getattr(request.app.state, "worker_tls_ca_certificates", None)
    )
    if peer is None:
        raise WorkerProtocolError(401, "WORKER_MTLS_REQUIRED")
    worker = await enroll_worker(
        db,
        worker_id=payload.worker_id,
        raw_token=payload.enrollment_token,
        certificate=peer,
        capabilities=payload.capabilities.model_dump(mode="json"),
        software_version=payload.software_version,
        runtime_version=payload.runtime_version,
    )
    return WorkerIdentityOut(
        worker_id=worker.worker_id,
        status=worker.status.value,
        certificate_fingerprint=worker.pending_certificate_fingerprint,
        expires_at=worker.pending_expires_at,
    )


@router.post("/heartbeat", response_model=WorkerOperationOut)
async def worker_heartbeat(
    payload: HeartbeatIn,
    db: AsyncSession = Depends(get_worker_db),
    worker: Worker = Depends(require_worker),
) -> WorkerOperationOut:
    await heartbeat(
        db, worker=worker, capabilities=payload.capabilities.model_dump(mode="json"),
        software_version=payload.software_version,
        runtime_version=payload.runtime_version,
        security_state=payload.security_state,
    )
    return WorkerOperationOut()


@router.post("/jobs/claim", response_model=JobOut | None)
async def claim_worker_job(
    db: AsyncSession = Depends(get_worker_db),
    worker: Worker = Depends(require_worker),
) -> JobOut | None:
    job = await claim_job(db, worker=worker)
    if job is None:
        return None
    return JobOut(
        execution_id=job.execution_id,
        execution_mode=job.execution_mode,
        execution_kind=job.execution_kind.value,
        match_id=job.match_id,
        match_side_id=await _job_side_id(db, job),
        match_purpose=(await db.get(Match, job.match_id)).purpose.value if job.match_id else None,
        worker_id=worker.worker_id,
        participant_id=job.participant_id,
        submission_id=job.submission_id,
        submission_version=job.submission_version,
        submission_version_id=job.submission_version_id,
        house_ai_id=job.house_ai_id,
        house_ai_version=job.house_ai_version,
        artifact_sha256=job.artifact_sha256,
        immutable_artifact_ref=job.immutable_artifact_ref,
        runtime_profile=job.runtime_profile,
        protocol_version=job.protocol_version,
        initial_state=job.initial_state,
        resource_policy=job.resource_policy,
        policy_digest=job.initial_state.get("policy_digest"),
        attempt_number=await _next_match_attempt(db, job),
        created_at=job.created_at,
        execution_expires_at=job.execution_expires_at,
        lease=LeaseOut(
            lease_id=job.lease_id,
            fencing_token=job.fencing_token,
            expires_at=job.lease_expires_at,
            next_sequence=job.last_sequence + 1,
        ),
    )


async def _job_side_id(db: AsyncSession, job) -> uuid.UUID | None:
    if job.match_id is None:
        return None
    binding = (
        ParticipantMatchSideBinding
        if job.execution_kind.value == "PARTICIPANT"
        else HouseAIMatchSideBinding
    )
    statement = select(MatchSide.id).join(
        binding, binding.match_side_id == MatchSide.id
    ).where(MatchSide.match_id == job.match_id)
    if job.execution_kind.value == "PARTICIPANT":
        statement = statement.where(
            MatchSide.kind == MatchSideKind.PARTICIPANT,
            binding.submission_version_id == job.submission_version_id,
        )
    else:
        statement = statement.where(
            MatchSide.kind == MatchSideKind.HOUSE_AI,
            binding.house_ai_id == job.house_ai_id,
            binding.house_ai_version == job.house_ai_version,
        )
    return await db.scalar(statement)


async def _next_match_attempt(db: AsyncSession, job) -> int:
    side_id = await _job_side_id(db, job)
    if side_id is None:
        return max(1, job.fencing_token)
    current_attempt = await db.scalar(
        select(MatchExecutionBinding.attempt_number).where(
            MatchExecutionBinding.execution_id == job.execution_id
        )
    )
    if current_attempt is not None:
        return current_attempt
    latest = await db.scalar(
        select(func.max(MatchExecutionBinding.attempt_number)).where(
            MatchExecutionBinding.match_side_id == side_id
        )
    )
    return (latest or 0) + 1


@router.post("/jobs/{execution_id}/acknowledgement", response_model=WorkerOperationOut)
async def acknowledge_worker_job(
    execution_id: uuid.UUID,
    payload: JobAcknowledgementIn,
    db: AsyncSession = Depends(get_worker_db),
    worker: Worker = Depends(require_worker),
) -> WorkerOperationOut:
    job = await acknowledge_job(
        db, worker=worker, execution_id=execution_id,
        lease_id=payload.lease_id, fencing_token=payload.fencing_token,
        sequence=payload.sequence, accepted=payload.accepted,
    )
    return WorkerOperationOut(
        execution_id=execution_id, sequence=payload.sequence, status=job.status.value
    )


@router.post("/jobs/{execution_id}/lease/renewal", response_model=LeaseOut)
async def renew_worker_lease(
    execution_id: uuid.UUID,
    payload: LeaseRenewalIn,
    db: AsyncSession = Depends(get_worker_db),
    worker: Worker = Depends(require_worker),
) -> LeaseOut:
    job = await renew_lease(
        db, worker=worker, execution_id=execution_id,
        lease_id=payload.lease_id, fencing_token=payload.fencing_token,
        sequence=payload.sequence,
    )
    return LeaseOut(
        lease_id=job.lease_id, fencing_token=job.fencing_token,
        expires_at=job.lease_expires_at, next_sequence=job.last_sequence + 1,
    )


@router.post("/jobs/{execution_id}/artifact-capability", response_model=ArtifactCapabilityOut)
async def request_artifact_capability(
    execution_id: uuid.UUID,
    payload: ArtifactCapabilityIn,
    db: AsyncSession = Depends(get_worker_db),
    worker: Worker = Depends(require_worker),
) -> ArtifactCapabilityOut:
    token, cap = await issue_artifact_capability(
        db, worker=worker, execution_id=execution_id,
        lease_id=payload.lease_id, fencing_token=payload.fencing_token,
        sequence=payload.sequence,
    )
    return ArtifactCapabilityOut(capability=token, expires_at=cap.expires_at)


@router.get("/jobs/{execution_id}/artifact")
async def transfer_artifact(
    execution_id: uuid.UUID,
    lease_id: uuid.UUID,
    fencing_token: int,
    sequence: int,
    x_artifact_capability: str = Header(min_length=32, max_length=128),
    db: AsyncSession = Depends(get_worker_db),
    worker: Worker = Depends(require_worker),
) -> Response:
    content, digest = await download_authorized_artifact(
        db, worker=worker, execution_id=execution_id,
        lease_id=lease_id, fencing_token=fencing_token,
        sequence=sequence, raw_capability=x_artifact_capability,
    )
    return Response(
        content=content,
        media_type="application/x-tar",
        headers={"X-Artifact-SHA256": digest, "Cache-Control": "no-store", "Pragma": "no-cache"},
    )


@router.post("/jobs/{execution_id}/match/next")
async def next_match_command(
    execution_id: uuid.UUID,
    payload: MatchCommandRequest,
    request: Request,
    db: AsyncSession = Depends(get_worker_db),
    worker: Worker = Depends(require_worker),
):
    authority = await _control_authority(
        db, worker=worker, execution_id=execution_id, lease_id=payload.lease_id,
        fence=payload.fencing_token, attempt=payload.attempt,
    )
    body = await run_in_threadpool(
        _post_match_control, request, "/internal/v1/commands/issue", authority
    )
    return body


@router.post("/jobs/{execution_id}/match/response")
async def submit_match_response(
    execution_id: uuid.UUID,
    payload: MatchResponseRequest,
    request: Request,
    db: AsyncSession = Depends(get_worker_db),
    worker: Worker = Depends(require_worker),
):
    authority = await _control_authority(
        db, worker=worker, execution_id=execution_id, lease_id=payload.lease_id,
        fence=payload.fencing_token, attempt=payload.attempt,
    )
    authority.update(command_id=payload.command_id, payload=payload.payload)
    return await run_in_threadpool(
        _post_match_control, request, "/internal/v1/commands/respond", authority
    )


async def _control_authority(db, *, worker, execution_id, lease_id, fence, attempt):
    job = await db.scalar(
        select(WorkerExecutionJob).where(
            WorkerExecutionJob.execution_id == execution_id
        )
    )
    if job is None:
        raise WorkerProtocolError(404, "EXECUTION_NOT_FOUND")
    _lease_valid(job, worker, lease_id, fence)
    if job.status != ExecutionJobStatus.ACKNOWLEDGED or job.match_id is None:
        raise WorkerProtocolError(409, "MATCH_EXECUTION_NOT_READY")
    side_id = await _job_side_id(db, job)
    side = await db.get(MatchSide, side_id) if side_id else None
    if side is None:
        raise WorkerProtocolError(409, "MATCH_SIDE_BINDING_MISSING")
    existing_attempt = await db.scalar(
        select(MatchExecutionBinding.attempt_number).where(
            MatchExecutionBinding.execution_id == execution_id
        )
    )
    if existing_attempt is None:
        last_attempt = await db.scalar(
            select(func.max(MatchExecutionBinding.attempt_number)).where(
                MatchExecutionBinding.match_side_id == side.id
            )
        )
        existing_attempt = (last_attempt or 0) + 1
    if attempt != existing_attempt:
        raise WorkerProtocolError(409, "EXECUTION_ATTEMPT_MISMATCH")
    match = await db.get(Match, job.match_id)
    if match is None or match.purpose.value != "DEVELOPMENT":
        raise WorkerProtocolError(409, "DEVELOPMENT_MATCH_REQUIRED")
    result = {
        "match_id": str(match.id),
        "side_id": str(side.id),
        "execution_id": str(execution_id),
        "worker_id": worker.worker_id,
        "worker_certificate_fingerprint": worker.certificate_fingerprint,
        "side": side.color.value,
        "lease_id": str(lease_id),
        "fence_token": fence,
        "attempt": attempt,
    }
    await db.commit()
    return result


def _post_match_control(request: Request, path: str, body: dict):
    endpoint = getattr(request.app.state, "match_coordinator_url", None)
    context = getattr(request.app.state, "match_coordinator_ssl_context", None)
    if not endpoint or context is None:
        raise WorkerProtocolError(503, "MATCH_CONTROL_UNAVAILABLE")
    req = urllib.request.Request(
        endpoint.rstrip("/") + path,
        data=json.dumps(body, separators=(",", ":"), allow_nan=False).encode(),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, context=context, timeout=5) as response:
            raw = response.read(MAX_MATCH_RESPONSE_BYTES + 1)
        if len(raw) > MAX_MATCH_RESPONSE_BYTES:
            raise WorkerProtocolError(502, "MATCH_CONTROL_RESPONSE_TOO_LARGE")
        result = json.loads(raw)
        if not isinstance(result, dict):
            raise WorkerProtocolError(502, "MATCH_CONTROL_RESPONSE_INVALID")
        return result
    except urllib.error.HTTPError as exc:
        try:
            response_body = json.loads(exc.read(MAX_WORKER_REQUEST_BYTES))
            detail = response_body.get("detail") if isinstance(response_body, dict) else None
        except (ValueError, OSError):
            detail = None
        if not isinstance(detail, str) or not detail.isidentifier() or len(detail) > 96:
            detail = f"HTTP_{exc.code}"
        raise WorkerProtocolError(409, f"MATCH_CONTROL_{detail}") from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise WorkerProtocolError(503, "MATCH_CONTROL_UNAVAILABLE") from exc
@router.post("/jobs/{execution_id}/evidence", response_model=WorkerOperationOut)
async def report_evidence(
    execution_id: uuid.UUID,
    payload: EvidenceIn,
    db: AsyncSession = Depends(get_worker_db),
    worker: Worker = Depends(require_worker),
) -> WorkerOperationOut:
    row = await submit_evidence(
        db, worker=worker, execution_id=execution_id,
        lease_id=payload.lease_id, fencing_token=payload.fencing_token,
        evidence=payload,
    )
    return WorkerOperationOut(execution_id=execution_id, sequence=row.sequence)


@router.post("/jobs/{execution_id}/cleanup", response_model=WorkerOperationOut)
async def report_cleanup(
    execution_id: uuid.UUID,
    payload: CleanupIn,
    db: AsyncSession = Depends(get_worker_db),
    worker: Worker = Depends(require_worker),
) -> WorkerOperationOut:
    status_value = ExecutionJobStatus(payload.final_status)
    job = await submit_cleanup(
        db, worker=worker, execution_id=execution_id,
        lease_id=payload.lease_id, fencing_token=payload.fencing_token,
        sequence=payload.sequence, cleanup_verified=payload.cleanup_verified,
        final_status=status_value, failure_class=payload.failure_class,
    )
    return WorkerOperationOut(
        execution_id=execution_id, sequence=payload.sequence, status=job.status.value
    )


def install_worker_error_handler(app) -> None:
    @app.exception_handler(WorkerProtocolError)
    async def worker_protocol_error_handler(request: Request, exc: WorkerProtocolError):
        await _record_rejected_request(request, exc.code)
        return JSONResponse(status_code=exc.status_code, content={"error": exc.code})

    @app.exception_handler(RequestValidationError)
    async def worker_validation_error_handler(request: Request, exc: RequestValidationError):
        # Never echo input values: validation errors may contain tokens or other secrets.
        await _record_rejected_request(request, "REQUEST_VALIDATION_FAILED")
        return JSONResponse(status_code=422, content={"error": "REQUEST_VALIDATION_FAILED"})


async def _record_rejected_request(request: Request, code: str) -> None:
    """Record a bounded rejection event; omit request body, headers, and credentials."""
    factory = getattr(request.app.state, "worker_session_factory", None)
    if factory is None:
        return
    peer = verified_peer_from_scope(
        request.scope, getattr(request.app.state, "worker_tls_ca_certificates", None)
    )
    try:
        async with factory() as session:
            from app.workers.service import _audit

            session.add(_audit(
                event_type="WORKER_REQUEST_REJECTED",
                actor_id=peer.worker_id if peer else "unidentified",
                worker_id=peer.worker_id if peer else None, severity="WARNING",
                details={"code": code[:64], "method": request.method[:8]},
            ))
            await session.commit()
    except Exception:
        # Security-event persistence must not replace the protocol response.
        return


def create_worker_gateway_app():
    """Create a separate ASGI app; deliberately not included in the participant API."""
    import os
    from contextlib import asynccontextmanager

    from fastapi import FastAPI
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db.url import get_asyncpg_connect_args, normalize_async_database_url

    @asynccontextmanager
    async def lifespan(app):
        database_url = os.environ.get("WORKER_DATABASE_URL")
        if not database_url:
            raise RuntimeError("WORKER_DATABASE_URL must be configured")
        control_url = os.environ.get("MATCH_COORDINATOR_URL")
        if not control_url:
            raise RuntimeError("MATCH_COORDINATOR_URL must be configured")
        parsed_control_url = urllib.parse.urlsplit(control_url)
        service_tls = GatewayServiceClientTLSConfig.from_env()
        if (
            parsed_control_url.scheme != "https"
            or parsed_control_url.hostname != service_tls.coordinator_server_name
            or parsed_control_url.username is not None
            or parsed_control_url.password is not None
            or parsed_control_url.query
            or parsed_control_url.fragment
        ):
            raise RuntimeError("MATCH_COORDINATOR_URL is invalid")
        app.state.match_coordinator_url = control_url.rstrip("/")
        app.state.match_coordinator_ssl_context = service_tls.build_ssl_context()
        tls_config = WorkerGatewayTLSConfig.from_env()
        app.state.worker_tls_config = tls_config
        app.state.worker_tls_ssl_context = tls_config.build_server_ssl_context()
        app.state.worker_tls_ca_certificates = tls_config.validate_server_material()
        normalized_db_url = normalize_async_database_url(database_url)
        connect_args = get_asyncpg_connect_args(database_url)
        engine = create_async_engine(
            normalized_db_url, connect_args=connect_args, pool_pre_ping=True
        )
        app.state.worker_engine = engine
        app.state.worker_session_factory = async_sessionmaker(engine, expire_on_commit=False)
        try:
            yield
        finally:
            await engine.dispose()

    app = FastAPI(
        title="AI Knockout Worker Gateway", docs_url=None, redoc_url=None,
        openapi_url=None, lifespan=lifespan,
    )
    app.include_router(router)
    install_worker_error_handler(app)
    app.add_middleware(WorkerRequestSizeLimit)
    return app
