"""Integration test for full end-to-end participant-vs-participant chess match execution.

Validates:
1. Two distinct participants register and submit valid chess agents.
2. Both submissions are validated, approved, and marked ELIGIBLE.
3. Official match is created between the two participants (MatchPurpose.OFFICIAL).
4. dispatch_participant_match_jobs creates two distinct PARTICIPANT execution jobs.
5. Two distinct mTLS-authenticated workers claim and acknowledge their jobs.
6. Match commands are served turn-by-turn via the private coordinator & worker gateway:
   - "hello" -> "ready"
   - "new_game" -> "ack"
   - "position" -> "ack"
   - "go" -> "bestmove"
7. Security checks verify:
   - Non-mTLS client rejected (401)
   - Worker identity forgery rejected (422)
   - Stale fencing token rejected (409)
8. The match progresses through 7 plies (Scholar's Mate) to CHECKMATE.
9. Match transitions to COMPLETED with outcome WHITE_WIN and reason CHECKMATE.
10. All 7 plies are persisted in match_moves.
11. Python-chess independently verifies the legal sequence and final checkmate board.
12. Tournament leaderboard standings reflect the completed match.
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

import chess
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
from app.matches.controller import MatchConfiguration
from app.matches.serve import serve_with_verified_peer_identity
from app.matches.service import create_match
from app.models.competition import SubmissionVersion
from app.models.match import Match, MatchMove, MatchPurpose, MatchState
from app.models.worker import ExecutionKind, Worker, WorkerExecutionJob, WorkerStatus
from app.tournaments.service import get_tournament_leaderboard
from app.workers.gateway import create_worker_gateway_app
from app.workers.service import dispatch_participant_match_jobs
from app.workers.tls import GatewayServiceClientTLSConfig
from tests.integration.test_development_matches import _eligible_submission


def _ca():
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "participant-match-test-ca")])
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    cert = (
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
    return key, cert


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
    cert = (
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
    return key, cert


def _write_identity(tmp_path: Path, prefix: str, key, cert) -> tuple[Path, Path]:
    cert_path = tmp_path / f"{prefix}.crt"
    key_path = tmp_path / f"{prefix}.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)  # TLS loader rejects group/world-readable keys on POSIX
    return cert_path, key_path


class _WorkerCertificateTransport:
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
async def test_participant_vs_participant_official_match_execution(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    test_session_factory,
    tmp_path: Path,
):
    # 1. Register two distinct participants with eligible submissions
    p1_id, p1_headers, p1_ver = await _eligible_submission(
        api_client, db_session, name=f"Participant_Alpha_{uuid.uuid4().hex[:6]}"
    )
    p2_id, p2_headers, p2_ver = await _eligible_submission(
        api_client, db_session, name=f"Participant_Beta_{uuid.uuid4().hex[:6]}"
    )
    assert p1_id != p2_id
    assert p1_ver.id != p2_ver.id

    from app.models.event import OfficialSubmissionSelection
    db_session.add(
        OfficialSubmissionSelection(
            participant_id=p1_id,
            submission_version_id=p1_ver.id,
            selected_at=datetime.now(timezone.utc),
        )
    )
    db_session.add(
        OfficialSubmissionSelection(
            participant_id=p2_id,
            submission_version_id=p2_ver.id,
            selected_at=datetime.now(timezone.utc),
        )
    )
    await db_session.flush()

    # 2. Create official match between P1 (White) and P2 (Black)
    config = MatchConfiguration(
        protocol_version="aik-jsonl/1",
        runtime_profile="python-3.12",
        policy_version="development-v1",
        rules_version="standard-chess/python-chess-1.11.2",
        initial_fen="rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        initial_clock_ms=180_000,
        increment_ms=2_000,
        move_timeout_ms=3_000,
        match_seed=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
    )
    match, _ = await create_match(
        db_session,
        white_submission_version_id=p1_ver.id,
        black_submission_version_id=p2_ver.id,
        configuration=config,
        purpose=MatchPurpose.OFFICIAL,
    )

    from app.models.event import TournamentRoundRecord, TournamentPairingRecord

    round_record = TournamentRoundRecord(
        id=uuid.uuid4(),
        tournament_id="official-v1",
        stage="SWISS",
        round_number=1,
        round_name="Swiss Round 1",
        status="IN_PROGRESS",
    )
    db_session.add(round_record)
    await db_session.flush()

    pairing_record = TournamentPairingRecord(
        id=uuid.uuid4(),
        round_id=round_record.id,
        pairing_index=1,
        stage_name="SWISS",
        participant1_id=p1_id,
        participant2_id=p2_id,
        is_bye=False,
        game1_match_id=match.id,
        game2_match_id=None,
        status="IN_PROGRESS",
    )
    db_session.add(pairing_record)
    await db_session.commit()
    assert match.state == MatchState.QUEUED
    assert match.purpose == MatchPurpose.OFFICIAL

    # 3. Dispatch participant match jobs
    white_job, black_job = await dispatch_participant_match_jobs(
        db_session,
        match_id=match.id,
        execution_mode="MOCK_PROTOCOL_ONLY",
    )
    assert white_job.match_id == match.id
    assert black_job.match_id == match.id
    assert white_job.execution_kind == ExecutionKind.PARTICIPANT
    assert black_job.execution_kind == ExecutionKind.PARTICIPANT
    assert white_job.participant_id == p1_id
    assert black_job.participant_id == p2_id

    # 4. Mint mTLS certificates for coordinator and 2 worker clients
    worker_ca_key, worker_ca_cert = _ca()
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
    gateway_key, gateway_cert = _leaf(
        service_ca_key,
        service_ca_cert,
        common_name="worker-gateway",
        uri="urn:aik:service:worker-gateway",
        eku=ExtendedKeyUsageOID.CLIENT_AUTH,
    )
    server_cert_path, server_key_path = _write_identity(
        tmp_path, "coordinator", server_key, server_cert
    )
    gateway_cert_path, gateway_key_path = _write_identity(
        tmp_path, "gateway-service", gateway_key, gateway_cert
    )

    worker_identities = {}
    for idx in (1, 2):
        w_id = f"worker-part-{idx}-{uuid.uuid4().hex[:6]}"
        w_key, w_cert = _leaf(
            worker_ca_key,
            worker_ca_cert,
            common_name=w_id,
            uri=f"urn:aik:worker:{w_id}",
            eku=ExtendedKeyUsageOID.CLIENT_AUTH,
        )
        worker_identities[w_id] = (w_key, w_cert)
        db_session.add(
            Worker(
                worker_id=w_id,
                certificate_fingerprint=hashlib.sha256(
                    w_cert.public_bytes(serialization.Encoding.DER)
                ).hexdigest(),
                certificate_identity=w_cert.subject.rfc4514_string(),
                certificate_serial=format(w_cert.serial_number, "x"),
                status=WorkerStatus.ACTIVE,
                issued_at=datetime.now(timezone.utc),
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                capabilities={
                    "sandbox_backend": "MODAL",
                    "protocol_versions": ["aik-jsonl/1"],
                    "execution_modes": ["MOCK_PROTOCOL_ONLY", "MODAL_AI_EXECUTION"],
                },
                software_version="participant-test-1.0",
                runtime_version="4.2.0",
                security_state="HEALTHY",
            )
        )
    await db_session.commit()

    # 5. Start Private Coordinator Listener on Loopback Port
    port = await _free_port()
    control_service = DatabaseControlService(test_session_factory)
    control_app = create_control_plane_app(control_service, (service_ca_cert,))

    h_cfg = Config()
    h_cfg.bind = [f"127.0.0.1:{port}"]
    h_cfg.certfile = str(server_cert_path)
    h_cfg.keyfile = str(server_key_path)
    h_cfg.ca_certs = str(service_ca_path)
    h_cfg.verify_mode = ssl.CERT_REQUIRED
    h_cfg.workers = 1
    h_cfg.use_reloader = False
    h_cfg.accesslog = None
    h_cfg.errorlog = None

    shutdown = asyncio.Event()
    listener = asyncio.create_task(
        serve_with_verified_peer_identity(control_app, h_cfg, shutdown_trigger=shutdown.wait)
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
        # Wait for listener to accept connections
        for _ in range(80):
            if listener.done():
                await listener
            try:
                reader, writer = await asyncio.open_connection(
                    "127.0.0.1",
                    port,
                    ssl=gateway_app.state.match_coordinator_ssl_context,
                    server_hostname="127.0.0.1",
                )
                writer.close()
                await writer.wait_closed()
                break
            except OSError:
                await asyncio.sleep(0.025)
        else:
            pytest.fail("Private coordinator listener failed to start")

        # 6. Verify security rejection: unauthenticated call to coordinator rejected with 401
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=control_app), base_url="http://control.local"
        ) as unauth_cli:
            unauth_resp = await unauth_cli.post(
                "/internal/v1/commands/issue", json={"match_id": str(match.id)}
            )
            assert unauth_resp.status_code == 401

        # 7. Workers claim and acknowledge jobs
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
            ack_resp = await client.post(
                f"/worker/v1/jobs/{job_out['execution_id']}/acknowledgement",
                json={
                    "lease_id": lease["lease_id"],
                    "fencing_token": lease["fencing_token"],
                    "sequence": lease["next_sequence"],
                    "accepted": True,
                },
            )
            assert ack_resp.status_code == 200, ack_resp.text
            clients[job_out["execution_id"]] = (client, job_out)

        # 8. Verify security rejections:
        # Forged worker ID rejected with 422
        first_cli, first_job = next(iter(clients.values()))
        forged = await first_cli.post(
            f"/worker/v1/jobs/{first_job['execution_id']}/match/next",
            json={
                "lease_id": first_job["lease"]["lease_id"],
                "fencing_token": first_job["lease"]["fencing_token"],
                "attempt": first_job["attempt_number"],
                "worker_id": "forged-worker-id",
            },
        )
        assert forged.status_code == 422

        # Stale fencing token rejected with 409
        stale_fence = await first_cli.post(
            f"/worker/v1/jobs/{first_job['execution_id']}/match/next",
            json={
                "lease_id": first_job["lease"]["lease_id"],
                "fencing_token": first_job["lease"]["fencing_token"] + 999,
                "attempt": first_job["attempt_number"],
            },
        )
        assert stale_fence.status_code == 409

        # 9. Execute 7-ply match (Scholar's Mate) turn-by-turn
        scholars_mate = ["e2e4", "e7e5", "d1h5", "b8c6", "f1c4", "g8f6", "h5f7"]

        async def poll_and_respond(client, job_info):
            exec_id = job_info["execution_id"]
            lease = job_info["lease"]
            req = {
                "lease_id": lease["lease_id"],
                "fencing_token": lease["fencing_token"],
                "attempt": job_info["attempt_number"],
            }
            res = await client.post(f"/worker/v1/jobs/{exec_id}/match/next", json=req)
            assert res.status_code == 200, res.text
            cmd = res.json()
            if cmd["status"] != "ACCEPTED":
                return False

            msg = json.loads(cmd["message"])
            resp_type = {
                "hello": "ready",
                "new_game": "ack",
                "position": "ack",
                "go": "bestmove",
            }[msg["type"]]
            payload = {
                "protocol_version": "aik-jsonl/1",
                "type": resp_type,
                "request_id": cmd["command_id"],
            }
            if msg["type"] == "go":
                async with test_session_factory() as db:
                    m = await db.get(Match, match.id)
                    ply = m.current_ply
                payload["move"] = scholars_mate[ply]

            sub = await client.post(
                f"/worker/v1/jobs/{exec_id}/match/response",
                json={
                    **req,
                    "command_id": cmd["command_id"],
                    "payload": json.dumps(payload, separators=(",", ":")) + "\n",
                },
            )
            assert sub.status_code == 200, sub.text
            return True

        for turn_idx in range(160):
            progressed = False
            for client, job_info in clients.values():
                progressed = await poll_and_respond(client, job_info) or progressed
            async with test_session_factory() as db:
                curr_match = await db.get(Match, match.id)
                if curr_match.state == MatchState.COMPLETED:
                    break
            assert progressed or curr_match.state == MatchState.RUNNING
        else:
            pytest.fail("Participant vs participant match did not reach terminal state")

        # 10. Verify match results in database
        async with test_session_factory() as db:
            persisted_match = await db.get(Match, match.id)
            moves = (
                await db.execute(
                    select(MatchMove)
                    .where(MatchMove.match_id == match.id)
                    .order_by(MatchMove.sequence)
                )
            ).scalars().all()

            assert persisted_match.state == MatchState.COMPLETED
            assert persisted_match.result_outcome == "WHITE_WIN"
            assert persisted_match.result_reason == "CHECKMATE"
            assert persisted_match.current_ply == 7
            assert len(moves) == 7

            # Independent replay via python-chess
            board = chess.Board()
            for m in moves:
                move_obj = chess.Move.from_uci(m.move_uci)
                assert move_obj in board.legal_moves, f"Illegal move in transcript: {m.move_uci}"
                board.push(move_obj)
            assert board.fen() == persisted_match.current_fen
            assert board.is_checkmate() is True

            # 11. Verify leaderboard aggregates the completed official match
            leaderboard = await get_tournament_leaderboard(db)
            assert leaderboard.total_matches >= 1
            standings_map = {s.participant_id: s for s in leaderboard.standings}
            assert p1_id in standings_map
            assert p2_id in standings_map
            assert standings_map[p1_id].wins >= 1
            assert standings_map[p2_id].losses >= 1

            # 12. Verify spectator API returns the complete move transcript and final state
            spec_resp = await api_client.get(f"/api/v1/tournaments/matches/{match.id}")
            assert spec_resp.status_code == 200, spec_resp.text
            spec_data = spec_resp.json()
            assert spec_data["state"] == "COMPLETED"
            assert spec_data["result_outcome"] == "WHITE_WIN"
            assert spec_data["result_reason"] == "CHECKMATE"
            assert len(spec_data["moves"]) == 7
            assert spec_data["moves"][0]["move_uci"] == "e2e4"
            assert spec_data["moves"][0]["from_square"] == "e2"
            assert spec_data["moves"][0]["to_square"] == "e4"
            assert spec_data["moves"][6]["move_uci"] == "h5f7"
            assert spec_data["moves"][6]["from_square"] == "h5"
            assert spec_data["moves"][6]["to_square"] == "f7"

    finally:
        await asyncio.gather(*(c.aclose() for c, _j in clients.values()))
        shutdown.set()
        await listener


@pytest.mark.integration
async def test_participant_match_illegal_move_results_in_disqualification(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    test_session_factory,
    tmp_path: Path,
):
    """Verify that an illegal chess move proposed by a participant agent immediately forfeits the match."""
    p1_id, _, p1_ver = await _eligible_submission(
        api_client, db_session, name=f"IllegalPlayer_A_{uuid.uuid4().hex[:6]}"
    )
    p2_id, _, p2_ver = await _eligible_submission(
        api_client, db_session, name=f"LegalPlayer_B_{uuid.uuid4().hex[:6]}"
    )

    from app.models.event import OfficialSubmissionSelection, TournamentRoundRecord, TournamentPairingRecord
    db_session.add(
        OfficialSubmissionSelection(
            participant_id=p1_id,
            submission_version_id=p1_ver.id,
            selected_at=datetime.now(timezone.utc),
        )
    )
    db_session.add(
        OfficialSubmissionSelection(
            participant_id=p2_id,
            submission_version_id=p2_ver.id,
            selected_at=datetime.now(timezone.utc),
        )
    )
    await db_session.flush()

    config = MatchConfiguration(
        protocol_version="aik-jsonl/1",
        runtime_profile="python-3.12",
        policy_version="development-v1",
        rules_version="standard-chess/python-chess-1.11.2",
        initial_fen="rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        initial_clock_ms=180_000,
        increment_ms=2_000,
        move_timeout_ms=3_000,
        match_seed=hashlib.sha256(uuid.uuid4().bytes).hexdigest(),
    )
    match, _ = await create_match(
        db_session,
        white_submission_version_id=p1_ver.id,
        black_submission_version_id=p2_ver.id,
        configuration=config,
        purpose=MatchPurpose.OFFICIAL,
    )

    round_record = TournamentRoundRecord(
        id=uuid.uuid4(),
        tournament_id="official-v1",
        stage="SWISS",
        round_number=2,
        round_name="Swiss Round 2",
        status="IN_PROGRESS",
    )
    db_session.add(round_record)
    await db_session.flush()

    pairing_record = TournamentPairingRecord(
        id=uuid.uuid4(),
        round_id=round_record.id,
        pairing_index=1,
        stage_name="SWISS",
        participant1_id=p1_id,
        participant2_id=p2_id,
        is_bye=False,
        game1_match_id=match.id,
        game2_match_id=None,
        status="IN_PROGRESS",
    )
    db_session.add(pairing_record)
    await db_session.commit()

    white_job, black_job = await dispatch_participant_match_jobs(
        db_session,
        match_id=match.id,
        execution_mode="MOCK_PROTOCOL_ONLY",
    )

    worker_ca_key, worker_ca_cert = _ca()
    service_ca_key, service_ca_cert = _ca()
    service_ca_path = tmp_path / "service-ca-2.crt"
    service_ca_path.write_bytes(service_ca_cert.public_bytes(serialization.Encoding.PEM))

    server_key, server_cert = _leaf(
        service_ca_key, service_ca_cert, common_name="coordinator-2",
        uri="urn:aik:service:match-coordinator", dns="localhost", ip="127.0.0.1",
        eku=ExtendedKeyUsageOID.SERVER_AUTH,
    )
    gateway_key, gateway_cert = _leaf(
        service_ca_key, service_ca_cert, common_name="worker-gateway-2",
        uri="urn:aik:service:worker-gateway", eku=ExtendedKeyUsageOID.CLIENT_AUTH,
    )
    server_cert_path, server_key_path = _write_identity(tmp_path, "coordinator-2", server_key, server_cert)
    gateway_cert_path, gateway_key_path = _write_identity(tmp_path, "gateway-2", gateway_key, gateway_cert)

    worker_identities = {}
    for idx in (1, 2):
        w_id = f"worker-illegal-{idx}-{uuid.uuid4().hex[:6]}"
        w_key, w_cert = _leaf(
            worker_ca_key, worker_ca_cert, common_name=w_id,
            uri=f"urn:aik:worker:{w_id}", eku=ExtendedKeyUsageOID.CLIENT_AUTH,
        )
        worker_identities[w_id] = (w_key, w_cert)
        db_session.add(
            Worker(
                worker_id=w_id,
                certificate_fingerprint=hashlib.sha256(w_cert.public_bytes(serialization.Encoding.DER)).hexdigest(),
                certificate_identity=w_cert.subject.rfc4514_string(),
                certificate_serial=format(w_cert.serial_number, "x"),
                status=WorkerStatus.ACTIVE,
                issued_at=datetime.now(timezone.utc),
                expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
                capabilities={
                    "sandbox_backend": "MODAL",
                    "protocol_versions": ["aik-jsonl/1"],
                    "execution_modes": ["MOCK_PROTOCOL_ONLY", "MODAL_AI_EXECUTION"],
                },
                software_version="test-1.0",
                runtime_version="4.2.0",
                security_state="HEALTHY",
            )
        )
    await db_session.commit()

    port = await _free_port()
    control_service = DatabaseControlService(test_session_factory)
    control_app = create_control_plane_app(control_service, (service_ca_cert,))

    h_cfg = Config()
    h_cfg.bind = [f"127.0.0.1:{port}"]
    h_cfg.certfile = str(server_cert_path)
    h_cfg.keyfile = str(server_key_path)
    h_cfg.ca_certs = str(service_ca_path)
    h_cfg.verify_mode = ssl.CERT_REQUIRED
    h_cfg.workers = 1
    h_cfg.use_reloader = False
    h_cfg.accesslog = None
    h_cfg.errorlog = None

    shutdown = asyncio.Event()
    listener = asyncio.create_task(
        serve_with_verified_peer_identity(control_app, h_cfg, shutdown_trigger=shutdown.wait)
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

        for worker_id, (_key, cert) in worker_identities.items():
            transport = httpx.ASGITransport(
                app=_WorkerCertificateTransport(
                    gateway_app, cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
                )
            )
            client = httpx.AsyncClient(transport=transport, base_url="https://gateway.local")
            claimed = await client.post("/worker/v1/jobs/claim")
            job_out = claimed.json()
            lease = job_out["lease"]
            await client.post(
                f"/worker/v1/jobs/{job_out['execution_id']}/acknowledgement",
                json={
                    "lease_id": lease["lease_id"],
                    "fencing_token": lease["fencing_token"],
                    "sequence": lease["next_sequence"],
                    "accepted": True,
                },
            )
            clients[job_out["execution_id"]] = (client, job_out)

        async def poll_and_respond_illegal(client, job_info):
            exec_id = job_info["execution_id"]
            lease = job_info["lease"]
            req = {
                "lease_id": lease["lease_id"],
                "fencing_token": lease["fencing_token"],
                "attempt": job_info["attempt_number"],
            }
            res = await client.post(f"/worker/v1/jobs/{exec_id}/match/next", json=req)
            cmd = res.json()
            if cmd["status"] != "ACCEPTED":
                return False

            msg = json.loads(cmd["message"])
            resp_type = {
                "hello": "ready",
                "new_game": "ack",
                "position": "ack",
                "go": "bestmove",
            }[msg["type"]]
            payload = {
                "protocol_version": "aik-jsonl/1",
                "type": resp_type,
                "request_id": cmd["command_id"],
            }
            if msg["type"] == "go":
                # Intentionally propose an illegal move: e2e5 is impossible on move 1
                payload["move"] = "e2e5"

            await client.post(
                f"/worker/v1/jobs/{exec_id}/match/response",
                json={
                    **req,
                    "command_id": cmd["command_id"],
                    "payload": json.dumps(payload, separators=(",", ":")) + "\n",
                },
            )
            return True

        for turn_idx in range(40):
            progressed = False
            for client, job_info in clients.values():
                progressed = await poll_and_respond_illegal(client, job_info) or progressed
            async with test_session_factory() as db:
                curr_match = await db.get(Match, match.id)
                if curr_match.state in {MatchState.ABORTED, MatchState.COMPLETED}:
                    break

        async with test_session_factory() as db:
            persisted_match = await db.get(Match, match.id)
            assert persisted_match.state == MatchState.ABORTED
            assert persisted_match.failure_reason == "ILLEGAL_MOVE"

    finally:
        await asyncio.gather(*(c.aclose() for c, _j in clients.values()))
        shutdown.set()
        await listener

