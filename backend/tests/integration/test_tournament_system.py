"""Integration tests for official tournament scheduling, spectator views, and admin control plane."""

from __future__ import annotations

import os
import uuid
import httpx
import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import Settings
from app.models.competition import ArtifactState
from tests.unit.test_submission_validation import _valid_tar


async def _register_and_submit_eligible(api_client: httpx.AsyncClient, name: str) -> dict:
    reg = await api_client.post("/api/v1/participants/register", json={"name": name, "team_name": f"Team {name}"})
    assert reg.status_code == 201
    pid, key = reg.json()["participant_id"], reg.json()["access_key"]

    login = await api_client.post("/api/v1/auth/login", json={"participant_id": pid, "access_key": key})
    assert login.status_code == 200
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}

    sub = await api_client.post("/api/v1/submissions", headers=headers)
    assert sub.status_code == 201
    sid = sub.json()["submission_id"]

    archive = _valid_tar(source=f"# {name} AI\n".encode())
    upload = await api_client.post(
        f"/api/v1/submissions/{sid}/versions",
        content=archive,
        headers={**headers, "Content-Type": "application/x-tar"},
    )
    assert upload.status_code == 201
    vid = upload.json()["version"]["id"]

    return {
        "participant_id": pid,
        "name": name,
        "token": token,
        "headers": headers,
        "submission_id": sid,
        "version_id": vid,
        "version": upload.json()["version"]["version"],
    }


@pytest.mark.asyncio
async def test_tournament_admin_and_spectator_flow(api_client: httpx.AsyncClient, db_session: AsyncSession):
    settings = Settings()
    admin_headers = {"X-Organizer-Secret": settings.secret_key}

    # 1. Verify admin requires secret
    unauthorized = await api_client.get("/api/v1/admin/overview")
    assert unauthorized.status_code == 401
    assert unauthorized.json()["detail"] == "ORGANIZER_AUTHENTICATION_REQUIRED"

    # 2. Check admin overview
    overview = await api_client.get("/api/v1/admin/overview", headers=admin_headers)
    assert overview.status_code == 200
    assert "participants_count" in overview.json()

    # 3. Register two participants and submit code
    p1 = await _register_and_submit_eligible(api_client, "Grandmaster_A")
    p2 = await _register_and_submit_eligible(api_client, "Grandmaster_B")

    # 4. Admin lists participants
    p_list = await api_client.get("/api/v1/admin/participants", headers=admin_headers)
    assert p_list.status_code == 200
    p_ids = {p["participant_id"] for p in p_list.json()}
    assert p1["participant_id"] in p_ids
    assert p2["participant_id"] in p_ids

    # 5. Admin lists submissions
    sub_list = await api_client.get("/api/v1/admin/submissions", headers=admin_headers)
    assert sub_list.status_code == 200
    sub_vids = {s["version_id"] for s in sub_list.json()}
    assert p1["version_id"] in sub_vids
    assert p2["version_id"] in sub_vids

    # 6. Admin approves submissions for eligibility
    app1 = await api_client.post(
        "/api/v1/admin/submissions/approve",
        headers=admin_headers,
        json={"submission_version_id": p1["version_id"], "reviewer_id": "chief-arbiter"},
    )
    assert app1.status_code == 200
    assert app1.json()["state"] == "ELIGIBLE"

    app2 = await api_client.post(
        "/api/v1/admin/submissions/approve",
        headers=admin_headers,
        json={"submission_version_id": p2["version_id"], "reviewer_id": "chief-arbiter"},
    )
    assert app2.status_code == 200
    assert app2.json()["state"] == "ELIGIBLE"

    # 7. Participants select their official submissions
    sel1 = await api_client.put(
        "/api/v1/participants/me/official-submission",
        headers=p1["headers"],
        json={"submission_id": p1["submission_id"], "version": p1["version"]},
    )
    assert sel1.status_code == 200

    sel2 = await api_client.put(
        "/api/v1/participants/me/official-submission",
        headers=p2["headers"],
        json={"submission_id": p2["submission_id"], "version": p2["version"]},
    )
    assert sel2.status_code == 200

    # 8. Admin triggers official freeze
    freeze = await api_client.post(
        "/api/v1/admin/freeze",
        headers=admin_headers,
        json={"reason_code": "OFFICIAL_CHAMPIONSHIP_FREEZE", "actor_id": "event-director"},
    )
    assert freeze.status_code == 200
    assert freeze.json()["status"] == "FROZEN"

    # 9. Verify public spectator bracket is generated
    bracket = await api_client.get("/api/v1/tournaments/bracket")
    assert bracket.status_code == 200
    bracket_data = bracket.json()
    assert bracket_data["status"] == "IN_PROGRESS"
    assert bracket_data["total_participants"] >= 2
    assert len(bracket_data["rounds"]) >= 1

    # 10. Admin schedules official tournament match between P1 and P2
    sched = await api_client.post(
        "/api/v1/admin/tournaments/schedule-match",
        headers=admin_headers,
        json={
            "white_submission_version_id": p1["version_id"],
            "black_submission_version_id": p2["version_id"],
            "initial_clock_ms": 180_000,
            "increment_ms": 2_000,
            "move_timeout_ms": 3_000,
        },
    )
    assert sched.status_code == 200
    match_id = sched.json()["match_id"]
    assert sched.json()["purpose"] == "OFFICIAL"

    # 11. Spectator queries tournament matches list
    t_matches = await api_client.get("/api/v1/tournaments/matches")
    assert t_matches.status_code == 200
    match_ids = [m["match_id"] for m in t_matches.json()]
    assert match_id in match_ids

    # 12. Spectator queries specific match detail
    detail = await api_client.get(f"/api/v1/tournaments/matches/{match_id}")
    assert detail.status_code == 200
    assert detail.json()["purpose"] == "OFFICIAL"
    assert detail.json()["state"] == "QUEUED"
    assert detail.json()["current_fen"] == "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"

    # 13. Public spectator leaderboard
    lb = await api_client.get("/api/v1/tournaments/leaderboard")
    assert lb.status_code == 200
    standings = lb.json()["standings"]
    assert len(standings) >= 2
    standing_names = {s["name"] for s in standings}
    assert "Grandmaster_A" in standing_names
    assert "Grandmaster_B" in standing_names


@pytest.mark.asyncio
async def test_empty_tournament_states_return_clean_200(api_client: httpx.AsyncClient):
    """Clean empty tournament queries return 200 with empty collections (not errors or nulls)."""
    # 1. Rounds endpoint
    res_rounds = await api_client.get("/api/v1/tournaments/rounds")
    assert res_rounds.status_code == 200
    assert isinstance(res_rounds.json(), list)

    # 2. Matches endpoint
    res_matches = await api_client.get("/api/v1/tournaments/matches")
    assert res_matches.status_code == 200
    assert isinstance(res_matches.json(), list)

    # 3. Leaderboard endpoint
    res_lb = await api_client.get("/api/v1/tournaments/leaderboard")
    assert res_lb.status_code == 200
    lb_data = res_lb.json()
    assert "standings" in lb_data
    assert isinstance(lb_data["standings"], list)

    # 4. Bracket endpoint
    res_bracket = await api_client.get("/api/v1/tournaments/bracket")
    assert res_bracket.status_code == 200
    bracket_data = res_bracket.json()
    assert "rounds" in bracket_data
    assert isinstance(bracket_data["rounds"], list)


@pytest.mark.asyncio
async def test_genuine_database_failures_return_explicit_500(
    api_client: httpx.AsyncClient, monkeypatch
):
    """When a database query genuinely fails, endpoints return explicit 500 errors instead of masking as empty."""
    from app.api.deps import get_db

    class FailingSession:
        async def execute(self, *args, **kwargs):
            raise RuntimeError("SIMULATED_DATABASE_CONNECTION_FAILURE")

        async def scalars(self, *args, **kwargs):
            raise RuntimeError("SIMULATED_DATABASE_CONNECTION_FAILURE")

        async def rollback(self):
            pass

    from app.main import app
    app.dependency_overrides[get_db] = lambda: FailingSession()

    try:
        # 1. Rounds fails with 500
        res_rounds = await api_client.get("/api/v1/tournaments/rounds")
        assert res_rounds.status_code == 500
        assert res_rounds.json()["detail"] == "TOURNAMENT_ROUNDS_UNAVAILABLE"

        # 2. Matches fails with 500
        res_matches = await api_client.get("/api/v1/tournaments/matches")
        assert res_matches.status_code == 500
        assert res_matches.json()["detail"] == "TOURNAMENT_MATCHES_UNAVAILABLE"

        # 3. Leaderboard fails with 500
        res_lb = await api_client.get("/api/v1/tournaments/leaderboard")
        assert res_lb.status_code == 500
        assert res_lb.json()["detail"] == "TOURNAMENT_LEADERBOARD_UNAVAILABLE"

        # 4. Bracket fails with 500
        res_bracket = await api_client.get("/api/v1/tournaments/bracket")
        assert res_bracket.status_code == 500
        assert res_bracket.json()["detail"] == "TOURNAMENT_BRACKET_UNAVAILABLE"
    finally:
        app.dependency_overrides.pop(get_db, None)
