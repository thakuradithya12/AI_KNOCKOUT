"""Exercise the real gateway and coordinator control APIs across the DB boundary.

The worker-facing ASGI TLS extension is synthesized like the existing worker
gateway integration suite. The gateway-to-coordinator hop uses a real local
HTTPS listener with mutual TLS and the production control-service app.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import ssl
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from hypercorn.config import Config
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.matches.control_api import create_control_plane_app
from app.matches.control_service import DatabaseControlService
from app.matches.serve import serve_with_verified_peer_identity
from app.models.match import Match, MatchMove, MatchState
from app.models.worker import Worker, WorkerStatus
from app.workers.gateway import create_worker_gateway_app
from app.workers.tls import GatewayServiceClientTLSConfig
from tests.integration.test_development_matches import (
    _configured_house_ai,
    _eligible_submission,
)


def _ca():
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "control-path-test-ca")])
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=3))
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
        .sign(key, hashes.SHA256())
    )
    return key, certificate


def _leaf(ca_key, ca_cert, *, common_name, uri=None, dns=None, ip=None, eku):
    import ipaddress
    key = ec.generate_private_key(ec.SECP256R1())
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    sans = []
    if uri:
        sans.append(x509.UniformResourceIdentifier(uri))
    if dns:
        sans.append(x509.DNSName(dns))
    if ip:
        sans.append(x509.IPAddress(ipaddress.ip_address(ip)))
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName(sans), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([eku]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    return key, certificate


def _write_identity(tmp_path: Path, prefix: str, key, cert) -> tuple[Path, Path]:
    cert_path = tmp_path / f"{prefix}.crt"
    key_path = tmp_path / f"{prefix}.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ))
    key_path.chmod(0o600)  # TLS loader rejects group/world-readable keys on POSIX
    return cert_path, key_path


class _WorkerCertificateTransport:
    """Pass a real test certificate to the app's ASGI TLS identity verifier."""

    def __init__(self, app, certificate_pem: str):
        self.app = app
        self.certificate_pem = certificate_pem

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            scope = dict(scope)
            extensions = dict(scope.get("extensions", {}))
            extensions["tls"] = {
                "spec_version": "0.3",
                "client_cert_chain": [self.certificate_pem],
            }
            scope["extensions"] = extensions
        await self.app(scope, receive, send)


async def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.integration
async def test_development_match_worker_control_vertical_path(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    test_session_factory,
    tmp_path: Path,
):
    pytest.importorskip("chess")

    # The real participant API creates the queued development match and its two jobs.
    await _configured_house_ai(db_session)
    _participant_id, headers, version = await _eligible_submission(api_client, db_session)
    created = await api_client.post(
        "/api/v1/participants/me/development-matches",
        headers={**headers, "Idempotency-Key": f"control-{uuid.uuid4().hex}"},
        json={"submission_id": str(version.submission_id), "version": version.version},
    )
    assert created.status_code == 201, created.text
    match_id = uuid.UUID(created.json()["match_id"])

    # Generate isolated worker and gateway service identities for this local test.
    worker_ca_key, worker_ca_cert = _ca()
    worker_ca_path = tmp_path / "worker-ca.crt"
    worker_ca_path.write_bytes(worker_ca_cert.public_bytes(serialization.Encoding.PEM))
    worker_identities = {}
    async with db_session.begin_nested():
        for index in (1, 2):
            worker_id = f"control-worker-{index}-{uuid.uuid4().hex[:8]}"
            key, cert = _leaf(
                worker_ca_key,
                worker_ca_cert,
                common_name=worker_id,
                uri=f"urn:aik:worker:{worker_id}",
                eku=ExtendedKeyUsageOID.CLIENT_AUTH,
            )
            worker_identities[worker_id] = (key, cert)
            db_session.add(Worker(
                worker_id=worker_id,
                certificate_fingerprint=hashlib.sha256(
                    cert.public_bytes(serialization.Encoding.DER)
                ).hexdigest(),
                certificate_identity=cert.subject.rfc4514_string(),
                certificate_serial=format(cert.serial_number, "x"),
                status=WorkerStatus.ACTIVE,
                issued_at=datetime.now(timezone.utc),
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                capabilities={
                    "sandbox_backend": "KATA",
                    "protocol_versions": ["aik-jsonl/1"],
                    "execution_modes": ["KATA_AI_EXECUTION"],
                },
                software_version="control-path-test",
                runtime_version="test",
                security_state="HEALTHY",
            ))
    await db_session.commit()

    service_ca_key, service_ca_cert = _ca()
    service_ca_path = tmp_path / "service-ca.crt"
    service_ca_path.write_bytes(service_ca_cert.public_bytes(serialization.Encoding.PEM))
    server_key, server_cert = _leaf(
        service_ca_key, service_ca_cert, common_name="coordinator",
        uri="urn:aik:service:match-coordinator", dns="localhost", ip="127.0.0.1",
        eku=ExtendedKeyUsageOID.SERVER_AUTH,
    )
    gateway_key, gateway_cert = _leaf(
        service_ca_key, service_ca_cert, common_name="worker-gateway",
        uri="urn:aik:service:worker-gateway", eku=ExtendedKeyUsageOID.CLIENT_AUTH,
    )
    server_cert_path, server_key_path = _write_identity(
        tmp_path, "coordinator", server_key, server_cert
    )
    gateway_cert_path, gateway_key_path = _write_identity(
        tmp_path, "gateway-service", gateway_key, gateway_cert
    )

    control_service = DatabaseControlService(test_session_factory)
    control_app = create_control_plane_app(control_service, (service_ca_cert,))

    # Direct app access and an unrelated worker certificate cannot authorize control calls.
    unauthenticated = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=control_app), base_url="http://control.local"
    )
    unauthorized = await unauthenticated.post(
        "/internal/v1/commands/issue",
        json={
            "match_id": str(match_id), "execution_id": str(uuid.uuid4()),
            "side_id": str(uuid.uuid4()), "worker_id": "forged-worker",
            "worker_certificate_fingerprint": "0" * 64, "side": "WHITE",
            "lease_id": str(uuid.uuid4()), "fence_token": 1, "attempt": 1,
        },
    )
    assert unauthorized.status_code == 401
    await unauthenticated.aclose()
    untrusted_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(
            app=_WorkerCertificateTransport(
                control_app,
                worker_identities[next(iter(worker_identities))][1]
                .public_bytes(serialization.Encoding.PEM).decode("ascii"),
            )
        ),
        base_url="https://control.local",
    )
    untrusted = await untrusted_client.post(
        "/internal/v1/commands/issue",
        json={
            "match_id": str(match_id), "execution_id": str(uuid.uuid4()),
            "side_id": str(uuid.uuid4()), "worker_id": "forged-worker",
            "worker_certificate_fingerprint": "0" * 64, "side": "WHITE",
            "lease_id": str(uuid.uuid4()), "fence_token": 1, "attempt": 1,
        },
    )
    assert untrusted.status_code == 401
    await untrusted_client.aclose()

    port = await _free_port()
    config = Config()
    config.bind = [f"127.0.0.1:{port}"]
    config.certfile = str(server_cert_path)
    config.keyfile = str(server_key_path)
    config.ca_certs = str(service_ca_path)
    config.verify_mode = ssl.CERT_REQUIRED
    config.workers = 1
    config.use_reloader = False
    config.accesslog = None
    config.errorlog = None
    shutdown = asyncio.Event()
    listener = asyncio.create_task(
        serve_with_verified_peer_identity(
            control_app, config, shutdown_trigger=shutdown.wait
        )
    )

    gateway_app = create_worker_gateway_app()
    gateway_app.state.worker_session_factory = test_session_factory
    gateway_app.state.worker_tls_ca_certificates = (worker_ca_cert,)
    service_client_tls = GatewayServiceClientTLSConfig(
        service_ca_path, gateway_cert_path, gateway_key_path, "127.0.0.1"
    )
    gateway_app.state.match_coordinator_url = f"https://127.0.0.1:{port}"
    gateway_app.state.match_coordinator_ssl_context = service_client_tls.build_ssl_context()

    clients = {}
    try:
        for _ in range(80):
            if listener.done():
                await listener
            try:
                reader, writer = await asyncio.open_connection(
                    "127.0.0.1", port, ssl=gateway_app.state.match_coordinator_ssl_context,
                    server_hostname="127.0.0.1",
                )
                writer.close()
                await writer.wait_closed()
                break
            except OSError:
                await asyncio.sleep(0.025)
        else:
            pytest.fail("private control listener did not start")

        from app.models.worker import WorkerExecutionJob

        jobs = (await db_session.execute(
            select(WorkerExecutionJob).where(WorkerExecutionJob.match_id == match_id)
        )).scalars().all()
        assert len(jobs) == 2
        await db_session.commit()
        for worker_id, (_key, cert) in worker_identities.items():
            transport = httpx.ASGITransport(
                app=_WorkerCertificateTransport(
                    gateway_app, cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
                )
            )
            client = httpx.AsyncClient(transport=transport, base_url="https://gateway.local")
            claimed = await client.post("/worker/v1/jobs/claim")
            assert claimed.status_code == 200, claimed.text
            job_out = claimed.json()
            assert job_out["execution_id"]
            lease = job_out["lease"]
            acknowledged = await client.post(
                f"/worker/v1/jobs/{job_out['execution_id']}/acknowledgement",
                json={
                    "lease_id": lease["lease_id"],
                    "fencing_token": lease["fencing_token"],
                    "sequence": lease["next_sequence"],
                    "accepted": True,
                },
            )
            assert acknowledged.status_code == 200, acknowledged.text
            clients[job_out["execution_id"]] = (client, job_out)

        # The worker protocol forbids a client from overriding its certificate-derived ID.
        client, job_out = next(iter(clients.values()))
        forged_worker_request = await client.post(
            f"/worker/v1/jobs/{job_out['execution_id']}/match/next",
            json={
                "lease_id": job_out["lease"]["lease_id"],
                "fencing_token": job_out["lease"]["fencing_token"],
                "attempt": job_out["attempt_number"],
                "worker_id": "another-workers-id",
            },
        )
        assert forged_worker_request.status_code == 422

        invalid_identity_tested = set()

        async def poll_and_answer(client, job_out):
            execution_id = job_out["execution_id"]
            lease = job_out["lease"]
            endpoint = f"/worker/v1/jobs/{execution_id}/match/next"
            request = {
                "lease_id": lease["lease_id"],
                "fencing_token": lease["fencing_token"],
                "attempt": job_out["attempt_number"],
            }
            if execution_id not in invalid_identity_tested:
                invalid = dict(request, fencing_token=lease["fencing_token"] + 1)
                rejected = await client.post(endpoint, json=invalid)
                assert rejected.status_code == 409
                invalid_identity_tested.add(execution_id)
            response = await client.post(endpoint, json=request)
            assert response.status_code == 200, response.text
            command = response.json()
            if command["status"] != "ACCEPTED":
                return False
            message = json.loads(command["message"])
            response_type = {
                "hello": "ready", "new_game": "ack", "position": "ack", "go": "bestmove"
            }[message["type"]]
            aik_response = {
                "protocol_version": "aik-jsonl/1",
                "type": response_type,
                "request_id": command["command_id"],
            }
            if message["type"] == "go":
                async with test_session_factory() as db:
                    current = await db.get(Match, match_id)
                    ply = current.current_ply
                sequence = ["e2e4", "e7e5", "d1h5", "b8c6", "f1c4", "g8f6", "h5f7"]
                aik_response["move"] = sequence[ply]
            submitted = await client.post(
                f"/worker/v1/jobs/{execution_id}/match/response",
                json={**request, "command_id": command["command_id"],
                      "payload": json.dumps(aik_response, separators=(",", ":")) + "\n"},
            )
            assert submitted.status_code == 200, submitted.text
            return True

        # Iterate through the normal gateway endpoints until the seven-ply mate is persisted.
        for _ in range(160):
            progressed = False
            for client, job_out in clients.values():
                progressed = await poll_and_answer(client, job_out) or progressed
            async with test_session_factory() as db:
                current = await db.get(Match, match_id)
                if current.state == MatchState.COMPLETED:
                    break
            assert progressed or current.state == MatchState.RUNNING
        else:
            pytest.fail("development match did not reach its persisted terminal result")

        async with test_session_factory() as db:
            persisted = await db.get(Match, match_id)
            moves = (await db.execute(
                select(MatchMove).where(MatchMove.match_id == match_id)
            )).scalars().all()
            assert persisted.state == MatchState.COMPLETED
            assert persisted.current_ply == 7
            assert persisted.current_fen != persisted.initial_fen
            assert persisted.result_outcome == "WHITE_WIN"
            assert persisted.result_reason == "CHECKMATE"
            assert len(moves) == 7
    finally:
        await asyncio.gather(*(client.aclose() for client, _job in clients.values()))
        shutdown.set()
        await listener


@pytest.mark.integration
async def test_trusted_house_ai_duel_worker_control_vertical_path(
    db_session: AsyncSession,
    test_session_factory,
    tmp_path: Path,
):
    """Verify that a queued trusted House AI duel executes to completion through Worker Gateway and Coordinator."""
    pytest.importorskip("chess")

    from app.models.participant import Participant, ParticipantStatus
    from app.services.development_matches import request_trusted_house_development_match
    from app.services.house_ai_seeding import seed_all_house_ais

    await seed_all_house_ais(db_session)
    participant_id = f"trusted_runner_{uuid.uuid4().hex[:8]}"
    db_session.add(
        Participant(
            participant_id=participant_id,
            name="Trusted Runner",
            status=ParticipantStatus.ACTIVE,
            access_key_hash=hashlib.sha256(b"dummy").hexdigest(),
            created_at=datetime.now(timezone.utc),
        )
    )
    await db_session.commit()

    match, _created = await request_trusted_house_development_match(
        db_session,
        participant_id=participant_id,
        idempotency_key=f"ctrl-duel-{uuid.uuid4().hex}",
        white_house_ai_id="house-ai-benchmark-baseline",
        black_house_ai_id="house-ai-maximum-difficulty",
    )
    await db_session.commit()
    match_id = match.id

    worker_ca_key, worker_ca_cert = _ca()
    worker_ca_path = tmp_path / "worker-ca.crt"
    worker_ca_path.write_bytes(worker_ca_cert.public_bytes(serialization.Encoding.PEM))
    worker_identities = {}
    async with db_session.begin_nested():
        for index in (1, 2):
            worker_id = f"control-house-worker-{index}-{uuid.uuid4().hex[:8]}"
            key, cert = _leaf(
                worker_ca_key,
                worker_ca_cert,
                common_name=worker_id,
                uri=f"urn:aik:worker:{worker_id}",
                eku=ExtendedKeyUsageOID.CLIENT_AUTH,
            )
            worker_identities[worker_id] = (key, cert)
            db_session.add(
                Worker(
                    worker_id=worker_id,
                    certificate_fingerprint=hashlib.sha256(
                        cert.public_bytes(serialization.Encoding.DER)
                    ).hexdigest(),
                    certificate_identity=cert.subject.rfc4514_string(),
                    certificate_serial=format(cert.serial_number, "x"),
                    status=WorkerStatus.ACTIVE,
                    issued_at=datetime.now(timezone.utc),
                    expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                    capabilities={
                        "sandbox_backend": "KATA",
                        "protocol_versions": ["aik-jsonl/1"],
                        "execution_modes": ["KATA_AI_EXECUTION"],
                    },
                    software_version="control-path-test",
                    runtime_version="test",
                    security_state="HEALTHY",
                )
            )
    await db_session.commit()

    service_ca_key, service_ca_cert = _ca()
    service_ca_path = tmp_path / "service-ca.crt"
    service_ca_path.write_bytes(service_ca_cert.public_bytes(serialization.Encoding.PEM))
    server_key, server_cert = _leaf(
        service_ca_key,
        service_ca_cert,
        common_name="coordinator",
        uri="urn:aik:service:match-coordinator",
        dns="localhost",
        ip="127.0.0.1",
        eku=ExtendedKeyUsageOID.SERVER_AUTH,
    )
    server_cert_path = tmp_path / "coordinator.crt"
    server_key_path = tmp_path / "coordinator.key"
    server_cert_path.write_bytes(server_cert.public_bytes(serialization.Encoding.PEM))
    server_key_path.write_bytes(
        server_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    server_key_path.chmod(0o600)  # TLS loader rejects group/world-readable keys on POSIX

    gateway_key, gateway_cert = _leaf(
        service_ca_key,
        service_ca_cert,
        common_name="worker-gateway",
        uri="urn:aik:service:worker-gateway",
        eku=ExtendedKeyUsageOID.CLIENT_AUTH,
    )
    gateway_cert_path = tmp_path / "gateway.crt"
    gateway_key_path = tmp_path / "gateway.key"
    gateway_cert_path.write_bytes(gateway_cert.public_bytes(serialization.Encoding.PEM))
    gateway_key_path.write_bytes(
        gateway_key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    gateway_key_path.chmod(0o600)  # TLS loader rejects group/world-readable keys on POSIX

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        coordinator_port = probe.getsockname()[1]

    control_service = DatabaseControlService(test_session_factory)
    control_app = create_control_plane_app(control_service, (service_ca_cert,))
    hypercorn_config = Config()
    hypercorn_config.bind = [f"127.0.0.1:{coordinator_port}"]
    hypercorn_config.certfile = str(server_cert_path)
    hypercorn_config.keyfile = str(server_key_path)
    hypercorn_config.ca_certs = str(service_ca_path)
    hypercorn_config.verify_mode = ssl.CERT_REQUIRED
    hypercorn_config.workers = 1
    hypercorn_config.use_reloader = False
    hypercorn_config.accesslog = None
    hypercorn_config.errorlog = None

    shutdown = asyncio.Event()
    listener = asyncio.create_task(
        serve_with_verified_peer_identity(
            control_app, hypercorn_config, shutdown_trigger=shutdown.wait
        )
    )

    gateway_app = create_worker_gateway_app()
    gateway_app.state.worker_session_factory = test_session_factory
    gateway_app.state.worker_tls_ca_certificates = (worker_ca_cert,)
    service_client_tls = GatewayServiceClientTLSConfig(
        service_ca_path, gateway_cert_path, gateway_key_path, "127.0.0.1"
    )
    gateway_app.state.match_coordinator_url = f"https://127.0.0.1:{coordinator_port}"
    gateway_app.state.match_coordinator_ssl_context = service_client_tls.build_ssl_context()

    for _ in range(50):
        try:
            reader, writer = await asyncio.open_connection(
                "127.0.0.1",
                coordinator_port,
                ssl=gateway_app.state.match_coordinator_ssl_context,
                server_hostname="127.0.0.1",
            )
            writer.close()
            await writer.wait_closed()
            break
        except OSError:
            await asyncio.sleep(0.05)
    else:
        shutdown.set()
        await listener
        raise RuntimeError("coordinator TLS listener did not accept connections")

    clients = {}
    try:
        for worker_id, (_key, cert) in worker_identities.items():
            cert_pem = cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
            transport = httpx.ASGITransport(
                app=_WorkerCertificateTransport(gateway_app, cert_pem)
            )
            client = httpx.AsyncClient(transport=transport, base_url="https://gateway.local")
            claim = await client.post("/worker/v1/jobs/claim")
            assert claim.status_code == 200, claim.text
            job_out = claim.json()
            assert job_out["match_id"] == str(match_id)
            ack = await client.post(
                f"/worker/v1/jobs/{job_out['execution_id']}/acknowledgement",
                json={
                    "lease_id": job_out["lease"]["lease_id"],
                    "fencing_token": job_out["lease"]["fencing_token"],
                    "sequence": job_out["lease"]["next_sequence"],
                    "accepted": True,
                },
            )
            assert ack.status_code == 200, ack.text
            clients[job_out["execution_id"]] = (client, job_out)

        assert len(clients) == 2

        async def poll_and_answer(client: httpx.AsyncClient, job_out: dict) -> bool:
            execution_id = job_out["execution_id"]
            lease = job_out["lease"]
            endpoint = f"/worker/v1/jobs/{execution_id}/match/next"
            request = {
                "lease_id": lease["lease_id"],
                "fencing_token": lease["fencing_token"],
                "attempt": job_out["attempt_number"],
            }
            response = await client.post(endpoint, json=request)
            assert response.status_code == 200, response.text
            command = response.json()
            if command["status"] != "ACCEPTED":
                return False
            message = json.loads(command["message"])
            response_type = {
                "hello": "ready",
                "new_game": "ack",
                "position": "ack",
                "go": "bestmove",
            }[message["type"]]
            aik_response = {
                "protocol_version": "aik-jsonl/1",
                "type": response_type,
                "request_id": command["command_id"],
            }
            if message["type"] == "go":
                async with test_session_factory() as db:
                    current = await db.get(Match, match_id)
                    ply = current.current_ply
                sequence = ["e2e4", "e7e5", "d1h5", "b8c6", "f1c4", "g8f6", "h5f7"]
                aik_response["move"] = sequence[ply]
            submitted = await client.post(
                f"/worker/v1/jobs/{execution_id}/match/response",
                json={
                    **request,
                    "command_id": command["command_id"],
                    "payload": json.dumps(aik_response, separators=(",", ":")) + "\n",
                },
            )
            assert submitted.status_code == 200, submitted.text
            return True

        for _ in range(160):
            progressed = False
            for client, job_out in clients.values():
                progressed = await poll_and_answer(client, job_out) or progressed
            async with test_session_factory() as db:
                current = await db.get(Match, match_id)
                if current.state == MatchState.COMPLETED:
                    break
            assert progressed or current.state == MatchState.RUNNING
        else:
            pytest.fail("House AI duel did not reach its persisted terminal result")

        async with test_session_factory() as db:
            persisted = await db.get(Match, match_id)
            moves = (
                await db.execute(select(MatchMove).where(MatchMove.match_id == match_id))
            ).scalars().all()
            assert persisted.state == MatchState.COMPLETED
            assert persisted.current_ply == 7
            assert persisted.current_fen != persisted.initial_fen
            assert persisted.result_outcome == "WHITE_WIN"
            assert persisted.result_reason == "CHECKMATE"
            assert len(moves) == 7
    finally:
        await asyncio.gather(*(client.aclose() for client, _job in clients.values()))
        shutdown.set()
        await listener

