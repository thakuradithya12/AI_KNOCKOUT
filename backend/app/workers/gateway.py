"""Dedicated worker gateway. This router is not mounted on the participant API."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Header, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.worker import ExecutionJobStatus, Worker
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
    WorkerEnrollmentIn,
    WorkerIdentityOut,
    WorkerOperationOut,
)
from app.workers.service import (
    WorkerProtocolError,
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
from app.workers.tls import WorkerGatewayTLSConfig, verified_peer_from_scope

router = APIRouter(prefix="/worker/v1", tags=["worker-gateway"])
MAX_WORKER_REQUEST_BYTES = 16 * 1024


class WorkerRequestSizeLimit:
    """Bound complete ASGI request bodies before protocol parsing."""

    def __init__(self, app, *, max_bytes: int = MAX_WORKER_REQUEST_BYTES):
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope.get("path", "").startswith("/worker/"):
            await self.app(scope, receive, send)
            return
        length = next(
            (value for key, value in scope.get("headers", []) if key.lower() == b"content-length"),
            None,
        )
        if length is not None:
            try:
                too_large = int(length) > self.max_bytes
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
            if total > self.max_bytes:
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
        execution_mode="MOCK_PROTOCOL_ONLY",
        match_id=job.match_id,
        worker_id=worker.worker_id,
        participant_id=job.participant_id,
        submission_id=job.submission_id,
        submission_version=job.submission_version,
        artifact_sha256=job.artifact_sha256,
        immutable_artifact_ref=job.immutable_artifact_ref,
        runtime_profile=job.runtime_profile,
        protocol_version=job.protocol_version,
        initial_state=job.initial_state,
        resource_policy=job.resource_policy,
        execution_expires_at=job.execution_expires_at,
        lease=LeaseOut(
            lease_id=job.lease_id,
            fencing_token=job.fencing_token,
            expires_at=job.lease_expires_at,
            next_sequence=job.last_sequence + 1,
        ),
    )


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

    @asynccontextmanager
    async def lifespan(app):
        database_url = os.environ.get("WORKER_DATABASE_URL")
        if not database_url:
            raise RuntimeError("WORKER_DATABASE_URL must be configured")
        tls_config = WorkerGatewayTLSConfig.from_env()
        app.state.worker_tls_config = tls_config
        app.state.worker_tls_ssl_context = tls_config.build_server_ssl_context()
        app.state.worker_tls_ca_certificates = tls_config.validate_server_material()
        engine = create_async_engine(database_url, pool_pre_ping=True)
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
