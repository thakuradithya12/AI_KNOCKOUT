"""Build sessions, authoritative submission timing, and official freeze.

Revision ID: 0007_event_mechanics
Revises: 0006_match_controller
"""

# PostgreSQL trigger bodies are intentionally kept as readable SQL blocks.
# ruff: noqa: E501

import sqlalchemy as sa
from alembic import op

revision = "0007_event_mechanics"
down_revision = "0006_match_controller"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "build_sessions",
        sa.Column("participant_id", sa.String(32), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("start_source", sa.String(32), nullable=False),
        sa.CheckConstraint(
            "start_source IN ('REGISTRATION','LEGACY_REGISTRATION')",
            name="ck_build_session_source",
        ),
        sa.ForeignKeyConstraint(
            ["participant_id"], ["participants.participant_id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("participant_id"),
    )
    op.execute(
        "INSERT INTO build_sessions (participant_id, started_at, start_source) "
        "SELECT participant_id, created_at, 'LEGACY_REGISTRATION' FROM participants"
    )
    op.add_column(
        "submission_versions",
        sa.Column("build_duration_seconds", sa.Integer(), nullable=True),
    )
    op.create_check_constraint(
        "ck_submission_build_duration_nonnegative",
        "submission_versions",
        "build_duration_seconds IS NULL OR build_duration_seconds >= 0",
    )

    op.create_table(
        "event_control",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("official_freeze_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("official_freeze_actor_id", sa.String(64), nullable=True),
        sa.Column("official_freeze_reason", sa.String(64), nullable=True),
        sa.CheckConstraint("id = 1", name="ck_event_control_singleton"),
        sa.CheckConstraint(
            "(official_freeze_at IS NULL AND official_freeze_actor_id IS NULL "
            "AND official_freeze_reason IS NULL) OR "
            "(official_freeze_at IS NOT NULL AND official_freeze_actor_id IS NOT NULL "
            "AND official_freeze_reason IS NOT NULL)",
            name="ck_event_control_freeze_audit",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute("INSERT INTO event_control (id) VALUES (1)")

    op.create_table(
        "official_submission_selections",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.String(32), nullable=False),
        sa.Column("submission_version_id", sa.Uuid(), nullable=False),
        sa.Column("selected_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["participant_id"], ["participants.participant_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["submission_version_id"], ["submission_versions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("participant_id", name="uq_official_selection_participant"),
    )
    op.create_index(
        "ix_official_selection_version",
        "official_submission_selections",
        ["submission_version_id"],
    )

    op.execute("""
    CREATE FUNCTION aik_build_session_immutable() RETURNS trigger AS $$
    BEGIN
      RAISE EXCEPTION 'build session identity and start time are immutable'
        USING ERRCODE = 'check_violation';
    END;
    $$ LANGUAGE plpgsql;
    """)
    op.execute("""
    CREATE TRIGGER build_sessions_immutable
    BEFORE UPDATE OR DELETE ON build_sessions
    FOR EACH ROW EXECUTE FUNCTION aik_build_session_immutable()
    """)

    op.execute("""
    CREATE FUNCTION aik_submission_build_duration_guard() RETURNS trigger AS $$
    DECLARE started timestamptz; expected integer;
    BEGIN
      IF TG_OP = 'UPDATE' THEN
        IF NEW.build_duration_seconds IS DISTINCT FROM OLD.build_duration_seconds THEN
          RAISE EXCEPTION 'submission build duration is immutable'
            USING ERRCODE = 'check_violation';
        END IF;
        IF OLD.state = 'ELIGIBLE' AND NEW.state IS DISTINCT FROM OLD.state AND EXISTS (
          SELECT 1 FROM event_control WHERE id = 1 AND official_freeze_at IS NOT NULL
        ) AND EXISTS (
          SELECT 1 FROM official_submission_selections
          WHERE submission_version_id = OLD.id
        ) THEN
          RAISE EXCEPTION 'frozen official submission eligibility is immutable'
            USING ERRCODE = 'check_violation';
        END IF;
        RETURN NEW;
      END IF;
      SELECT started_at INTO started FROM build_sessions
        WHERE participant_id = NEW.participant_id FOR KEY SHARE;
      IF NOT FOUND OR NEW.build_duration_seconds IS NULL THEN
        RAISE EXCEPTION 'submission requires an authoritative build session and duration'
          USING ERRCODE = 'check_violation';
      END IF;
      expected := GREATEST(0, floor(extract(epoch FROM NEW.created_at - started))::integer);
      IF NEW.build_duration_seconds <> expected THEN
        RAISE EXCEPTION 'submission build duration is not server-derived'
          USING ERRCODE = 'check_violation';
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """)
    op.execute("""
    CREATE TRIGGER submission_build_duration_guard
    BEFORE INSERT OR UPDATE ON submission_versions
    FOR EACH ROW EXECUTE FUNCTION aik_submission_build_duration_guard()
    """)

    op.execute("""
    CREATE FUNCTION aik_official_selection_guard() RETURNS trigger AS $$
    DECLARE frozen_at timestamptz; version_owner text; version_state text;
    BEGIN
      SELECT official_freeze_at INTO frozen_at FROM event_control WHERE id = 1 FOR KEY SHARE;
      IF frozen_at IS NOT NULL THEN
        RAISE EXCEPTION 'official submission selection is frozen'
          USING ERRCODE = 'check_violation';
      END IF;
      IF TG_OP = 'DELETE' THEN
        RETURN OLD;
      END IF;
      SELECT participant_id, state INTO version_owner, version_state
        FROM submission_versions WHERE id = NEW.submission_version_id FOR KEY SHARE;
      IF NOT FOUND OR version_owner <> NEW.participant_id OR version_state <> 'ELIGIBLE' THEN
        RAISE EXCEPTION 'official selection requires the participant owned eligible version'
          USING ERRCODE = 'check_violation';
      END IF;
      NEW.selected_at := clock_timestamp();
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """)
    op.execute("""
    CREATE TRIGGER official_selection_guard
    BEFORE INSERT OR UPDATE OR DELETE ON official_submission_selections
    FOR EACH ROW EXECUTE FUNCTION aik_official_selection_guard()
    """)

    op.execute("""
    CREATE FUNCTION aik_event_freeze_guard() RETURNS trigger AS $$
    BEGIN
      IF OLD.official_freeze_at IS NOT NULL OR NEW.official_freeze_at IS NULL OR
         NEW.official_freeze_actor_id IS NULL OR NEW.official_freeze_reason IS NULL THEN
        RAISE EXCEPTION 'official submission freeze is one-way and requires audited actor/reason'
          USING ERRCODE = 'check_violation';
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """)
    op.execute("""
    CREATE TRIGGER event_control_freeze_guard
    BEFORE UPDATE ON event_control
    FOR EACH ROW EXECUTE FUNCTION aik_event_freeze_guard()
    """)


def downgrade() -> None:
    for table, trigger in (
        ("event_control", "event_control_freeze_guard"),
        ("official_submission_selections", "official_selection_guard"),
        ("submission_versions", "submission_build_duration_guard"),
        ("build_sessions", "build_sessions_immutable"),
    ):
        op.execute(f"DROP TRIGGER IF EXISTS {trigger} ON {table}")
    for function in (
        "aik_event_freeze_guard",
        "aik_official_selection_guard",
        "aik_submission_build_duration_guard",
        "aik_build_session_immutable",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {function}()")
    op.drop_index("ix_official_selection_version", table_name="official_submission_selections")
    op.drop_table("official_submission_selections")
    op.drop_table("event_control")
    op.drop_constraint(
        "ck_submission_build_duration_nonnegative", "submission_versions", type_="check"
    )
    op.drop_column("submission_versions", "build_duration_seconds")
    op.drop_table("build_sessions")
