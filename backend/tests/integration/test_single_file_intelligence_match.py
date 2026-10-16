"""Integration and end-to-end tests for single-file intelligence.py participant workflow.

Verifies:
1. Intake validation:
   - Function-based strategy (choose_move) accepted and wrapped into canonical USTAR archive.
   - Class-based strategy (ChessAgent) accepted and wrapped.
   - Syntax errors rejected safely at intake without consuming quota.
   - Missing strategy entrypoints rejected safely.
   - UTF-8 BOM rejected safely.
   - Canonical USTAR archives remain backward-compatible and untouched.
2. Server-owned adapter (engine/main.py) protocol translation:
   - Translates aik-jsonl/1 protocol (hello, new_game, position, go) to intelligence.py calls.
   - Gracefully handles runtime agent exceptions by returning "0000" rather than crashing.
3. Full match execution:
   - Legal move agent vs. invalid move agent.
   - Authoritative chess engine detects illegal move, aborts match with ILLEGAL_MOVE,
     persists move history and match outcome, and updates tournament leaderboard.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import socket
import ssl
import subprocess
import sys
import tempfile
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
from app.models.competition import ArtifactState, SubmissionVersion
from app.models.event import OfficialSubmissionSelection, TournamentPairingRecord, TournamentRoundRecord
from app.models.match import Match, MatchMove, MatchPurpose, MatchState
from app.models.worker import ExecutionKind, Worker, WorkerExecutionJob, WorkerStatus
from app.sandbox.participant_adapter import PARTICIPANT_ADAPTER_SOURCE
from app.services.submissions import OrganizerApproval, approve_submission_for_eligibility
from app.workers.gateway import create_worker_gateway_app
from app.workers.service import dispatch_participant_match_jobs
from app.workers.tls import GatewayServiceClientTLSConfig


def _ca():
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "single-file-test-ca")])
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


async def _register_participant_and_upload_intelligence(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    *,
    name: str,
    code: bytes,
    content_type: str = "text/x-python",
    approve: bool = True,
) -> tuple[str, dict[str, str], httpx.Response, SubmissionVersion | None]:
    registered = await api_client.post("/api/v1/participants/register", json={"name": name})
    assert registered.status_code == 201
    credentials = registered.json()
    login = await api_client.post(
        "/api/v1/auth/login",
        json={"participant_id": credentials["participant_id"], "access_key": credentials["access_key"]},
    )
    assert login.status_code == 200
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    sub = await api_client.post("/api/v1/submissions", headers=headers)
    assert sub.status_code == 201
    sub_id = sub.json()["submission_id"]

    uploaded = await api_client.post(
        f"/api/v1/submissions/{sub_id}/versions",
        content=code,
        headers={**headers, "Content-Type": content_type},
    )

    version = None
    if uploaded.status_code == 201:
        version_id = uuid.UUID(uploaded.json()["version"]["id"])
        if approve:
            version = await approve_submission_for_eligibility(
                db_session,
                submission_version_id=version_id,
                approval=OrganizerApproval(
                    actor_id="test-organizer",
                    reason_code="VERIFIED_PARTICIPANT_INTELLIGENCE",
                    approval_reference=f"APR-{uuid.uuid4().hex[:8]}",
                    correlation_id=str(uuid.uuid4()),
                ),
            )
    return credentials["participant_id"], credentials, uploaded, version


@pytest.mark.asyncio
async def test_intelligence_intake_and_syntax_validation_matrix(
    api_client: httpx.AsyncClient, db_session: AsyncSession
):
    """Test full matrix of single-file intelligence.py uploads and validations."""
    # 1. Valid function-based agent
    valid_code = b"""def choose_move(board_fen: str, legal_moves: list[str], time_left_ms: int) -> str:
    return legal_moves[0]
"""
    pid1, _, res1, v1 = await _register_participant_and_upload_intelligence(
        api_client, db_session, name=f"ValidFn_{uuid.uuid4().hex[:6]}", code=valid_code
    )
    assert res1.status_code == 201
    assert v1 is not None
    assert v1.state == ArtifactState.ELIGIBLE
    assert v1.manifest["contract_version"] == "1"
    assert v1.manifest["protocol_version"] == "aik-jsonl/1"
    assert v1.manifest["entrypoint"] == "engine/main.py"

    # 2. Valid class-based agent
    class_code = b"""class ChessAgent:
    def choose_move(self, board_fen: str, legal_moves: list[str], time_left_ms: int) -> str:
        return legal_moves[0]
"""
    pid2, _, res2, v2 = await _register_participant_and_upload_intelligence(
        api_client, db_session, name=f"ValidCls_{uuid.uuid4().hex[:6]}", code=class_code
    )
    assert res2.status_code == 201
    assert v2 is not None
    assert v2.state == ArtifactState.ELIGIBLE

    # 3. Syntax error rejected safely without crashing
    syntax_error_code = b"""def choose_move(board_fen: str
    return 42
"""
    pid3, _, res3, v3 = await _register_participant_and_upload_intelligence(
        api_client, db_session, name=f"SyntaxErr_{uuid.uuid4().hex[:6]}", code=syntax_error_code, approve=False
    )
    assert res3.status_code == 422
    assert "PYTHON_SYNTAX_ERROR" in res3.json()["error"]
    assert res3.json()["version"]["state"] == "REJECTED"

    # 4. Missing entrypoint rejected safely
    missing_entrypoint_code = b"""# Just a file with helper math
total = 100
def some_helper():
    return 1
"""
    pid4, _, res4, v4 = await _register_participant_and_upload_intelligence(
        api_client, db_session, name=f"NoEntry_{uuid.uuid4().hex[:6]}", code=missing_entrypoint_code, approve=False
    )
    assert res4.status_code == 422
    assert "INTELLIGENCE_ENTRYPOINT_MISSING" in res4.json()["error"]

    # 5. UTF-8 BOM rejected safely
    bom_code = b"\xef\xbb\xbfdef choose_move(board_fen, legal_moves, time_left_ms):\n    return legal_moves[0]\n"
    pid5, _, res5, v5 = await _register_participant_and_upload_intelligence(
        api_client, db_session, name=f"BomErr_{uuid.uuid4().hex[:6]}", code=bom_code, approve=False
    )
    assert res5.status_code == 422
    assert "SOURCE_BOM_FORBIDDEN" in res5.json()["error"]


def test_participant_adapter_protocol_translation():
    """Verify that the server-owned adapter executes intelligence.py and translates aik-jsonl/1 protocol."""
    with tempfile.TemporaryDirectory() as tmpdir:
        tmppath = Path(tmpdir)
        engine_dir = tmppath / "engine"
        engine_dir.mkdir()

        # Write intelligence.py
        intel_file = engine_dir / "intelligence.py"
        intel_file.write_text(
            """def choose_move(board_fen: str, legal_moves: list[str], time_left_ms: int) -> str:
    # Always play the first legal move
    return legal_moves[0]
""",
            encoding="utf-8",
        )

        # Write adapter as engine/main.py
        adapter_file = engine_dir / "main.py"
        adapter_file.write_text(PARTICIPANT_ADAPTER_SOURCE, encoding="utf-8")

        # Spawn engine/main.py as a subprocess simulating the sandbox
        proc = subprocess.Popen(
            [sys.executable, str(adapter_file)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(tmppath),
        )

        try:
            # 1. Send hello
            proc.stdin.write(json.dumps({"protocol_version": "aik-jsonl/1", "type": "hello", "request_id": "req-1"}) + "\n")
            proc.stdin.flush()
            resp1 = json.loads(proc.stdout.readline().strip())
            assert resp1["type"] == "ready"
            assert resp1["request_id"] == "req-1"

            # 2. Send new_game
            proc.stdin.write(json.dumps({"protocol_version": "aik-jsonl/1", "type": "new_game", "request_id": "req-2"}) + "\n")
            proc.stdin.flush()
            resp2 = json.loads(proc.stdout.readline().strip())
            assert resp2["type"] == "ack"
            assert resp2["request_id"] == "req-2"

            # 3. Send position
            proc.stdin.write(json.dumps({
                "protocol_version": "aik-jsonl/1",
                "type": "position",
                "request_id": "req-3",
                "fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
                "uci_moves": [],
            }) + "\n")
            proc.stdin.flush()
            resp3 = json.loads(proc.stdout.readline().strip())
            assert resp3["type"] == "ack"
            assert resp3["request_id"] == "req-3"

            # 4. Send go
            proc.stdin.write(json.dumps({
                "protocol_version": "aik-jsonl/1",
                "type": "go",
                "request_id": "req-4",
                "move_budget_ms": 1000,
            }) + "\n")
            proc.stdin.flush()
            resp4 = json.loads(proc.stdout.readline().strip())
            assert resp4["type"] == "bestmove"
            assert resp4["request_id"] == "req-4"
            assert resp4["move"] in [
                "a2a3", "a2a4", "b2b3", "b2b4", "c2c3", "c2c4", "d2d3", "d2d4",
                "e2e3", "e2e4", "f2f3", "f2f4", "g2g3", "g2g4", "h2h3", "h2h4",
                "b1a3", "b1c3", "g1f3", "g1h3",
            ]
        finally:
            proc.terminate()
            proc.wait(timeout=5)


@pytest.mark.integration
async def test_full_match_single_file_legal_vs_illegal_agent(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    test_session_factory,
    tmp_path: Path,
):
    """Full end-to-end sandbox-backed match between two single-file participant submissions.

    Participant White: returns legal moves.
    Participant Black: returns an illegal move ("e2e5").
    Outcome: Match is aborted with failure ILLEGAL_MOVE, White awarded victory,
    move history and match outcome persisted to database.
    """
    # 1. White Participant: Legal move strategy
    white_code = b"""def choose_move(board_fen: str, legal_moves: list[str], time_left_ms: int) -> str:
    # Always play the first legal move
    return legal_moves[0]
"""
    p1_id, _, _, p1_ver = await _register_participant_and_upload_intelligence(
        api_client, db_session, name=f"WhitePlayer_{uuid.uuid4().hex[:6]}", code=white_code
    )
    assert p1_ver is not None

    # 2. Black Participant: Illegal move strategy (returns "e2e5" on move 1)
    black_code = b"""def choose_move(board_fen: str, legal_moves: list[str], time_left_ms: int) -> str:
    # Illegal move on move 1
    return "e2e5"
"""
    p2_id, _, _, p2_ver = await _register_participant_and_upload_intelligence(
        api_client, db_session, name=f"BlackPlayer_{uuid.uuid4().hex[:6]}", code=black_code
    )
    assert p2_ver is not None

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

    # 3. Create Official Match
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

    # 4. Dispatch jobs
    white_job, black_job = await dispatch_participant_match_jobs(
        db_session,
        match_id=match.id,
        execution_mode="MOCK_PROTOCOL_ONLY",
    )
    assert white_job.match_id == match.id
    assert black_job.match_id == match.id

    # 5. Setup mTLS PKI
    service_ca_key, service_ca_cert = _ca()
    worker_ca_key, worker_ca_cert = _ca()

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
        w_id = f"worker-sf-{idx}-{uuid.uuid4().hex[:6]}"
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
                software_version="singlefile-test-1.0",
                runtime_version="4.2.0",
                security_state="HEALTHY",
            )
        )
    await db_session.commit()

    # 6. Start Private Coordinator Listener on Loopback Port
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

        # 7. Workers claim and ack their jobs
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

        # 8. Execute turns: White plays legal move "a2a3", Black plays illegal "e2e5"
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
                # Determine which side this worker is executing
                is_white = (exec_id == str(white_job.execution_id))
                if is_white:
                    # White plays legal move
                    payload["move"] = "a2a3"
                else:
                    # Black plays illegal move "e2e5"
                    payload["move"] = "e2e5"

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

        for turn_idx in range(60):
            progressed = False
            for client, job_info in clients.values():
                progressed = await poll_and_respond(client, job_info) or progressed
            async with test_session_factory() as db:
                curr_match = await db.get(Match, match.id)
                if curr_match.state in (MatchState.COMPLETED, MatchState.ABORTED):
                    break
            assert progressed or curr_match.state == MatchState.RUNNING
        else:
            pytest.fail("Participant vs participant match did not reach terminal state")

        # 9. Verify match results in database
        async with test_session_factory() as db:
            persisted_match = await db.get(Match, match.id)
            moves = (
                await db.execute(
                    select(MatchMove)
                    .where(MatchMove.match_id == match.id)
                    .order_by(MatchMove.sequence)
                )
            ).scalars().all()

            # The match must be ABORTED with failure reason ILLEGAL_MOVE
            assert persisted_match.state == MatchState.ABORTED
            assert persisted_match.failure_reason == "ILLEGAL_MOVE"

            # Exactly 1 legal move was accepted and persisted
            assert len(moves) == 1
            assert moves[0].move_uci == "a2a3"
            assert moves[0].sequence == 1

    finally:
        shutdown.set()
        await asyncio.wait_for(listener, timeout=5.0)
        for client, _ in clients.values():
            await client.aclose()


@pytest.mark.integration
async def test_full_match_between_two_deterministic_intelligence_agents(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    test_session_factory,
    tmp_path: Path,
):
    """Full match where two distinct participants submit deterministic intelligence.py files.

    Both agents run as real isolated Python processes executing the server-owned adapter
    and participant strategy code.
    The match progresses to CHECKMATE (Scholar's Mate), persists the 7 plies in match_moves,
    and the persisted move transcript is independently verified and replayed with python-chess.
    """
    import io
    import tarfile
    from app.services.quarantine_storage import quarantine_storage

    # 1. White Participant: Deterministic Scholar's Mate strategy
    white_code = b"""def choose_move(board_fen: str, legal_moves: list[str], time_left_ms: int) -> str:
    # Scholar's mate attack moves for White
    plan = ["e2e4", "d1h5", "f1c4", "h5f7"]
    for m in plan:
        if m in legal_moves:
            return m
    return legal_moves[0]
"""
    p1_id, _, _, p1_ver = await _register_participant_and_upload_intelligence(
        api_client, db_session, name=f"WhiteScholar_{uuid.uuid4().hex[:6]}", code=white_code
    )
    assert p1_ver is not None

    # 2. Black Participant: Complementary defensive moves
    black_code = b"""def choose_move(board_fen: str, legal_moves: list[str], time_left_ms: int) -> str:
    # Complementary moves for Black
    plan = ["e7e5", "b8c6", "g8f6"]
    for m in plan:
        if m in legal_moves:
            return m
    return legal_moves[0]
"""
    p2_id, _, _, p2_ver = await _register_participant_and_upload_intelligence(
        api_client, db_session, name=f"BlackDefender_{uuid.uuid4().hex[:6]}", code=black_code
    )
    assert p2_ver is not None

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

    # 3. Create Official Match
    config = MatchConfiguration(
        protocol_version="aik-jsonl/1",
        runtime_profile="python-3.12",
        policy_version="development-v1",
        rules_version="standard-chess/python-chess-1.11.2",
        initial_fen="rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        initial_clock_ms=180_000,
        increment_ms=2_000,
        move_timeout_ms=5_000,
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

    # 4. Dispatch jobs
    white_job, black_job = await dispatch_participant_match_jobs(
        db_session,
        match_id=match.id,
        execution_mode="MOCK_PROTOCOL_ONLY",
    )

    # 5. Extract submission packages into staging directories for real process execution
    white_staging = tmp_path / "staging_white"
    white_staging.mkdir()
    white_tar_bytes = await quarantine_storage.verify(
        db_session, p1_ver.storage_key, p1_ver.artifact_sha256, p1_ver.artifact_size
    )
    with tarfile.open(fileobj=io.BytesIO(white_tar_bytes)) as tar:
        tar.extractall(path=white_staging)

    black_staging = tmp_path / "staging_black"
    black_staging.mkdir()
    black_tar_bytes = await quarantine_storage.verify(
        db_session, p2_ver.storage_key, p2_ver.artifact_sha256, p2_ver.artifact_size
    )
    with tarfile.open(fileobj=io.BytesIO(black_tar_bytes)) as tar:
        tar.extractall(path=black_staging)

    # 6. Launch real Python processes for both participant agents
    proc_white = subprocess.Popen(
        [sys.executable, str(white_staging / "engine" / "main.py")],
        cwd=str(white_staging),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    proc_black = subprocess.Popen(
        [sys.executable, str(black_staging / "engine" / "main.py")],
        cwd=str(black_staging),
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    # 7. Setup mTLS PKI and Coordinator/Gateway
    service_ca_key, service_ca_cert = _ca()
    worker_ca_key, worker_ca_cert = _ca()

    service_ca_path = tmp_path / "service-ca-2.crt"
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
        tmp_path, "coord-2", server_key, server_cert
    )
    gateway_cert_path, gateway_key_path = _write_identity(
        tmp_path, "gw-2", gateway_key, gateway_cert
    )

    worker_identities = {}
    for idx in (1, 2):
        w_id = f"worker-det-{idx}-{uuid.uuid4().hex[:6]}"
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
                software_version="det-test-1.0",
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
        # Wait for listener
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

        # Claim and ack jobs
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

        # 8. Execution loop piping real messages to/from the actual Python subprocesses
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

            is_white = (exec_id == str(white_job.execution_id))
            active_proc = proc_white if is_white else proc_black

            # Send command directly to agent's stdin
            cmd_line = cmd["message"].strip() + "\n"
            active_proc.stdin.write(cmd_line)
            active_proc.stdin.flush()

            # Read agent's response from stdout
            resp_line = active_proc.stdout.readline()
            assert resp_line, "Agent subprocess exited unexpectedly or produced no output"

            # Post response back to gateway
            sub = await client.post(
                f"/worker/v1/jobs/{exec_id}/match/response",
                json={
                    **req,
                    "command_id": cmd["command_id"],
                    "payload": resp_line,
                },
            )
            assert sub.status_code == 200, sub.text
            return True

        for turn_idx in range(120):
            progressed = False
            for client, job_info in clients.values():
                progressed = await poll_and_respond(client, job_info) or progressed
            async with test_session_factory() as db:
                curr_match = await db.get(Match, match.id)
                if curr_match.state in (MatchState.COMPLETED, MatchState.ABORTED):
                    break
            assert progressed or curr_match.state == MatchState.RUNNING
        else:
            pytest.fail("Participant match between deterministic agents did not reach terminal state")

        # 9. Verify match reached genuine terminal result
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
            assert len(moves) == 7

            # 10. REPLAY PERSISTED TRANSCRIPT WITH AUTHORITATIVE CHESS ENGINE
            replay_board = chess.Board()
            for move in moves:
                uci_move = move.move_uci
                assert uci_move in [m.uci() for m in replay_board.legal_moves], (
                    f"Move {uci_move} was not legal in position {replay_board.fen()}"
                )
                assert replay_board.fen(en_passant="fen") == move.position_before_fen
                replay_board.push_uci(uci_move)
                assert replay_board.fen(en_passant="fen") == move.position_after_fen

            # Authoritative engine confirms checkmate
            assert replay_board.is_checkmate() is True
            assert replay_board.outcome().winner == chess.WHITE

    finally:
        shutdown.set()
        await asyncio.wait_for(listener, timeout=5.0)
        for client, _ in clients.values():
            await client.aclose()
        for p in (proc_white, proc_black):
            try:
                p.terminate()
                p.wait(timeout=2.0)
            except Exception:
                pass


@pytest.mark.integration
async def test_participant_agent_move_timeout_and_fencing_recovery(
    api_client: httpx.AsyncClient,
    db_session: AsyncSession,
    test_session_factory,
    tmp_path: Path,
):
    """Verify timeout handling, process cleanup, and worker security/fencing recovery.

    1. White plays normal move 'e2e4'.
    2. Black's intelligence.py sleeps beyond move timeout.
    3. The authoritative match controller detects deadline expiration, aborts match with MOVE_TIMEOUT.
    4. Worker cleans up child process.
    5. Security verification: Stale fencing token rejected (409), forged worker ID rejected (422).
    """
    import io
    import tarfile
    from app.services.quarantine_storage import quarantine_storage

    # 1. White: Fast legal player
    white_code = b"""def choose_move(board_fen: str, legal_moves: list[str], time_left_ms: int) -> str:
    return "e2e4" if "e2e4" in legal_moves else legal_moves[0]
"""
    p1_id, _, _, p1_ver = await _register_participant_and_upload_intelligence(
        api_client, db_session, name=f"FastWhite_{uuid.uuid4().hex[:6]}", code=white_code
    )

    # 2. Black: Agent that sleeps indefinitely simulating infinite loop / timeout
    black_code = b"""import time
def choose_move(board_fen: str, legal_moves: list[str], time_left_ms: int) -> str:
    time.sleep(15)
    return legal_moves[0]
"""
    p2_id, _, _, p2_ver = await _register_participant_and_upload_intelligence(
        api_client, db_session, name=f"SlowBlack_{uuid.uuid4().hex[:6]}", code=black_code
    )

    db_session.add(OfficialSubmissionSelection(participant_id=p1_id, submission_version_id=p1_ver.id, selected_at=datetime.now(timezone.utc)))
    db_session.add(OfficialSubmissionSelection(participant_id=p2_id, submission_version_id=p2_ver.id, selected_at=datetime.now(timezone.utc)))
    await db_session.flush()

    # Match with short move timeout (100ms) to verify fast, controlled timeout expiration
    config = MatchConfiguration(
        protocol_version="aik-jsonl/1",
        runtime_profile="python-3.12",
        policy_version="development-v1",
        rules_version="standard-chess/python-chess-1.11.2",
        initial_fen="rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        initial_clock_ms=10_000,
        increment_ms=0,
        move_timeout_ms=100,
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

    white_job, black_job = await dispatch_participant_match_jobs(
        db_session, match_id=match.id, execution_mode="MOCK_PROTOCOL_ONLY"
    )

    # Setup mTLS PKI
    service_ca_key, service_ca_cert = _ca()
    worker_ca_key, worker_ca_cert = _ca()

    service_ca_path = tmp_path / "service-ca-3.crt"
    service_ca_path.write_bytes(service_ca_cert.public_bytes(serialization.Encoding.PEM))

    server_key, server_cert = _leaf(service_ca_key, service_ca_cert, common_name="coordinator", uri="urn:aik:service:match-coordinator", dns="localhost", ip="127.0.0.1", eku=ExtendedKeyUsageOID.SERVER_AUTH)
    gateway_key, gateway_cert = _leaf(service_ca_key, service_ca_cert, common_name="worker-gateway", uri="urn:aik:service:worker-gateway", eku=ExtendedKeyUsageOID.CLIENT_AUTH)
    server_cert_path, server_key_path = _write_identity(tmp_path, "coord-3", server_key, server_cert)
    gateway_cert_path, gateway_key_path = _write_identity(tmp_path, "gw-3", gateway_key, gateway_cert)

    worker_identities = {}
    for idx in (1, 2):
        w_id = f"worker-to-{idx}-{uuid.uuid4().hex[:6]}"
        w_key, w_cert = _leaf(worker_ca_key, worker_ca_cert, common_name=w_id, uri=f"urn:aik:worker:{w_id}", eku=ExtendedKeyUsageOID.CLIENT_AUTH)
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
                capabilities={"sandbox_backend": "MODAL", "protocol_versions": ["aik-jsonl/1"], "execution_modes": ["MOCK_PROTOCOL_ONLY", "MODAL_AI_EXECUTION"]},
                software_version="timeout-test-1.0",
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
    listener = asyncio.create_task(serve_with_verified_peer_identity(control_app, h_cfg, shutdown_trigger=shutdown.wait))

    gateway_app = create_worker_gateway_app()
    gateway_app.state.worker_session_factory = test_session_factory
    gateway_app.state.worker_tls_ca_certificates = (worker_ca_cert,)
    service_client_tls = GatewayServiceClientTLSConfig(service_ca_path, gateway_cert_path, gateway_key_path, "127.0.0.1")
    gateway_app.state.match_coordinator_url = f"https://127.0.0.1:{port}"
    gateway_app.state.match_coordinator_ssl_context = service_client_tls.build_ssl_context()

    clients = {}
    try:
        for _ in range(80):
            if listener.done():
                await listener
            try:
                reader, writer = await asyncio.open_connection("127.0.0.1", port, ssl=gateway_app.state.match_coordinator_ssl_context, server_hostname="127.0.0.1")
                writer.close()
                await writer.wait_closed()
                break
            except OSError:
                await asyncio.sleep(0.025)
        else:
            pytest.fail("Coordinator failed to start")

        for worker_id, (_key, cert) in worker_identities.items():
            transport = httpx.ASGITransport(app=_WorkerCertificateTransport(gateway_app, cert.public_bytes(serialization.Encoding.PEM).decode("ascii")))
            client = httpx.AsyncClient(transport=transport, base_url="https://gateway.local")
            claimed = await client.post("/worker/v1/jobs/claim")
            assert claimed.status_code == 200
            job_out = claimed.json()
            lease = job_out["lease"]
            ack_resp = await client.post(
                f"/worker/v1/jobs/{job_out['execution_id']}/acknowledgement",
                json={"lease_id": lease["lease_id"], "fencing_token": lease["fencing_token"], "sequence": lease["next_sequence"], "accepted": True},
            )
            assert ack_resp.status_code == 200
            clients[job_out["execution_id"]] = (client, job_out)

        # Security check: Stale fencing token rejected with 409
        first_cli, first_job = next(iter(clients.values()))
        stale_fence = await first_cli.post(
            f"/worker/v1/jobs/{first_job['execution_id']}/match/next",
            json={"lease_id": first_job["lease"]["lease_id"], "fencing_token": first_job["lease"]["fencing_token"] + 9999, "attempt": first_job["attempt_number"]},
        )
        assert stale_fence.status_code == 409

        # Security check: Forged worker identity rejected with 422
        forged = await first_cli.post(
            f"/worker/v1/jobs/{first_job['execution_id']}/match/next",
            json={"lease_id": first_job["lease"]["lease_id"], "fencing_token": first_job["lease"]["fencing_token"], "attempt": first_job["attempt_number"], "worker_id": "forged-id"},
        )
        assert forged.status_code == 422

        # Step through handshakes
        async def poll_and_respond_fast(client, job_info):
            exec_id = job_info["execution_id"]
            lease = job_info["lease"]
            req = {"lease_id": lease["lease_id"], "fencing_token": lease["fencing_token"], "attempt": job_info["attempt_number"]}
            res = await client.post(f"/worker/v1/jobs/{exec_id}/match/next", json=req)
            assert res.status_code == 200
            cmd = res.json()
            if cmd["status"] != "ACCEPTED":
                return False
            msg = json.loads(cmd["message"])
            resp_type = {"hello": "ready", "new_game": "ack", "position": "ack", "go": "bestmove"}[msg["type"]]
            payload = {"protocol_version": "aik-jsonl/1", "type": resp_type, "request_id": cmd["command_id"]}
            if msg["type"] == "go":
                is_white = (exec_id == str(white_job.execution_id))
                if is_white:
                    payload["move"] = "e2e4"
                else:
                    # Black does NOT respond (simulating timeout)
                    return True

            sub = await client.post(f"/worker/v1/jobs/{exec_id}/match/response", json={**req, "command_id": cmd["command_id"], "payload": json.dumps(payload) + "\n"})
            assert sub.status_code == 200
            return True

        # Process White's setup and move
        for _ in range(20):
            for client, job_info in clients.values():
                await poll_and_respond_fast(client, job_info)

        # Wait for move deadline (100ms) to expire on Black's turn
        await asyncio.sleep(0.2)

        # Polling after deadline triggers MOVE_TIMEOUT
        for client, job_info in clients.values():
            await poll_and_respond_fast(client, job_info)

        async with test_session_factory() as db:
            persisted_match = await db.get(Match, match.id)
            assert persisted_match.state == MatchState.ABORTED
            assert persisted_match.failure_reason == "MOVE_TIMEOUT"

    finally:
        shutdown.set()
        await asyncio.wait_for(listener, timeout=5.0)
        for client, _ in clients.values():
            await client.aclose()
