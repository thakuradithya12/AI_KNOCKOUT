"""Enforce one current job per match and execution subject.

Revision ID: 0011_current_match_jobs
Revises: 0010_kata_execution_mode
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0011_current_match_jobs"
down_revision = "0010_kata_execution_mode"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # QUEUED/leased jobs are the one current attempt per side. Terminal jobs
    # remain as history and do not prevent a replacement attempt.
    op.create_index(
        "uq_worker_job_current_match_participant",
        "worker_execution_jobs",
        ["match_id", "participant_id"],
        unique=True,
        postgresql_where=sa.text(
            "match_id IS NOT NULL AND participant_id IS NOT NULL AND status IN "
            "('QUEUED','LEASED','ACKNOWLEDGED','CLEANUP_REPORTED')"
        ),
        sqlite_where=sa.text(
            "match_id IS NOT NULL AND participant_id IS NOT NULL AND status IN "
            "('QUEUED','LEASED','ACKNOWLEDGED','CLEANUP_REPORTED')"
        ),
    )
    op.create_index(
        "uq_worker_job_current_match_house_ai",
        "worker_execution_jobs",
        ["match_id", "house_ai_id"],
        unique=True,
        postgresql_where=sa.text(
            "match_id IS NOT NULL AND house_ai_id IS NOT NULL AND status IN "
            "('QUEUED','LEASED','ACKNOWLEDGED','CLEANUP_REPORTED')"
        ),
        sqlite_where=sa.text(
            "match_id IS NOT NULL AND house_ai_id IS NOT NULL AND status IN "
            "('QUEUED','LEASED','ACKNOWLEDGED','CLEANUP_REPORTED')"
        ),
    )


def downgrade() -> None:
    op.drop_index("uq_worker_job_current_match_house_ai", table_name="worker_execution_jobs")
    op.drop_index("uq_worker_job_current_match_participant", table_name="worker_execution_jobs")
