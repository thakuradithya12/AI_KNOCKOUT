"""Persist approved development policy, explicit House AI selection and artifact bytes.

Revision ID: 0009_development_match_v1
Revises: 0008_typed_match_sides
"""

from __future__ import annotations

import json

import sqlalchemy as sa
from alembic import op

revision = "0009_development_match_v1"
down_revision = "0008_typed_match_sides"
branch_labels = None
depends_on = None

POLICY = {
    "policy_id": "DEVELOPMENT",
    "version": "development-v1",
    "policy_version": "development-v1",
    "protocol_version": "aik-jsonl/1",
    "runtime_profile": "python-3.12",
    "rules_version": "standard-chess/python-chess-1.11.2",
    "initial_fen": "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1",
    "initial_clock_ms": 300000,
    "increment_ms": 0,
    "move_timeout_ms": 2000,
    "resources": {
        "cpu_millicores": 1000,
        "memory_bytes": 268435456,
        "pid_limit": 16,
        "tmpfs_bytes": 33554432,
        "inode_limit": 128,
        "nofile_limit": 64,
        "stdout_line_bytes": 65536,
        "stdout_total_bytes": 1048576,
        "stderr_total_bytes": 262144,
        "startup_timeout_ms": 5000,
        "ready_timeout_ms": 2000,
        "move_timeout_ms": 2000,
        "process_lifetime_ms": 900000,
        "concurrency_per_worker": 1,
        "network_mode": "DENY_ALL",
    },
}
POLICY_DIGEST = "a64214feecc2a3e346d8a53ecd21be79cc8e888325f2dfe93c5b93d895cf9e33"


def upgrade() -> None:
    op.add_column("house_ai_artifacts", sa.Column("archive_bytes", sa.LargeBinary(), nullable=True))
    op.drop_constraint("ck_house_ai_artifact_size", "house_ai_artifacts", type_="check")
    op.create_check_constraint(
        "ck_house_ai_artifact_size", "house_ai_artifacts",
        "artifact_size BETWEEN 0 AND 16777216",
    )
    op.drop_constraint("ck_house_ai_version_size", "house_ai_versions", type_="check")
    op.create_check_constraint(
        "ck_house_ai_version_size", "house_ai_versions",
        "artifact_size BETWEEN 0 AND 16777216",
    )

    op.create_table(
        "development_match_policies",
        sa.Column("policy_id", sa.String(32), nullable=False),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("policy_digest", sa.String(64), nullable=False),
        sa.Column("policy_document", sa.JSON(), nullable=False),
        sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("policy_id", "version"),
        sa.UniqueConstraint("policy_id", "version", "policy_digest",
                            name="uq_development_policy_digest_identity"),
        sa.UniqueConstraint("policy_digest", name="uq_development_policy_digest"),
        sa.CheckConstraint("policy_digest ~ '^[0-9a-f]{64}$'",
                           name="ck_development_policy_digest"),
    )
    policy_json = json.dumps(POLICY, sort_keys=True, separators=(",", ":"))
    op.get_bind().execute(
        sa.text(
            "INSERT INTO development_match_policies "
            "(policy_id, version, policy_digest, policy_document, frozen_at) "
            "VALUES ('DEVELOPMENT', 'development-v1', :policy_digest, "
            "CAST(:policy_document AS JSON), CURRENT_TIMESTAMP)"
        ),
        {"policy_digest": POLICY_DIGEST, "policy_document": policy_json},
    )

    op.create_table(
        "event_house_ai_selection_control",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.CheckConstraint("id = 1", name="ck_house_ai_selection_control_singleton"),
    )
    op.bulk_insert(
        sa.table("event_house_ai_selection_control", sa.column("id", sa.Integer())),
        [{"id": 1}],
    )
    op.create_table(
        "event_house_ai_selections",
        sa.Column("selection_id", sa.Uuid(), nullable=False),
        sa.Column("house_ai_id", sa.String(64), nullable=False),
        sa.Column("house_ai_version", sa.Integer(), nullable=False),
        sa.Column("artifact_sha256", sa.String(64), nullable=False),
        sa.Column("protocol_version", sa.String(64), nullable=False),
        sa.Column("runtime_profile", sa.String(64), nullable=False),
        sa.Column("runtime_identity_sha256", sa.String(64), nullable=False),
        sa.Column("rules_version", sa.String(64), nullable=False),
        sa.Column("policy_version", sa.String(64), nullable=False),
        sa.Column("policy_digest", sa.String(64), nullable=False),
        sa.Column("selected_by", sa.String(64), nullable=False),
        sa.Column("selection_reason", sa.String(64), nullable=False),
        sa.Column("selected_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deactivated_by", sa.String(64), nullable=True),
        sa.Column("deactivation_reason", sa.String(64), nullable=True),
        sa.Column("deactivated_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("selection_id"),
        sa.ForeignKeyConstraint(
            ["house_ai_id", "house_ai_version", "artifact_sha256"],
            ["house_ai_versions.house_ai_id", "house_ai_versions.version",
             "house_ai_versions.artifact_sha256"],
            ondelete="RESTRICT", name="fk_event_house_ai_exact_version",
        ),
        sa.CheckConstraint("artifact_sha256 ~ '^[0-9a-f]{64}$'",
                           name="ck_event_house_ai_digest"),
        sa.CheckConstraint(
            "(deactivated_at IS NULL AND deactivated_by IS NULL "
            "AND deactivation_reason IS NULL) OR "
            "(deactivated_at IS NOT NULL AND deactivated_by IS NOT NULL "
            "AND deactivation_reason IS NOT NULL)",
            name="ck_event_house_ai_deactivation_audit",
        ),
    )
    op.create_index(
        "uq_event_house_ai_one_active", "event_house_ai_selections", [sa.text("(1)")],
        unique=True, postgresql_where=sa.text("deactivated_at IS NULL"),
        sqlite_where=sa.text("deactivated_at IS NULL"),
    )

    op.add_column("matches", sa.Column("policy_id", sa.String(32), nullable=True))
    op.add_column("matches", sa.Column("policy_digest", sa.String(64), nullable=True))
    op.create_foreign_key(
        "fk_match_development_policy", "matches", "development_match_policies",
        ["policy_id", "policy_version", "policy_digest"],
        ["policy_id", "version", "policy_digest"], ondelete="RESTRICT",
    )
    op.create_check_constraint(
        "ck_match_policy_purpose", "matches",
        "(purpose = 'DEVELOPMENT' AND policy_id IS NOT NULL AND policy_digest IS NOT NULL) OR "
        "(purpose <> 'DEVELOPMENT' AND policy_id IS NULL AND policy_digest IS NULL)",
    )
    op.create_check_constraint(
        "ck_match_policy_digest", "matches",
        "policy_digest IS NULL OR policy_digest ~ '^[0-9a-f]{64}$'",
    )
    op.create_index(
        "uq_match_active_development_participant", "matches", ["idempotency_participant_id"],
        unique=True,
        postgresql_where=sa.text(
            "purpose = 'DEVELOPMENT' AND state IN ('QUEUED','STARTING','RUNNING')"
        ),
        sqlite_where=sa.text(
            "purpose = 'DEVELOPMENT' AND state IN ('QUEUED','STARTING','RUNNING')"
        ),
    )

    op.add_column(
        "worker_artifact_capabilities",
        sa.Column("execution_kind", sa.String(16), nullable=False, server_default="PARTICIPANT"),
    )
    op.add_column(
        "worker_artifact_capabilities",
        sa.Column("house_ai_id", sa.String(64), nullable=True),
    )
    op.add_column(
        "worker_artifact_capabilities",
        sa.Column("house_ai_version", sa.Integer(), nullable=True),
    )
    op.alter_column("worker_artifact_capabilities", "submission_version_id", nullable=True)
    op.create_check_constraint(
        "ck_worker_capability_subject_kind", "worker_artifact_capabilities",
        "(execution_kind = 'PARTICIPANT' AND submission_version_id IS NOT NULL "
        "AND house_ai_id IS NULL AND house_ai_version IS NULL) OR "
        "(execution_kind = 'HOUSE_AI' AND submission_version_id IS NULL "
        "AND house_ai_id IS NOT NULL AND house_ai_version IS NOT NULL)",
    )
    op.create_foreign_key(
        "fk_worker_capability_house_ai_subject", "worker_artifact_capabilities",
        "house_ai_versions", ["house_ai_id", "house_ai_version", "artifact_sha256"],
        ["house_ai_id", "version", "artifact_sha256"], ondelete="RESTRICT",
    )

    op.execute("""
    CREATE FUNCTION aik_development_policy_append_only() RETURNS trigger AS $$
    BEGIN
      RAISE EXCEPTION 'development match policy is immutable' USING ERRCODE='check_violation';
    END; $$ LANGUAGE plpgsql;
    CREATE TRIGGER development_match_policies_append_only
      BEFORE UPDATE OR DELETE ON development_match_policies
      FOR EACH ROW EXECUTE FUNCTION aik_development_policy_append_only();
    """)
    op.execute("""
    CREATE FUNCTION aik_house_ai_selection_insert_guard() RETURNS trigger AS $$
    DECLARE v house_ai_versions%ROWTYPE;
    BEGIN
      SELECT * INTO v FROM house_ai_versions
       WHERE house_ai_id=NEW.house_ai_id AND version=NEW.house_ai_version
         AND artifact_sha256=NEW.artifact_sha256 FOR KEY SHARE;
      IF NOT FOUND OR v.frozen_at IS NULL OR
         ROW(NEW.protocol_version, NEW.runtime_profile, NEW.runtime_identity_sha256,
             NEW.rules_version, NEW.policy_version, NEW.policy_digest) IS DISTINCT FROM
         ROW(v.protocol_version, v.runtime_profile, v.runtime_identity_sha256,
             v.rules_version, v.policy_version, v.policy_digest) THEN
        RAISE EXCEPTION 'House AI selection must bind an exact frozen version'
          USING ERRCODE='check_violation';
      END IF;
      IF NEW.selected_at IS NULL OR NEW.selected_by='' OR NEW.selection_reason='' OR
         NEW.deactivated_at IS NOT NULL OR NEW.deactivated_by IS NOT NULL OR
         NEW.deactivation_reason IS NOT NULL OR NOT EXISTS (
           SELECT 1 FROM development_match_policies
            WHERE policy_id='DEVELOPMENT' AND version=NEW.policy_version
              AND policy_digest=NEW.policy_digest
         ) THEN
        RAISE EXCEPTION 'House AI selection audit is invalid' USING ERRCODE='check_violation';
      END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql;
    CREATE FUNCTION aik_house_ai_selection_update_guard() RETURNS trigger AS $$
    BEGIN
      IF ROW(NEW.selection_id, NEW.house_ai_id, NEW.house_ai_version, NEW.artifact_sha256,
             NEW.protocol_version, NEW.runtime_profile, NEW.runtime_identity_sha256,
             NEW.rules_version, NEW.policy_version, NEW.policy_digest, NEW.selected_by,
             NEW.selection_reason, NEW.selected_at) IS DISTINCT FROM
         ROW(OLD.selection_id, OLD.house_ai_id, OLD.house_ai_version, OLD.artifact_sha256,
             OLD.protocol_version, OLD.runtime_profile, OLD.runtime_identity_sha256,
             OLD.rules_version, OLD.policy_version, OLD.policy_digest, OLD.selected_by,
             OLD.selection_reason, OLD.selected_at) OR
         OLD.deactivated_at IS NOT NULL OR NEW.deactivated_at IS NULL OR
         NEW.deactivated_by IS NULL OR NEW.deactivation_reason IS NULL THEN
        RAISE EXCEPTION 'House AI selection permits only one-way audited deactivation'
          USING ERRCODE='check_violation';
      END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql;
    CREATE FUNCTION aik_house_ai_selection_delete_guard() RETURNS trigger AS $$
    BEGIN
      RAISE EXCEPTION 'House AI selection is append-only' USING ERRCODE='check_violation';
    END; $$ LANGUAGE plpgsql;
    CREATE TRIGGER event_house_ai_selection_insert_guard BEFORE INSERT ON event_house_ai_selections
      FOR EACH ROW EXECUTE FUNCTION aik_house_ai_selection_insert_guard();
    CREATE TRIGGER event_house_ai_selection_update_guard BEFORE UPDATE ON event_house_ai_selections
      FOR EACH ROW EXECUTE FUNCTION aik_house_ai_selection_update_guard();
    CREATE TRIGGER event_house_ai_selection_delete_guard BEFORE DELETE ON event_house_ai_selections
      FOR EACH ROW EXECUTE FUNCTION aik_house_ai_selection_delete_guard();
    CREATE FUNCTION aik_house_ai_selection_control_update_guard() RETURNS trigger AS $$
    BEGIN
      IF NEW.id IS DISTINCT FROM OLD.id THEN
        RAISE EXCEPTION 'House AI selection control is immutable' USING ERRCODE='check_violation';
      END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql;
    CREATE FUNCTION aik_house_ai_selection_control_delete_guard() RETURNS trigger AS $$
    BEGIN
      RAISE EXCEPTION 'House AI selection control is immutable' USING ERRCODE='check_violation';
    END; $$ LANGUAGE plpgsql;
    CREATE TRIGGER event_house_ai_selection_control_update_guard
      BEFORE UPDATE ON event_house_ai_selection_control
      FOR EACH ROW EXECUTE FUNCTION aik_house_ai_selection_control_update_guard();
    CREATE TRIGGER event_house_ai_selection_control_delete_guard
      BEFORE DELETE ON event_house_ai_selection_control
      FOR EACH ROW EXECUTE FUNCTION aik_house_ai_selection_control_delete_guard();
    """)
    op.execute("""
    CREATE FUNCTION aik_match_development_policy_guard() RETURNS trigger AS $$
    BEGIN
      IF TG_OP='UPDATE' AND ROW(NEW.policy_id, NEW.policy_digest) IS DISTINCT FROM
                            ROW(OLD.policy_id, OLD.policy_digest) THEN
        RAISE EXCEPTION 'match development policy identity is immutable'
          USING ERRCODE='check_violation';
      END IF;
      IF TG_OP='INSERT' AND (
        (NEW.purpose='DEVELOPMENT' AND (NEW.policy_id IS NULL OR NEW.policy_digest IS NULL)) OR
        (NEW.purpose<>'DEVELOPMENT' AND
         (NEW.policy_id IS NOT NULL OR NEW.policy_digest IS NOT NULL))
      ) THEN
        RAISE EXCEPTION 'match policy identity must agree with match purpose'
          USING ERRCODE='check_violation';
      END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql;
    CREATE TRIGGER match_development_policy_guard BEFORE INSERT OR UPDATE ON matches
      FOR EACH ROW EXECUTE FUNCTION aik_match_development_policy_guard();
    CREATE FUNCTION aik_worker_capability_identity_guard() RETURNS trigger AS $$
    BEGIN
      IF ROW(NEW.id, NEW.token_hash, NEW.worker_id, NEW.execution_id, NEW.lease_id,
             NEW.fencing_token, NEW.execution_kind, NEW.artifact_sha256,
             NEW.submission_version_id, NEW.house_ai_id, NEW.house_ai_version,
             NEW.operation, NEW.expires_at, NEW.max_uses, NEW.created_at) IS DISTINCT FROM
         ROW(OLD.id, OLD.token_hash, OLD.worker_id, OLD.execution_id, OLD.lease_id,
             OLD.fencing_token, OLD.execution_kind, OLD.artifact_sha256,
             OLD.submission_version_id, OLD.house_ai_id, OLD.house_ai_version,
             OLD.operation, OLD.expires_at, OLD.max_uses, OLD.created_at) OR
         NEW.use_count NOT IN (OLD.use_count, OLD.use_count + 1) OR
         (OLD.revoked_at IS NOT NULL AND NEW.revoked_at IS DISTINCT FROM OLD.revoked_at) THEN
        RAISE EXCEPTION 'artifact capability identity is immutable' USING ERRCODE='check_violation';
      END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql;
    CREATE TRIGGER worker_artifact_capability_identity_guard
      BEFORE UPDATE ON worker_artifact_capabilities
      FOR EACH ROW EXECUTE FUNCTION aik_worker_capability_identity_guard();
    """)
    op.execute("""
    CREATE FUNCTION aik_worker_artifact_capability_insert_guard() RETURNS trigger AS $$
    DECLARE j worker_execution_jobs%ROWTYPE; w workers%ROWTYPE;
    BEGIN
      SELECT * INTO j FROM worker_execution_jobs
       WHERE execution_id=NEW.execution_id FOR SHARE;
      IF NOT FOUND THEN
        RAISE EXCEPTION 'artifact capability execution does not exist'
          USING ERRCODE='check_violation';
      END IF;
      SELECT * INTO w FROM workers WHERE worker_id=NEW.worker_id FOR KEY SHARE;
      IF NOT FOUND OR w.status <> 'ACTIVE' OR w.revoked_at IS NOT NULL OR
         j.status <> 'ACKNOWLEDGED' OR j.assigned_worker_id <> NEW.worker_id OR
         j.lease_id IS DISTINCT FROM NEW.lease_id OR
         j.fencing_token <> NEW.fencing_token OR
         j.execution_kind <> NEW.execution_kind OR
         j.artifact_sha256 <> NEW.artifact_sha256 OR
         j.lease_expires_at IS NULL OR j.lease_expires_at <= CURRENT_TIMESTAMP OR
         j.execution_expires_at <= CURRENT_TIMESTAMP OR
         NEW.expires_at <= CURRENT_TIMESTAMP OR
         NEW.expires_at > j.lease_expires_at OR
         NEW.expires_at > j.execution_expires_at OR
         NEW.use_count <> 0 OR NEW.revoked_at IS NOT NULL OR
         ((j.execution_kind='PARTICIPANT') AND
          (j.submission_version_id IS DISTINCT FROM NEW.submission_version_id OR
           NEW.house_ai_id IS NOT NULL OR NEW.house_ai_version IS NOT NULL)) OR
         ((j.execution_kind='HOUSE_AI') AND
          (j.submission_version_id IS NOT NULL OR NEW.submission_version_id IS NOT NULL OR
           j.house_ai_id IS DISTINCT FROM NEW.house_ai_id OR
           j.house_ai_version IS DISTINCT FROM NEW.house_ai_version)) THEN
        RAISE EXCEPTION 'artifact capability does not match the current typed worker lease'
          USING ERRCODE='check_violation';
      END IF;
      RETURN NEW;
    END; $$ LANGUAGE plpgsql;
    CREATE TRIGGER worker_artifact_capability_insert_guard
      BEFORE INSERT ON worker_artifact_capabilities
      FOR EACH ROW EXECUTE FUNCTION aik_worker_artifact_capability_insert_guard();
    """)

    # Extend the least-privilege gateway role only for digest-scoped House AI reads.
    op.execute("GRANT SELECT ON house_ai_versions, house_ai_artifacts TO aik_worker_gateway")
    op.execute(
        "GRANT INSERT (execution_kind, house_ai_id, house_ai_version) "
        "ON worker_artifact_capabilities TO aik_worker_gateway"
    )


def downgrade() -> None:
    raise RuntimeError(
        "0009 creates immutable development policy and House AI evidence; use forward recovery"
    )
