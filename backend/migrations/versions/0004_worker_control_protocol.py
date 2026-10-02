"""Worker enrollment, execution leases, artifact capabilities and evidence.

Revision ID: 0004_worker_control_protocol
Revises: 0003_submission_quarantine
"""

from alembic import op
import sqlalchemy as sa


revision = "0004_worker_control_protocol"
down_revision = "0003_submission_quarantine"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "workers",
        sa.Column("worker_id", sa.String(64), primary_key=True),
        sa.Column("certificate_fingerprint", sa.String(64), nullable=True, unique=True),
        sa.Column("certificate_identity", sa.String(512), nullable=True),
        sa.Column("certificate_serial", sa.String(128), nullable=True),
        sa.Column("pending_certificate_fingerprint", sa.String(64), nullable=True),
        sa.Column("pending_certificate_identity", sa.String(512), nullable=True),
        sa.Column("pending_certificate_serial", sa.String(128), nullable=True),
        sa.Column("pending_issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pending_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("pending_capabilities", sa.JSON(), nullable=False),
        sa.Column("pending_software_version", sa.String(128), nullable=True),
        sa.Column("pending_runtime_version", sa.String(128), nullable=True),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("software_version", sa.String(128), nullable=True),
        sa.Column("runtime_version", sa.String(128), nullable=True),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("security_state", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(
            "status IN ('PENDING','ACTIVE','SUSPENDED','REVOKED','DECOMMISSIONED')",
            name="ck_worker_status",
        ),
        sa.CheckConstraint(
            "certificate_fingerprint IS NULL OR certificate_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_worker_fingerprint",
        ),
        sa.CheckConstraint(
            "pending_certificate_fingerprint IS NULL OR pending_certificate_fingerprint ~ '^[0-9a-f]{64}$'",
            name="ck_worker_pending_fingerprint",
        ),
    )
    op.create_index("ix_workers_certificate_fingerprint", "workers", ["certificate_fingerprint"], unique=True)
    op.create_index("ux_workers_pending_certificate_fingerprint", "workers", ["pending_certificate_fingerprint"], unique=True)

    op.create_table(
        "worker_enrollment_tokens",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("worker_id", sa.String(64), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("approved_by", sa.String(128), nullable=False),
        sa.Column("purpose", sa.String(16), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["worker_id"], ["workers.worker_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("token_hash ~ '^[0-9a-f]{64}$'", name="ck_worker_enrollment_token_hash"),
        sa.CheckConstraint("purpose IN ('ENROLL','ROTATE')", name="ck_worker_enrollment_token_purpose"),
    )
    op.create_index("ix_worker_enrollment_tokens_worker_id", "worker_enrollment_tokens", ["worker_id"])

    op.create_table(
        "worker_execution_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False, unique=True),
        sa.Column("match_id", sa.Uuid(), nullable=True),
        sa.Column("participant_id", sa.String(32), nullable=False),
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("submission_version", sa.Integer(), nullable=False),
        sa.Column("submission_version_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_sha256", sa.String(64), nullable=False),
        sa.Column("immutable_artifact_ref", sa.String(128), nullable=False),
        sa.Column("runtime_profile", sa.String(64), nullable=False),
        sa.Column("protocol_version", sa.String(64), nullable=False),
        sa.Column("execution_mode", sa.String(32), nullable=False),
        sa.Column("initial_state", sa.JSON(), nullable=False),
        sa.Column("resource_policy", sa.JSON(), nullable=False),
        sa.Column("execution_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(24), nullable=False),
        sa.Column("assigned_worker_id", sa.String(64), nullable=True),
        sa.Column("lease_id", sa.Uuid(), nullable=True),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_sequence", sa.BigInteger(), nullable=False),
        sa.Column("last_operation_hash", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["submission_version_id"], ["submission_versions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["assigned_worker_id"], ["workers.worker_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("submission_version > 0", name="ck_worker_job_version_positive"),
        sa.CheckConstraint("fencing_token >= 0 AND last_sequence >= 0", name="ck_worker_job_counters_nonnegative"),
        sa.CheckConstraint("artifact_sha256 ~ '^[0-9a-f]{64}$'", name="ck_worker_job_sha256"),
        sa.CheckConstraint("execution_mode = 'MOCK_PROTOCOL_ONLY'", name="ck_worker_job_mock_only"),
        sa.CheckConstraint(
            "status IN ('QUEUED','LEASED','ACKNOWLEDGED','COMPLETED','FAILED','CANCELED','EXPIRED')",
            name="ck_worker_job_status",
        ),
    )
    op.create_index("ix_worker_execution_jobs_assigned_worker_id", "worker_execution_jobs", ["assigned_worker_id"])
    op.create_index("ix_worker_jobs_claim", "worker_execution_jobs", ["status", "execution_expires_at", "created_at"])

    op.create_table(
        "worker_artifact_capabilities",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("worker_id", sa.String(64), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("lease_id", sa.Uuid(), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("artifact_sha256", sa.String(64), nullable=False),
        sa.Column("submission_version_id", sa.Uuid(), nullable=False),
        sa.Column("operation", sa.String(32), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("max_uses", sa.Integer(), nullable=False),
        sa.Column("use_count", sa.Integer(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["worker_id"], ["workers.worker_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["execution_id"], ["worker_execution_jobs.execution_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["submission_version_id"], ["submission_versions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("token_hash ~ '^[0-9a-f]{64}$'", name="ck_artifact_capability_hash"),
        sa.CheckConstraint("artifact_sha256 ~ '^[0-9a-f]{64}$'", name="ck_artifact_capability_sha256"),
        sa.CheckConstraint("operation = 'DOWNLOAD_ARCHIVE'", name="ck_artifact_capability_operation"),
        sa.CheckConstraint("max_uses = 1 AND use_count BETWEEN 0 AND 1", name="ck_artifact_capability_usage"),
    )
    op.create_index("ix_worker_artifact_capabilities_worker_id", "worker_artifact_capabilities", ["worker_id"])

    op.create_table(
        "worker_evidence",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("worker_id", sa.String(64), nullable=False),
        sa.Column("sequence", sa.BigInteger(), nullable=False),
        sa.Column("event_type", sa.String(48), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("failure_class", sa.String(40), nullable=True),
        sa.Column("termination_reason", sa.String(64), nullable=True),
        sa.Column("resource_usage", sa.JSON(), nullable=False),
        sa.Column("artifact_digest_verified", sa.Boolean(), nullable=False),
        sa.Column("cleanup_verified", sa.Boolean(), nullable=False),
        sa.Column("stdout_bytes", sa.BigInteger(), nullable=False),
        sa.Column("stdout_sha256", sa.String(64), nullable=True),
        sa.Column("stderr_bytes", sa.BigInteger(), nullable=False),
        sa.Column("stderr_sha256", sa.String(64), nullable=True),
        sa.Column("protocol_violation", sa.String(64), nullable=True),
        sa.Column("sandbox_violation", sa.String(64), nullable=True),
        sa.Column("evidence_json", sa.JSON(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["execution_id"], ["worker_execution_jobs.execution_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["worker_id"], ["workers.worker_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("execution_id", "sequence", name="uq_worker_evidence_sequence"),
        sa.CheckConstraint("sequence > 0 AND stdout_bytes >= 0 AND stderr_bytes >= 0", name="ck_worker_evidence_counters"),
        sa.CheckConstraint("stdout_sha256 IS NULL OR stdout_sha256 ~ '^[0-9a-f]{64}$'", name="ck_worker_evidence_stdout_hash"),
        sa.CheckConstraint("stderr_sha256 IS NULL OR stderr_sha256 ~ '^[0-9a-f]{64}$'", name="ck_worker_evidence_stderr_hash"),
    )

    op.create_table(
        "worker_security_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("worker_id", sa.String(64), nullable=True),
        sa.Column("execution_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(64), nullable=False),
        sa.Column("severity", sa.String(16), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("details", sa.JSON(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("severity IN ('INFO','WARNING','CRITICAL')", name="ck_worker_security_event_severity"),
    )
    op.create_index("ix_worker_security_events_worker_id", "worker_security_events", ["worker_id"])
    op.create_index("ix_worker_security_events_execution_id", "worker_security_events", ["execution_id"])

    op.execute("""
    CREATE FUNCTION aik_worker_append_only() RETURNS trigger AS $$
    BEGIN
      RAISE EXCEPTION 'worker audit records are append-only' USING ERRCODE = 'check_violation';
    END;
    $$ LANGUAGE plpgsql;
    """)
    for table in ("worker_evidence", "worker_security_events"):
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            f"FOR EACH ROW EXECUTE FUNCTION aik_worker_append_only()"
        )

    op.execute("""
    CREATE FUNCTION aik_worker_job_insert_guard() RETURNS trigger AS $$
    DECLARE v record;
    BEGIN
      SELECT * INTO v FROM submission_versions WHERE id = NEW.submission_version_id;
      IF NOT FOUND OR v.state <> 'ELIGIBLE' OR v.participant_id <> NEW.participant_id OR
         v.submission_id <> NEW.submission_id OR v.version <> NEW.submission_version OR
         v.artifact_sha256 <> NEW.artifact_sha256 OR v.storage_key <> NEW.immutable_artifact_ref OR
         v.runtime_profile <> NEW.runtime_profile OR v.protocol_version <> NEW.protocol_version THEN
        RAISE EXCEPTION 'worker job does not match an eligible immutable artifact' USING ERRCODE = 'check_violation';
      END IF;
      IF NEW.execution_mode <> 'MOCK_PROTOCOL_ONLY' THEN
        RAISE EXCEPTION 'real execution is disabled in worker prototype' USING ERRCODE = 'check_violation';
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """)
    op.execute("CREATE TRIGGER worker_job_insert_guard BEFORE INSERT ON worker_execution_jobs FOR EACH ROW EXECUTE FUNCTION aik_worker_job_insert_guard()")

    op.execute("""
    CREATE FUNCTION aik_worker_job_immutable_guard() RETURNS trigger AS $$
    BEGIN
      IF ROW(NEW.execution_id, NEW.match_id, NEW.participant_id, NEW.submission_id,
             NEW.submission_version, NEW.submission_version_id, NEW.artifact_sha256,
             NEW.immutable_artifact_ref, NEW.runtime_profile, NEW.protocol_version,
             NEW.execution_mode, NEW.initial_state, NEW.resource_policy, NEW.execution_expires_at)
         IS DISTINCT FROM
         ROW(OLD.execution_id, OLD.match_id, OLD.participant_id, OLD.submission_id,
             OLD.submission_version, OLD.submission_version_id, OLD.artifact_sha256,
             OLD.immutable_artifact_ref, OLD.runtime_profile, OLD.protocol_version,
             OLD.execution_mode, OLD.initial_state, OLD.resource_policy, OLD.execution_expires_at) THEN
        RAISE EXCEPTION 'worker job identity/policy is immutable' USING ERRCODE = 'check_violation';
      END IF;
      IF NEW.fencing_token < OLD.fencing_token OR NEW.last_sequence < OLD.last_sequence THEN
        RAISE EXCEPTION 'worker job fence/sequence cannot decrease' USING ERRCODE = 'check_violation';
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """)
    op.execute("CREATE TRIGGER worker_job_immutable_guard BEFORE UPDATE ON worker_execution_jobs FOR EACH ROW EXECUTE FUNCTION aik_worker_job_immutable_guard()")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS worker_job_immutable_guard ON worker_execution_jobs")
    op.execute("DROP TRIGGER IF EXISTS worker_job_insert_guard ON worker_execution_jobs")
    op.execute("DROP FUNCTION IF EXISTS aik_worker_job_immutable_guard()")
    op.execute("DROP FUNCTION IF EXISTS aik_worker_job_insert_guard()")
    op.execute("DROP TRIGGER IF EXISTS worker_evidence_append_only ON worker_evidence")
    op.execute("DROP TRIGGER IF EXISTS worker_security_events_append_only ON worker_security_events")
    op.execute("DROP FUNCTION IF EXISTS aik_worker_append_only()")
    op.drop_index("ix_worker_security_events_execution_id", table_name="worker_security_events")
    op.drop_index("ix_worker_security_events_worker_id", table_name="worker_security_events")
    op.drop_table("worker_security_events")
    op.drop_table("worker_evidence")
    op.drop_index("ix_worker_artifact_capabilities_worker_id", table_name="worker_artifact_capabilities")
    op.drop_table("worker_artifact_capabilities")
    op.drop_index("ix_worker_jobs_claim", table_name="worker_execution_jobs")
    op.drop_index("ix_worker_execution_jobs_assigned_worker_id", table_name="worker_execution_jobs")
    op.drop_table("worker_execution_jobs")
    op.drop_index("ix_worker_enrollment_tokens_worker_id", table_name="worker_enrollment_tokens")
    op.drop_table("worker_enrollment_tokens")
    op.drop_index("ix_workers_certificate_fingerprint", table_name="workers")
    op.drop_index("ux_workers_pending_certificate_fingerprint", table_name="workers")
    op.drop_table("workers")
