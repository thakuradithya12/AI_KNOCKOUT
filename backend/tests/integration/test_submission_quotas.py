"""Integration tests for submission quotas, organizer allowances, and API endpoints."""

from __future__ import annotations

import uuid
import httpx
import pytest

from tests.unit.test_submission_validation import _valid_tar

ORGANIZER_SECRET = "isolated-test-only-secret-key-0123456789"


async def _register_and_login(client: httpx.AsyncClient, name_prefix: str = "Player") -> tuple[str, dict[str, str]]:
    name = f"{name_prefix}_{uuid.uuid4().hex[:8]}"
    reg = await client.post("/api/v1/participants/register", json={"name": name})
    assert reg.status_code == 201, reg.text
    part_id = reg.json()["participant_id"]
    access_key = reg.json()["access_key"]

    login = await client.post("/api/v1/auth/login", json={"participant_id": part_id, "access_key": access_key})
    assert login.status_code == 200, login.text
    token = login.json()["access_token"]
    return part_id, {"Authorization": f"Bearer {token}"}


@pytest.mark.asyncio
async def test_participant_quota_lifecycle_and_enforcement(api_client: httpx.AsyncClient):
    # 1. Register participant
    part_id, headers = await _register_and_login(api_client, "QuotaUser")

    # 2. Check initial quota
    quota_res = await api_client.get("/api/v1/submissions/quota", headers=headers)
    assert quota_res.status_code == 200
    quota_data = quota_res.json()
    assert quota_data["participant_id"] == part_id
    assert quota_data["submission_limit"] == 10
    assert quota_data["accepted_submissions_count"] == 0
    assert quota_data["remaining_submissions"] == 10
    assert quota_data["has_pending_request"] is False

    # 3. Create a submission record
    sub_res = await api_client.post("/api/v1/submissions", headers=headers)
    assert sub_res.status_code == 201
    submission_id = sub_res.json()["submission_id"]

    # 4. Upload 10 valid versions
    for i in range(1, 11):
        archive = _valid_tar(source=f"# engine v{i}\n".encode())
        up = await api_client.post(
            f"/api/v1/submissions/{submission_id}/versions",
            content=archive,
            headers={**headers, "Content-Type": "application/x-tar"},
        )
        assert up.status_code == 201, f"Version {i} upload failed: {up.text}"
        assert up.json()["version"]["version"] == i
        assert up.json()["version"]["state"] == "APPROVED"

    # 5. Check quota is now exhausted (10/10)
    quota_res = await api_client.get("/api/v1/submissions/quota", headers=headers)
    assert quota_res.status_code == 200
    assert quota_res.json()["accepted_submissions_count"] == 10
    assert quota_res.json()["remaining_submissions"] == 0

    # 6. Retry of version 10 (identical digest) succeeds idempotently (HTTP 200)
    archive_10 = _valid_tar(source=b"# engine v10\n")
    dup_res = await api_client.post(
        f"/api/v1/submissions/{submission_id}/versions",
        content=archive_10,
        headers={**headers, "Content-Type": "application/x-tar"},
    )
    assert dup_res.status_code == 200
    assert dup_res.json()["version"]["duplicate"] is True

    # 7. Upload 11 is rejected with HTTP 403 SUBMISSION_LIMIT_EXCEEDED
    archive_11 = _valid_tar(source=b"# engine v11\n")
    up11 = await api_client.post(
        f"/api/v1/submissions/{submission_id}/versions",
        content=archive_11,
        headers={**headers, "Content-Type": "application/x-tar"},
    )
    assert up11.status_code == 403
    assert up11.json()["error"] == "SUBMISSION_LIMIT_EXCEEDED"
    assert up11.json()["limit"] == 10
    assert up11.json()["current"] == 10

    # 8. Participant requests quota allowance increase
    req_res = await api_client.post(
        "/api/v1/submissions/allowance-requests",
        json={"requested_limit": 15, "reason": "Need extra attempts for bug fixes"},
        headers=headers,
    )
    assert req_res.status_code == 201
    request_id = req_res.json()["id"]
    assert req_res.json()["status"] == "PENDING"
    assert req_res.json()["requested_limit"] == 15

    # 9. Quota now shows pending request
    quota_res = await api_client.get("/api/v1/submissions/quota", headers=headers)
    assert quota_res.json()["has_pending_request"] is True

    # Participant lists their own requests
    my_requests = await api_client.get("/api/v1/submissions/allowance-requests", headers=headers)
    assert my_requests.status_code == 200
    assert len(my_requests.json()["items"]) == 1
    assert my_requests.json()["items"][0]["id"] == request_id

    # 10. Non-organizer cannot access admin allowance endpoints
    unauth_admin = await api_client.get("/api/v1/admin/allowance-requests")
    assert unauth_admin.status_code == 401

    participant_as_admin = await api_client.get(
        "/api/v1/admin/allowance-requests",
        headers={"X-Organizer-Secret": "wrong-secret"},
    )
    assert participant_as_admin.status_code == 401

    # 11. Organizer lists allowance requests
    admin_headers = {"X-Organizer-Secret": ORGANIZER_SECRET}
    admin_list = await api_client.get("/api/v1/admin/allowance-requests", headers=admin_headers)
    assert admin_list.status_code == 200
    items = admin_list.json()
    assert any(item["id"] == request_id for item in items)

    # 12. Organizer approves the allowance request
    decision_res = await api_client.post(
        f"/api/v1/admin/allowance-requests/{request_id}/decision",
        json={"decision": "APPROVED", "notes": "Approved for official competition prep"},
        headers=admin_headers,
    )
    assert decision_res.status_code == 200
    assert decision_res.json()["status"] == "APPROVED"

    # 13. Participant quota is now 15 with 5 remaining!
    quota_res = await api_client.get("/api/v1/submissions/quota", headers=headers)
    assert quota_res.status_code == 200
    assert quota_res.json()["submission_limit"] == 15
    assert quota_res.json()["accepted_submissions_count"] == 10
    assert quota_res.json()["remaining_submissions"] == 5

    # 14. Upload 11 now succeeds!
    up11_retry = await api_client.post(
        f"/api/v1/submissions/{submission_id}/versions",
        content=archive_11,
        headers={**headers, "Content-Type": "application/x-tar"},
    )
    assert up11_retry.status_code == 201
    assert up11_retry.json()["version"]["version"] == 11
    assert up11_retry.json()["version"]["state"] == "APPROVED"

    # 15. Organizer directly updates allowance to 20
    direct_res = await api_client.post(
        f"/api/v1/admin/participants/{part_id}/allowance",
        json={"submission_limit": 20, "reason": "VIP allowance expansion"},
        headers=admin_headers,
    )
    assert direct_res.status_code == 200
    assert direct_res.json()["submission_limit"] == 20

    # Participant quota is now 20
    quota_res = await api_client.get("/api/v1/submissions/quota", headers=headers)
    assert quota_res.json()["submission_limit"] == 20
    assert quota_res.json()["accepted_submissions_count"] == 11
    assert quota_res.json()["remaining_submissions"] == 9

    # 16. Verify admin participants endpoint includes quota data
    admin_parts = await api_client.get("/api/v1/admin/participants", headers=admin_headers)
    assert admin_parts.status_code == 200
    part_entry = next((p for p in admin_parts.json() if p["participant_id"] == part_id), None)
    assert part_entry is not None
    assert part_entry["submission_limit"] == 20
    assert part_entry["accepted_submissions_count"] == 11
    assert part_entry["remaining_submissions"] == 9

    # 17. Verify audit log includes allowance events
    audit_res = await api_client.get("/api/v1/admin/audit-log", headers=admin_headers)
    assert audit_res.status_code == 200
    event_types = [e["event_type"] for e in audit_res.json()]
    assert "submission_allowance.requested" in event_types
    assert "submission_allowance.approved" in event_types
    assert "submission_allowance.updated" in event_types


@pytest.mark.asyncio
async def test_validation_failure_does_not_consume_quota(api_client: httpx.AsyncClient):
    part_id, headers = await _register_and_login(api_client, "ValFailUser")

    sub_res = await api_client.post("/api/v1/submissions", headers=headers)
    submission_id = sub_res.json()["submission_id"]

    # Upload broken tar that fails static validation
    invalid_tar = b"not-a-valid-tar-archive-contents"
    res = await api_client.post(
        f"/api/v1/submissions/{submission_id}/versions",
        content=invalid_tar,
        headers={**headers, "Content-Type": "application/x-tar"},
    )
    assert res.status_code == 422
    assert res.json()["version"]["state"] == "REJECTED"

    # Quota remains 0/10
    quota_res = await api_client.get("/api/v1/submissions/quota", headers=headers)
    assert quota_res.json()["accepted_submissions_count"] == 0
    assert quota_res.json()["remaining_submissions"] == 10


@pytest.mark.asyncio
async def test_concurrent_uploads_cannot_exceed_quota(api_client: httpx.AsyncClient):
    import asyncio
    part_id, headers = await _register_and_login(api_client, "ConcurUser")

    sub_res = await api_client.post("/api/v1/submissions", headers=headers)
    submission_id = sub_res.json()["submission_id"]

    # Fill quota to 9/10
    for i in range(1, 10):
        archive = _valid_tar(source=f"# engine {i}\n".encode())
        res = await api_client.post(
            f"/api/v1/submissions/{submission_id}/versions",
            content=archive,
            headers={**headers, "Content-Type": "application/x-tar"},
        )
        assert res.status_code == 201

    quota_res = await api_client.get("/api/v1/submissions/quota", headers=headers)
    assert quota_res.json()["accepted_submissions_count"] == 9
    assert quota_res.json()["remaining_submissions"] == 1

    # Launch 5 concurrent uploads for the last slot
    async def _upload_one(index: int):
        archive = _valid_tar(source=f"# concurrent engine {index}\n".encode())
        return await api_client.post(
            f"/api/v1/submissions/{submission_id}/versions",
            content=archive,
            headers={**headers, "Content-Type": "application/x-tar"},
        )

    responses = await asyncio.gather(*[_upload_one(i) for i in range(10, 15)])
    success_count = sum(1 for r in responses if r.status_code == 201)
    forbidden_count = sum(1 for r in responses if r.status_code == 403 and r.json().get("error") == "SUBMISSION_LIMIT_EXCEEDED")

    assert success_count == 1, f"Expected exactly 1 upload to succeed, got {success_count}"
    assert forbidden_count == 4, f"Expected 4 uploads to be rejected with 403, got {forbidden_count}"

    # Final accepted count must be exactly 10
    final_quota = await api_client.get("/api/v1/submissions/quota", headers=headers)
    assert final_quota.json()["accepted_submissions_count"] == 10
    assert final_quota.json()["remaining_submissions"] == 0


@pytest.mark.asyncio
async def test_non_organizers_cannot_modify_allowances(api_client: httpx.AsyncClient):
    part_id, headers = await _register_and_login(api_client, "AttackerUser")

    # 1. Unauthenticated participant endpoints reject with 401
    assert (await api_client.get("/api/v1/submissions/quota")).status_code == 401
    assert (await api_client.post("/api/v1/submissions/allowance-requests", json={"requested_limit": 15, "reason": "No auth"})).status_code == 401
    assert (await api_client.get("/api/v1/submissions/allowance-requests")).status_code == 401

    # 2. Participant token cannot access admin list endpoint
    assert (await api_client.get("/api/v1/admin/allowance-requests", headers=headers)).status_code == 401
    assert (await api_client.get("/api/v1/admin/allowance-requests")).status_code == 401
    assert (await api_client.get("/api/v1/admin/allowance-requests", headers={"X-Organizer-Secret": "wrong"})).status_code == 401

    # 3. Participant token cannot decide requests
    dummy_req_id = uuid.uuid4()
    assert (await api_client.post(
        f"/api/v1/admin/allowance-requests/{dummy_req_id}/decision",
        json={"decision": "APPROVED", "notes": "Attacker approval"},
        headers=headers,
    )).status_code == 401
    assert (await api_client.post(
        f"/api/v1/admin/allowance-requests/{dummy_req_id}/decision",
        json={"decision": "APPROVED", "notes": "Attacker approval"},
        headers={"X-Organizer-Secret": "wrong"},
    )).status_code == 401

    # 4. Participant tries to directly modify allowance
    res = await api_client.post(
        f"/api/v1/admin/participants/{part_id}/allowance",
        json={"submission_limit": 100, "reason": "Self-grant"},
        headers=headers,
    )
    assert res.status_code == 401

    # 5. Participant tries to call with forged secret header
    res_forged = await api_client.post(
        f"/api/v1/admin/participants/{part_id}/allowance",
        json={"submission_limit": 100, "reason": "Self-grant"},
        headers={**headers, "X-Organizer-Secret": "bad-secret"},
    )
    assert res_forged.status_code == 401

    # Allowance remains default 10
    quota_res = await api_client.get("/api/v1/submissions/quota", headers=headers)
    assert quota_res.json()["submission_limit"] == 10


@pytest.mark.asyncio
async def test_postgresql_row_locking_serializes_concurrent_uploads_across_connections():
    """Verify that PostgreSQL row-level locking (with_for_update) serializes concurrent uploads
    across separate database connections/processes without relying on Python in-memory locks.
    """
    import asyncio
    import os
    from datetime import datetime, timezone
    from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
    from sqlalchemy.engine import make_url
    from app.db.url import normalize_async_database_url, get_asyncpg_connect_args
    from app.models.participant import Participant, ParticipantStatus
    from app.models.event import BuildSession, ParticipantSubmissionAllowance
    from app.models.competition import Submission
    from app.services.submissions import _accept_upload_impl, SubmissionLimitExceeded, count_accepted_submissions
    from app.services.submission_validation import DEFAULT_LIMITS

    pg_url = os.environ.get("AIK_TEST_POSTGRES_URL")
    if not pg_url:
        pytest.skip("AIK_TEST_POSTGRES_URL must point to a disposable PostgreSQL verification database")
    parsed_url = make_url(pg_url)
    if parsed_url.host not in {"127.0.0.1", "localhost", "::1"}:
        pytest.fail("PostgreSQL quota verification is restricted to a loopback test server")
    if not (parsed_url.database or "").startswith("aik_verify"):
        pytest.fail("PostgreSQL quota verification requires an aik_verify* disposable database")

    normalized = normalize_async_database_url(pg_url)
    connect_args = get_asyncpg_connect_args(pg_url)
    engine = create_async_engine(normalized, connect_args=connect_args)
    async with engine.connect():
        pass

    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    p_id = f"part_pg_{uuid.uuid4().hex[:8]}"

    async with session_factory() as session:
        part = Participant(
            participant_id=p_id,
            name=f"PG Concurrency Player {p_id}",
            email=f"{p_id}@example.internal",
            access_key_hash="dummy_hash",
            status=ParticipantStatus.ACTIVE,
            created_at=datetime.now(timezone.utc),
        )
        session.add(part)
        await session.flush()
        session.add(BuildSession(participant_id=p_id, started_at=datetime.now(timezone.utc), start_source="REGISTRATION"))
        session.add(ParticipantSubmissionAllowance(participant_id=p_id, submission_limit=1))
        sub = Submission(id=uuid.uuid4(), participant_id=p_id, created_at=datetime.now(timezone.utc))
        session.add(sub)
        await session.commit()
        sub_id = sub.id

    archive_1 = _valid_tar(source=b"# pg worker 1\n")
    archive_2 = _valid_tar(source=b"# pg worker 2\n")

    results = []
    errors = []

    async def upload_worker(idx, archive_bytes):
        async with session_factory() as worker_session:
            try:
                # Direct call to _accept_upload_impl bypasses Python in-memory asyncio.Lock,
                # proving that PostgreSQL row-level locking (with_for_update) alone prevents
                # concurrent uploads across separate processes from exceeding the quota.
                res = await _accept_upload_impl(
                    worker_session,
                    participant_id=p_id,
                    submission_id=sub_id,
                    archive_bytes=archive_bytes,
                    correlation_id=f"worker-{idx}",
                    limits=DEFAULT_LIMITS,
                )
                results.append((idx, res))
            except SubmissionLimitExceeded as exc:
                errors.append((idx, "SubmissionLimitExceeded", exc.limit, exc.current))

    await asyncio.gather(
        upload_worker(1, archive_1),
        upload_worker(2, archive_2),
    )

    assert len(results) == 1, f"Expected exactly 1 upload to succeed, got {len(results)}"
    assert len(errors) == 1, f"Expected 1 upload to be rejected, got {len(errors)}"
    assert errors[0][1] == "SubmissionLimitExceeded"

    async with session_factory() as session:
        cnt = await count_accepted_submissions(session, p_id)
        assert cnt == 1

    await engine.dispose()


