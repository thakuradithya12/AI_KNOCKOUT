"""Permit explicitly selected Kata-backed worker execution jobs.

Revision ID: 0010_kata_execution_mode
Revises: 0009_development_match_v1
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0010_kata_execution_mode"
down_revision = "0009_development_match_v1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Preserve existing MOCK_PROTOCOL_ONLY rows and their immutable semantics.
    op.drop_constraint("ck_worker_job_mock_only", "worker_execution_jobs", type_="check")
    op.create_check_constraint(
        "ck_worker_job_execution_mode",
        "worker_execution_jobs",
        "execution_mode IN ('MOCK_PROTOCOL_ONLY', 'KATA_AI_EXECUTION')",
    )


def downgrade() -> None:
    # A downgrade must never silently strand or rewrite a real execution row.
    op.execute(sa.text("""
        DO $$ BEGIN
          IF EXISTS (
            SELECT 1 FROM worker_execution_jobs
            WHERE execution_mode = 'KATA_AI_EXECUTION'
          ) THEN
            RAISE EXCEPTION 'KATA execution jobs exist; downgrade is unsafe';
          END IF;
        END $$;
    """))
    op.drop_constraint("ck_worker_job_execution_mode", "worker_execution_jobs", type_="check")
    op.create_check_constraint(
        "ck_worker_job_mock_only",
        "worker_execution_jobs",
        "execution_mode = 'MOCK_PROTOCOL_ONLY'",
    )
