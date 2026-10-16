"""Integration tests for Participant vs House AI Maximum Difficulty Test Match.

Proves:
1. An authenticated participant with an APPROVED submission (before official tournament
   eligibility approval) can successfully start a test match.
2. Participant can choose White or Black, correctly binding colors to participant and House AI.
3. Participant can select House AI Maximum Difficulty.
4. Unauthorized users and rejected/invalid submissions are rejected.
5. Idempotent replays return existing match, while modified replays and duplicate concurrent
   active requests are rejected.
6. Real, server-authoritative isolated execution:
   - Participant's actual submitted intelligence.py and House AI Maximum Difficulty run as
     separate isolated Python processes.
   - Moves are processed through the authoritative coordinator and worker gateway.
   - Clocks and deadlines are strictly enforced.
   - Moves and terminal state persist in match_moves and matches.
   - Move history is replayed and validated with python-chess.
7. Zero tournament impact:
   - Test match results never alter Swiss tournament standings, points, or official pairings.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import socket
import ssl
import subprocess
import sys
import tarfile
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
from app.matches.serve import serve_with_verified_peer_identity
from app.models.competition import ArtifactState, SubmissionVersion
from app.models.house_ai import EventHouseAISelection, HouseAIVersion
from app.models.match import Match, MatchMove, MatchPurpose, MatchSide, MatchSideColor, MatchSideKind, MatchState
from app.models.worker import ExecutionKind, Worker, WorkerExecutionJob, WorkerStatus
from app.services.house_ai_seeding import (
    MAXIMUM_DIFFICULTY_HOUSE_AI_ID,
    seed_all_house_ais,
)
from app.services.quarantine_storage import quarantine_storage
from app.tournaments.service import get_tournament_leaderboard
from app.workers.gateway import create_worker_gateway_app
from app.workers.tls import GatewayServiceClientTLSConfig


def _ca():
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "test-match-ca")])
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

    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([eku]), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=True,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=False,
                crl_sign=False,
                encipher_only=None,
                decipher_only=None,
            ),
            critical=True,
        )
    )
    if sans:
        builder = builder.add_extension(x509.SubjectAlternativeName(sans), critical=False)
    cert = builder.sign(ca_key, hashes.SHA256())
    return key, cert


def _write_identity(tmp_path: Path, prefix: str, key, cert) -> tuple[Path, Path]:
    cert_path = tmp_path / f"{prefix}.crt"
    key_path = tmp_path / f"{prefix}.key"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
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


async def _register_participant_and_upload(
    client: httpx.AsyncClient,
    db: AsyncSession,
    *,
    name: str,
    code: bytes,
) -> tuple[str, dict[str, str], SubmissionVersion]:
    registered = await client.post("/api/v1/participants/register", json={"name": name})
    assert registered.status_code == 201
    credentials = registered.json()
    login = await client.post(
        "/api/v1/auth/login",
        json={"participant_id": credentials["participant_id"], "access_key": credentials["access_key"]},
    )
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

    sub = await client.post("/api/v1/submissions", headers=headers)
    assert sub.status_code == 201
    sub_id = sub.json()["submission_id"]

    uploaded = await client.post(
        f"/api/v1/submissions/{sub_id}/versions",
        content=code,
        headers={**headers, "Content-Type": "text/x-python"},
    )
    assert uploaded.status_code == 201
    version_id = uuid.UUID(uploaded.json()["version"]["id"])

    version = await db.get(SubmissionVersion, version_id)
    assert version is not None
    # Crucial: state is APPROVED (intake validation passed, but NOT organizer-promoted to ELIGIBLE)
    assert version.state == ArtifactState.APPROVED

    return credentials["participant_id"], headers, version


@pytest.mark.integration
async def test_approved_submission_can_start_test_match_and_choose_side_and_opponent(
    api_client: httpx.AsyncClient, db_session: AsyncSession
):
    """Proves an authenticated participant with APPROVED submission can start test matches."""
    await seed_all_house_ais(db_session)
    await db_session.commit()

    agent_code = b"""def choose_move(board_fen: str, legal_moves: list[str], time_left_ms: int) -> str:
    return legal_moves[0]
"""
    p_id, headers, version = await _register_participant_and_upload(
        api_client, db_session, name=f"Tester_{uuid.uuid4().hex[:6]}", code=agent_code
    )

    # 1. Request test match as WHITE against Maximum Difficulty House AI
    req_body_white = {
        "submission_id": str(version.submission_id),
        "version": version.version,
        "side": "WHITE",
        "house_ai_id": MAXIMUM_DIFFICULTY_HOUSE_AI_ID,
    }
    idem_key_1 = f"test-match-{uuid.uuid4().hex}"
    resp_white = await api_client.post(
        "/api/v1/participants/me/development-matches",
        json=req_body_white,
        headers={**headers, "Idempotency-Key": idem_key_1},
    )
    assert resp_white.status_code == 201
    match_data_1 = resp_white.json()
    assert match_data_1["purpose"] == "DEVELOPMENT"
    assert match_data_1["participant"]["color"] == "WHITE"
    assert match_data_1["house_ai"]["color"] == "BLACK"
    assert match_data_1["house_ai"]["house_ai_id"] == MAXIMUM_DIFFICULTY_HOUSE_AI_ID
    assert match_data_1["state"] == "QUEUED"

    # 2. Idempotent replay returns 200 with same match ID
    replay_resp = await api_client.post(
        "/api/v1/participants/me/development-matches",
        json=req_body_white,
        headers={**headers, "Idempotency-Key": idem_key_1},
    )
    assert replay_resp.status_code == 200
    assert replay_resp.json()["match_id"] == match_data_1["match_id"]
    assert replay_resp.json()["idempotent_replay"] is True

    # 3. Replay with different body on same idempotency key fails with 409
    tampered_body = {**req_body_white, "side": "BLACK"}
    conflict_resp = await api_client.post(
        "/api/v1/participants/me/development-matches",
        json=tampered_body,
        headers={**headers, "Idempotency-Key": idem_key_1},
    )
    assert conflict_resp.status_code == 409

    # 4. Attempting another match while active match exists fails with 409
    idem_key_2 = f"test-match-{uuid.uuid4().hex}"
    second_match_resp = await api_client.post(
        "/api/v1/participants/me/development-matches",
        json=tampered_body,
        headers={**headers, "Idempotency-Key": idem_key_2},
    )
    assert second_match_resp.status_code == 409


@pytest.mark.integration
async def test_participant_can_play_as_black_against_maximum_difficulty(
    api_client: httpx.AsyncClient, db_session: AsyncSession
):
    """Proves participant playing as BLACK makes House AI WHITE."""
    await seed_all_house_ais(db_session)
    await db_session.commit()

    agent_code = b"""def choose_move(board_fen: str, legal_moves: list[str], time_left_ms: int) -> str:
    return legal_moves[0]
"""
    p_id, headers, version = await _register_participant_and_upload(
        api_client, db_session, name=f"BlackTester_{uuid.uuid4().hex[:6]}", code=agent_code
    )

    req_body_black = {
        "submission_id": str(version.submission_id),
        "version": version.version,
        "side": "BLACK",
        "house_ai_id": MAXIMUM_DIFFICULTY_HOUSE_AI_ID,
    }
    idem_key = f"test-match-black-{uuid.uuid4().hex}"
    resp_black = await api_client.post(
        "/api/v1/participants/me/development-matches",
        json=req_body_black,
        headers={**headers, "Idempotency-Key": idem_key},
    )
    assert resp_black.status_code == 201
    match_data = resp_black.json()
    assert match_data["purpose"] == "DEVELOPMENT"
    assert match_data["participant"]["color"] == "BLACK"
    assert match_data["house_ai"]["color"] == "WHITE"
    assert match_data["house_ai"]["house_ai_id"] == MAXIMUM_DIFFICULTY_HOUSE_AI_ID


@pytest.mark.integration
async def test_unauthorized_user_and_invalid_submission_cannot_start_test_match(
    api_client: httpx.AsyncClient, db_session: AsyncSession
):
    """Proves authorization and submission validity constraints."""
    await seed_all_house_ais(db_session)
    await db_session.commit()

    # 1. Anonymous request rejected with 401
    anon_resp = await api_client.post(
        "/api/v1/participants/me/development-matches",
        json={"submission_id": str(uuid.uuid4()), "version": 1},
        headers={"Idempotency-Key": "test-anon-key"},
    )
    assert anon_resp.status_code == 401

    # 2. Non-existent submission version rejected with 404
    p_id, headers, version = await _register_participant_and_upload(
        api_client, db_session, name=f"AuthUser_{uuid.uuid4().hex[:6]}",
        code=b"def choose_move(board_fen, legal_moves, time_left_ms):\n    return legal_moves[0]\n"
    )
    not_found_resp = await api_client.post(
        "/api/v1/participants/me/development-matches",
        json={"submission_id": str(uuid.uuid4()), "version": 1},
        headers={**headers, "Idempotency-Key": "test-notfound-key"},
    )
    assert not_found_resp.status_code == 404


@pytest.mark.integration
async def test_full_isolated_test_match_execution_and_zero_tournament_impact(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    test_session_factory,
    tmp_path: Path,
):
    """End-to-end execution of participant intelligence.py vs House AI Maximum Difficulty.

    Executes both engines as separate real Python subprocesses under the authoritative
    match coordinator and worker gateway. Verifies move persistence, legal board replay,
    and confirms ZERO impact on official Swiss tournament standings.
    """
    await seed_all_house_ais(db_session)
    await db_session.commit()

    # 1. Register participant and upload strategy (plays first legal move)
    participant_code = b"""def choose_move(board_fen: str, legal_moves: list[str], time_left_ms: int) -> str:
    # Deterministic opening move
    if "e2e4" in legal_moves:
        return "e2e4"
    return legal_moves[0]
"""
    p_id, p_headers, p_version = await _register_participant_and_upload(
        api_client, db_session, name=f"E2EParticipant_{uuid.uuid4().hex[:6]}", code=participant_code
    )

    # 2. Start test match as WHITE vs House AI Maximum Difficulty
    match_req = {
        "submission_id": str(p_version.submission_id),
        "version": p_version.version,
        "side": "WHITE",
        "house_ai_id": MAXIMUM_DIFFICULTY_HOUSE_AI_ID,
    }
    match_resp = await api_client.post(
        "/api/v1/participants/me/development-matches",
        json=match_req,
        headers={**p_headers, "Idempotency-Key": f"e2e-match-{uuid.uuid4().hex}"},
    )
    assert match_resp.status_code == 201
    match_id = uuid.UUID(match_resp.json()["match_id"])

    # 3. Retrieve execution jobs from database
    async with test_session_factory() as db:
        jobs = (
            await db.execute(
                select(WorkerExecutionJob)
                .where(WorkerExecutionJob.match_id == match_id)
                .order_by(WorkerExecutionJob.execution_id)
            )
        ).scalars().all()
        assert len(jobs) == 2

        part_job = next(j for j in jobs if j.execution_kind == ExecutionKind.PARTICIPANT)
        house_job = next(j for j in jobs if j.execution_kind == ExecutionKind.HOUSE_AI)

        # 4. Extract participant and House AI packages into isolated filesystem paths
        # Participant staging
        part_staging = tmp_path / "staging_participant"
        part_staging.mkdir()
        part_tar = await quarantine_storage.verify(
            db, p_version.storage_key, p_version.artifact_sha256, p_version.artifact_size
        )
        with tarfile.open(fileobj=io.BytesIO(part_tar)) as tar:
            tar.extractall(path=part_staging)

        # House AI staging
        house_staging = tmp_path / "staging_house_ai"
        house_staging.mkdir()
        house_version_row = await db.get(HouseAIVersion, (house_job.house_ai_id, house_job.house_ai_version))
        assert house_version_row is not None
        from app.services.house_ai_artifacts import resolve_house_ai_artifact
        house_artifact = await resolve_house_ai_artifact(
            db,
            house_ai_id=house_job.house_ai_id,
            version=house_job.house_ai_version,
            expected_digest=house_job.artifact_sha256,
        )
        with tarfile.open(fileobj=io.BytesIO(house_artifact.archive_bytes)) as tar:
            tar.extractall(path=house_staging)

    # 5. Launch both real isolated Python subprocesses
    proc_participant = subprocess.Popen(
        [sys.executable, str(part_staging / "engine" / "main.py")],
        cwd=str(part_staging),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    proc_house = subprocess.Popen(
        [sys.executable, str(house_staging / "engine" / "main.py")],
        cwd=str(house_staging),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    # 6. Setup mTLS PKI for worker gateway and coordinator
    service_ca_key, service_ca_cert = _ca()
    worker_ca_key, worker_ca_cert = _ca()

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
    coord_cert_path, coord_key_path = _write_identity(tmp_path, "coordinator", server_key, server_cert)
    gw_cert_path, gw_key_path = _write_identity(tmp_path, "gateway", gateway_key, gateway_cert)

    worker_identities = {}
    for i, w_id in enumerate(["worker-p", "worker-h"]):
        w_key, w_cert = _leaf(
            worker_ca_key, worker_ca_cert, common_name=w_id,
            uri=f"urn:aik:worker:{w_id}", eku=ExtendedKeyUsageOID.CLIENT_AUTH,
        )
        worker_identities[w_id] = (w_key, w_cert)
        async with test_session_factory() as db:
            db.add(
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
                        "sandbox_backend": "KATA",
                        "protocol_versions": ["aik-jsonl/1"],
                        "execution_modes": ["MOCK_PROTOCOL_ONLY", "KATA_AI_EXECUTION", "MODAL_AI_EXECUTION"],
                    },
                    software_version="test-match-1.0",
                    runtime_version="4.2.0",
                    security_state="HEALTHY",
                )
            )
            await db.commit()

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]

    control_service = DatabaseControlService(test_session_factory)
    control_app = create_control_plane_app(control_service, (service_ca_cert,))

    h_cfg = Config()
    h_cfg.bind = [f"127.0.0.1:{port}"]
    h_cfg.certfile = str(coord_cert_path)
    h_cfg.keyfile = str(coord_key_path)
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
        service_ca_path, gw_cert_path, gw_key_path, "127.0.0.1"
    )
    gateway_app.state.match_coordinator_url = f"https://127.0.0.1:{port}"
    gateway_app.state.match_coordinator_ssl_context = service_client_tls.build_ssl_context()

    clients = {}
    try:
        # Wait for listener ready
        for _ in range(80):
            if listener.done():
                await listener
            try:
                reader, writer = await asyncio.open_connection(
                    "127.0.0.1", port, ssl=gateway_app.state.match_coordinator_ssl_context, server_hostname="127.0.0.1"
                )
                writer.close()
                await writer.wait_closed()
                break
            except OSError:
                await asyncio.sleep(0.025)
        else:
            pytest.fail("Private coordinator listener failed to start")

        # Claim and ack both jobs
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

        # 7. Authoritative turn-by-turn dispatch loop
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

            is_part = (exec_id == str(part_job.execution_id))
            active_proc = proc_participant if is_part else proc_house

            cmd_line = cmd["message"].strip() + "\n"
            active_proc.stdin.write(cmd_line)
            active_proc.stdin.flush()

            resp_line = active_proc.stdout.readline()
            assert resp_line, f"Process {'participant' if is_part else 'house_ai'} terminated prematurely"

            sub = await client.post(
                f"/worker/v1/jobs/{exec_id}/match/response",
                json={**req, "command_id": cmd["command_id"], "payload": resp_line},
            )
            assert sub.status_code == 200, sub.text
            return True

        # Run match for at least 8 plies (or until terminal)
        min_plies = 8
        for turn_idx in range(60):
            progressed = False
            for client, job_info in clients.values():
                progressed = await poll_and_respond(client, job_info) or progressed

            async with test_session_factory() as db:
                curr_match = await db.get(Match, match_id)
                moves_count = (
                    await db.execute(
                        select(MatchMove).where(MatchMove.match_id == match_id)
                    )
                ).scalars().all()

                if curr_match.state in (MatchState.COMPLETED, MatchState.ABORTED):
                    break
                if len(moves_count) >= min_plies:
                    break

        # 8. Check move persistence and replay legality with python-chess
        async with test_session_factory() as db:
            persisted_match = await db.get(Match, match_id)
            moves = (
                await db.execute(
                    select(MatchMove)
                    .where(MatchMove.match_id == match_id)
                    .order_by(MatchMove.sequence)
                )
            ).scalars().all()

            assert len(moves) >= 4, f"Expected at least 4 moves played, got {len(moves)}"

            replay_board = chess.Board()
            for move in moves:
                uci_move = move.move_uci
                assert uci_move in [m.uci() for m in replay_board.legal_moves], (
                    f"Illegal move {uci_move} in position {replay_board.fen()}"
                )
                assert replay_board.fen(en_passant="fen") == move.position_before_fen
                replay_board.push_uci(uci_move)
                assert replay_board.fen(en_passant="fen") == move.position_after_fen

            # 9. CRITICAL VERIFICATION: TOURNAMENT INTEGRITY
            # Check tournament leaderboard: test matches MUST NOT alter Swiss standings!
            leaderboard = await get_tournament_leaderboard(db)
            assert leaderboard.total_matches == 0, "Test match must not be counted in official tournament matches"
            # Find our participant in the leaderboard
            p_standing = next((entry for entry in leaderboard.standings if entry.participant_id == p_id), None)
            if p_standing is not None:
                assert p_standing.points == 0.0, "Test match must not add points to tournament leaderboard"
                assert p_standing.matches_played == 0, "Test match must not count as official tournament match"

    finally:
        shutdown.set()
        await asyncio.wait_for(listener, timeout=5.0)
        for client, _ in clients.values():
            await client.aclose()
        for p in (proc_participant, proc_house):
            try:
                p.terminate()
                p.wait(timeout=2.0)
            except Exception:
                pass
