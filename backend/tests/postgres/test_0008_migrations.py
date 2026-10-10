"""Fresh PostgreSQL migration and pre-0008 backfill checks.

This test is run only by the disposable PostgreSQL PowerShell harness. It uses
an explicit loopback admin URL and Alembic's explicit DATABASE_URL_SYNC input;
it does not load application settings or a .env file.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse, urlunparse

import psycopg2
import pytest
from psycopg2.extensions import parse_dsn
from psycopg2.extras import register_uuid
from test_typed_match_postgres import _eligible_version

register_uuid()
BACKEND = Path(__file__).resolve().parents[2]


def _admin_url() -> str:
    value = os.environ.get("AIK_TEST_POSTGRES_ADMIN_URL")
    if not value:
        pytest.skip("AIK_TEST_POSTGRES_ADMIN_URL is required for disposable migration checks")
    parsed = parse_dsn(value)
    if parsed.get("host") not in {"127.0.0.1", "localhost", "::1"}:
        pytest.fail("Migration verification is restricted to a loopback PostgreSQL server")
    if parsed.get("dbname") != "postgres":
        pytest.fail("Admin URL must connect to the disposable server's postgres database")
    return value


def _db_url(admin_url: str, database: str) -> str:
    parts = urlparse(admin_url)
    return urlunparse(parts._replace(path=f"/{database}"))


def _alembic(admin_url: str, database: str, target: str) -> None:
    env = os.environ.copy()
    # The migration environment reads only DATABASE_URL_SYNC. Remove names that
    # could point elsewhere rather than inheriting event configuration.
    for key in ("DATABASE_URL", "POSTGRES_USER", "POSTGRES_PASSWORD", "POSTGRES_HOST",
                "POSTGRES_PORT", "POSTGRES_DB"):
        env.pop(key, None)
    env["DATABASE_URL_SYNC"] = _db_url(admin_url, database).replace(
        "postgresql://", "postgresql+psycopg2://", 1
    )
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", target],
        cwd=BACKEND,
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )


def test_pg01_fresh_migrations_and_pg02_legacy_backfill():
    admin_url = _admin_url()
    admin = psycopg2.connect(admin_url)
    admin.autocommit = True
    cur = admin.cursor()

    # PG-02 uses a fresh pre-0008 schema and representative legacy participant
    # rows. The 0008 migration then performs the actual backfill.
    cur.execute("CREATE DATABASE aik_verify_backfill")
    _alembic(admin_url, "aik_verify_backfill", "0007_event_mechanics")
    backfill_url = _db_url(admin_url, "aik_verify_backfill")
    with psycopg2.connect(backfill_url) as legacy:
        cursor = legacy.cursor()
        white, black = _eligible_version(cursor), _eligible_version(cursor)
        match_id = uuid.uuid4()
        cursor.execute(
            """INSERT INTO matches
               (id,state,protocol_version,runtime_profile,policy_version,rules_version,match_seed,
                initial_fen,current_fen,side_to_move,current_ply,initial_clock_ms,increment_ms,
                move_timeout_ms,white_remaining_ms,black_remaining_ms,created_at)
               VALUES (%s,'QUEUED','aik-jsonl/1','runtime-v1','policy-v1','rules-v1',%s,
                       'initial-fen','initial-fen','WHITE',0,10000,0,1000,10000,10000,now())""",
            (match_id, "e" * 64),
        )
        for color, subject in (("WHITE", white), ("BLACK", black)):
            cursor.execute(
                """INSERT INTO match_sides
                   (id,match_id,color,participant_id,submission_id,submission_version_id,
                    artifact_sha256,protocol_version,runtime_profile,created_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,'aik-jsonl/1','runtime-v1',now())""",
                (uuid.uuid4(), match_id, color, subject["participant_id"],
                 subject["submission_id"], subject["version_id"], subject["digest"]),
            )
    _alembic(admin_url, "aik_verify_backfill", "0008_typed_match_sides")
    with psycopg2.connect(backfill_url) as migrated:
        cursor = migrated.cursor()
        cursor.execute(
            """SELECT s.color,s.kind,p.participant_id,p.submission_id,
                      p.submission_version_id,p.artifact_sha256
               FROM match_sides s JOIN participant_match_side_bindings p
                 ON p.match_side_id=s.id WHERE s.match_id=%s ORDER BY s.color""",
            (match_id,),
        )
        rows = cursor.fetchall()
        assert len(rows) == 2
        assert all(row[1] == "PARTICIPANT" for row in rows)
        expected = {"WHITE": white, "BLACK": black}
        for color, _kind, participant_id, submission_id, version_id, digest in rows:
            subject = expected[color]
            assert (participant_id, submission_id, version_id, digest) == (
                subject["participant_id"], subject["submission_id"],
                subject["version_id"], subject["digest"],
            )
        cursor.execute(
            "SELECT purpose FROM matches WHERE id=%s", (match_id,)
        )
        assert cursor.fetchone()[0] == "PEER_TEST"
        cursor.execute(
            "SELECT count(*) FROM information_schema.columns "
            "WHERE table_schema='public' AND table_name='match_sides' "
            "AND column_name IN ('participant_id','submission_id','submission_version_id',"
            "'artifact_sha256')"
        )
        assert cursor.fetchone()[0] == 0
    print("PG-02: PASS")

    # A role created by 0005 is cluster-wide. Discard the only DB that owns its
    # grants before running the independent empty-database migration scenario.
    cur.execute("DROP DATABASE aik_verify_backfill WITH (FORCE)")
    cur.execute("DROP ROLE aik_worker_gateway")
    cur.execute("CREATE DATABASE aik_verify_empty")
    _alembic(admin_url, "aik_verify_empty", "0008_typed_match_sides")
    with psycopg2.connect(_db_url(admin_url, "aik_verify_empty")) as empty:
        cursor = empty.cursor()
        cursor.execute("SELECT version_num FROM alembic_version")
        assert cursor.fetchone()[0] == "0008_typed_match_sides"
    print("PG-01: PASS")

    # Create the isolated empty target for the trigger matrix after removing
    # the one global role left by the independent PG-01 scenario.
    cur.execute("DROP DATABASE aik_verify_empty WITH (FORCE)")
    cur.execute("DROP ROLE aik_worker_gateway")
    cur.execute("CREATE DATABASE aik_verify")
    _alembic(admin_url, "aik_verify", "0008_typed_match_sides")
    admin.close()
