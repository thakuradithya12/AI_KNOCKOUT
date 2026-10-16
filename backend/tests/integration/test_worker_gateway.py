"""Mock-only worker protocol tests; these do not prove a sandbox security boundary."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from pydantic import ValidationError
from sqlalchemy import insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.competition import (
    ArtifactState,
    ArtifactStateTransition,
    QuarantineObject,
    Submission,
    SubmissionVersion,
)
from app.models.participant import Participant, ParticipantStatus
from app.models.worker import (
    ExecutionJobStatus,
    Worker,
    WorkerArtifactCapability,
    WorkerEnrollmentToken,
    WorkerEvidence,
    WorkerExecutionJob,
    WorkerSecurityEvent,
    WorkerStatus,
)
from app.workers.gateway import create_worker_gateway_app
from app.workers.mock import MockArtifactRejected, MockWorker
from app.workers.schemas import EvidenceIn, ResourcePolicy, WorkerCapabilities
from app.workers.service import (
    WorkerProtocolError,
    activate_worker,
    authenticate_worker,
    create_execution_job,
    enroll_worker,
    finalize_execution_job,
    issue_certificate_rotation_token,
    issue_enrollment_token,
    revoke_worker,
)
from app.workers.tls import (
    VerifiedWorkerCertificate,
    WorkerGatewayTLSConfig,
    WorkerTLSConfigurationError,
    server_ssl_context,
    verified_peer_from_scope,
    worker_client_ssl_context,
)

MOCK_ARCHIVE = b"controlled test fixture bytes; never executable in this test"
_CA_KEY = ec.generate_private_key(ec.SECP256R1())
_CA_NAME = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "isolated-worker-test-ca")])
_CA_CERT = (
    x509.CertificateBuilder()
    .subject_name(_CA_NAME)
    .issuer_name(_CA_NAME)
    .public_key(_CA_KEY.public_key())
    .serial_number(x509.random_serial_number())
    .not_valid_before(datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1))
    .not_valid_after(datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=365))
    .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
    .add_extension(
        x509.KeyUsage(
            digital_signature=True,
            content_commitment=False,
            key_encipherment=False,
            data_encipherment=False,
            key_agreement=False,
            key_cert_sign=True,
            crl_sign=True,
            encipher_only=None,
            decipher_only=None,
        ),
        critical=True,
    )
    .sign(_CA_KEY, hashes.SHA256())
)


def _signed_certificate(
    *,
    common_name: str,
    worker_id: str | None,
    expired: bool = False,
    server_name: str | None = None,
):
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc)
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    builder = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(_CA_NAME)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before((now - timedelta(days=5)).replace(tzinfo=None))
        .not_valid_after(
            (now - timedelta(days=1) if expired else now + timedelta(days=30)).replace(tzinfo=None)
        )
    )
    sans = []
    if worker_id:
        sans.append(x509.UniformResourceIdentifier(f"urn:aik:worker:{worker_id}"))
        eku = ExtendedKeyUsageOID.CLIENT_AUTH
    if server_name:
        sans.append(x509.DNSName(server_name))
        eku = ExtendedKeyUsageOID.SERVER_AUTH
    cert = (
        builder.add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([eku]), critical=False)
        .sign(_CA_KEY, hashes.SHA256())
    )
    return key, cert


class _MockTlsExtension:
    """Test transport shim. It simulates the ASGI TLS extension and is not a TLS server."""

    def __init__(self, app, pem: str | None):
        self.app = app
        self.pem = pem

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            scope = dict(scope)
            extensions = dict(scope.get("extensions", {}))
            extensions["tls"] = {
                "tls_version": 0x0304,
                "client_cert_error": None,
                "client_cert_chain": [self.pem] if self.pem else [],
                "client_cert_name": "mock transport only",
            }
            scope["extensions"] = extensions
        await self.app(scope, receive, send)


def _mock_certificate(*, common_name: str, expired: bool = False, worker_id: str | None = None):
    worker_id = worker_id or common_name
    _, cert = _signed_certificate(common_name=common_name, worker_id=worker_id, expired=expired)
    pem = cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
    from app.workers.tls import verified_peer_from_scope

    peer = verified_peer_from_scope(
        {
            "extensions": {
                "tls": {
                    "tls_version": 0x0304,
                    "client_cert_error": None,
                    "client_cert_chain": [pem],
                }
            }
        },
        (_CA_CERT,),
    )
    return pem, peer


def _capabilities() -> dict:
    return {
        "sandbox_backend": "MOCK_TEST_ONLY",
        "sandbox_version": "mock/1",
        "sandbox_digest": "a" * 64,
        "protocol_versions": ["aik-jsonl/1"],
    }


async def _gateway_client(test_session_factory, pem: str | None) -> httpx.AsyncClient:
    app = create_worker_gateway_app()
    app.state.worker_session_factory = test_session_factory
    app.state.worker_tls_ca_certificates = (_CA_CERT,)
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_MockTlsExtension(app, pem)),
        base_url="https://worker-gateway.mock",
    )


async def _enroll(
    db_session: AsyncSession,
    test_session_factory,
    *,
    worker_id: str,
    common_name: str | None = None,
) -> tuple[Worker, str, VerifiedWorkerCertificate]:
    pem, peer = _mock_certificate(common_name=common_name or worker_id)
    _, raw_token = await issue_enrollment_token(
        db_session, worker_id=worker_id, approved_by="operator-test"
    )
    async with await _gateway_client(test_session_factory, pem) as client:
        result = await client.post(
            "/worker/v1/enrollment",
            json={
                "worker_id": worker_id,
                "enrollment_token": raw_token,
                "capabilities": _capabilities(),
                "software_version": "mock-agent/1",
                "runtime_version": "not-installed",
            },
        )
    assert result.status_code == 201, result.text
    worker = await db_session.get(Worker, worker_id)
    assert worker is not None
    await activate_worker(db_session, worker_id=worker_id, operator_id="operator-test")
    await db_session.refresh(worker)
    return worker, pem, peer


async def _eligible_fixture_version(db: AsyncSession) -> tuple[SubmissionVersion, bytes]:
    participant_id = f"part_{uuid.uuid4().hex[:16]}"
    participant = Participant(
        participant_id=participant_id,
        name=f"Worker test {uuid.uuid4().hex[:8]}",
        access_key_hash="test-only-not-a-credential",
        status=ParticipantStatus.ACTIVE,
    )
    submission = Submission(id=uuid.uuid4(), participant_id=participant_id)
    digest = hashlib.sha256(MOCK_ARCHIVE).hexdigest()
    quarantine = QuarantineObject(
        artifact_sha256=digest, artifact_size=len(MOCK_ARCHIVE), archive_bytes=MOCK_ARCHIVE
    )
    version = SubmissionVersion(
        id=uuid.uuid4(),
        submission_id=submission.id,
        participant_id=participant_id,
        version=1,
        artifact_sha256=digest,
        artifact_size=len(MOCK_ARCHIVE),
        storage_key=f"sha256:{digest}",
        manifest={"fixture": "worker-gateway-test"},
        contract_version="1",
        protocol_version="aik-jsonl/1",
        runtime_profile="python-3.12",
        state=ArtifactState.QUARANTINED,
    )
    db.add_all([participant, submission, quarantine, version])
    await db.commit()
    for from_state, to_state in (
        (ArtifactState.QUARANTINED, ArtifactState.INSPECTING),
        (ArtifactState.INSPECTING, ArtifactState.VALIDATING),
        (ArtifactState.VALIDATING, ArtifactState.APPROVED),
        (ArtifactState.APPROVED, ArtifactState.ELIGIBLE),
    ):
        await db.execute(
            insert(ArtifactStateTransition).values(
                submission_version_id=version.id,
                from_state=from_state.value,
                to_state=to_state.value,
                actor_kind="test-fixture",
                actor_id="fixture",
                reason_code="TEST_FIXTURE_ONLY",
                recorded_at=datetime.now(timezone.utc),
            )
        )
        await db.execute(
            update(SubmissionVersion)
            .where(SubmissionVersion.id == version.id)
            .values(state=to_state.value)
        )
    await db.refresh(version)
    await db.commit()
    return version, MOCK_ARCHIVE


def _test_resource_policy() -> ResourcePolicy:
    return ResourcePolicy(
        move_timeout_ms=2000,
        process_lifetime_ms=900000,
        concurrency_per_worker=1,
    )


async def _new_job(db: AsyncSession, version: SubmissionVersion) -> WorkerExecutionJob:
    return await create_execution_job(
        db,
        submission_version_id=version.id,
        initial_state={"kind": "TEST_FIXTURE", "fixture": "empty-board-context"},
        resource_policy=_test_resource_policy(),
        execution_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )


async def _claim_and_ack(client: httpx.AsyncClient) -> dict:
    claimed = await client.post("/worker/v1/jobs/claim")
    assert claimed.status_code == 200, claimed.text
    job = claimed.json()
    assert job["execution_mode"] == "MOCK_PROTOCOL_ONLY"
    ack = await client.post(
        f"/worker/v1/jobs/{job['execution_id']}/acknowledgement",
        json={
            "lease_id": job["lease"]["lease_id"],
            "fencing_token": job["lease"]["fencing_token"],
            "sequence": job["lease"]["next_sequence"],
            "accepted": True,
        },
    )
    assert ack.status_code == 200, ack.text
    job["next_sequence"] = job["lease"]["next_sequence"] + 1
    return job


@pytest.mark.asyncio
async def test_unknown_expired_and_unregistered_certificates_fail_closed(
    db_session: AsyncSession, test_session_factory
):
    _, unknown_pem, _ = await _enroll(
        db_session, test_session_factory, worker_id=f"worker-{uuid.uuid4().hex[:8]}"
    )
    unknown_peer_pem, _ = _mock_certificate(common_name="never-enrolled")
    async with await _gateway_client(test_session_factory, unknown_peer_pem) as client:
        assert (
            await client.post(
                "/worker/v1/heartbeat",
                json={
                    "software_version": "x",
                    "runtime_version": "x",
                    "capabilities": _capabilities(),
                },
            )
        ).json()["error"] == "WORKER_CERTIFICATE_UNKNOWN"
    _, expired_pem = _mock_certificate(common_name="expired", expired=True)
    async with await _gateway_client(test_session_factory, expired_pem) as client:
        result = await client.post(
            "/worker/v1/heartbeat",
            json={"software_version": "x", "runtime_version": "x", "capabilities": _capabilities()},
        )
        assert result.status_code == 401
        assert result.json()["error"] == "WORKER_MTLS_REQUIRED"
    assert unknown_pem


@pytest.mark.asyncio
async def test_enrollment_requires_token_certificate_and_consumes_token_once(
    db_session: AsyncSession, test_session_factory
):
    worker_id = f"worker-{uuid.uuid4().hex[:8]}"
    worker, raw_token = await issue_enrollment_token(
        db_session, worker_id=worker_id, approved_by="operator-test"
    )
    pem, _ = _mock_certificate(common_name=worker_id)
    payload = {
        "worker_id": worker_id,
        "enrollment_token": raw_token,
        "capabilities": _capabilities(),
        "software_version": "mock-agent/1",
        "runtime_version": "not-installed",
    }
    async with await _gateway_client(test_session_factory, None) as client:
        missing_cert = await client.post("/worker/v1/enrollment", json=payload)
        assert missing_cert.status_code == 401
    async with await _gateway_client(test_session_factory, pem) as client:
        invalid = await client.post(
            "/worker/v1/enrollment", json={**payload, "enrollment_token": "x" * 40}
        )
        assert invalid.status_code == 401
        first = await client.post("/worker/v1/enrollment", json=payload)
        assert first.status_code == 201
        replay = await client.post("/worker/v1/enrollment", json=payload)
        assert replay.status_code == 401
    await db_session.refresh(worker)
    assert worker.status == WorkerStatus.PENDING
    assert worker.pending_certificate_fingerprint
    active = await db_session.get(Worker, worker_id)
    assert active.status == WorkerStatus.PENDING
    assert active.certificate_fingerprint is None
    assert active.pending_certificate_fingerprint
    assert active.pending_expires_at is not None
    assert active.pending_certificate_fingerprint != raw_token
    rejected = (
        (
            await db_session.execute(
                select(WorkerSecurityEvent).where(
                    WorkerSecurityEvent.event_type == "WORKER_REQUEST_REJECTED"
                )
            )
        )
        .scalars()
        .all()
    )
    assert rejected
    assert all(raw_token not in repr(event.details) for event in rejected)


@pytest.mark.asyncio
async def test_expired_enrollment_token_and_wrong_worker_binding_rejected(
    db_session: AsyncSession, test_session_factory
):
    worker_id = f"worker-{uuid.uuid4().hex[:8]}"
    _, token = await issue_enrollment_token(
        db_session, worker_id=worker_id, approved_by="operator-test"
    )
    token_row = (await db_session.execute(select(WorkerEnrollmentToken))).scalar_one()
    token_row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    await db_session.commit()
    pem, _ = _mock_certificate(common_name=worker_id)
    async with await _gateway_client(test_session_factory, pem) as client:
        expired = await client.post(
            "/worker/v1/enrollment",
            json={
                "worker_id": worker_id,
                "enrollment_token": token,
                "capabilities": _capabilities(),
                "software_version": "x",
                "runtime_version": "x",
            },
        )
    assert expired.status_code == 401
    assert expired.json()["error"] == "ENROLLMENT_TOKEN_INVALID"

    other_id = f"worker-{uuid.uuid4().hex[:8]}"
    _, other_token = await issue_enrollment_token(
        db_session, worker_id=other_id, approved_by="operator-test"
    )
    async with await _gateway_client(test_session_factory, pem) as client:
        wrong = await client.post(
            "/worker/v1/enrollment",
            json={
                "worker_id": other_id,
                "enrollment_token": other_token,
                "capabilities": _capabilities(),
                "software_version": "x",
                "runtime_version": "x",
            },
        )
    assert wrong.status_code == 409
    assert wrong.json()["error"] == "CERTIFICATE_WORKER_ID_MISMATCH"


def test_peer_certificate_requires_trusted_ca():
    pem, peer = _mock_certificate(common_name="worker-valid", worker_id="worker-valid")
    assert peer is not None and peer.worker_id == "worker-valid"
    untrusted_key = ec.generate_private_key(ec.SECP256R1())
    untrusted_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "untrusted")])
    untrusted = (
        x509.CertificateBuilder()
        .subject_name(untrusted_name)
        .issuer_name(untrusted_name)
        .public_key(untrusted_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1))
        .not_valid_after(datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .sign(untrusted_key, hashes.SHA256())
    )
    scope = {"extensions": {"tls": {"client_cert_chain": [pem], "tls_version": 0x0304}}}
    assert verified_peer_from_scope(scope, (untrusted,)) is None
    assert verified_peer_from_scope(scope, (_CA_CERT,)).worker_id == "worker-valid"
    impostor_key = ec.generate_private_key(ec.SECP256R1())
    impostor_leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "worker-valid")]))
        .issuer_name(_CA_NAME)
        .public_key(impostor_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=1))
        .not_valid_after(datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName(
                [x509.UniformResourceIdentifier("urn:aik:worker:worker-valid")]
            ),
            critical=False,
        )
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .sign(impostor_key, hashes.SHA256())
    )
    invalid_chain = {
        "extensions": {
            "tls": {
                "client_cert_chain": [
                    impostor_leaf.public_bytes(serialization.Encoding.PEM).decode("ascii")
                ]
            }
        }
    }
    assert verified_peer_from_scope(invalid_chain, (_CA_CERT,)) is None


@pytest.mark.asyncio
async def test_operator_activation_and_certificate_rotation_are_atomic(
    db_session: AsyncSession, test_session_factory
):
    worker_id = f"worker-{uuid.uuid4().hex[:8]}"
    worker, old_pem, old_peer = await _enroll(db_session, test_session_factory, worker_id=worker_id)
    original_fingerprint = worker.certificate_fingerprint
    _, rotation_token = await issue_certificate_rotation_token(
        db_session, worker_id=worker_id, approved_by="rotation-operator"
    )
    new_pem, new_peer = _mock_certificate(common_name=worker_id, worker_id=worker_id)
    async with await _gateway_client(test_session_factory, new_pem) as client:
        staged = await client.post(
            "/worker/v1/enrollment",
            json={
                "worker_id": worker_id,
                "enrollment_token": rotation_token,
                "capabilities": _capabilities(),
                "software_version": "agent/rotated",
                "runtime_version": "mock/2",
            },
        )
        assert staged.status_code == 201
    await db_session.refresh(worker)
    assert worker.certificate_fingerprint == original_fingerprint
    assert worker.pending_certificate_fingerprint == new_peer.fingerprint
    assert (await authenticate_worker(db_session, old_peer)).worker_id == worker_id
    with pytest.raises(WorkerProtocolError, match="WORKER_CERTIFICATE_UNKNOWN"):
        await authenticate_worker(db_session, new_peer)

    await activate_worker(db_session, worker_id=worker_id, operator_id="rotation-operator")
    await db_session.refresh(worker)
    assert worker.certificate_fingerprint == new_peer.fingerprint
    assert worker.pending_certificate_fingerprint is None
    assert (await authenticate_worker(db_session, new_peer)).worker_id == worker_id
    with pytest.raises(WorkerProtocolError, match="WORKER_CERTIFICATE_UNKNOWN"):
        await authenticate_worker(db_session, old_peer)
    async with await _gateway_client(test_session_factory, new_pem) as client:
        replay = await client.post(
            "/worker/v1/enrollment",
            json={
                "worker_id": worker_id,
                "enrollment_token": rotation_token,
                "capabilities": _capabilities(),
                "software_version": "agent/rotated",
                "runtime_version": "mock/2",
            },
        )
    assert replay.status_code == 401


@pytest.mark.asyncio
async def test_worker_heartbeat_and_participant_bearer_are_separate(
    db_session: AsyncSession, test_session_factory
):
    worker, pem, _ = await _enroll(
        db_session, test_session_factory, worker_id=f"worker-{uuid.uuid4().hex[:8]}"
    )
    async with await _gateway_client(test_session_factory, pem) as client:
        bearer = await client.post(
            "/worker/v1/heartbeat",
            headers={"Authorization": "Bearer participant-access-key"},
            json={"software_version": "x", "runtime_version": "x", "capabilities": _capabilities()},
        )
        assert bearer.status_code == 401
        ok = await client.post(
            "/worker/v1/heartbeat",
            json={
                "software_version": "mock-agent/2",
                "runtime_version": "mock/2",
                "capabilities": _capabilities(),
                "security_state": "OK",
            },
        )
        assert ok.status_code == 200
    await db_session.refresh(worker)
    assert worker.last_heartbeat_at is not None
    assert worker.software_version == "mock-agent/2"

    async with await _gateway_client(test_session_factory, None) as client:
        denied = await client.post(
            "/worker/v1/heartbeat",
            json={"software_version": "x", "runtime_version": "x", "capabilities": _capabilities()},
        )
    assert denied.status_code == 401
    assert denied.json()["error"] == "WORKER_MTLS_REQUIRED"


@pytest.mark.asyncio
async def test_job_payload_is_scoped_acknowledged_and_has_no_authority_fields(
    db_session: AsyncSession, test_session_factory
):
    version, _ = await _eligible_fixture_version(db_session)
    job = await _new_job(db_session, version)
    _, pem, _ = await _enroll(
        db_session, test_session_factory, worker_id=f"worker-{uuid.uuid4().hex[:8]}"
    )
    async with await _gateway_client(test_session_factory, pem) as client:
        claimed = await client.post("/worker/v1/jobs/claim")
        assert claimed.status_code == 200
        payload = claimed.json()
        assert payload["execution_id"] == str(job.execution_id)
        assert payload["artifact_sha256"] == hashlib.sha256(MOCK_ARCHIVE).hexdigest()
        assert payload["participant_id"] == version.participant_id
        assert payload["execution_mode"] == "MOCK_PROTOCOL_ONLY"
        assert "result" not in payload and "winner" not in payload and "board" not in payload
        ack = await client.post(
            f"/worker/v1/jobs/{job.execution_id}/acknowledgement",
            json={
                "lease_id": payload["lease"]["lease_id"],
                "fencing_token": payload["lease"]["fencing_token"],
                "sequence": 1,
                "accepted": True,
            },
        )
        assert ack.status_code == 200
        assert ack.json()["status"] == "ACKNOWLEDGED"


@pytest.mark.asyncio
async def test_scoped_artifact_capability_one_time_transfer_and_mock_digest_verification(
    db_session: AsyncSession, test_session_factory
):
    version, fixture_bytes = await _eligible_fixture_version(db_session)
    job = await _new_job(db_session, version)
    worker, pem, _ = await _enroll(
        db_session, test_session_factory, worker_id=f"worker-{uuid.uuid4().hex[:8]}"
    )
    mock = MockWorker({hashlib.sha256(fixture_bytes).hexdigest(): fixture_bytes})
    async with await _gateway_client(test_session_factory, pem) as client:
        claimed = await _claim_and_ack(client)
        common = {
            "lease_id": claimed["lease"]["lease_id"],
            "fencing_token": claimed["lease"]["fencing_token"],
        }
        cap_response = await client.post(
            f"/worker/v1/jobs/{job.execution_id}/artifact-capability",
            json={**common, "sequence": 2},
        )
        assert cap_response.status_code == 200, cap_response.text
        capability = cap_response.json()["capability"]
        transferred = await client.get(
            f"/worker/v1/jobs/{job.execution_id}/artifact",
            params={**common, "sequence": 3},
            headers={"X-Artifact-Capability": capability},
        )
        assert transferred.status_code == 200
        assert transferred.content == fixture_bytes
        assert transferred.headers["x-artifact-sha256"] == hashlib.sha256(fixture_bytes).hexdigest()
        observation = mock.verify_fixture(
            expected_digest=transferred.headers["x-artifact-sha256"],
            artifact_bytes=transferred.content,
        )
        assert observation.digest_verified
        assert observation.security_label == "MOCKED_DOES_NOT_PROVE_SANDBOX_SECURITY"
        # Revocation fences subsequent requests. It cannot recall an already
        # completed application-layer response buffer; no wire timing is implied.
        transferred_bytes = transferred.content
        await revoke_worker(db_session, worker_id=worker.worker_id, operator_id="operator-test")
        assert transferred.content == transferred_bytes == fixture_bytes
        replay = await client.get(
            f"/worker/v1/jobs/{job.execution_id}/artifact",
            params={**common, "sequence": 4},
            headers={"X-Artifact-Capability": capability},
        )
        assert replay.status_code == 403
        assert replay.json()["error"] == "WORKER_NOT_ACTIVE"
    assert worker.worker_id == claimed["worker_id"]
    with pytest.raises(MockArtifactRejected, match="MOCK_FIXTURE_NOT_ALLOWLISTED"):
        MockWorker({}).verify_fixture(
            expected_digest=hashlib.sha256(fixture_bytes).hexdigest(), artifact_bytes=fixture_bytes
        )


@pytest.mark.asyncio
async def test_capability_cannot_cross_worker_or_execution_scope(
    db_session: AsyncSession, test_session_factory
):
    version, _ = await _eligible_fixture_version(db_session)
    job_one = await _new_job(db_session, version)
    job_two = await _new_job(db_session, version)
    worker_a, pem_a, _ = await _enroll(
        db_session, test_session_factory, worker_id=f"worker-{uuid.uuid4().hex[:8]}"
    )
    worker_b, pem_b, _ = await _enroll(
        db_session, test_session_factory, worker_id=f"worker-{uuid.uuid4().hex[:8]}"
    )
    async with await _gateway_client(test_session_factory, pem_a) as client_a:
        a = await _claim_and_ack(client_a)
        cap = await client_a.post(
            f"/worker/v1/jobs/{job_one.execution_id}/artifact-capability",
            json={
                "lease_id": a["lease"]["lease_id"],
                "fencing_token": a["lease"]["fencing_token"],
                "sequence": 2,
            },
        )
        assert cap.status_code == 200
    async with await _gateway_client(test_session_factory, pem_b) as client_b:
        b = await _claim_and_ack(client_b)
        cross_worker = await client_b.get(
            f"/worker/v1/jobs/{job_one.execution_id}/artifact",
            params={
                "lease_id": a["lease"]["lease_id"],
                "fencing_token": a["lease"]["fencing_token"],
                "sequence": 3,
            },
            headers={"X-Artifact-Capability": cap.json()["capability"]},
        )
        assert cross_worker.status_code == 409
        cross_execution = await client_b.get(
            f"/worker/v1/jobs/{job_two.execution_id}/artifact",
            params={
                "lease_id": b["lease"]["lease_id"],
                "fencing_token": b["lease"]["fencing_token"],
                "sequence": 2,
            },
            headers={"X-Artifact-Capability": cap.json()["capability"]},
        )
        assert cross_execution.status_code == 403
        assert cross_execution.json()["error"] == "ARTIFACT_CAPABILITY_SCOPE_MISMATCH"
    cap_row = (await db_session.execute(select(WorkerArtifactCapability))).scalar_one()
    cap_row.operation = "UNAUTHORIZED_OPERATION"
    await db_session.commit()
    async with await _gateway_client(test_session_factory, pem_a) as client_a:
        wrong_operation = await client_a.get(
            f"/worker/v1/jobs/{job_one.execution_id}/artifact",
            params={
                "lease_id": a["lease"]["lease_id"],
                "fencing_token": a["lease"]["fencing_token"],
                "sequence": 3,
            },
            headers={"X-Artifact-Capability": cap.json()["capability"]},
        )
    assert wrong_operation.status_code == 403
    assert wrong_operation.json()["error"] == "ARTIFACT_CAPABILITY_SCOPE_MISMATCH"
    cap_row.operation = "DOWNLOAD_ARCHIVE"
    cap_row.artifact_sha256 = "f" * 64
    await db_session.commit()
    async with await _gateway_client(test_session_factory, pem_a) as client_a:
        wrong_digest = await client_a.get(
            f"/worker/v1/jobs/{job_one.execution_id}/artifact",
            params={
                "lease_id": a["lease"]["lease_id"],
                "fencing_token": a["lease"]["fencing_token"],
                "sequence": 3,
            },
            headers={"X-Artifact-Capability": cap.json()["capability"]},
        )
    assert wrong_digest.status_code == 403
    assert wrong_digest.json()["error"] == "ARTIFACT_CAPABILITY_SCOPE_MISMATCH"
    cap_row.artifact_sha256 = job_one.artifact_sha256
    await db_session.execute(
        update(WorkerExecutionJob)
        .where(WorkerExecutionJob.execution_id == job_one.execution_id)
        .values(status=ExecutionJobStatus.CANCELED)
    )
    await db_session.commit()
    async with await _gateway_client(test_session_factory, pem_a) as client_a:
        revoked_execution = await client_a.get(
            f"/worker/v1/jobs/{job_one.execution_id}/artifact",
            params={
                "lease_id": a["lease"]["lease_id"],
                "fencing_token": a["lease"]["fencing_token"],
                "sequence": 3,
            },
            headers={"X-Artifact-Capability": cap.json()["capability"]},
        )
    assert revoked_execution.status_code == 409
    assert revoked_execution.json()["error"] == "JOB_NOT_ACKNOWLEDGED"
    assert worker_a.worker_id != worker_b.worker_id


@pytest.mark.asyncio
async def test_expired_capability_stale_fence_expired_lease_and_sequence_replay_rejected(
    db_session: AsyncSession, test_session_factory, monkeypatch
):
    version, _ = await _eligible_fixture_version(db_session)
    job = await _new_job(db_session, version)
    _, pem, _ = await _enroll(
        db_session, test_session_factory, worker_id=f"worker-{uuid.uuid4().hex[:8]}"
    )
    async with await _gateway_client(test_session_factory, pem) as client:
        claimed = await _claim_and_ack(client)
        common = {
            "lease_id": claimed["lease"]["lease_id"],
            "fencing_token": claimed["lease"]["fencing_token"],
        }
        cap = await client.post(
            f"/worker/v1/jobs/{job.execution_id}/artifact-capability",
            json={**common, "sequence": 2},
        )
        assert cap.status_code == 200
        cap_row = (await db_session.execute(select(WorkerArtifactCapability))).scalar_one()
        cap_row.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        await db_session.commit()
        expired = await client.get(
            f"/worker/v1/jobs/{job.execution_id}/artifact",
            params={**common, "sequence": 3},
            headers={"X-Artifact-Capability": cap.json()["capability"]},
        )
        assert expired.status_code == 403
        assert expired.json()["error"] == "ARTIFACT_CAPABILITY_INVALID"
        replay_ack = await client.post(
            f"/worker/v1/jobs/{job.execution_id}/acknowledgement",
            json={**common, "sequence": 1, "accepted": True},
        )
        assert replay_ack.status_code == 409

    stale_worker = await db_session.get(Worker, claimed["worker_id"])
    job_row = await db_session.get(WorkerExecutionJob, job.id)
    await db_session.refresh(job_row)
    job_row.fencing_token += 1
    await db_session.commit()
    async with await _gateway_client(test_session_factory, pem) as client:
        stale = await client.post(
            f"/worker/v1/jobs/{job.execution_id}/lease/renewal",
            json={**common, "sequence": 3},
        )
        assert stale.status_code == 409
        assert stale.json()["error"] == "STALE_FENCING_TOKEN"
        future = await client.post(
            f"/worker/v1/jobs/{job.execution_id}/lease/renewal",
            json={
                "lease_id": common["lease_id"],
                "fencing_token": common["fencing_token"] + 2,
                "sequence": 3,
            },
        )
        assert future.status_code == 409
        assert future.json()["error"] == "STALE_FENCING_TOKEN"
    assert stale_worker.status == WorkerStatus.ACTIVE


@pytest.mark.asyncio
async def test_wrong_worker_cannot_ack_or_renew_another_workers_job(
    db_session: AsyncSession, test_session_factory
):
    version, _ = await _eligible_fixture_version(db_session)
    job = await _new_job(db_session, version)
    _, pem_a, _ = await _enroll(
        db_session, test_session_factory, worker_id=f"worker-{uuid.uuid4().hex[:8]}"
    )
    _, pem_b, _ = await _enroll(
        db_session, test_session_factory, worker_id=f"worker-{uuid.uuid4().hex[:8]}"
    )
    async with await _gateway_client(test_session_factory, pem_a) as client_a:
        claimed = await client_a.post("/worker/v1/jobs/claim")
    lease = claimed.json()["lease"]
    async with await _gateway_client(test_session_factory, pem_b) as client_b:
        wrong = await client_b.post(
            f"/worker/v1/jobs/{job.execution_id}/acknowledgement",
            json={
                "lease_id": lease["lease_id"],
                "fencing_token": lease["fencing_token"],
                "sequence": 1,
                "accepted": True,
            },
        )
    assert wrong.status_code == 409
    assert wrong.json()["error"] == "EXECUTION_LEASE_MISMATCH"


@pytest.mark.asyncio
async def test_revocation_invalidates_lease_capability_and_future_heartbeat(
    db_session: AsyncSession, test_session_factory
):
    version, _ = await _eligible_fixture_version(db_session)
    job = await _new_job(db_session, version)
    worker, pem, _ = await _enroll(
        db_session, test_session_factory, worker_id=f"worker-{uuid.uuid4().hex[:8]}"
    )
    async with await _gateway_client(test_session_factory, pem) as client:
        claimed = await _claim_and_ack(client)
        cap = await client.post(
            f"/worker/v1/jobs/{job.execution_id}/artifact-capability",
            json={
                "lease_id": claimed["lease"]["lease_id"],
                "fencing_token": claimed["lease"]["fencing_token"],
                "sequence": 2,
            },
        )
        assert cap.status_code == 200
    await revoke_worker(db_session, worker_id=worker.worker_id, operator_id="operator-test")
    revoked_cap = (await db_session.execute(select(WorkerArtifactCapability))).scalar_one()
    assert revoked_cap.revoked_at is not None
    job_after = await db_session.get(WorkerExecutionJob, job.id)
    await db_session.refresh(job_after)
    assert job_after.status == ExecutionJobStatus.QUEUED
    assert job_after.fencing_token > claimed["lease"]["fencing_token"]
    async with await _gateway_client(test_session_factory, pem) as client:
        response = await client.post(
            "/worker/v1/heartbeat",
            json={"software_version": "x", "runtime_version": "x", "capabilities": _capabilities()},
        )
    assert response.status_code == 403
    assert response.json()["error"] == "WORKER_NOT_ACTIVE"
    async with await _gateway_client(test_session_factory, pem) as client:
        renew = await client.post(
            f"/worker/v1/jobs/{job.execution_id}/lease/renewal",
            json={
                "lease_id": claimed["lease"]["lease_id"],
                "fencing_token": claimed["lease"]["fencing_token"],
                "sequence": 3,
            },
        )
    assert renew.status_code == 403
    assert renew.json()["error"] == "WORKER_NOT_ACTIVE"


@pytest.mark.asyncio
async def test_unknown_execution_expired_lease_and_reassignment_fence_old_worker(
    db_session: AsyncSession, test_session_factory
):
    version, _ = await _eligible_fixture_version(db_session)
    job = await _new_job(db_session, version)
    _, pem_a, _ = await _enroll(
        db_session, test_session_factory, worker_id=f"worker-{uuid.uuid4().hex[:8]}"
    )
    _, pem_b, _ = await _enroll(
        db_session, test_session_factory, worker_id=f"worker-{uuid.uuid4().hex[:8]}"
    )
    async with await _gateway_client(test_session_factory, pem_a) as client_a:
        claimed = await _claim_and_ack(client_a)
        lease = claimed["lease"]
        missing = await client_a.post(
            f"/worker/v1/jobs/{uuid.uuid4()}/lease/renewal",
            json={
                "lease_id": lease["lease_id"],
                "fencing_token": lease["fencing_token"],
                "sequence": 2,
            },
        )
        assert missing.status_code == 404
    await db_session.execute(
        update(WorkerExecutionJob)
        .where(WorkerExecutionJob.execution_id == job.execution_id)
        .values(lease_expires_at=datetime.now(timezone.utc) - timedelta(seconds=1))
    )
    await db_session.commit()
    async with await _gateway_client(test_session_factory, pem_a) as client_a:
        expired = await client_a.post(
            f"/worker/v1/jobs/{job.execution_id}/lease/renewal",
            json={
                "lease_id": lease["lease_id"],
                "fencing_token": lease["fencing_token"],
                "sequence": 2,
            },
        )
    assert expired.status_code == 409
    assert expired.json()["error"] == "EXECUTION_LEASE_EXPIRED"
    async with await _gateway_client(test_session_factory, pem_b) as client_b:
        reassigned = await client_b.post("/worker/v1/jobs/claim")
    assert reassigned.status_code == 200
    assert reassigned.json()["execution_id"] == str(job.execution_id)
    assert reassigned.json()["lease"]["fencing_token"] > lease["fencing_token"]
    async with await _gateway_client(test_session_factory, pem_a) as client_a:
        stale = await client_a.post(
            f"/worker/v1/jobs/{job.execution_id}/evidence",
            json={
                "lease_id": lease["lease_id"],
                "fencing_token": lease["fencing_token"],
                "sequence": 2,
                "event_type": "FAILED",
            },
        )
    assert stale.status_code == 409
    assert stale.json()["error"] == "EXECUTION_LEASE_MISMATCH"


@pytest.mark.asyncio
async def test_digest_mismatch_fails_closed_and_records_security_event(
    db_session: AsyncSession, test_session_factory
):
    version, _ = await _eligible_fixture_version(db_session)
    job = await _new_job(db_session, version)
    _, pem, _ = await _enroll(
        db_session, test_session_factory, worker_id=f"worker-{uuid.uuid4().hex[:8]}"
    )
    async with await _gateway_client(test_session_factory, pem) as client:
        claimed = await _claim_and_ack(client)
        common = {
            "lease_id": claimed["lease"]["lease_id"],
            "fencing_token": claimed["lease"]["fencing_token"],
        }
        cap = await client.post(
            f"/worker/v1/jobs/{job.execution_id}/artifact-capability",
            json={**common, "sequence": 2},
        )
    # Corrupt storage below the ORM guard to simulate out-of-band disk damage.
    await db_session.execute(
        text(
            "UPDATE quarantine_objects SET archive_bytes = :bytes WHERE artifact_sha256 = :digest"
        ),
        {"bytes": b"tampered controlled fixture", "digest": version.artifact_sha256},
    )
    await db_session.commit()
    async with await _gateway_client(test_session_factory, pem) as client:
        failed = await client.get(
            f"/worker/v1/jobs/{job.execution_id}/artifact",
            params={**common, "sequence": 3},
            headers={"X-Artifact-Capability": cap.json()["capability"]},
        )
    assert failed.status_code == 409
    assert failed.json()["error"] == "ARTIFACT_INTEGRITY_FAILURE"
    job_after = await db_session.get(WorkerExecutionJob, job.id)
    await db_session.refresh(job_after)
    assert job_after.status == ExecutionJobStatus.FAILED
    event = (
        await db_session.execute(
            select(WorkerSecurityEvent).where(
                WorkerSecurityEvent.event_type == "WORKER_ARTIFACT_INTEGRITY_FAILURE"
            )
        )
    ).scalar_one()
    assert event.severity == "CRITICAL"


@pytest.mark.asyncio
async def test_evidence_is_bounded_append_only_and_cannot_set_match_result(
    db_session: AsyncSession, test_session_factory
):
    version, _ = await _eligible_fixture_version(db_session)
    job = await _new_job(db_session, version)
    _, pem, _ = await _enroll(
        db_session, test_session_factory, worker_id=f"worker-{uuid.uuid4().hex[:8]}"
    )
    async with await _gateway_client(test_session_factory, pem) as client:
        claimed = await _claim_and_ack(client)
        common = {
            "lease_id": claimed["lease"]["lease_id"],
            "fencing_token": claimed["lease"]["fencing_token"],
        }
        evidence = await client.post(
            f"/worker/v1/jobs/{job.execution_id}/evidence",
            json={
                **common,
                "sequence": 2,
                "event_type": "RESOURCE_LIMIT",
                "failure_class": "RESOURCE_EXHAUSTION",
                "resource_usage": {
                    "cpu_usec": 0,
                    "memory_current_bytes": 0,
                    "pids_current": 0,
                    "pids_peak": 0,
                },
                "resource_measurement_source": "cgroup-v2",
                "resource_measurement_scope": "execution-cgroup-host-tasks",
                "resource_measurement_container_id": f"aik-{job.execution_id.hex}",
                "resource_measurement_started_monotonic": 1.0,
                "resource_measurement_ended_monotonic": 2.0,
                "unavailable_measurements": ["filesystem_bytes", "filesystem_inodes"],
                "stdout_bytes": 42,
                "stdout_sha256": "b" * 64,
            },
        )
        assert evidence.status_code == 200
        forbidden = await client.post(
            f"/worker/v1/jobs/{job.execution_id}/evidence",
            json={
                **common,
                "sequence": 3,
                "event_type": "FAILED",
                "stdout": "raw participant output",
                "result": "WHITE_WIN",
            },
        )
        assert forbidden.status_code == 422
    stored = (await db_session.execute(select(WorkerEvidence))).scalars().all()
    assert len(stored) == 1
    assert stored[0].stdout_bytes == 42
    assert stored[0].resource_usage["memory_current_bytes"] == 0
    assert stored[0].evidence_json["measurement_source"] == "cgroup-v2"
    assert stored[0].evidence_json["measurement_container_id"] == f"aik-{job.execution_id.hex}"
    assert stored[0].evidence_json["unavailable_measurements"] == [
        "filesystem_bytes",
        "filesystem_inodes",
    ]
    assert "stdout" not in stored[0].evidence_json
    stored[0].event_type = "MUTATED"
    with pytest.raises(ValueError, match="WORKER_AUDIT_RECORD_APPEND_ONLY"):
        await db_session.flush()


@pytest.mark.asyncio
async def test_cleanup_report_requires_verified_cleanup_and_finishes_mock_job(
    db_session: AsyncSession, test_session_factory
):
    version, _ = await _eligible_fixture_version(db_session)
    job = await _new_job(db_session, version)
    _, pem, _ = await _enroll(
        db_session, test_session_factory, worker_id=f"worker-{uuid.uuid4().hex[:8]}"
    )
    async with await _gateway_client(test_session_factory, pem) as client:
        claimed = await _claim_and_ack(client)
        common = {
            "lease_id": claimed["lease"]["lease_id"],
            "fencing_token": claimed["lease"]["fencing_token"],
        }
        report = await client.post(
            f"/worker/v1/jobs/{job.execution_id}/cleanup",
            json={**common, "sequence": 2, "cleanup_verified": False, "final_status": "COMPLETED"},
        )
        assert report.status_code == 200
        assert report.json()["status"] == "CLEANUP_REPORTED"
        await db_session.refresh(job)
        assert job.status == ExecutionJobStatus.CLEANUP_REPORTED
        assert job.status != ExecutionJobStatus.COMPLETED
        finalized = await finalize_execution_job(
            db_session, execution_id=job.execution_id,
            final_status=ExecutionJobStatus.COMPLETED, actor_id="trusted-control-plane",
        )
        assert finalized.status == ExecutionJobStatus.COMPLETED


def test_worker_metadata_and_timestamp_bounds():
    base = {
        "sandbox_backend": "MOCK_TEST_ONLY", "sandbox_version": "mock/1",
        "sandbox_digest": "a" * 64,
    }
    with pytest.raises(ValidationError):
        WorkerCapabilities(**base, protocol_versions=["x" * 65])
    with pytest.raises(ValidationError):
        WorkerCapabilities(**base, protocol_versions=["v" * 64] * 8)
    valid = WorkerCapabilities(**base, protocol_versions=["aik-jsonl/1"])
    assert valid.protocol_versions == ["aik-jsonl/1"]
    lease = {"lease_id": uuid.uuid4(), "fencing_token": 1, "sequence": 1}
    with pytest.raises(ValidationError):
        EvidenceIn(**lease, event_type="STARTED",
                   started_at="2026-01-02T00:00:00+00:00",
                   ended_at="2026-01-01T00:00:00+00:00")
    with pytest.raises(ValidationError):
        EvidenceIn(**lease, event_type="STARTED", started_at="2026-01-01T00:00:00")
    # Distant time remains untrusted telemetry; server recorded_at is authoritative.
    assert EvidenceIn(**lease, event_type="STARTED",
                      started_at="2099-01-01T00:00:00+00:00").started_at.year == 2099


def test_worker_database_role_migration_is_scoped():
    migration = (
        Path(__file__).parents[2]
        / "migrations"
        / "versions"
        / "0005_worker_gateway_privileges.py"
    ).read_text(encoding="utf-8")
    assert "CREATE ROLE aik_worker_gateway NOLOGIN NOSUPERUSER NOCREATEDB" in migration
    assert "NOCREATEROLE NOBYPASSRLS NOINHERIT" in migration
    assert (
        "GRANT SELECT ON submission_versions, quarantine_objects TO aik_worker_gateway"
        in migration
    )
    assert "GRANT ALL" not in migration
    assert "participants" not in migration
    assert "participant_sessions" not in migration
    assert "pg_has_role(session_user, 'aik_worker_gateway', 'member')" in migration


@pytest.mark.asyncio
async def test_rotation_authorization_is_single_use_and_revocable(db_session: AsyncSession):
    worker_id = f"worker-{uuid.uuid4().hex[:8]}"
    now = datetime.now(timezone.utc)
    worker = Worker(
        worker_id=worker_id, status=WorkerStatus.ACTIVE,
        certificate_fingerprint="a" * 64, expires_at=now + timedelta(days=10),
        capabilities={}, security_state="UNREVIEWED",
    )
    db_session.add(worker)
    await db_session.commit()
    _, first = await issue_certificate_rotation_token(
        db_session, worker_id=worker_id, approved_by="operator-test"
    )
    with pytest.raises(WorkerProtocolError, match="WORKER_ROTATION_TOKEN_OUTSTANDING"):
        await issue_certificate_rotation_token(
            db_session, worker_id=worker_id, approved_by="operator-test"
        )
    cert = VerifiedWorkerCertificate(
        fingerprint="b" * 64, subject="CN=rotation", common_name="rotation",
        worker_id=worker_id, serial="42", not_before=now - timedelta(minutes=1),
        not_after=now + timedelta(days=30),
    )
    staged = await enroll_worker(
        db_session, worker_id=worker_id, raw_token=first, certificate=cert,
        capabilities=_capabilities(), software_version="test", runtime_version="test",
    )
    assert staged.pending_certificate_fingerprint == cert.fingerprint
    active = await activate_worker(db_session, worker_id=worker_id, operator_id="operator-test")
    assert active.certificate_fingerprint == cert.fingerprint
    assert active.pending_certificate_fingerprint is None
    with pytest.raises(WorkerProtocolError, match="ENROLLMENT_TOKEN_INVALID"):
        await enroll_worker(
            db_session, worker_id=worker_id, raw_token=first, certificate=cert,
            capabilities=_capabilities(), software_version="test", runtime_version="test",
        )
    _, second = await issue_certificate_rotation_token(
        db_session, worker_id=worker_id, approved_by="operator-test"
    )
    await revoke_worker(db_session, worker_id=worker_id, operator_id="operator-test")
    second_hash = hashlib.sha256(second.encode()).hexdigest()
    token = (await db_session.execute(
        select(WorkerEnrollmentToken).where(
            WorkerEnrollmentToken.token_hash == second_hash
        )
    )).scalar_one()
    assert token.revoked_at is not None
    with pytest.raises(WorkerProtocolError, match="ENROLLMENT_TOKEN_INVALID"):
        await enroll_worker(
            db_session, worker_id=worker_id, raw_token=second, certificate=cert,
            capabilities=_capabilities(), software_version="test", runtime_version="test",
        )


@pytest.mark.asyncio
async def test_worker_gateway_rejects_oversized_asgi_body(test_session_factory):
    async with await _gateway_client(test_session_factory, None) as client:
        response = await client.post("/worker/v1/enrollment", content=b"x" * (16 * 1024 + 1))
    assert response.status_code == 413
    assert response.json() == {"error": "REQUEST_TOO_LARGE"}


@pytest.mark.asyncio
async def test_policies_are_control_plane_owned_and_job_binding_is_immutable(
    db_session: AsyncSession, test_session_factory
):
    version, _ = await _eligible_fixture_version(db_session)
    job = await _new_job(db_session, version)
    _, pem, _ = await _enroll(
        db_session, test_session_factory, worker_id=f"worker-{uuid.uuid4().hex[:8]}"
    )
    async with await _gateway_client(test_session_factory, pem) as client:
        extra = await client.post(
            f"/worker/v1/jobs/{job.execution_id}/acknowledgement",
            json={
                "lease_id": str(uuid.uuid4()),
                "fencing_token": 1,
                "sequence": 1,
                "accepted": True,
                "resource_policy": {"memory_bytes": 1},
            },
        )
        assert extra.status_code == 422
    job.artifact_sha256 = "0" * 64
    with pytest.raises(ValueError, match="WORKER_JOB_IDENTITY_IMMUTABLE"):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_mock_worker_refuses_non_fixture_and_does_not_run_artifact():
    fixture_digest = hashlib.sha256(MOCK_ARCHIVE).hexdigest()
    mock = MockWorker({fixture_digest: MOCK_ARCHIVE})
    observed = mock.verify_fixture(expected_digest=fixture_digest, artifact_bytes=MOCK_ARCHIVE)
    assert observed.digest_verified
    assert "MOCKED" in observed.security_label
    with pytest.raises(MockArtifactRejected, match="ARTIFACT_INTEGRITY_FAILURE"):
        mock.verify_fixture(
            expected_digest=fixture_digest, artifact_bytes=MOCK_ARCHIVE + b"tampered"
        )
    with pytest.raises(MockArtifactRejected, match="MOCK_FIXTURE_NOT_ALLOWLISTED"):
        mock.verify_fixture(expected_digest="c" * 64, artifact_bytes=MOCK_ARCHIVE)


def test_mtls_contexts_require_ca_and_client_identity(tmp_path, monkeypatch):
    server_key, server_cert = _signed_certificate(
        common_name="worker-gateway", worker_id=None, server_name="worker-gateway.test"
    )
    client_key, client_cert = _signed_certificate(common_name="worker-ctx", worker_id="worker-ctx")
    server_file = tmp_path / "server.pem"
    server_key_file = tmp_path / "server-key.pem"
    client_file = tmp_path / "client.pem"
    client_key_file = tmp_path / "client-key.pem"
    ca_file = tmp_path / "ca.pem"
    server_file.write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
    server_key_file.write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    server_key_file.chmod(0o600)  # TLS loader rejects group/world-readable keys on POSIX
    client_file.write_bytes(client_cert.public_bytes(serialization.Encoding.PEM))
    client_key_file.write_bytes(
        client_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    client_key_file.chmod(0o600)  # TLS loader rejects group/world-readable keys on POSIX
    ca_file.write_bytes(_CA_CERT.public_bytes(serialization.Encoding.PEM))
    for name in (
        "WORKER_TLS_ENABLED",
        "WORKER_TLS_CA_CERT",
        "WORKER_TLS_SERVER_CERT",
        "WORKER_TLS_SERVER_KEY",
        "WORKER_TLS_SERVER_NAME",
        "WORKER_TLS_BIND_HOST",
    ):
        monkeypatch.delenv(name, raising=False)
    with pytest.raises(WorkerTLSConfigurationError, match="WORKER_TLS_ENABLED"):
        WorkerGatewayTLSConfig.from_env()
    monkeypatch.setenv("WORKER_TLS_ENABLED", "true")
    monkeypatch.setenv("WORKER_TLS_CA_CERT", str(ca_file))
    monkeypatch.setenv("WORKER_TLS_SERVER_CERT", str(server_file))
    monkeypatch.setenv("WORKER_TLS_SERVER_KEY", str(server_key_file))
    monkeypatch.setenv("WORKER_TLS_SERVER_NAME", "worker-gateway.test")
    monkeypatch.setenv("WORKER_TLS_BIND_HOST", "127.0.0.1")
    tls_config = WorkerGatewayTLSConfig.from_env()
    assert tls_config.port == 8443
    assert tls_config.build_server_ssl_context().verify_mode == 2
    server_context = server_ssl_context(
        ca_file, server_file, server_key_file, server_name="worker-gateway.test"
    )
    client_context = worker_client_ssl_context(
        ca_file,
        client_file,
        client_key_file,
        worker_id="worker-ctx",
        server_name="worker-gateway.test",
    )
    assert server_context.verify_mode == 2  # ssl.CERT_REQUIRED
    assert client_context.verify_mode == 2  # ssl.CERT_REQUIRED
    assert client_context.check_hostname
    assert server_context.minimum_version >= 771  # TLS 1.2


def test_hypercorn_listener_context_pins_tls12_and_requires_client_cert(monkeypatch):
    import ssl
    import sys
    import types

    class FakeConfig:
        def __init__(self):
            self.certfile = self.keyfile = self.ca_certs = None

        def create_ssl_context(self):
            return ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)

    package = types.ModuleType("hypercorn")
    package.__path__ = []
    config_module = types.ModuleType("hypercorn.config")
    config_module.Config = FakeConfig
    logging_module = types.ModuleType("hypercorn.logging")

    class FakeLogger:
        messages = []

        def __init__(self, *args, **kwargs):
            pass

        def error(self, message, *args, **kwargs):
            self.messages.append(message)

        def warning(self, message, *args, **kwargs):
            self.messages.append(message)

    logging_module.Logger = FakeLogger
    monkeypatch.setitem(sys.modules, "hypercorn", package)
    monkeypatch.setitem(sys.modules, "hypercorn.config", config_module)
    monkeypatch.setitem(sys.modules, "hypercorn.logging", logging_module)
    from app.workers.serve import build_hypercorn_config

    config = build_hypercorn_config(WorkerGatewayTLSConfig(
        ca_certificate=Path("ca.pem"),
        server_certificate=Path("server.pem"),
        server_private_key=Path("server-key.pem"),
        server_name="worker-gateway.test", bind_host="127.0.0.1", port=8443,
    ))
    context = config.create_ssl_context()
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2
    assert context.verify_mode == ssl.CERT_REQUIRED
    assert config.accesslog is None
    assert config.insecure_bind == []
    assert config.logger_class.__name__ == "BoundedWorkerLogger"
    logger = config.logger_class()
    logger.error("raw certificate and credential details must not be forwarded")
    assert FakeLogger.messages[-1] == "WORKER_LISTENER_ERROR"


@pytest.mark.asyncio
async def test_worker_gateway_routes_are_not_mounted_on_participant_api(api_client):
    response = await api_client.post("/worker/v1/jobs/claim")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_control_authority_match_purpose_authorization_and_fencing(
    db_session: AsyncSession, test_session_factory
):
    from app.models.event import TournamentPairingRecord, TournamentRoundRecord
    from app.models.match import (
        Match,
        MatchPurpose,
        MatchSide,
        MatchSideColor,
        MatchSideKind,
        MatchState,
        ParticipantMatchSideBinding,
    )
    from app.models.worker import ExecutionKind, ExecutionJobStatus, WorkerExecutionJob
    from app.workers.gateway import _control_authority

    worker, pem, _ = await _enroll(
        db_session, test_session_factory, worker_id=f"worker-{uuid.uuid4().hex[:8]}"
    )
    version, _ = await _eligible_fixture_version(db_session)
    now = datetime.now(timezone.utc)

    match_id = uuid.uuid4()
    match = Match(
        id=match_id,
        state=MatchState.QUEUED,
        purpose=MatchPurpose.OFFICIAL,
        protocol_version="aik-jsonl/1",
        runtime_profile="python-3.12",
        policy_version="development-v1",
        rules_version="standard-chess/python-chess-1.11.2",
        match_seed="0" * 64,
        initial_fen="rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        current_fen="rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        side_to_move=MatchSideColor.WHITE,
        current_ply=0,
        initial_clock_ms=180000,
        increment_ms=2000,
        move_timeout_ms=3000,
        white_remaining_ms=180000,
        black_remaining_ms=180000,
        created_at=now,
        started_at=None,
    )
    db_session.add(match)

    side_id = uuid.uuid4()
    side = MatchSide(
        id=side_id,
        match_id=match_id,
        color=MatchSideColor.WHITE,
        kind=MatchSideKind.PARTICIPANT,
        protocol_version="aik-jsonl/1",
        runtime_profile="python-3.12",
        created_at=now,
    )
    binding = ParticipantMatchSideBinding(
        match_side_id=side_id,
        participant_id=version.participant_id,
        submission_id=version.submission_id,
        submission_version_id=version.id,
        artifact_sha256=version.artifact_sha256,
    )
    db_session.add_all([side, binding])

    lease_id = uuid.uuid4()
    execution_id = uuid.uuid4()
    job = WorkerExecutionJob(
        id=uuid.uuid4(),
        execution_id=execution_id,
        match_id=match_id,
        execution_kind=ExecutionKind.PARTICIPANT,
        participant_id=version.participant_id,
        submission_id=version.submission_id,
        submission_version=1,
        submission_version_id=version.id,
        artifact_sha256=version.artifact_sha256,
        immutable_artifact_ref=version.artifact_sha256,
        runtime_profile="python-3.12",
        protocol_version="aik-jsonl/1",
        execution_mode="KATA_AI_EXECUTION",
        initial_state={"purpose": "OFFICIAL"},
        resource_policy={"move_timeout_ms": 2000, "process_lifetime_ms": 900000, "concurrency_per_worker": 1},
        execution_expires_at=now + timedelta(minutes=5),
        status=ExecutionJobStatus.ACKNOWLEDGED,
        assigned_worker_id=worker.worker_id,
        lease_id=lease_id,
        fencing_token=1,
        lease_expires_at=now + timedelta(minutes=1),
        last_sequence=0,
    )
    db_session.add(job)
    await db_session.commit()

    # 1. Unauthorized official match (no TournamentPairingRecord) -> 403 UNAUTHORIZED_OFFICIAL_MATCH
    with pytest.raises(WorkerProtocolError) as exc_unauth:
        await _control_authority(
            db_session,
            worker=worker,
            execution_id=execution_id,
            lease_id=lease_id,
            fence=1,
            attempt=1,
        )
    assert exc_unauth.value.status_code == 403
    assert exc_unauth.value.code == "UNAUTHORIZED_OFFICIAL_MATCH"

    # 2. Authorized official match (with TournamentPairingRecord) -> succeeds
    round_record = TournamentRoundRecord(
        id=uuid.uuid4(),
        tournament_id="official-v1",
        stage="SWISS",
        round_number=1,
        round_name="Swiss Round 1",
        status="IN_PROGRESS",
    )
    db_session.add(round_record)
    pairing = TournamentPairingRecord(
        id=uuid.uuid4(),
        round_id=round_record.id,
        pairing_index=1,
        stage_name="SWISS",
        participant1_id=version.participant_id,
        participant2_id=version.participant_id,
        game1_match_id=match_id,
        status="QUEUED",
    )
    db_session.add(pairing)
    await db_session.commit()

    res = await _control_authority(
        db_session,
        worker=worker,
        execution_id=execution_id,
        lease_id=lease_id,
        fence=1,
        attempt=1,
    )
    assert res["match_id"] == str(match_id)
    assert res["worker_id"] == worker.worker_id
    assert res["fence_token"] == 1

    # 3. Conflicting fencing token -> 409 WORKER_FENCE_STALE
    with pytest.raises(WorkerProtocolError) as exc_stale:
        await _control_authority(
            db_session,
            worker=worker,
            execution_id=execution_id,
            lease_id=lease_id,
            fence=2,
            attempt=1,
        )
    assert exc_stale.value.status_code == 409
    assert exc_stale.value.code == "STALE_FENCING_TOKEN"

    # 4. Expired lease -> 409 EXECUTION_LEASE_EXPIRED
    job.lease_expires_at = now - timedelta(seconds=1)
    await db_session.commit()
    with pytest.raises(WorkerProtocolError) as exc_expired:
        await _control_authority(
            db_session,
            worker=worker,
            execution_id=execution_id,
            lease_id=lease_id,
            fence=1,
            attempt=1,
        )
    assert exc_expired.value.status_code == 409
    assert exc_expired.value.code == "EXECUTION_LEASE_EXPIRED"

    # Reset lease
    job.lease_expires_at = now + timedelta(minutes=1)
    await db_session.commit()

    # 5. Purpose mismatch -> 409 MATCH_PURPOSE_MISMATCH
    match2_id = uuid.uuid4()
    match2 = Match(
        id=match2_id,
        state=MatchState.QUEUED,
        purpose=MatchPurpose.OFFICIAL,
        protocol_version="aik-jsonl/1",
        runtime_profile="python-3.12",
        policy_version="development-v1",
        rules_version="standard-chess/python-chess-1.11.2",
        match_seed="1" * 64,
        initial_fen="rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        current_fen="rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        side_to_move=MatchSideColor.WHITE,
        current_ply=0,
        initial_clock_ms=180000,
        increment_ms=2000,
        move_timeout_ms=3000,
        white_remaining_ms=180000,
        black_remaining_ms=180000,
        created_at=now,
        started_at=None,
    )
    side2_id = uuid.uuid4()
    side2 = MatchSide(
        id=side2_id,
        match_id=match2_id,
        color=MatchSideColor.WHITE,
        kind=MatchSideKind.PARTICIPANT,
        protocol_version="aik-jsonl/1",
        runtime_profile="python-3.12",
        created_at=now,
    )
    binding2 = ParticipantMatchSideBinding(
        match_side_id=side2_id,
        participant_id=version.participant_id,
        submission_id=version.submission_id,
        submission_version_id=version.id,
        artifact_sha256=version.artifact_sha256,
    )
    db_session.add_all([match2, side2, binding2])

    mismatch_job = WorkerExecutionJob(
        id=uuid.uuid4(),
        execution_id=uuid.uuid4(),
        match_id=match2_id,
        execution_kind=ExecutionKind.PARTICIPANT,
        participant_id=version.participant_id,
        submission_id=version.submission_id,
        submission_version=1,
        submission_version_id=version.id,
        artifact_sha256=version.artifact_sha256,
        immutable_artifact_ref=version.artifact_sha256,
        runtime_profile="python-3.12",
        protocol_version="aik-jsonl/1",
        execution_mode="KATA_AI_EXECUTION",
        initial_state={"purpose": "DEVELOPMENT"},
        resource_policy={"move_timeout_ms": 2000, "process_lifetime_ms": 900000, "concurrency_per_worker": 1},
        execution_expires_at=now + timedelta(minutes=5),
        status=ExecutionJobStatus.ACKNOWLEDGED,
        assigned_worker_id=worker.worker_id,
        lease_id=uuid.uuid4(),
        fencing_token=1,
        lease_expires_at=now + timedelta(minutes=1),
        last_sequence=0,
    )
    db_session.add(mismatch_job)
    await db_session.commit()
    with pytest.raises(WorkerProtocolError) as exc_mismatch:
        await _control_authority(
            db_session,
            worker=worker,
            execution_id=mismatch_job.execution_id,
            lease_id=mismatch_job.lease_id,
            fence=1,
            attempt=1,
        )
    assert exc_mismatch.value.status_code == 409
    assert exc_mismatch.value.code == "MATCH_PURPOSE_MISMATCH"

    # 6. Unsupported match purpose -> 409 UNSUPPORTED_MATCH_PURPOSE
    from unittest.mock import AsyncMock, MagicMock
    fake_job = MagicMock()
    fake_job.execution_id = mismatch_job.execution_id
    fake_job.assigned_worker_id = worker.worker_id
    fake_job.lease_id = mismatch_job.lease_id
    fake_job.fencing_token = 1
    fake_job.lease_expires_at = now + timedelta(minutes=1)
    fake_job.execution_expires_at = now + timedelta(minutes=5)
    fake_job.status = ExecutionJobStatus.ACKNOWLEDGED
    fake_job.match_id = match2_id
    fake_job.execution_kind = ExecutionKind.PARTICIPANT
    fake_job.initial_state = {"purpose": "CUSTOM_PURPOSE"}

    mock_db = AsyncMock()
    mock_db.scalar.side_effect = [fake_job, side2_id, 1]
    fake_match = MagicMock()
    fake_match.purpose = "CUSTOM_PURPOSE"
    mock_db.get.side_effect = [side2, fake_match]
    with pytest.raises(WorkerProtocolError) as exc_unsupported:
        await _control_authority(
            mock_db,
            worker=worker,
            execution_id=fake_job.execution_id,
            lease_id=fake_job.lease_id,
            fence=1,
            attempt=1,
        )
    assert exc_unsupported.value.status_code == 409
    assert exc_unsupported.value.code == "UNSUPPORTED_MATCH_PURPOSE"

