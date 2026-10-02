"""Participant submissions, immutable quarantine artifacts and validation attempts.

Revision ID: 0003_submission_quarantine
Revises: 0002_phase2_identity_auth
"""

from alembic import op
import sqlalchemy as sa


revision = "0003_submission_quarantine"
down_revision = "0002_phase2_identity_auth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "submissions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["participant_id"], ["participants.participant_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "participant_id", name="uq_submissions_id_owner"),
    )
    op.create_index("ix_submissions_participant_created", "submissions", ["participant_id", "created_at"])

    op.create_table(
        "quarantine_objects",
        sa.Column("artifact_sha256", sa.String(64), nullable=False),
        sa.Column("artifact_size", sa.BigInteger(), nullable=False),
        sa.Column("archive_bytes", sa.LargeBinary(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("artifact_size >= 0", name="ck_quarantine_size_nonnegative"),
        sa.CheckConstraint("artifact_sha256 ~ '^[0-9a-f]{64}$'", name="ck_quarantine_sha256_hex"),
        sa.CheckConstraint("octet_length(archive_bytes) = artifact_size", name="ck_quarantine_byte_size"),
        sa.PrimaryKeyConstraint("artifact_sha256"),
        sa.UniqueConstraint("artifact_sha256", "artifact_size", name="uq_quarantine_digest_size"),
    )

    op.create_table(
        "submission_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.String(32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("artifact_sha256", sa.String(64), nullable=False),
        sa.Column("artifact_size", sa.BigInteger(), nullable=False),
        sa.Column("storage_key", sa.String(80), nullable=False),
        sa.Column("manifest", sa.JSON(), nullable=True),
        sa.Column("contract_version", sa.String(32), nullable=True),
        sa.Column("protocol_version", sa.String(64), nullable=True),
        sa.Column("runtime_profile", sa.String(64), nullable=True),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("rejection_code", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("version > 0", name="ck_submission_version_positive"),
        sa.CheckConstraint("artifact_size >= 0", name="ck_submission_version_size_nonnegative"),
        sa.CheckConstraint(
            "state IN ('QUARANTINED','INSPECTING','VALIDATING','APPROVED','ELIGIBLE','REJECTED','RETRYABLE_FAILURE','DEPRECATED','REVOKED')",
            name="ck_submission_version_state",
        ),
        sa.ForeignKeyConstraint(
            ["submission_id", "participant_id"],
            ["submissions.id", "submissions.participant_id"],
            ondelete="RESTRICT", name="fk_submission_version_owner",
        ),
        sa.ForeignKeyConstraint(
            ["artifact_sha256", "artifact_size"],
            ["quarantine_objects.artifact_sha256", "quarantine_objects.artifact_size"],
            ondelete="RESTRICT", name="fk_submission_version_object",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("submission_id", "version", name="uq_submission_version_number"),
    )
    op.create_index("ix_submission_versions_owner", "submission_versions", ["participant_id", "submission_id"])
    op.create_index("ix_submission_versions_digest", "submission_versions", ["artifact_sha256"])

    op.create_table(
        "validation_attempts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("submission_version_id", sa.Uuid(), nullable=False),
        sa.Column("validator_version", sa.String(64), nullable=False),
        sa.Column("policy_version", sa.String(64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("outcome", sa.String(32), nullable=False),
        sa.Column("report", sa.JSON(), nullable=False),
        sa.Column("rejection_code", sa.String(64), nullable=True),
        sa.CheckConstraint("completed_at >= started_at", name="ck_validation_attempt_time_order"),
        sa.CheckConstraint("outcome IN ('PASSED','REJECTED','RETRYABLE_FAILURE')", name="ck_validation_outcome"),
        sa.ForeignKeyConstraint(["submission_version_id"], ["submission_versions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_validation_attempt_version", "validation_attempts", ["submission_version_id", "completed_at"])

    op.create_table(
        "artifact_state_transitions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("submission_version_id", sa.Uuid(), nullable=False),
        sa.Column("from_state", sa.String(32), nullable=True),
        sa.Column("to_state", sa.String(32), nullable=False),
        sa.Column("actor_kind", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("reason_code", sa.String(64), nullable=False),
        sa.Column("correlation_id", sa.String(64), nullable=True),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["submission_version_id"], ["submission_versions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_artifact_transitions_version", "artifact_state_transitions", ["submission_version_id", "recorded_at"])

    op.create_table(
        "competition_events",
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("actor_kind", sa.String(32), nullable=False),
        sa.Column("actor_id", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(16), server_default="info", nullable=False),
        sa.Column("aggregate_type", sa.String(32), nullable=False),
        sa.Column("aggregate_id", sa.Uuid(), nullable=False),
        sa.Column("aggregate_sequence", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column("correlation_id", sa.String(64), nullable=True),
        sa.Column("causation_id", sa.String(64), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("aggregate_sequence > 0", name="ck_competition_event_sequence_positive"),
        sa.PrimaryKeyConstraint("event_id"),
        sa.UniqueConstraint(
            "aggregate_type", "aggregate_id", "aggregate_sequence",
            name="uq_competition_event_aggregate_sequence",
        ),
    )
    op.create_index("ix_competition_events_recorded", "competition_events", ["recorded_at"])

    op.execute("""
    CREATE FUNCTION aik_reject_record_mutation() RETURNS trigger AS $$
    BEGIN
      RAISE EXCEPTION 'append-only AI Knockout record cannot be changed' USING ERRCODE = 'check_violation';
    END;
    $$ LANGUAGE plpgsql;
    """)
    for table in ("quarantine_objects", "validation_attempts", "artifact_state_transitions", "competition_events"):
        op.execute(f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} FOR EACH ROW EXECUTE FUNCTION aik_reject_record_mutation()")

    op.execute("""
    CREATE FUNCTION aik_submission_immutable() RETURNS trigger AS $$
    BEGIN
      RAISE EXCEPTION 'submission identity and owner are immutable' USING ERRCODE = 'check_violation';
    END;
    $$ LANGUAGE plpgsql;
    """)
    op.execute("CREATE TRIGGER submissions_immutable BEFORE UPDATE OR DELETE ON submissions FOR EACH ROW EXECUTE FUNCTION aik_submission_immutable()")

    op.execute("""
    CREATE FUNCTION aik_submission_version_insert_guard() RETURNS trigger AS $$
    BEGIN
      IF NEW.state <> 'QUARANTINED' OR NEW.rejection_code IS NOT NULL OR NEW.storage_key <> 'sha256:' || NEW.artifact_sha256 THEN
        RAISE EXCEPTION 'new artifact versions must enter quarantine with digest storage identity' USING ERRCODE = 'check_violation';
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """)
    op.execute("CREATE TRIGGER submission_versions_insert_guard BEFORE INSERT ON submission_versions FOR EACH ROW EXECUTE FUNCTION aik_submission_version_insert_guard()")

    op.execute("""
    CREATE FUNCTION aik_submission_version_update_guard() RETURNS trigger AS $$
    DECLARE transition_reason text;
    BEGIN
      IF ROW(NEW.id, NEW.submission_id, NEW.participant_id, NEW.version, NEW.artifact_sha256,
             NEW.artifact_size, NEW.storage_key, NEW.manifest, NEW.contract_version,
             NEW.protocol_version, NEW.runtime_profile, NEW.created_at)
         IS DISTINCT FROM
         ROW(OLD.id, OLD.submission_id, OLD.participant_id, OLD.version, OLD.artifact_sha256,
             OLD.artifact_size, OLD.storage_key, OLD.manifest, OLD.contract_version,
             OLD.protocol_version, OLD.runtime_profile, OLD.created_at) THEN
        RAISE EXCEPTION 'submission version artifact identity is immutable' USING ERRCODE = 'check_violation';
      END IF;
      IF NEW.state IS DISTINCT FROM OLD.state THEN
        SELECT reason_code INTO transition_reason FROM artifact_state_transitions
          WHERE submission_version_id = OLD.id AND from_state = OLD.state AND to_state = NEW.state
          ORDER BY recorded_at DESC LIMIT 1;
        IF transition_reason IS NULL THEN
          RAISE EXCEPTION 'artifact state change requires a matching transition record' USING ERRCODE = 'check_violation';
        END IF;
        IF NOT (
          (OLD.state = 'QUARANTINED' AND NEW.state IN ('INSPECTING','REVOKED')) OR
          (OLD.state = 'INSPECTING' AND NEW.state IN ('VALIDATING','REJECTED','RETRYABLE_FAILURE','REVOKED')) OR
          (OLD.state = 'VALIDATING' AND NEW.state IN ('APPROVED','REJECTED','RETRYABLE_FAILURE','REVOKED')) OR
          (OLD.state = 'RETRYABLE_FAILURE' AND NEW.state IN ('INSPECTING','REVOKED')) OR
          (OLD.state = 'APPROVED' AND NEW.state IN ('ELIGIBLE','DEPRECATED','REVOKED')) OR
          (OLD.state = 'ELIGIBLE' AND NEW.state IN ('APPROVED','DEPRECATED','REVOKED')) OR
          (OLD.state = 'DEPRECATED' AND NEW.state = 'REVOKED')
        ) THEN
          RAISE EXCEPTION 'invalid artifact state transition' USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.state = 'REJECTED' AND NEW.rejection_code IS DISTINCT FROM transition_reason THEN
          RAISE EXCEPTION 'rejection code must match the recorded transition reason' USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.state <> 'REJECTED' AND NEW.rejection_code IS DISTINCT FROM OLD.rejection_code THEN
          RAISE EXCEPTION 'rejection code may change only when entering REJECTED' USING ERRCODE = 'check_violation';
        END IF;
      ELSIF NEW.rejection_code IS DISTINCT FROM OLD.rejection_code THEN
        RAISE EXCEPTION 'rejection code may change only with an artifact state transition' USING ERRCODE = 'check_violation';
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """)
    op.execute("CREATE TRIGGER submission_versions_update_guard BEFORE UPDATE ON submission_versions FOR EACH ROW EXECUTE FUNCTION aik_submission_version_update_guard()")
    op.execute("CREATE TRIGGER submission_versions_delete_guard BEFORE DELETE ON submission_versions FOR EACH ROW EXECUTE FUNCTION aik_reject_record_mutation()")

    op.execute("""
    CREATE FUNCTION aik_artifact_transition_insert_guard() RETURNS trigger AS $$
    DECLARE current_state text;
    BEGIN
      SELECT state INTO current_state FROM submission_versions WHERE id = NEW.submission_version_id FOR UPDATE;
      IF NOT FOUND THEN
        RAISE EXCEPTION 'submission version not found' USING ERRCODE = 'foreign_key_violation';
      END IF;
      IF NEW.from_state IS NULL THEN
        IF NEW.to_state <> 'QUARANTINED' OR current_state <> 'QUARANTINED' OR EXISTS (
          SELECT 1 FROM artifact_state_transitions WHERE submission_version_id = NEW.submission_version_id
        ) THEN
          RAISE EXCEPTION 'initial artifact transition must enter quarantine once' USING ERRCODE = 'check_violation';
        END IF;
      ELSE
        IF current_state <> NEW.from_state THEN
          RAISE EXCEPTION 'artifact transition source does not match current state' USING ERRCODE = 'check_violation';
        END IF;
        IF NOT (
          (NEW.from_state = 'QUARANTINED' AND NEW.to_state IN ('INSPECTING','REVOKED')) OR
          (NEW.from_state = 'INSPECTING' AND NEW.to_state IN ('VALIDATING','REJECTED','RETRYABLE_FAILURE','REVOKED')) OR
          (NEW.from_state = 'VALIDATING' AND NEW.to_state IN ('APPROVED','REJECTED','RETRYABLE_FAILURE','REVOKED')) OR
          (NEW.from_state = 'RETRYABLE_FAILURE' AND NEW.to_state IN ('INSPECTING','REVOKED')) OR
          (NEW.from_state = 'APPROVED' AND NEW.to_state IN ('ELIGIBLE','DEPRECATED','REVOKED')) OR
          (NEW.from_state = 'ELIGIBLE' AND NEW.to_state IN ('APPROVED','DEPRECATED','REVOKED')) OR
          (NEW.from_state = 'DEPRECATED' AND NEW.to_state = 'REVOKED')
        ) THEN
          RAISE EXCEPTION 'invalid artifact transition' USING ERRCODE = 'check_violation';
        END IF;
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """)
    op.execute("CREATE TRIGGER artifact_transition_insert_guard BEFORE INSERT ON artifact_state_transitions FOR EACH ROW EXECUTE FUNCTION aik_artifact_transition_insert_guard()")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS artifact_transition_insert_guard ON artifact_state_transitions")
    op.execute("DROP TRIGGER IF EXISTS submission_versions_delete_guard ON submission_versions")
    op.execute("DROP TRIGGER IF EXISTS submission_versions_update_guard ON submission_versions")
    op.execute("DROP TRIGGER IF EXISTS submission_versions_insert_guard ON submission_versions")
    op.execute("DROP TRIGGER IF EXISTS submissions_immutable ON submissions")
    for table in ("competition_events", "artifact_state_transitions", "validation_attempts", "quarantine_objects"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    for function in (
        "aik_artifact_transition_insert_guard", "aik_submission_version_update_guard",
        "aik_submission_version_insert_guard", "aik_submission_immutable",
        "aik_reject_record_mutation",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {function}()")
    op.drop_index("ix_competition_events_recorded", table_name="competition_events")
    op.drop_table("competition_events")
    op.drop_index("ix_artifact_transitions_version", table_name="artifact_state_transitions")
    op.drop_table("artifact_state_transitions")
    op.drop_index("ix_validation_attempt_version", table_name="validation_attempts")
    op.drop_table("validation_attempts")
    op.drop_index("ix_submission_versions_digest", table_name="submission_versions")
    op.drop_index("ix_submission_versions_owner", table_name="submission_versions")
    op.drop_table("submission_versions")
    op.drop_table("quarantine_objects")
    op.drop_index("ix_submissions_participant_created", table_name="submissions")
    op.drop_table("submissions")
