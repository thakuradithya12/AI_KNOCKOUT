"""End-to-end submission tests against isolated ASGI + in-memory SQLite fixtures."""

from __future__ import annotations

import hashlib
import uuid

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.competition import (
    ArtifactState,
    ArtifactStateTransition,
    CompetitionEvent,
    QuarantineObject,
    SubmissionVersion,
    ValidationAttempt,
)
from tests.unit.test_submission_validation import _valid_tar


async def _register(api_client: httpx.AsyncClient, *, suffix: str | None = None) -> tuple[str, str]:
    response = await api_client.post(
        "/api/v1/participants/register",
        json={"name": f"Submission_{suffix or uuid.uuid4().hex}"},
    )
    assert response.status_code == 201, response.text
    return response.json()["participant_id"], response.json()["access_key"]


async def _authenticated(api_client: httpx.AsyncClient) -> tuple[str, dict[str, str]]:
    participant_id, key = await _register(api_client)
    response = await api_client.post(
        "/api/v1/auth/login",
        json={"participant_id": participant_id, "access_key": key},
    )
    assert response.status_code == 200, response.text
    return participant_id, {"Authorization": f"Bearer {response.json()['access_token']}"}


async def _create_submission(client: httpx.AsyncClient, headers: dict[str, str]) -> str:
    response = await client.post("/api/v1/submissions", headers=headers)
    assert response.status_code == 201, response.text
    return response.json()["submission_id"]


@pytest.mark.integration
class TestSubmissionIntake:
    async def test_submission_creation_uses_existing_auth_identity(self, api_client: httpx.AsyncClient):
        participant_id, headers = await _authenticated(api_client)
        created = await api_client.post("/api/v1/submissions", headers=headers)
        assert created.status_code == 201
        assert created.json()["participant_id"] == participant_id
        mine = await api_client.get("/api/v1/submissions", headers=headers)
        assert mine.status_code == 200
        assert mine.json()["items"][0]["submission_id"] == created.json()["submission_id"]

    async def test_upload_is_hashed_quarantined_and_validated_without_execution(
        self, api_client: httpx.AsyncClient, db_session: AsyncSession
    ):
        participant_id, headers = await _authenticated(api_client)
        submission_id = await _create_submission(api_client, headers)
        archive = _valid_tar(source=b"raise RuntimeError('must not execute')\n")
        response = await api_client.post(
            f"/api/v1/submissions/{submission_id}/versions",
            content=archive,
            headers={**headers, "Content-Type": "application/x-tar"},
        )
        assert response.status_code == 201, response.text
        payload = response.json()
        version = payload["version"]
        assert version["participant_id"] == participant_id
        assert version["state"] == "APPROVED"
        assert version["artifact_sha256"] == hashlib.sha256(archive).hexdigest()
        assert payload["validation"]["outcome"] == "PASSED"

        blob = await db_session.get(QuarantineObject, version["artifact_sha256"])
        assert blob is not None
        assert blob.archive_bytes == archive
        assert blob.artifact_size == len(archive)
        assert (await db_session.get(SubmissionVersion, uuid.UUID(version["id"]))).state == ArtifactState.APPROVED
        assert (await db_session.execute(select(func.count(ValidationAttempt.id)))).scalar_one() == 1
        transitions = (
            await db_session.execute(
                select(ArtifactStateTransition)
                .where(ArtifactStateTransition.submission_version_id == uuid.UUID(version["id"]))
                .order_by(ArtifactStateTransition.recorded_at)
            )
        ).scalars().all()
        assert [row.to_state for row in transitions] == [
            ArtifactState.QUARANTINED,
            ArtifactState.INSPECTING,
            ArtifactState.VALIDATING,
            ArtifactState.APPROVED,
        ]

    async def test_identical_upload_is_idempotent_and_keeps_one_version_attempt(
        self, api_client: httpx.AsyncClient, db_session: AsyncSession
    ):
        _, headers = await _authenticated(api_client)
        submission_id = await _create_submission(api_client, headers)
        archive = _valid_tar()
        path = f"/api/v1/submissions/{submission_id}/versions"
        first = await api_client.post(path, content=archive, headers={**headers, "Content-Type": "application/x-tar"})
        again = await api_client.post(path, content=archive, headers={**headers, "Content-Type": "application/x-tar"})
        assert first.status_code == 201
        assert again.status_code == 200
        assert again.json()["version"]["duplicate"] is True
        assert again.json()["version"]["id"] == first.json()["version"]["id"]
        assert await db_session.scalar(select(func.count(SubmissionVersion.id))) == 1
        assert await db_session.scalar(select(func.count(ValidationAttempt.id))) == 1

    async def test_invalid_manifest_is_quarantined_with_stable_reason_and_attempt(
        self, api_client: httpx.AsyncClient, db_session: AsyncSession
    ):
        _, headers = await _authenticated(api_client)
        submission_id = await _create_submission(api_client, headers)
        archive = _valid_tar(manifest=(
            b'{"contract_version":"1","protocol_version":"aik-jsonl/1",'
            b'"engine_name":"E","engine_version":"1.0.0","runtime":"python-3.12",'
            b'"entrypoint":"engine/main.py","cpu_limit":1}'
        ))
        response = await api_client.post(
            f"/api/v1/submissions/{submission_id}/versions", content=archive,
            headers={**headers, "Content-Type": "application/x-tar"},
        )
        assert response.status_code == 422
        body = response.json()
        assert body["error"] == "MANIFEST_UNKNOWN_FIELD"
        assert body["version"]["state"] == "REJECTED"
        assert body["validation"]["report"]["code"] == "MANIFEST_UNKNOWN_FIELD"
        assert await db_session.scalar(select(func.count(QuarantineObject.artifact_sha256))) == 1
        assert await db_session.scalar(select(func.count(ValidationAttempt.id))) == 1

    async def test_unsupported_versions_fail_closed(self, api_client: httpx.AsyncClient):
        _, headers = await _authenticated(api_client)
        submission_id = await _create_submission(api_client, headers)
        for archive, code in (
            (_valid_tar(manifest=b'{"contract_version":"2","protocol_version":"aik-jsonl/1","engine_name":"E","engine_version":"1.0.0","runtime":"python-3.12","entrypoint":"engine/main.py"}'), "UNSUPPORTED_CONTRACT_VERSION"),
            (_valid_tar(manifest=b'{"contract_version":"1","protocol_version":"uci","engine_name":"E","engine_version":"1.0.0","runtime":"python-3.12","entrypoint":"engine/main.py"}'), "UNSUPPORTED_PROTOCOL_VERSION"),
            (_valid_tar(manifest=b'{"contract_version":"1","protocol_version":"aik-jsonl/1","engine_name":"E","engine_version":"1.0.0","runtime":"python-3.10","entrypoint":"engine/main.py"}'), "UNSUPPORTED_RUNTIME"),
        ):
            response = await api_client.post(
                f"/api/v1/submissions/{submission_id}/versions", content=archive,
                headers={**headers, "Content-Type": "application/x-tar"},
            )
            assert response.status_code == 422
            assert response.json()["error"] == code

    async def test_same_digest_shared_across_owners_without_identity_collision(
        self, api_client: httpx.AsyncClient, db_session: AsyncSession
    ):
        _, headers_a = await _authenticated(api_client)
        _, headers_b = await _authenticated(api_client)
        submission_a = await _create_submission(api_client, headers_a)
        submission_b = await _create_submission(api_client, headers_b)
        archive = _valid_tar()
        versions = []
        for submission, headers in ((submission_a, headers_a), (submission_b, headers_b)):
            response = await api_client.post(
                f"/api/v1/submissions/{submission}/versions", content=archive,
                headers={**headers, "Content-Type": "application/x-tar"},
            )
            assert response.status_code == 201
            versions.append(response.json()["version"])
        assert versions[0]["artifact_sha256"] == versions[1]["artifact_sha256"]
        assert versions[0]["participant_id"] != versions[1]["participant_id"]
        assert await db_session.scalar(select(func.count(QuarantineObject.artifact_sha256))) == 1
        assert await db_session.scalar(select(func.count(SubmissionVersion.id))) == 2

    async def test_new_digest_appends_version_and_preserves_previous_artifact(
        self, api_client: httpx.AsyncClient, db_session: AsyncSession
    ):
        _, headers = await _authenticated(api_client)
        submission_id = await _create_submission(api_client, headers)
        first_bytes = _valid_tar(source=b"# one\n")
        second_bytes = _valid_tar(source=b"# two\n")
        path = f"/api/v1/submissions/{submission_id}/versions"
        first = await api_client.post(path, content=first_bytes, headers={**headers, "Content-Type": "application/x-tar"})
        second = await api_client.post(path, content=second_bytes, headers={**headers, "Content-Type": "application/x-tar"})
        assert [first.json()["version"]["version"], second.json()["version"]["version"]] == [1, 2]
        assert await db_session.get(QuarantineObject, first.json()["version"]["artifact_sha256"]) is not None
        assert first.json()["version"]["artifact_sha256"] != second.json()["version"]["artifact_sha256"]

    async def test_status_and_validation_results_are_owner_scoped(
        self, api_client: httpx.AsyncClient
    ):
        _, headers_a = await _authenticated(api_client)
        _, headers_b = await _authenticated(api_client)
        submission_id = await _create_submission(api_client, headers_a)
        response = await api_client.post(
            f"/api/v1/submissions/{submission_id}/versions", content=_valid_tar(),
            headers={**headers_a, "Content-Type": "application/x-tar"},
        )
        assert response.status_code == 201
        assert (await api_client.get(f"/api/v1/submissions/{submission_id}", headers=headers_b)).status_code == 404
        assert (await api_client.get(
            f"/api/v1/submissions/{submission_id}/versions/1/validation", headers=headers_b
        )).status_code == 404
        assert (await api_client.post(
            f"/api/v1/submissions/{submission_id}/versions", content=_valid_tar(),
            headers={**headers_b, "Content-Type": "application/x-tar"},
        )).status_code == 404

    async def test_validation_status_endpoint_returns_persisted_attempt(self, api_client: httpx.AsyncClient):
        _, headers = await _authenticated(api_client)
        submission_id = await _create_submission(api_client, headers)
        uploaded = await api_client.post(
            f"/api/v1/submissions/{submission_id}/versions", content=_valid_tar(),
            headers={**headers, "Content-Type": "application/x-tar"},
        )
        result = await api_client.get(
            f"/api/v1/submissions/{submission_id}/versions/1/validation", headers=headers
        )
        assert result.status_code == 200
        assert result.json()["state"] == "APPROVED"
        assert result.json()["attempts"][0]["attempt_id"] == uploaded.json()["validation"]["attempt_id"]

    async def test_unauthenticated_upload_and_arbitrary_state_change_are_denied(self, api_client: httpx.AsyncClient):
        created = await api_client.post("/api/v1/submissions")
        assert created.status_code == 401
        _, headers = await _authenticated(api_client)
        submission_id = await _create_submission(api_client, headers)
        path = f"/api/v1/submissions/{submission_id}/versions"
        denied = await api_client.post(path, content=b"{}", headers={**headers, "Content-Type": "application/json"})
        assert denied.status_code == 415
        assert (await api_client.patch(f"/api/v1/submissions/{submission_id}", json={"state": "ELIGIBLE"}, headers=headers)).status_code == 405

    async def test_oversized_content_length_is_rejected_before_read_or_persistence(
        self, api_client: httpx.AsyncClient, db_session: AsyncSession
    ):
        _, headers = await _authenticated(api_client)
        submission_id = await _create_submission(api_client, headers)
        response = await api_client.post(
            f"/api/v1/submissions/{submission_id}/versions",
            content=b"",
            headers={**headers, "Content-Type": "application/x-tar", "Content-Length": str(16 * 1024 * 1024 + 1)},
        )
        assert response.status_code == 413
        assert response.json()["error"] == "PACKAGE_TOO_LARGE"
        assert await db_session.scalar(select(func.count(QuarantineObject.artifact_sha256))) == 0

    async def test_empty_truncated_package_gets_stable_rejection_and_quarantine_record(
        self, api_client: httpx.AsyncClient, db_session: AsyncSession
    ):
        _, headers = await _authenticated(api_client)
        submission_id = await _create_submission(api_client, headers)
        response = await api_client.post(
            f"/api/v1/submissions/{submission_id}/versions", content=b"short",
            headers={**headers, "Content-Type": "application/x-tar"},
        )
        assert response.status_code == 422
        assert response.json()["error"] == "ARCHIVE_MALFORMED"
        assert await db_session.scalar(select(func.count(ValidationAttempt.id))) == 1
        stored = await db_session.get(QuarantineObject, hashlib.sha256(b"short").hexdigest())
        assert stored is not None and stored.archive_bytes == b"short"

    async def test_model_layer_rejects_artifact_mutation_and_append_only_edits(
        self, api_client: httpx.AsyncClient, db_session: AsyncSession
    ):
        _, headers = await _authenticated(api_client)
        submission_id = await _create_submission(api_client, headers)
        response = await api_client.post(
            f"/api/v1/submissions/{submission_id}/versions", content=_valid_tar(),
            headers={**headers, "Content-Type": "application/x-tar"},
        )
        version_id = uuid.UUID(response.json()["version"]["id"])
        version = await db_session.get(SubmissionVersion, version_id)
        version.artifact_sha256 = "0" * 64
        with pytest.raises(ValueError, match="ARTIFACT_IDENTITY_IMMUTABLE"):
            await db_session.flush()
        await db_session.rollback()

    async def test_invalid_state_transition_and_validation_edit_are_rejected(
        self, api_client: httpx.AsyncClient, db_session: AsyncSession
    ):
        _, headers = await _authenticated(api_client)
        submission_id = await _create_submission(api_client, headers)
        response = await api_client.post(
            f"/api/v1/submissions/{submission_id}/versions", content=_valid_tar(),
            headers={**headers, "Content-Type": "application/x-tar"},
        )
        version = await db_session.get(SubmissionVersion, uuid.UUID(response.json()["version"]["id"]))
        version.state = ArtifactState.VALIDATING
        with pytest.raises(ValueError, match="INVALID_ARTIFACT_STATE_TRANSITION"):
            await db_session.flush()
        await db_session.rollback()

        attempt = (
            await db_session.execute(select(ValidationAttempt))
        ).scalars().one()
        attempt.report = {"changed": True}
        with pytest.raises(ValueError, match="APPEND_ONLY_RECORD_IMMUTABLE"):
            await db_session.flush()
        await db_session.rollback()

    async def test_tampered_stored_bytes_fail_closed_on_duplicate_upload(
        self, api_client: httpx.AsyncClient, db_session: AsyncSession
    ):
        _, headers = await _authenticated(api_client)
        submission_id = await _create_submission(api_client, headers)
        archive = _valid_tar()
        path = f"/api/v1/submissions/{submission_id}/versions"
        first = await api_client.post(path, content=archive, headers={**headers, "Content-Type": "application/x-tar"})
        assert first.status_code == 201
        tampered = bytearray(archive)
        tampered[-1025] ^= 1
        # Deliberately bypass the ORM guard to simulate storage-layer corruption
        # inside this test's disposable in-memory SQLite database only.
        await db_session.execute(
            text("UPDATE quarantine_objects SET archive_bytes = :body WHERE artifact_sha256 = :digest"),
            {"body": bytes(tampered), "digest": hashlib.sha256(archive).hexdigest()},
        )
        await db_session.commit()
        replay = await api_client.post(path, content=archive, headers={**headers, "Content-Type": "application/x-tar"})
        assert replay.status_code == 503
        assert replay.json() == {"error": "ARTIFACT_STORAGE_INTEGRITY_FAILURE"}

    async def test_competition_events_are_distinct_and_created_transactionally(
        self, api_client: httpx.AsyncClient, db_session: AsyncSession
    ):
        _, headers = await _authenticated(api_client)
        submission_id = await _create_submission(api_client, headers)
        response = await api_client.post(
            f"/api/v1/submissions/{submission_id}/versions", content=_valid_tar(),
            headers={**headers, "Content-Type": "application/x-tar"},
        )
        events = (
            await db_session.execute(
                select(CompetitionEvent).where(
                    CompetitionEvent.aggregate_id == uuid.UUID(response.json()["version"]["id"])
                ).order_by(CompetitionEvent.aggregate_sequence)
            )
        ).scalars().all()
        assert [event.event_type for event in events] == [
            "submission.quarantined",
            "submission.state_changed",
            "validation.started",
            "submission.state_changed",
            "submission.state_changed",
            "validation.passed",
        ]
        assert [event.aggregate_sequence for event in events] == list(range(1, 7))
