"""Forward migration tests for explicit result-source compatibility."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

MIGRATION_PATH = (
    Path(__file__).parents[2]
    / "migrations"
    / "versions"
    / "0018_match_result_provenance.py"
)
SPEC = importlib.util.spec_from_file_location("migration_result_provenance", MIGRATION_PATH)
MIGRATION = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MIGRATION)


def _run_migration(connection, action) -> None:
    context = MigrationContext.configure(connection)
    with Operations.context(context):
        action()


def test_upgrade_marks_legacy_completion_unknown_and_enforces_source_values() -> None:
    engine = create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE matches ("
                "id INTEGER PRIMARY KEY, state TEXT NOT NULL, result_outcome TEXT, "
                "result_reason TEXT, failure_reason TEXT, "
                "CONSTRAINT ck_matches_terminal_authority CHECK ("
                "(state = 'COMPLETED' AND result_outcome IS NOT NULL AND result_reason IS NOT NULL "
                "AND failure_reason IS NULL) OR "
                "(state = 'ABORTED' AND result_outcome IS NULL AND result_reason IS NULL "
                "AND failure_reason IS NOT NULL) OR "
                "(state NOT IN ('COMPLETED','ABORTED') AND result_outcome IS NULL "
                "AND result_reason IS NULL AND failure_reason IS NULL)))"
            )
        )
        connection.execute(
            text("INSERT INTO matches VALUES (1, 'COMPLETED', 'DRAW', 'STALEMATE', NULL)")
        )
        connection.execute(
            text("INSERT INTO matches VALUES (2, 'QUEUED', NULL, NULL, NULL)")
        )

        _run_migration(connection, MIGRATION.upgrade)

        rows = connection.execute(
            text("SELECT id, result_source FROM matches ORDER BY id")
        ).all()
        assert rows == [(1, "unknown"), (2, None)]
        constraints = inspect(connection).get_check_constraints("matches")
        assert {row["name"] for row in constraints} >= {
            "ck_matches_result_source_allowed",
            "ck_matches_terminal_authority",
        }

        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(
                    text(
                        "INSERT INTO matches "
                        "(id,state,result_outcome,result_reason,failure_reason,result_source) "
                        "VALUES (3,'COMPLETED','DRAW','STALEMATE',NULL,'forged')"
                    )
                )

        with pytest.raises(IntegrityError):
            with connection.begin_nested():
                connection.execute(
                    text(
                        "INSERT INTO matches "
                        "(id,state,result_outcome,result_reason,failure_reason,result_source) "
                        "VALUES (4,'COMPLETED','DRAW','STALEMATE',NULL,NULL)"
                    )
                )

        _run_migration(connection, MIGRATION.downgrade)
        column_names = {
            column["name"] for column in inspect(connection).get_columns("matches")
        }
        assert "result_source" not in column_names
        connection.execute(
            text("INSERT INTO matches VALUES (5, 'COMPLETED', 'DRAW', 'STALEMATE', NULL)")
        )
    engine.dispose()
