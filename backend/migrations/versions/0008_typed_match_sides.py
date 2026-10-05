"""Typed match sides, House AI identity and execution kind foundation.

Revision ID: 0008_typed_match_sides
Revises: 0007_event_mechanics
"""

# PostgreSQL trigger bodies are intentionally kept as readable SQL blocks.
# ruff: noqa: E501

import sqlalchemy as sa
from alembic import op

revision = "0008_typed_match_sides"
down_revision = "0007_event_mechanics"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "house_ais",
        sa.Column("house_ai_id", sa.String(64), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_table(
        "house_ai_artifacts",
        sa.Column("artifact_sha256", sa.String(64), primary_key=True),
        sa.Column("artifact_size", sa.Integer(), nullable=False),
        sa.Column("storage_ref", sa.String(128), nullable=False, unique=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("artifact_size >= 0", name="ck_house_ai_artifact_size"),
        sa.CheckConstraint("artifact_sha256 ~ '^[0-9a-f]{64}$'", name="ck_house_ai_artifact_digest"),
        sa.UniqueConstraint("artifact_sha256", "artifact_size", name="uq_house_ai_artifact_digest_size"),
    )
    op.create_table(
        "house_ai_versions",
        sa.Column("house_ai_id", sa.String(64), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("artifact_sha256", sa.String(64), nullable=False),
        sa.Column("artifact_size", sa.Integer(), nullable=False),
        sa.Column("protocol_version", sa.String(64), nullable=False),
        sa.Column("runtime_profile", sa.String(64), nullable=False),
        sa.Column("runtime_identity_sha256", sa.String(64), nullable=False),
        sa.Column("rules_version", sa.String(64), nullable=False),
        sa.Column("policy_version", sa.String(64), nullable=False),
        sa.Column("policy_digest", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("frozen_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("version > 0", name="ck_house_ai_version_positive"),
        sa.CheckConstraint("artifact_size >= 0", name="ck_house_ai_version_size"),
        sa.CheckConstraint("artifact_sha256 ~ '^[0-9a-f]{64}$'", name="ck_house_ai_version_digest"),
        sa.CheckConstraint("runtime_identity_sha256 ~ '^[0-9a-f]{64}$'", name="ck_house_ai_runtime_digest"),
        sa.CheckConstraint("policy_digest ~ '^[0-9a-f]{64}$'", name="ck_house_ai_policy_digest"),
        sa.ForeignKeyConstraint(["house_ai_id"], ["house_ais.house_ai_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["artifact_sha256", "artifact_size"],
            ["house_ai_artifacts.artifact_sha256", "house_ai_artifacts.artifact_size"],
            ondelete="RESTRICT", name="fk_house_ai_version_artifact",
        ),
        sa.PrimaryKeyConstraint("house_ai_id", "version"),
        sa.UniqueConstraint("house_ai_id", "version", "artifact_sha256", name="uq_house_ai_version_digest"),
    )

    op.create_unique_constraint(
        "uq_submission_version_exact_identity", "submission_versions",
        ["id", "participant_id", "submission_id", "artifact_sha256"],
    )
    op.add_column(
        "matches", sa.Column("purpose", sa.String(16), nullable=False,
                              server_default="PEER_TEST")
    )
    op.create_check_constraint(
        "ck_matches_purpose", "matches", "purpose IN ('PEER_TEST','DEVELOPMENT','OFFICIAL')"
    )
    op.add_column("matches", sa.Column("idempotency_participant_id", sa.String(32), nullable=True))
    op.add_column("matches", sa.Column("idempotency_key", sa.String(128), nullable=True))
    op.add_column("matches", sa.Column("request_hash", sa.String(64), nullable=True))
    op.create_foreign_key(
        "fk_match_idempotency_participant", "matches", "participants",
        ["idempotency_participant_id"], ["participant_id"], ondelete="RESTRICT",
    )
    op.create_unique_constraint(
        "uq_match_participant_idempotency_key", "matches",
        ["idempotency_participant_id", "idempotency_key"],
    )
    op.create_check_constraint(
        "ck_match_idempotency_purpose", "matches",
        "(purpose = 'DEVELOPMENT' AND idempotency_participant_id IS NOT NULL "
        "AND idempotency_key IS NOT NULL AND request_hash IS NOT NULL) OR "
        "(purpose <> 'DEVELOPMENT' AND idempotency_participant_id IS NULL "
        "AND idempotency_key IS NULL AND request_hash IS NULL)",
    )
    op.create_check_constraint(
        "ck_match_request_hash_length", "matches",
        "request_hash IS NULL OR length(request_hash) = 64",
    )
    op.create_check_constraint(
        "ck_match_idempotency_key_length", "matches",
        "idempotency_key IS NULL OR length(idempotency_key) BETWEEN 1 AND 128",
    )
    op.add_column(
        "match_sides", sa.Column("kind", sa.String(16), nullable=False,
                                 server_default="PARTICIPANT")
    )
    op.create_check_constraint(
        "ck_match_sides_kind", "match_sides", "kind IN ('PARTICIPANT','HOUSE_AI')"
    )
    op.create_table(
        "participant_match_side_bindings",
        sa.Column("match_side_id", sa.Uuid(), nullable=False),
        sa.Column("participant_id", sa.String(32), nullable=False),
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("submission_version_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_sha256", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(["match_side_id"], ["match_sides.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["participant_id"], ["participants.participant_id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["submission_id"], ["submissions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["submission_version_id", "participant_id", "submission_id", "artifact_sha256"],
            ["submission_versions.id", "submission_versions.participant_id",
             "submission_versions.submission_id", "submission_versions.artifact_sha256"],
            ondelete="RESTRICT", name="fk_participant_side_exact_version",
        ),
        sa.PrimaryKeyConstraint("match_side_id"),
        sa.UniqueConstraint("match_side_id", "participant_id", name="uq_participant_side_identity"),
        sa.UniqueConstraint("match_side_id", "submission_version_id", "artifact_sha256",
                            name="uq_participant_side_artifact"),
    )
    op.create_index(
        "ix_participant_side_owner", "participant_match_side_bindings", ["participant_id"]
    )
    op.create_table(
        "house_ai_match_side_bindings",
        sa.Column("match_side_id", sa.Uuid(), nullable=False),
        sa.Column("house_ai_id", sa.String(64), nullable=False),
        sa.Column("house_ai_version", sa.Integer(), nullable=False),
        sa.Column("artifact_sha256", sa.String(64), nullable=False),
        sa.ForeignKeyConstraint(["match_side_id"], ["match_sides.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["house_ai_id", "house_ai_version", "artifact_sha256"],
            ["house_ai_versions.house_ai_id", "house_ai_versions.version",
             "house_ai_versions.artifact_sha256"],
            ondelete="RESTRICT", name="fk_house_side_exact_version",
        ),
        sa.PrimaryKeyConstraint("match_side_id"),
    )
    op.add_column(
        "match_execution_bindings",
        sa.Column("execution_kind", sa.String(16), nullable=False,
                  server_default="PARTICIPANT"),
    )
    op.add_column(
        "match_execution_bindings", sa.Column("house_ai_id", sa.String(64), nullable=True)
    )
    op.add_column(
        "match_execution_bindings", sa.Column("house_ai_version", sa.Integer(), nullable=True)
    )
    op.alter_column("match_execution_bindings", "submission_version_id", nullable=True)
    op.create_check_constraint(
        "ck_match_execution_subject_kind", "match_execution_bindings",
        "(execution_kind = 'PARTICIPANT' AND submission_version_id IS NOT NULL "
        "AND house_ai_id IS NULL AND house_ai_version IS NULL) OR "
        "(execution_kind = 'HOUSE_AI' AND submission_version_id IS NULL "
        "AND house_ai_id IS NOT NULL AND house_ai_version IS NOT NULL)",
    )
    op.create_foreign_key(
        "fk_match_execution_house_ai_subject", "match_execution_bindings", "house_ai_versions",
        ["house_ai_id", "house_ai_version", "artifact_sha256"],
        ["house_ai_id", "version", "artifact_sha256"], ondelete="RESTRICT",
    )

    op.execute("""
      INSERT INTO participant_match_side_bindings
        (match_side_id, participant_id, submission_id, submission_version_id, artifact_sha256)
      SELECT id, participant_id, submission_id, submission_version_id, artifact_sha256
      FROM match_sides
    """)
    for name in (
        "uq_match_sides_participant",
        "match_sides_participant_id_fkey",
        "match_sides_submission_id_fkey",
        "match_sides_submission_version_id_fkey",
        "match_sides_artifact_sha256_fkey",
        "ck_match_side_sha256",
    ):
        op.drop_constraint(name, "match_sides", type_="unique" if name.startswith("uq_") else "foreignkey" if name.endswith("_fkey") else "check")
    for column in ("participant_id", "submission_id", "submission_version_id", "artifact_sha256"):
        op.drop_column("match_sides", column)

    # Add an exact subject discriminator to the single durable worker-job/lease
    # record. Checks and FKs make the participant and House AI subject branches
    # mutually exclusive; the existing worker control plane remains shared.
    op.add_column(
        "worker_execution_jobs",
        sa.Column("execution_kind", sa.String(16), nullable=False,
                  server_default="PARTICIPANT"),
    )
    op.add_column("worker_execution_jobs", sa.Column("house_ai_id", sa.String(64), nullable=True))
    op.add_column("worker_execution_jobs", sa.Column("house_ai_version", sa.Integer(), nullable=True))
    for column in ("participant_id", "submission_id", "submission_version", "submission_version_id"):
        op.alter_column("worker_execution_jobs", column, nullable=True)
    op.drop_constraint("ck_worker_job_version_positive", "worker_execution_jobs", type_="check")
    op.create_check_constraint(
        "ck_worker_job_subject_kind", "worker_execution_jobs",
        "(execution_kind = 'PARTICIPANT' AND participant_id IS NOT NULL AND submission_id IS NOT NULL "
        "AND submission_version IS NOT NULL AND submission_version_id IS NOT NULL AND house_ai_id IS NULL "
        "AND house_ai_version IS NULL) OR (execution_kind = 'HOUSE_AI' AND participant_id IS NULL "
        "AND submission_id IS NULL AND submission_version IS NULL AND submission_version_id IS NULL "
        "AND house_ai_id IS NOT NULL AND house_ai_version IS NOT NULL)",
    )
    op.create_check_constraint(
        "ck_worker_job_version_positive", "worker_execution_jobs",
        "submission_version IS NULL OR submission_version > 0",
    )
    op.drop_constraint(
        "worker_execution_jobs_submission_version_id_fkey",
        "worker_execution_jobs", type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_worker_job_participant_subject", "worker_execution_jobs", "submission_versions",
        ["submission_version_id", "participant_id", "submission_id", "artifact_sha256"],
        ["id", "participant_id", "submission_id", "artifact_sha256"],
        ondelete="RESTRICT",
    )
    op.create_foreign_key(
        "fk_worker_job_subject_house_ai", "worker_execution_jobs", "house_ai_versions",
        ["house_ai_id", "house_ai_version", "artifact_sha256"],
        ["house_ai_id", "version", "artifact_sha256"], ondelete="RESTRICT",
    )

    # Old PostgreSQL guards referenced participant-only columns directly. Replace
    # them with kind-aware guards; participant jobs retain the same exact checks.
    op.execute("DROP TRIGGER IF EXISTS worker_job_insert_guard ON worker_execution_jobs")
    op.execute("DROP TRIGGER IF EXISTS worker_job_immutable_guard ON worker_execution_jobs")
    op.execute("DROP FUNCTION IF EXISTS aik_worker_job_insert_guard()")
    op.execute("DROP FUNCTION IF EXISTS aik_worker_job_immutable_guard()")
    op.execute("""
    CREATE FUNCTION aik_worker_job_insert_guard() RETURNS trigger AS $$
    DECLARE v submission_versions%ROWTYPE; h house_ai_versions%ROWTYPE; a house_ai_artifacts%ROWTYPE;
    BEGIN
      IF NEW.execution_kind = 'PARTICIPANT' THEN
        SELECT * INTO v FROM submission_versions
          WHERE id = NEW.submission_version_id FOR SHARE;
        IF NOT FOUND OR v.state <> 'ELIGIBLE' OR v.participant_id <> NEW.participant_id OR
           v.submission_id <> NEW.submission_id OR v.version <> NEW.submission_version OR
           v.artifact_sha256 <> NEW.artifact_sha256 OR v.storage_key <> NEW.immutable_artifact_ref OR
           v.protocol_version <> NEW.protocol_version OR v.runtime_profile <> NEW.runtime_profile THEN
          RAISE EXCEPTION 'participant execution subject does not match eligible artifact'
            USING ERRCODE = 'check_violation';
        END IF;
      ELSIF NEW.execution_kind = 'HOUSE_AI' THEN
        SELECT * INTO h FROM house_ai_versions
          WHERE house_ai_id = NEW.house_ai_id AND version = NEW.house_ai_version FOR SHARE;
        SELECT * INTO a FROM house_ai_artifacts
          WHERE artifact_sha256 = NEW.artifact_sha256 FOR SHARE;
        IF NOT FOUND OR h.frozen_at IS NULL OR h.artifact_sha256 <> NEW.artifact_sha256 OR
           h.protocol_version <> NEW.protocol_version OR h.runtime_profile <> NEW.runtime_profile OR
           a.storage_ref <> NEW.immutable_artifact_ref THEN
          RAISE EXCEPTION 'House AI execution subject does not match frozen artifact'
            USING ERRCODE = 'check_violation';
        END IF;
      ELSE
        RAISE EXCEPTION 'execution kind is invalid' USING ERRCODE = 'check_violation';
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """)
    op.execute("""
    CREATE TRIGGER worker_job_insert_guard BEFORE INSERT ON worker_execution_jobs
    FOR EACH ROW EXECUTE FUNCTION aik_worker_job_insert_guard()
    """)
    op.execute("""
    CREATE FUNCTION aik_worker_job_immutable_guard() RETURNS trigger AS $$
    BEGIN
      IF ROW(NEW.execution_id, NEW.match_id, NEW.execution_kind, NEW.participant_id,
             NEW.submission_id, NEW.submission_version, NEW.submission_version_id,
             NEW.house_ai_id, NEW.house_ai_version, NEW.artifact_sha256,
             NEW.immutable_artifact_ref, NEW.runtime_profile, NEW.protocol_version,
             NEW.execution_mode, NEW.initial_state::jsonb, NEW.resource_policy::jsonb,
             NEW.execution_expires_at) IS DISTINCT FROM
         ROW(OLD.execution_id, OLD.match_id, OLD.execution_kind, OLD.participant_id,
             OLD.submission_id, OLD.submission_version, OLD.submission_version_id,
             OLD.house_ai_id, OLD.house_ai_version, OLD.artifact_sha256,
             OLD.immutable_artifact_ref, OLD.runtime_profile, OLD.protocol_version,
             OLD.execution_mode, OLD.initial_state::jsonb, OLD.resource_policy::jsonb,
             OLD.execution_expires_at) THEN
        RAISE EXCEPTION 'worker execution job identity is immutable'
          USING ERRCODE = 'check_violation';
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """)
    op.execute("""
    CREATE TRIGGER worker_job_immutable_guard BEFORE UPDATE ON worker_execution_jobs
    FOR EACH ROW EXECUTE FUNCTION aik_worker_job_immutable_guard()
    """)

    # Kind-aware side insert guard and deferred exact-one child identity guard.
    op.execute("DROP TRIGGER IF EXISTS match_sides_insert_guard ON match_sides")
    op.execute("DROP FUNCTION IF EXISTS aik_match_side_insert_guard()")
    op.execute("""
    CREATE FUNCTION aik_match_side_insert_guard() RETURNS trigger AS $$
    DECLARE parent_state text;
    BEGIN
      SELECT state INTO parent_state FROM matches
        WHERE id = NEW.match_id FOR UPDATE;
      IF NOT FOUND OR parent_state <> 'QUEUED' THEN
        RAISE EXCEPTION 'match side must be attached to a queued match'
          USING ERRCODE = 'check_violation';
      END IF;
      IF NEW.kind NOT IN ('PARTICIPANT','HOUSE_AI') THEN
        RAISE EXCEPTION 'match side kind is invalid' USING ERRCODE = 'check_violation';
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """)
    op.execute("""
    CREATE TRIGGER match_sides_insert_guard BEFORE INSERT ON match_sides
    FOR EACH ROW EXECUTE FUNCTION aik_match_side_insert_guard()
    """)
    op.execute("""
    CREATE FUNCTION aik_match_side_identity_from_side_guard() RETURNS trigger AS $$
    DECLARE sid uuid; k text; p integer; h integer;
    BEGIN
      IF TG_OP = 'DELETE' THEN
        sid := OLD.id;
      ELSE
        sid := NEW.id;
      END IF;
      SELECT kind INTO k FROM match_sides WHERE id = sid;
      IF NOT FOUND THEN RETURN NULL; END IF;
      SELECT count(*) INTO p FROM participant_match_side_bindings WHERE match_side_id = sid;
      SELECT count(*) INTO h FROM house_ai_match_side_bindings WHERE match_side_id = sid;
      IF (k = 'PARTICIPANT' AND (p <> 1 OR h <> 0)) OR
         (k = 'HOUSE_AI' AND (p <> 0 OR h <> 1)) THEN
        RAISE EXCEPTION 'match side requires exactly one kind-matching identity'
          USING ERRCODE = 'check_violation';
      END IF;
      RETURN NULL;
    END;
    $$ LANGUAGE plpgsql;
    """)
    op.execute("""
    CREATE FUNCTION aik_participant_side_identity_guard() RETURNS trigger AS $$
    DECLARE sid uuid;
    BEGIN
      IF TG_OP = 'DELETE' THEN sid := OLD.match_side_id;
      ELSE sid := NEW.match_side_id;
      END IF;
      PERFORM aik_check_match_side_identity(sid);
      RETURN NULL;
    END;
    $$ LANGUAGE plpgsql;
    """)
    # Keep the shared check typed by passing only a UUID; trigger functions
    # access fields only on their actual table row types.
    op.execute("""
    CREATE FUNCTION aik_check_match_side_identity(sid uuid) RETURNS void AS $$
    DECLARE k text; p integer; h integer;
    BEGIN
      SELECT kind INTO k FROM match_sides WHERE id = sid;
      IF NOT FOUND THEN RETURN; END IF;
      SELECT count(*) INTO p FROM participant_match_side_bindings WHERE match_side_id = sid;
      SELECT count(*) INTO h FROM house_ai_match_side_bindings WHERE match_side_id = sid;
      IF (k = 'PARTICIPANT' AND (p <> 1 OR h <> 0)) OR
         (k = 'HOUSE_AI' AND (p <> 0 OR h <> 1)) THEN
        RAISE EXCEPTION 'match side requires exactly one kind-matching identity'
          USING ERRCODE = 'check_violation';
      END IF;
    END;
    $$ LANGUAGE plpgsql;
    """)
    op.execute("""
    CREATE FUNCTION aik_house_side_identity_guard() RETURNS trigger AS $$
    DECLARE sid uuid;
    BEGIN
      IF TG_OP = 'DELETE' THEN sid := OLD.match_side_id;
      ELSE sid := NEW.match_side_id;
      END IF;
      PERFORM aik_check_match_side_identity(sid);
      RETURN NULL;
    END;
    $$ LANGUAGE plpgsql;
    """)
    op.execute("""
    CREATE CONSTRAINT TRIGGER match_sides_typed_identity_check
    AFTER INSERT OR UPDATE OR DELETE ON match_sides DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION aik_match_side_identity_from_side_guard()
    """)
    op.execute("""
    CREATE CONSTRAINT TRIGGER participant_match_side_bindings_typed_identity_check
    AFTER INSERT OR UPDATE OR DELETE ON participant_match_side_bindings DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION aik_participant_side_identity_guard()
    """)
    op.execute("""
    CREATE CONSTRAINT TRIGGER house_ai_match_side_bindings_typed_identity_check
    AFTER INSERT OR UPDATE OR DELETE ON house_ai_match_side_bindings DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION aik_house_side_identity_guard()
    """)
    op.execute("""
    CREATE FUNCTION aik_participant_side_insert_guard() RETURNS trigger AS $$
    DECLARE parent_match_id uuid; parent_purpose text;
    BEGIN
      SELECT m.id, m.purpose INTO parent_match_id, parent_purpose
        FROM match_sides s JOIN matches m ON m.id = s.match_id
        WHERE s.id = NEW.match_side_id AND s.kind = 'PARTICIPANT' AND m.state = 'QUEUED'
        FOR UPDATE OF m;
      IF NOT FOUND THEN
        RAISE EXCEPTION 'participant side must pin eligible exact version'
          USING ERRCODE = 'check_violation';
      END IF;
      IF NOT EXISTS (
        SELECT 1 FROM match_sides s JOIN matches m ON m.id = s.match_id
        JOIN submission_versions v ON v.id = NEW.submission_version_id
        WHERE s.id = NEW.match_side_id AND s.match_id = parent_match_id
          AND s.kind = 'PARTICIPANT' AND m.state = 'QUEUED'
          AND v.state = 'ELIGIBLE' AND v.participant_id = NEW.participant_id
          AND v.submission_id = NEW.submission_id AND v.artifact_sha256 = NEW.artifact_sha256
          AND m.protocol_version = s.protocol_version AND m.runtime_profile = s.runtime_profile
          AND v.protocol_version = s.protocol_version AND v.runtime_profile = s.runtime_profile
        FOR SHARE OF v
      ) THEN
        RAISE EXCEPTION 'participant side must pin eligible exact version'
          USING ERRCODE = 'check_violation';
      END IF;
      IF parent_purpose IN ('PEER_TEST','OFFICIAL') AND EXISTS (
        SELECT 1 FROM match_sides s
        JOIN participant_match_side_bindings p ON p.match_side_id = s.id
        WHERE s.match_id = parent_match_id AND s.id <> NEW.match_side_id
          AND p.participant_id = NEW.participant_id
      ) THEN
        RAISE EXCEPTION 'participant cannot occupy both sides of this match'
          USING ERRCODE = 'unique_violation';
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """)
    op.execute("""
    CREATE TRIGGER participant_side_insert_guard BEFORE INSERT ON participant_match_side_bindings
    FOR EACH ROW EXECUTE FUNCTION aik_participant_side_insert_guard()
    """)
    op.execute("""
    CREATE FUNCTION aik_house_side_insert_guard() RETURNS trigger AS $$
    DECLARE parent_match_id uuid;
    BEGIN
      SELECT m.id INTO parent_match_id
        FROM match_sides s JOIN matches m ON m.id = s.match_id
        WHERE s.id = NEW.match_side_id AND s.kind = 'HOUSE_AI' AND m.state = 'QUEUED'
        FOR UPDATE OF m;
      IF NOT FOUND THEN
        RAISE EXCEPTION 'House AI side must pin exact frozen compatible version'
          USING ERRCODE = 'check_violation';
      END IF;
      IF NOT EXISTS (
        SELECT 1 FROM match_sides s JOIN matches m ON m.id = s.match_id
        JOIN house_ai_versions h ON h.house_ai_id = NEW.house_ai_id
          AND h.version = NEW.house_ai_version AND h.artifact_sha256 = NEW.artifact_sha256
        WHERE s.id = NEW.match_side_id AND s.match_id = parent_match_id
          AND s.kind = 'HOUSE_AI' AND m.state = 'QUEUED'
          AND h.frozen_at IS NOT NULL AND m.protocol_version = s.protocol_version
          AND m.runtime_profile = s.runtime_profile AND h.protocol_version = s.protocol_version
          AND h.runtime_profile = s.runtime_profile AND h.rules_version = m.rules_version
          AND h.policy_version = m.policy_version
        FOR SHARE OF h
      ) THEN
        RAISE EXCEPTION 'House AI side must pin exact frozen compatible version'
          USING ERRCODE = 'check_violation';
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """)
    op.execute("""
    CREATE TRIGGER house_side_insert_guard BEFORE INSERT ON house_ai_match_side_bindings
    FOR EACH ROW EXECUTE FUNCTION aik_house_side_insert_guard()
    """)
    # Serialize selection changes with the one-way event freeze. The 0007
    # selection guard uses FOR KEY SHARE, which is compatible with the
    # non-key UPDATE used to freeze event_control. FOR UPDATE closes that race.
    op.execute("""
    CREATE OR REPLACE FUNCTION aik_official_selection_guard() RETURNS trigger AS $$
    DECLARE frozen_at timestamptz; version_owner text; version_state text;
    BEGIN
      SELECT official_freeze_at INTO frozen_at FROM event_control
        WHERE id = 1 FOR UPDATE;
      IF frozen_at IS NOT NULL THEN
        RAISE EXCEPTION 'official submission selection is frozen'
          USING ERRCODE = 'check_violation';
      END IF;
      IF TG_OP = 'DELETE' THEN RETURN OLD; END IF;
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
    # Existing execution/move guards must resolve participant identity through
    # the typed side binding, while preserving all lease, fence and readiness checks.
    op.execute("DROP TRIGGER IF EXISTS match_execution_insert_guard ON match_execution_bindings")
    op.execute("DROP FUNCTION IF EXISTS aik_match_execution_insert_guard()")
    op.execute("""
    CREATE FUNCTION aik_match_execution_insert_guard() RETURNS trigger AS $$
    DECLARE expected_attempt integer;
    BEGIN
      IF NEW.execution_kind = 'PARTICIPANT' AND NOT EXISTS (
        SELECT 1 FROM submission_versions v
        WHERE v.id = NEW.submission_version_id AND v.state = 'ELIGIBLE'
        FOR SHARE OF v
      ) THEN
        RAISE EXCEPTION 'participant execution version is no longer eligible'
          USING ERRCODE = 'check_violation';
      END IF;
      PERFORM w.worker_id FROM workers w
        JOIN worker_execution_jobs j ON j.assigned_worker_id = w.worker_id
        WHERE j.execution_id = NEW.execution_id AND w.worker_id = NEW.worker_id
          AND w.status = 'ACTIVE' AND w.revoked_at IS NULL
        FOR SHARE OF w;
      IF NOT FOUND THEN
        RAISE EXCEPTION 'execution binding worker is not active'
          USING ERRCODE = 'check_violation';
      END IF;
      PERFORM j.execution_id FROM worker_execution_jobs j
        WHERE j.execution_id = NEW.execution_id AND j.assigned_worker_id = NEW.worker_id
          AND j.lease_id = NEW.lease_id AND j.fencing_token = NEW.fencing_token
          AND j.status = 'ACKNOWLEDGED' AND j.lease_expires_at > clock_timestamp()
          AND j.execution_expires_at > clock_timestamp()
        FOR SHARE OF j;
      IF NOT FOUND THEN
        RAISE EXCEPTION 'execution binding lease is stale or expired'
          USING ERRCODE = 'check_violation';
      END IF;
      SELECT COALESCE(MAX(attempt_number), 0) + 1 INTO expected_attempt
        FROM match_execution_bindings WHERE match_side_id = NEW.match_side_id;
      IF NEW.attempt_number <> expected_attempt OR NOT EXISTS (
        SELECT 1 FROM matches m JOIN match_sides s ON s.id = NEW.match_side_id
        JOIN worker_execution_jobs j ON j.execution_id = NEW.execution_id
        JOIN workers w ON w.worker_id = j.assigned_worker_id
        WHERE m.id = NEW.match_id AND m.state = 'STARTING' AND s.match_id = m.id
          AND s.kind = NEW.execution_kind AND j.execution_kind = NEW.execution_kind
          AND j.match_id = NEW.match_id AND j.artifact_sha256 = NEW.artifact_sha256
          AND j.assigned_worker_id = NEW.worker_id AND j.lease_id = NEW.lease_id
          AND j.fencing_token = NEW.fencing_token AND j.status = 'ACKNOWLEDGED'
          AND j.protocol_version = s.protocol_version AND j.runtime_profile = s.runtime_profile
          AND j.lease_expires_at > now() AND j.execution_expires_at > now()
          AND w.status = 'ACTIVE' AND w.revoked_at IS NULL
          AND (
            (NEW.execution_kind = 'PARTICIPANT' AND EXISTS (
              SELECT 1 FROM participant_match_side_bindings p
              WHERE p.match_side_id = s.id
                AND p.submission_version_id = NEW.submission_version_id
                AND p.artifact_sha256 = NEW.artifact_sha256
                AND p.participant_id = j.participant_id AND p.submission_id = j.submission_id
                AND j.submission_version_id = p.submission_version_id
            )) OR
            (NEW.execution_kind = 'HOUSE_AI' AND EXISTS (
              SELECT 1 FROM house_ai_match_side_bindings h
              WHERE h.match_side_id = s.id AND h.house_ai_id = NEW.house_ai_id
                AND h.house_ai_version = NEW.house_ai_version
                AND h.artifact_sha256 = NEW.artifact_sha256
                AND h.house_ai_id = j.house_ai_id
                AND h.house_ai_version = j.house_ai_version
            ))
          )
      ) THEN
        RAISE EXCEPTION 'execution binding must match typed side and current lease'
          USING ERRCODE = 'check_violation';
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """)
    op.execute("""
    CREATE TRIGGER match_execution_insert_guard BEFORE INSERT ON match_execution_bindings
    FOR EACH ROW EXECUTE FUNCTION aik_match_execution_insert_guard()
    """)
    op.execute("DROP TRIGGER IF EXISTS match_moves_insert_guard ON match_moves")
    op.execute("DROP FUNCTION IF EXISTS aik_match_move_insert_guard()")
    op.execute("""
    CREATE FUNCTION aik_match_move_insert_guard() RETURNS trigger AS $$
    DECLARE expected_ply integer; side_kind text; side_version_id uuid;
            binding_worker_id text; binding_execution_id uuid;
    BEGIN
      SELECT current_ply + 1 INTO expected_ply FROM matches
        WHERE id = NEW.match_id AND state = 'RUNNING' FOR UPDATE;
      IF NOT FOUND OR NEW.sequence <> expected_ply THEN
        RAISE EXCEPTION 'move sequence, side or execution binding is stale'
          USING ERRCODE = 'check_violation';
      END IF;
      SELECT s.kind, p.submission_version_id INTO side_kind, side_version_id
        FROM match_sides s LEFT JOIN participant_match_side_bindings p ON p.match_side_id = s.id
        WHERE s.id = NEW.match_side_id AND s.match_id = NEW.match_id;
      IF side_kind = 'PARTICIPANT' AND NOT EXISTS (
        SELECT 1 FROM submission_versions v
        WHERE v.id = side_version_id AND v.state = 'ELIGIBLE'
        FOR SHARE OF v
      ) THEN
        RAISE EXCEPTION 'participant execution version is no longer eligible'
          USING ERRCODE = 'check_violation';
      END IF;
      SELECT worker_id, execution_id INTO binding_worker_id, binding_execution_id
        FROM match_execution_bindings WHERE id = NEW.execution_binding_id;
      PERFORM w.worker_id FROM workers w
        WHERE w.worker_id = binding_worker_id AND w.status = 'ACTIVE' AND w.revoked_at IS NULL
        FOR SHARE OF w;
      IF NOT FOUND THEN
        RAISE EXCEPTION 'move worker is revoked or inactive'
          USING ERRCODE = 'check_violation';
      END IF;
      PERFORM j.execution_id FROM worker_execution_jobs j
        WHERE j.execution_id = binding_execution_id AND j.status = 'ACKNOWLEDGED'
          AND j.lease_expires_at > clock_timestamp()
          AND j.execution_expires_at > clock_timestamp()
        FOR SHARE OF j;
      IF NOT FOUND THEN
        RAISE EXCEPTION 'move lease is stale or expired'
          USING ERRCODE = 'check_violation';
      END IF;
      IF NOT EXISTS (
        SELECT 1 FROM matches m JOIN match_sides s ON s.id = NEW.match_side_id
        JOIN match_execution_bindings b ON b.id = NEW.execution_binding_id
        JOIN worker_execution_jobs j ON j.execution_id = b.execution_id
        JOIN workers w ON w.worker_id = j.assigned_worker_id
        WHERE m.id = NEW.match_id AND s.match_id = m.id AND s.kind = b.execution_kind
          AND s.color = m.side_to_move AND b.match_id = m.id AND b.match_side_id = s.id
          AND b.artifact_sha256 = j.artifact_sha256 AND j.execution_kind = b.execution_kind
          AND b.attempt_number = (SELECT max(b2.attempt_number) FROM match_execution_bindings b2
                                 WHERE b2.match_side_id = s.id)
          AND j.match_id = b.match_id AND j.assigned_worker_id = b.worker_id
          AND j.lease_id = b.lease_id AND j.fencing_token = b.fencing_token
          AND j.status = 'ACKNOWLEDGED' AND j.lease_expires_at > now()
          AND j.execution_expires_at > now() AND w.status = 'ACTIVE' AND w.revoked_at IS NULL
          AND (
            (b.execution_kind = 'PARTICIPANT' AND EXISTS (
              SELECT 1 FROM participant_match_side_bindings p
              WHERE p.match_side_id = s.id AND p.submission_version_id = b.submission_version_id
                AND p.artifact_sha256 = b.artifact_sha256
                AND p.participant_id = j.participant_id AND p.submission_id = j.submission_id
                AND j.submission_version_id = p.submission_version_id
            )) OR
            (b.execution_kind = 'HOUSE_AI' AND EXISTS (
              SELECT 1 FROM house_ai_match_side_bindings h
              WHERE h.match_side_id = s.id AND h.house_ai_id = b.house_ai_id
                AND h.house_ai_version = b.house_ai_version
                AND h.artifact_sha256 = b.artifact_sha256
                AND h.house_ai_id = j.house_ai_id AND h.house_ai_version = j.house_ai_version
            ))
          )
      ) THEN
        RAISE EXCEPTION 'move sequence, side or execution binding is stale'
          USING ERRCODE = 'check_violation';
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """)
    op.execute("""
    CREATE TRIGGER match_moves_insert_guard BEFORE INSERT ON match_moves
    FOR EACH ROW EXECUTE FUNCTION aik_match_move_insert_guard()
    """)

    # Existing legacy matches are peer tests, never silently official.
    # Runtime trigger immutable tuple must now also protect purpose.
    op.execute("""
    CREATE OR REPLACE FUNCTION aik_match_update_guard() RETURNS trigger AS $$
    DECLARE frozen_at timestamptz;
    BEGIN
      IF ROW(NEW.id, NEW.purpose, NEW.idempotency_participant_id, NEW.idempotency_key,
             NEW.request_hash, NEW.protocol_version, NEW.runtime_profile, NEW.policy_version,
             NEW.rules_version, NEW.match_seed, NEW.initial_fen, NEW.initial_clock_ms, NEW.increment_ms,
             NEW.move_timeout_ms, NEW.created_at) IS DISTINCT FROM
         ROW(OLD.id, OLD.purpose, OLD.idempotency_participant_id, OLD.idempotency_key,
             OLD.request_hash, OLD.protocol_version, OLD.runtime_profile, OLD.policy_version,
             OLD.rules_version, OLD.match_seed, OLD.initial_fen, OLD.initial_clock_ms, OLD.increment_ms,
             OLD.move_timeout_ms, OLD.created_at) THEN
        RAISE EXCEPTION 'match identity and configuration are immutable' USING ERRCODE = 'check_violation';
      END IF;
      IF (OLD.started_at IS NOT NULL AND NEW.started_at IS DISTINCT FROM OLD.started_at) OR
         (OLD.completed_at IS NOT NULL AND NEW.completed_at IS DISTINCT FROM OLD.completed_at) OR
         OLD.state IN ('COMPLETED','ABORTED') THEN
        RAISE EXCEPTION 'match terminal identity/timestamps are immutable' USING ERRCODE = 'check_violation';
      END IF;
      IF NEW.state IS DISTINCT FROM OLD.state AND NOT (
        (OLD.state = 'QUEUED' AND NEW.state IN ('STARTING','ABORTED')) OR
        (OLD.state = 'STARTING' AND NEW.state IN ('RUNNING','ABORTED')) OR
        (OLD.state = 'RUNNING' AND NEW.state IN ('COMPLETED','ABORTED'))
      ) THEN
        RAISE EXCEPTION 'invalid match state transition' USING ERRCODE = 'check_violation';
      END IF;
      IF OLD.state = 'QUEUED' AND NEW.state = 'STARTING' AND (
        NEW.started_at IS NOT NULL OR NEW.completed_at IS NOT NULL OR
        (SELECT count(*) FROM match_sides WHERE match_id = NEW.id) <> 2 OR
        (NEW.purpose IN ('PEER_TEST','OFFICIAL') AND
          (SELECT count(*) FROM match_sides WHERE match_id=NEW.id AND kind='PARTICIPANT') <> 2) OR
        (NEW.purpose = 'DEVELOPMENT' AND
          (SELECT count(*) FROM match_sides WHERE match_id=NEW.id AND kind='PARTICIPANT') <> 1) OR
        (NEW.purpose = 'DEVELOPMENT' AND
          (SELECT count(*) FROM match_sides WHERE match_id=NEW.id AND kind='HOUSE_AI') <> 1)
      ) THEN
        RAISE EXCEPTION 'match purpose requires compatible exact side composition'
          USING ERRCODE = 'check_violation';
      END IF;
      IF OLD.state = 'QUEUED' AND NEW.state = 'STARTING' THEN
        PERFORM v.id FROM submission_versions v
          JOIN participant_match_side_bindings p ON p.submission_version_id = v.id
          JOIN match_sides s ON s.id = p.match_side_id
          WHERE s.match_id = NEW.id FOR SHARE OF v;
        IF EXISTS (
          SELECT 1 FROM participant_match_side_bindings p
          JOIN match_sides s ON s.id = p.match_side_id
          JOIN submission_versions v ON v.id = p.submission_version_id
          WHERE s.match_id = NEW.id AND v.state <> 'ELIGIBLE'
        ) THEN
          RAISE EXCEPTION 'match sides require currently eligible participant versions'
            USING ERRCODE = 'check_violation';
        END IF;
        IF NEW.purpose = 'OFFICIAL' THEN
          SELECT official_freeze_at INTO frozen_at FROM event_control
            WHERE id = 1 FOR SHARE;
          IF frozen_at IS NULL THEN
            RAISE EXCEPTION 'official match requires frozen official selections'
              USING ERRCODE = 'check_violation';
          END IF;
          PERFORM os.id FROM official_submission_selections os
            JOIN match_sides s ON s.match_id = NEW.id
            JOIN participant_match_side_bindings p ON p.match_side_id = s.id
            WHERE os.participant_id = p.participant_id
              AND os.submission_version_id = p.submission_version_id
            FOR SHARE OF os;
          IF (SELECT count(*) FROM match_sides s
              JOIN participant_match_side_bindings p ON p.match_side_id = s.id
              JOIN official_submission_selections os
                ON os.participant_id = p.participant_id
               AND os.submission_version_id = p.submission_version_id
              WHERE s.match_id = NEW.id) <> 2 THEN
            RAISE EXCEPTION 'official match sides must equal the frozen selections'
              USING ERRCODE = 'check_violation';
          END IF;
        END IF;
      END IF;
      IF NEW.state = 'RUNNING' AND OLD.state <> 'RUNNING' AND (
        NEW.started_at IS NULL OR NEW.completed_at IS NOT NULL OR
        (SELECT count(*) FROM match_sides WHERE match_id=NEW.id) <> 2 OR
        (SELECT count(DISTINCT b.match_side_id) FROM match_execution_bindings b
          WHERE b.match_id=NEW.id AND b.attempt_number=(SELECT max(b2.attempt_number)
            FROM match_execution_bindings b2 WHERE b2.match_side_id=b.match_side_id)
            AND EXISTS (SELECT 1 FROM competition_events e WHERE e.aggregate_type='match'
              AND e.aggregate_id=NEW.id AND e.event_type='EXECUTION_READY'
              AND e.metadata ->> 'binding_id'=b.id::text)) <> 2
      ) THEN
        RAISE EXCEPTION 'running requires both current executions ready'
          USING ERRCODE = 'check_violation';
      END IF;
      IF NEW.state = 'RUNNING' AND OLD.state <> 'RUNNING' THEN
        -- Lock worker rows before job rows, matching revoke_worker() order.
        -- This serializes activation against revocation and lease reassignment.
        PERFORM w.worker_id FROM workers w
          JOIN worker_execution_jobs j ON j.assigned_worker_id = w.worker_id
          JOIN match_execution_bindings b ON b.execution_id = j.execution_id
          WHERE b.match_id = NEW.id
            AND b.attempt_number = (SELECT max(b2.attempt_number)
              FROM match_execution_bindings b2 WHERE b2.match_side_id = b.match_side_id)
          ORDER BY w.worker_id FOR SHARE OF w;
        PERFORM j.execution_id FROM worker_execution_jobs j
          JOIN match_execution_bindings b ON b.execution_id = j.execution_id
          WHERE b.match_id = NEW.id
            AND b.attempt_number = (SELECT max(b2.attempt_number)
              FROM match_execution_bindings b2 WHERE b2.match_side_id = b.match_side_id)
          ORDER BY j.execution_id FOR SHARE OF j;
        IF (SELECT count(DISTINCT b.match_side_id)
            FROM match_execution_bindings b
            JOIN worker_execution_jobs j ON j.execution_id = b.execution_id
            JOIN workers w ON w.worker_id = j.assigned_worker_id
            WHERE b.match_id = NEW.id
              AND b.attempt_number = (SELECT max(b2.attempt_number)
                FROM match_execution_bindings b2 WHERE b2.match_side_id = b.match_side_id)
              AND j.status = 'ACKNOWLEDGED' AND j.assigned_worker_id = b.worker_id
              AND j.lease_id = b.lease_id AND j.fencing_token = b.fencing_token
              AND j.lease_expires_at > clock_timestamp()
              AND j.execution_expires_at > clock_timestamp()
              AND w.status = 'ACTIVE' AND w.revoked_at IS NULL) <> 2 THEN
          RAISE EXCEPTION 'running requires two current active worker leases'
            USING ERRCODE = 'check_violation';
        END IF;
        PERFORM v.id FROM submission_versions v
          JOIN participant_match_side_bindings p ON p.submission_version_id = v.id
          JOIN match_sides s ON s.id = p.match_side_id
          WHERE s.match_id = NEW.id FOR SHARE OF v;
        IF EXISTS (
          SELECT 1 FROM participant_match_side_bindings p
          JOIN match_sides s ON s.id = p.match_side_id
          JOIN submission_versions v ON v.id = p.submission_version_id
          WHERE s.match_id = NEW.id AND v.state <> 'ELIGIBLE'
        ) THEN
          RAISE EXCEPTION 'running match requires currently eligible participant versions'
            USING ERRCODE = 'check_violation';
        END IF;
      END IF;
      IF NEW.current_ply <> OLD.current_ply THEN
        IF NEW.current_ply <> OLD.current_ply + 1 OR NOT EXISTS (
          SELECT 1 FROM match_moves mv JOIN match_sides s ON s.id=mv.match_side_id
          WHERE mv.match_id=NEW.id AND mv.sequence=NEW.current_ply
            AND mv.position_after_fen=NEW.current_fen AND s.color=OLD.side_to_move
            AND NEW.side_to_move<>OLD.side_to_move
        ) THEN RAISE EXCEPTION 'match position must advance with one authoritative move'
          USING ERRCODE='check_violation'; END IF;
      ELSIF ROW(NEW.current_fen, NEW.side_to_move, NEW.white_remaining_ms, NEW.black_remaining_ms)
            IS DISTINCT FROM ROW(OLD.current_fen, OLD.side_to_move, OLD.white_remaining_ms, OLD.black_remaining_ms) THEN
        RAISE EXCEPTION 'position and clocks require an accepted move' USING ERRCODE='check_violation';
      END IF;
      IF NEW.state='COMPLETED' AND (NEW.result_outcome IS NULL OR NEW.result_reason IS NULL OR NEW.failure_reason IS NOT NULL) THEN
        RAISE EXCEPTION 'completed match requires server-derived result' USING ERRCODE='check_violation';
      END IF;
      IF NEW.state='ABORTED' AND (NEW.failure_reason IS NULL OR NEW.result_outcome IS NOT NULL OR NEW.result_reason IS NOT NULL) THEN
        RAISE EXCEPTION 'aborted match cannot declare chess result' USING ERRCODE='check_violation';
      END IF;
      IF NEW.state IN ('COMPLETED','ABORTED') AND NEW.completed_at IS NULL THEN
        RAISE EXCEPTION 'terminal match requires completion timestamp' USING ERRCODE='check_violation';
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """)

    # Subject-identity tables and House AI inventory are append-only.
    for table in ("house_ais", "house_ai_artifacts", "house_ai_versions",
                  "participant_match_side_bindings", "house_ai_match_side_bindings"):
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION aik_match_immutable_record()"
        )


def downgrade() -> None:
    raise RuntimeError(
        "0008 contains typed immutable identities; downgrade requires a verified forward recovery"
    )
