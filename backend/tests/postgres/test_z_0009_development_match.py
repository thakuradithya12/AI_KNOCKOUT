"""PostgreSQL-only migration 0009 policy, House AI and capability checks.

This module upgrades a fresh disposable database through migration 0009. It runs
after the frozen 0008 trigger matrix and relies only on explicit test URLs.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timedelta, timezone

import psycopg2
import pytest
from psycopg2.extras import Json, register_uuid
from test_0008_migrations import _admin_url, _alembic, _db_url
from test_typed_match_postgres import _house_version, _match, _participant

register_uuid()

POLICY_DIGEST = "a64214feecc2a3e346d8a53ecd21be79cc8e888325f2dfe93c5b93d895cf9e33"
POLICY_ID = "DEVELOPMENT"
POLICY_VERSION = "development-v1"


def _selection(
    cursor,
    *,
    house_ai_id: str,
    version: int,
    digest: str,
    policy_digest: str = POLICY_DIGEST,
) -> uuid.UUID:
    selection_id = uuid.uuid4()
    now = datetime.now(timezone.utc)
    cursor.execute(
        """INSERT INTO event_house_ai_selections
           (selection_id,house_ai_id,house_ai_version,artifact_sha256,protocol_version,
            runtime_profile,runtime_identity_sha256,rules_version,policy_version,policy_digest,
            selected_by,selection_reason,selected_at)
           SELECT %s,%s,%s,%s,'aik-jsonl/1','python-3.12',%s,
                  'standard-chess/python-chess-1.11.2',%s,%s,'pg-test','PG_TEST',%s""",
        (selection_id, house_ai_id, version, digest, "c" * 64,
         POLICY_VERSION, policy_digest, now),
    )
    return selection_id


def _insert_house_ai(cursor, *, house_ai_id: str, version: int, payload: bytes,
                     policy_digest: str = POLICY_DIGEST) -> str:
    digest = hashlib.sha256(payload).hexdigest()
    now = datetime.now(timezone.utc)
    cursor.execute(
        "INSERT INTO house_ais (house_ai_id,created_at) VALUES (%s,%s) "
        "ON CONFLICT (house_ai_id) DO NOTHING",
        (house_ai_id, now),
    )
    cursor.execute(
        """INSERT INTO house_ai_artifacts
           (artifact_sha256,artifact_size,storage_ref,archive_bytes,created_at)
           VALUES (%s,%s,%s,%s,%s) ON CONFLICT (artifact_sha256) DO NOTHING""",
        (digest, len(payload), f"house-ai://sha256/{digest}", psycopg2.Binary(payload), now),
    )
    cursor.execute(
        """INSERT INTO house_ai_versions
           (house_ai_id,version,artifact_sha256,artifact_size,protocol_version,runtime_profile,
            runtime_identity_sha256,rules_version,policy_version,policy_digest,created_at,frozen_at)
           VALUES (%s,%s,%s,%s,'aik-jsonl/1','python-3.12',%s,
                   'standard-chess/python-chess-1.11.2',%s,%s,%s,%s)""",
        (house_ai_id, version, digest, len(payload), "c" * 64, POLICY_VERSION,
         policy_digest, now, now),
    )
    return digest


def test_pg_0009_fresh_chain_policy_selection_and_typed_capability():
    admin_url = _admin_url()
    admin = psycopg2.connect(admin_url)
    admin.autocommit = True
    cursor = admin.cursor()
    database = "aik_verify_0009"
    try:
        # The 0008 PostgreSQL matrix has completed at this point. Recreate a
        # fresh database so the migration chain and role grants are exercised.
        cursor.execute("DROP DATABASE IF EXISTS aik_verify WITH (FORCE)")
        cursor.execute("DROP ROLE IF EXISTS aik_worker_gateway")
        cursor.execute(f"CREATE DATABASE {database}")
        _alembic(admin_url, database, "head")
        dsn = _db_url(admin_url, database)

        with psycopg2.connect(dsn) as conn:
            cur = conn.cursor()
            cur.execute("SELECT version_num FROM alembic_version")
            assert cur.fetchone()[0] == "0009_development_match_v1"
            cur.execute(
                "SELECT policy_id,version,policy_digest FROM development_match_policies"
            )
            assert cur.fetchone() == (POLICY_ID, POLICY_VERSION, POLICY_DIGEST)

            house_id = f"house-pg-{uuid.uuid4().hex[:12]}"
            payload = b"PostgreSQL controlled inert House AI bytes"
            digest = _insert_house_ai(cur, house_ai_id=house_id, version=1, payload=payload)
            _selection(
                cur, house_ai_id=house_id, version=1, digest=digest
            )

        with pytest.raises(psycopg2.Error):
            with psycopg2.connect(dsn) as conn:
                _selection(
                    conn.cursor(), house_ai_id=house_id, version=1, digest=digest
                )

        # Selection identity is append-only; only one-way deactivation allows
        # replacing the active House AI selection.
        with psycopg2.connect(dsn) as conn:
            cur = conn.cursor()
            cur.execute(
                """UPDATE event_house_ai_selections
                   SET deactivated_at=now(),deactivated_by='pg-test',
                       deactivation_reason='PG_ROTATION'
                   WHERE selection_id=(SELECT selection_id FROM event_house_ai_selections
                                       WHERE deactivated_at IS NULL)"""
            )
            _selection(cur, house_ai_id=house_id, version=1, digest=digest)
            cur.execute(
                "SELECT count(*) FROM event_house_ai_selections WHERE deactivated_at IS NULL"
            )
            assert cur.fetchone()[0] == 1

        with pytest.raises(psycopg2.Error):
            with psycopg2.connect(dsn) as conn:
                conn.cursor().execute(
                    "UPDATE development_match_policies SET frozen_at=now()"
                )

        with pytest.raises(psycopg2.Error):
            with psycopg2.connect(dsn) as conn:
                cur = conn.cursor()
                cur.execute(
                    "INSERT INTO house_ai_artifacts "
                    "(artifact_sha256,artifact_size,storage_ref,created_at) "
                    "VALUES (%s,16777217,'oversized-fixture',now())",
                    ("d" * 64,),
                )

        # DEVELOPMENT cannot be inserted without its persisted policy binding.
        with pytest.raises(psycopg2.Error):
            with psycopg2.connect(dsn) as conn:
                cur = conn.cursor()
                participant = _participant(cur)
                _match(cur, "DEVELOPMENT", participant)

        # The typed capability row binds its exact worker execution and House AI
        # subject; a mixed participant/House AI subject fails the SQL check.
        with psycopg2.connect(dsn) as conn:
            cur = conn.cursor()
            worker_id = f"worker-pg-{uuid.uuid4().hex[:12]}"
            execution_id, job_id, lease_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
            now = datetime.now(timezone.utc)
            cur.execute(
                """INSERT INTO workers
                   (worker_id,status,pending_capabilities,capabilities,security_state,created_at)
                   VALUES (%s,'ACTIVE','{}','{}','APPROVED',%s)""",
                (worker_id, now),
            )
            house = _house_version(cur)
            cur.execute(
                """INSERT INTO worker_execution_jobs
                   (id,execution_id,match_id,execution_kind,participant_id,submission_id,
                    submission_version,submission_version_id,house_ai_id,house_ai_version,
                    artifact_sha256,immutable_artifact_ref,runtime_profile,protocol_version,
                    execution_mode,initial_state,resource_policy,execution_expires_at,status,
                    assigned_worker_id,lease_id,fencing_token,lease_expires_at,last_sequence)
                   VALUES (%s,%s,NULL,'HOUSE_AI',NULL,NULL,NULL,NULL,%s,%s,%s,%s,
                           'runtime-v1','aik-jsonl/1','MOCK_PROTOCOL_ONLY',%s,%s,%s,
                           'ACKNOWLEDGED',%s,%s,1,%s,0)""",
                (job_id, execution_id, house["house_ai_id"], house["house_ai_version"],
                 house["digest"], house["storage_ref"], Json({"fixture": True}), Json({}),
                 now + timedelta(minutes=1), worker_id, lease_id, now + timedelta(minutes=1)),
            )
            cur.execute(
                """INSERT INTO worker_artifact_capabilities
                   (id,token_hash,worker_id,execution_id,lease_id,fencing_token,execution_kind,
                    artifact_sha256,submission_version_id,house_ai_id,house_ai_version,
                    operation,expires_at,max_uses,use_count,created_at)
                   VALUES (%s,%s,%s,%s,%s,1,'HOUSE_AI',%s,NULL,%s,%s,
                           'DOWNLOAD_ARCHIVE',%s,1,0,%s)""",
                (uuid.uuid4(), "e" * 64, worker_id, execution_id, lease_id,
                 house["digest"], house["house_ai_id"], house["house_ai_version"],
                 now + timedelta(seconds=30), now),
            )

        with pytest.raises(psycopg2.Error):
            with psycopg2.connect(dsn) as conn:
                cur = conn.cursor()
                cur.execute(
                    """INSERT INTO worker_artifact_capabilities
                       (id,token_hash,worker_id,execution_id,lease_id,fencing_token,
                        execution_kind,artifact_sha256,submission_version_id,house_ai_id,
                        house_ai_version,operation,expires_at,max_uses,use_count,created_at)
                       VALUES (%s,%s,%s,%s,%s,1,'PARTICIPANT',%s,NULL,%s,%s,
                               'DOWNLOAD_ARCHIVE',now()+interval '30 seconds',1,0,now())""",
                    (uuid.uuid4(), "a" * 64, worker_id, execution_id, lease_id,
                     house["digest"], house["house_ai_id"], house["house_ai_version"]),
                )

        with pytest.raises(psycopg2.Error):
            with psycopg2.connect(dsn) as conn:
                cur = conn.cursor()
                cur.execute(
                    """INSERT INTO worker_artifact_capabilities
                       (id,token_hash,worker_id,execution_id,lease_id,fencing_token,
                        execution_kind,artifact_sha256,submission_version_id,house_ai_id,
                        house_ai_version,operation,expires_at,max_uses,use_count,created_at)
                       VALUES (%s,%s,%s,%s,%s,1,'HOUSE_AI',%s,NULL,%s,%s,
                               'DOWNLOAD_ARCHIVE',now()+interval '30 seconds',1,0,now())""",
                    (uuid.uuid4(), "9" * 64, worker_id, execution_id, uuid.uuid4(),
                     house["digest"], house["house_ai_id"], house["house_ai_version"]),
                )
    finally:
        cursor.execute(f"DROP DATABASE IF EXISTS {database} WITH (FORCE)")
        cursor.execute("DROP ROLE IF EXISTS aik_worker_gateway")
        admin.close()
