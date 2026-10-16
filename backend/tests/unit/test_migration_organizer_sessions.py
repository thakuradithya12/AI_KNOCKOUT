"""Unit test for database migration 0014_organizer_sessions.

Verifies that the Alembic migration executes upgrade() and downgrade() cleanly,
creating the organizer_sessions table, columns, and indexes, and dropping them on downgrade,
all inside an isolated, local in-memory SQLite database without accessing staging or production.
"""

from __future__ import annotations

import importlib
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect


def test_migration_0014_organizer_sessions_upgrade_and_downgrade_isolated():
    """Test upgrade() and downgrade() of migration 0014 in an isolated in-memory database."""
    engine = create_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            migration = importlib.import_module("migrations.versions.0014_organizer_sessions")

            # 1. Execute upgrade()
            migration.upgrade()

            # 2. Inspect created table and columns
            inspector = inspect(conn)
            tables = inspector.get_table_names()
            assert "organizer_sessions" in tables, "Table organizer_sessions must be created"

            cols = {c["name"]: c for c in inspector.get_columns("organizer_sessions")}
            expected_columns = {
                "id",
                "session_token_hash",
                "organizer_email",
                "organizer_name",
                "csrf_token",
                "expires_at",
                "last_active_at",
                "revoked_at",
                "created_ip",
                "user_agent",
                "created_at",
                "updated_at",
            }
            assert expected_columns.issubset(set(cols.keys())), (
                f"Missing columns in organizer_sessions: {expected_columns - set(cols.keys())}"
            )

            # Check non-nullable columns
            assert cols["session_token_hash"]["nullable"] is False
            assert cols["organizer_email"]["nullable"] is False
            assert cols["csrf_token"]["nullable"] is False
            assert cols["expires_at"]["nullable"] is False
            assert cols["last_active_at"]["nullable"] is False

            # 3. Inspect indexes
            indexes = {ix["name"]: ix for ix in inspector.get_indexes("organizer_sessions")}
            expected_indexes = {
                "ix_organizer_sessions_session_token_hash",
                "ix_organizer_sessions_organizer_email",
                "ix_organizer_sessions_expires_at",
                "ix_organizer_sessions_last_active_at",
                "ix_organizer_sessions_revoked_at",
            }
            assert expected_indexes.issubset(set(indexes.keys())), (
                f"Missing indexes in organizer_sessions: {expected_indexes - set(indexes.keys())}"
            )
            assert bool(indexes["ix_organizer_sessions_session_token_hash"]["unique"]) is True

            # 4. Execute downgrade()
            migration.downgrade()

            # 5. Inspect post-downgrade table removal
            inspector_after = inspect(conn)
            tables_after = inspector_after.get_table_names()
            assert "organizer_sessions" not in tables_after, (
                "Table organizer_sessions must be dropped on downgrade"
            )


def test_migration_0012_to_0014_full_upgrade_and_downgrade_chain():
    """Verify that migrations 0012, 0013, and 0014 can be upgraded in sequence and downgraded cleanly."""
    from sqlalchemy import text

    engine = create_engine("sqlite:///:memory:")
    with engine.connect() as conn:
        # Base tables required by foreign keys
        conn.execute(text("CREATE TABLE participants (participant_id VARCHAR(32) PRIMARY KEY, name VARCHAR(255))"))
        conn.execute(text("CREATE TABLE submission_versions (id BLOB PRIMARY KEY, participant_id VARCHAR(32))"))
        conn.execute(text("INSERT INTO participants VALUES ('part_test_1', 'Test Participant')"))
        conn.commit()

        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            m12 = importlib.import_module("migrations.versions.0012_submission_quotas")
            m13 = importlib.import_module("migrations.versions.0013_tournament_engine")
            m14 = importlib.import_module("migrations.versions.0014_organizer_sessions")

            # --- FORWARD UPGRADE CHAIN ---
            m12.upgrade()
            insp = inspect(conn)
            assert "participant_submission_allowances" in insp.get_table_names()
            assert "submission_allowance_requests" in insp.get_table_names()
            limit = conn.execute(
                text("SELECT submission_limit FROM participant_submission_allowances WHERE participant_id = 'part_test_1'")
            ).scalar()
            assert limit == 10

            m13.upgrade()
            insp = inspect(conn)
            assert "tournament_snapshots" in insp.get_table_names()
            assert "tournament_rounds" in insp.get_table_names()
            assert "tournament_pairings" in insp.get_table_names()

            m14.upgrade()
            insp = inspect(conn)
            assert "organizer_sessions" in insp.get_table_names()

            # --- REVERSE DOWNGRADE CHAIN (TESTED ROLLBACK PATH) ---
            # 1. Rollback 0014: drops organizer_sessions
            m14.downgrade()
            insp = inspect(conn)
            assert "organizer_sessions" not in insp.get_table_names()
            assert "tournament_rounds" in insp.get_table_names()
            assert "participant_submission_allowances" in insp.get_table_names()

            # 2. Rollback 0013: drops tournament_pairings, tournament_rounds, tournament_snapshots
            m13.downgrade()
            insp = inspect(conn)
            assert "tournament_pairings" not in insp.get_table_names()
            assert "tournament_rounds" not in insp.get_table_names()
            assert "tournament_snapshots" not in insp.get_table_names()
            assert "participant_submission_allowances" in insp.get_table_names()

            # 3. Rollback 0012: drops submission_allowance_requests, participant_submission_allowances
            m12.downgrade()
            insp = inspect(conn)
            assert "submission_allowance_requests" not in insp.get_table_names()
            assert "participant_submission_allowances" not in insp.get_table_names()
            assert "participants" in insp.get_table_names()

