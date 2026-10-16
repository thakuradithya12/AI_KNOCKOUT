"""Complete end-to-end tournament lifecycle integration test:
Registration -> Submission -> Practice Match -> Official Tournament Freeze ->
Category Round-Robin -> Final Clash -> Final Champion Crowning.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.competition import ArtifactState, Submission, SubmissionVersion
from app.models.match import Match, MatchPurpose, MatchResultSource, MatchState
from app.models.event import DevelopmentMatchPolicy, EventControl, TournamentPairingRecord, TournamentRoundRecord
from app.models.participant import Participant
from app.services.development_matches import calculate_policy_digest
from app.services.house_ai_configuration import (
    DEVELOPMENT_POLICY_ID,
    DEVELOPMENT_POLICY_VERSION,
    activate_house_ai_version,
    register_and_freeze_house_ai_version,
)
from app.services.event import freeze_official_submissions
from app.services.submissions import accept_upload
from app.tournaments.service import (
    advance_final_clash_stage,
    initialize_final_clash,
    schedule_category_round_robin_round,
)
from tests.fixtures.trusted_engine.builder import build_trusted_engine_archive


def _policy_document() -> dict:
    return {
        "policy_id": DEVELOPMENT_POLICY_ID,
        "version": DEVELOPMENT_POLICY_VERSION,
        "policy_version": DEVELOPMENT_POLICY_VERSION,
        "protocol_version": "aik-jsonl/1",
        "runtime_profile": "python-3.12",
        "rules_version": "standard-chess/python-chess-1.11.2",
        "initial_fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
        "initial_clock_ms": 300000,
        "increment_ms": 0,
        "move_timeout_ms": 2000,
        "resources": {
            "cpu_millicores": 1000,
            "memory_bytes": 268435456,
            "pid_limit": 16,
            "tmpfs_bytes": 33554432,
            "inode_limit": 128,
            "nofile_limit": 64,
            "stdout_line_bytes": 65536,
            "stdout_total_bytes": 1048576,
            "stderr_total_bytes": 262144,
            "startup_timeout_ms": 5000,
            "ready_timeout_ms": 2000,
            "move_timeout_ms": 2000,
            "process_lifetime_ms": 900000,
            "concurrency_per_worker": 1,
            "network_mode": "DENY_ALL",
        },
    }


async def _setup_house_ai_and_policy(db: AsyncSession) -> None:
    doc = _policy_document()
    digest = calculate_policy_digest(doc)
    existing_policy = await db.get(DevelopmentMatchPolicy, (DEVELOPMENT_POLICY_ID, DEVELOPMENT_POLICY_VERSION))
    if not existing_policy:
        db.add(
            DevelopmentMatchPolicy(
                policy_id=DEVELOPMENT_POLICY_ID,
                version=DEVELOPMENT_POLICY_VERSION,
                policy_digest=digest,
                policy_document=doc,
                frozen_at=datetime.now(timezone.utc),
            )
        )
        await db.flush()
    house_ai_id = f"house-{uuid.uuid4().hex[:16]}"
    await register_and_freeze_house_ai_version(
        db,
        house_ai_id=house_ai_id,
        version=1,
        archive_bytes=b"trusted inert House AI fixture",
        protocol_version="aik-jsonl/1",
        runtime_profile="python-3.12",
        runtime_identity_sha256="c" * 64,
        rules_version="standard-chess/python-chess-1.11.2",
        policy_version=DEVELOPMENT_POLICY_VERSION,
        policy_digest=digest,
        actor_id="test-organizer",
        reason_code="TEST_SETUP",
    )
    await activate_house_ai_version(
        db,
        house_ai_id=house_ai_id,
        version=1,
        actor_id="test-organizer",
        reason_code="TEST_SELECTION",
    )
    await db.commit()


async def _complete_match(db: AsyncSession, match_id: uuid.UUID, outcome: str = "WHITE_WIN") -> None:
    """Transition a match through its valid state machine to COMPLETED."""
    now = datetime.now(timezone.utc)
    m = await db.get(Match, match_id)
    assert m is not None
    m.state = MatchState.STARTING
    await db.flush()
    m.state = MatchState.RUNNING
    m.started_at = now
    await db.flush()
    m.state = MatchState.COMPLETED
    m.result_outcome = outcome
    m.result_reason = "CHECKMATE"
    m.result_source = MatchResultSource.UNKNOWN
    m.completed_at = now
    await db.flush()


@pytest.mark.integration
class TestEndToEndTournamentWorkflow:
    """Comprehensive end-to-end verification of registration, submission, practice, freeze,
    category round-robin, and final clash progression using isolated local data.
    """

    async def test_full_registration_to_champion_lifecycle(
        self,
        api_client: httpx.AsyncClient,
        db_session: AsyncSession,
    ):
        tournament_id = f"e2e-tourney-{uuid.uuid4().hex[:8]}"

        # ====================================================================
        # 1. REGISTRATION: Register 6 participants
        # ====================================================================
        participants_data = []
        names = [
            "Alpha Grandmaster",
            "Beta Tactician",
            "Gamma Strategist",
            "Delta Challenger",
            "Epsilon Master",
            "Zeta Champion",
        ]

        for name in names:
            idem_key = f"reg-{uuid.uuid4().hex}"
            reg_res = await api_client.post(
                "/api/v1/participants/register",
                json={"name": name, "team_name": f"Team {name.split()[0]}"},
                headers={"Idempotency-Key": idem_key},
            )
            assert reg_res.status_code == 201
            data = reg_res.json()
            assert data["participant_id"].startswith("part_")
            assert data["access_key"].startswith("ak_")
            participants_data.append(data)

        # ====================================================================
        # 2. AUTHENTICATION & LOGIN
        # ====================================================================
        tokens = []
        for p in participants_data:
            login_res = await api_client.post(
                "/api/v1/auth/login",
                json={"participant_id": p["participant_id"], "access_key": p["access_key"]},
            )
            assert login_res.status_code == 200
            token = login_res.json()["access_token"]
            tokens.append(token)

            # Verify profile access
            me_res = await api_client.get(
                "/api/v1/participants/me",
                headers={"Authorization": f"Bearer {token}"},
            )
            assert me_res.status_code == 200
            assert me_res.json()["name"] == p["name"]

        # ====================================================================
        # 3. SUBMISSION INTAKE & VALIDATION
        # ====================================================================
        archive = build_trusted_engine_archive()
        sub_versions = []

        for idx, (p, token) in enumerate(zip(participants_data, tokens)):
            auth_headers = {"Authorization": f"Bearer {token}"}
            # Create submission
            create_res = await api_client.post("/api/v1/submissions", headers=auth_headers)
            assert create_res.status_code == 201
            sub_id = create_res.json()["submission_id"]

            # Upload version
            upload_res = await api_client.post(
                f"/api/v1/submissions/{sub_id}/versions",
                headers={**auth_headers, "Content-Type": "application/x-tar"},
                content=archive,
            )
            assert upload_res.status_code == 201
            up_data = upload_res.json()
            assert up_data["validation"]["outcome"] == "PASSED"
            assert up_data["version"]["state"] == "APPROVED"
            version_id = up_data["version"]["id"]

            # Organizer approves submission for eligibility
            settings = Settings()
            app_res = await api_client.post(
                "/api/v1/admin/submissions/approve",
                headers={"X-Organizer-Secret": settings.secret_key},
                json={"submission_version_id": version_id, "reviewer_id": "chief-arbiter"},
            )
            assert app_res.status_code == 200
            assert app_res.json()["state"] == "ELIGIBLE"

            # Select official submission
            sel_res = await api_client.put(
                "/api/v1/participants/me/official-submission",
                headers=auth_headers,
                json={"submission_id": sub_id, "version": 1},
            )
            assert sel_res.status_code == 200
            sub_versions.append((p["participant_id"], uuid.UUID(sub_id), 1))

        # ====================================================================
        # 4. PRACTICE (DEVELOPMENT) MATCH
        # ====================================================================
        await _setup_house_ai_and_policy(db_session)
        dev_res = await api_client.post(
            "/api/v1/participants/me/development-matches",
            headers={
                "Authorization": f"Bearer {tokens[0]}",
                "Idempotency-Key": f"dev-{uuid.uuid4().hex}",
            },
            json={"submission_id": str(sub_versions[0][1]), "version": 1},
        )
        assert dev_res.status_code == 201
        dev_match_data = dev_res.json()
        dev_match_id = dev_match_data["match_id"]
        assert dev_match_data["purpose"] == "DEVELOPMENT"

        # Complete development match
        await _complete_match(db_session, uuid.UUID(dev_match_id), outcome="WHITE_WIN")
        await db_session.commit()

        # ====================================================================
        # 5. SUBMISSION FREEZE & AUDIT
        # ====================================================================
        await freeze_official_submissions(
            db_session,
            actor_id="organizer-test",
            reason_code="OFFICIAL_FREEZE_COMMENCED",
        )
        await db_session.commit()

        # 5a. Verify participants CANNOT upload post-freeze (Requirement 4)
        post_freeze_upload = await api_client.post(
            f"/api/v1/submissions/{sub_versions[0][1]}/versions",
            headers={"Authorization": f"Bearer {tokens[0]}", "Content-Type": "application/x-tar"},
            content=archive,
        )
        assert post_freeze_upload.status_code == 409
        assert post_freeze_upload.json()["error"] == "OFFICIAL_SUBMISSIONS_FROZEN"

        # 5b. Verify direct service call fails closed with bypass_freeze=False
        from app.services.submission_validation import DEFAULT_LIMITS
        with pytest.raises(ValueError, match="OFFICIAL_SUBMISSIONS_FROZEN"):
            await accept_upload(
                db_session,
                participant_id=participants_data[0]["participant_id"],
                submission_id=sub_versions[0][1],
                archive_bytes=archive,
                correlation_id=None,
                limits=DEFAULT_LIMITS,
                bypass_freeze=False,
            )

        # ====================================================================
        # 6. CATEGORY ROUND-ROBIN SCHEDULING (Requirement 2 & 5)
        # ====================================================================
        # 4 participants -> Category A gets 2, Category B gets 2 (both even)
        round_a1 = await schedule_category_round_robin_round(
            db_session,
            category="A",
            round_number=1,
            tournament_id=tournament_id,
            use_house_ai_filler=True,
        )
        assert round_a1.tournament_id == f"{tournament_id}:CAT_A"
        assert round_a1.status == "IN_PROGRESS"

        round_b1 = await schedule_category_round_robin_round(
            db_session,
            category="B",
            round_number=1,
            tournament_id=tournament_id,
            use_house_ai_filler=True,
        )
        assert round_b1.tournament_id == f"{tournament_id}:CAT_B"
        assert round_b1.status == "IN_PROGRESS"

        # Verify leaderboard returns category standings and combined standings
        lb_res = await api_client.get("/api/v1/tournaments/leaderboard")
        assert lb_res.status_code == 200
        lb_data = lb_res.json()
        assert "A" in lb_data["categories"]
        assert "B" in lb_data["categories"]
        assert len(lb_data["categories"]["A"]) >= 3
        assert len(lb_data["categories"]["B"]) >= 3

        # Complete round-robin matches for both categories
        pairings_r1 = (
            await db_session.execute(
                select(TournamentPairingRecord).where(
                    TournamentPairingRecord.round_id.in_([round_a1.id, round_b1.id])
                )
            )
        ).scalars().all()

        for p in pairings_r1:
            if not p.is_bye:
                if p.game1_match_id:
                    await _complete_match(db_session, p.game1_match_id, outcome="WHITE_WIN")
                if p.game2_match_id:
                    await _complete_match(db_session, p.game2_match_id, outcome="BLACK_WIN")
        await db_session.commit()

        # ====================================================================
        # 7. FINAL CLASH PLAYOFFS & PROGRESSION
        # ====================================================================
        qf_round = await initialize_final_clash(db_session, tournament_id=tournament_id)
        assert qf_round is not None
        assert "Quarterfinals" in qf_round.round_name
        assert qf_round.tournament_id == tournament_id

        # Complete Quarterfinals matches
        qf_pairings = (
            await db_session.execute(
                select(TournamentPairingRecord).where(
                    TournamentPairingRecord.round_id == qf_round.id
                )
            )
        ).scalars().all()

        for p in qf_pairings:
            if not p.is_bye:
                if p.game1_match_id:
                    await _complete_match(db_session, p.game1_match_id, outcome="WHITE_WIN")
                if p.game2_match_id:
                    await _complete_match(db_session, p.game2_match_id, outcome="BLACK_WIN")
        await db_session.commit()

        # Advance to Semifinals
        sf_round = await advance_final_clash_stage(db_session, tournament_id=tournament_id)
        assert sf_round is not None
        assert "Semifinals" in sf_round.round_name

        # Complete Semifinals matches
        sf_pairings = (
            await db_session.execute(
                select(TournamentPairingRecord).where(
                    TournamentPairingRecord.round_id == sf_round.id
                )
            )
        ).scalars().all()

        for p in sf_pairings:
            if not p.is_bye:
                if p.game1_match_id:
                    await _complete_match(db_session, p.game1_match_id, outcome="WHITE_WIN")
                if p.game2_match_id:
                    await _complete_match(db_session, p.game2_match_id, outcome="BLACK_WIN")
        await db_session.commit()

        # Advance to Finals
        finals_round = await advance_final_clash_stage(db_session, tournament_id=tournament_id)
        assert finals_round is not None
        assert "Finals" in finals_round.round_name

        # Complete Finals matches
        finals_pairings = (
            await db_session.execute(
                select(TournamentPairingRecord).where(
                    TournamentPairingRecord.round_id == finals_round.id
                )
            )
        ).scalars().all()

        for p in finals_pairings:
            if not p.is_bye:
                if p.game1_match_id:
                    await _complete_match(db_session, p.game1_match_id, outcome="WHITE_WIN")
                if p.game2_match_id:
                    await _complete_match(db_session, p.game2_match_id, outcome="BLACK_WIN")
        await db_session.commit()

        # Advance to crown Champion
        final_completed_round = await advance_final_clash_stage(db_session, tournament_id=tournament_id)
        assert final_completed_round is not None
        assert final_completed_round.status == "COMPLETED"

        # Verify finalized rounds and champion via public tournament API
        rounds_res = await api_client.get("/api/v1/tournaments/rounds")
        assert rounds_res.status_code == 200
        rounds_data = rounds_res.json()
        assert len(rounds_data) >= 3

        final_round_data = next((r for r in rounds_data if "Finals" in r["round_name"]), None)
        assert final_round_data is not None
        assert final_round_data["status"] == "COMPLETED"
        assert len(final_round_data["pairings"]) == 1
        champion_id = final_round_data["pairings"][0]["winner_id"]
        assert champion_id is not None
        assert champion_id.startswith("part_")

        # Verify public bracket endpoint returns clean 200
        bracket_res = await api_client.get("/api/v1/tournaments/bracket")
        assert bracket_res.status_code == 200
        assert "rounds" in bracket_res.json()
