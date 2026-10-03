"""Least-privilege worker role and worker-authority database guards.

Revision ID: 0005_worker_gateway_privileges
Revises: 0004_worker_control_protocol
"""

from alembic import op

revision = "0005_worker_gateway_privileges"
down_revision = "0004_worker_control_protocol"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # This is a NOLOGIN privilege group, not a credential. Deployment creates a
    # separate login principal and grants membership after reviewing its DSN.
    op.execute(
        "CREATE ROLE aik_worker_gateway NOLOGIN NOSUPERUSER NOCREATEDB "
        "NOCREATEROLE NOBYPASSRLS NOINHERIT"
    )
    op.execute("GRANT USAGE ON SCHEMA public TO aik_worker_gateway")

    # Keep gateway reads to the worker tables and artifact-bearing tables listed here.
    op.execute(
        "GRANT SELECT ON submission_versions, quarantine_objects TO aik_worker_gateway"
    )
    op.execute("GRANT SELECT ON workers, worker_enrollment_tokens, "
               "worker_execution_jobs, worker_artifact_capabilities TO aik_worker_gateway")
    op.execute("GRANT UPDATE (pending_certificate_fingerprint, "
               "pending_certificate_identity, pending_certificate_serial, pending_issued_at, "
               "pending_expires_at, pending_capabilities, pending_software_version, "
               "pending_runtime_version, capabilities, software_version, runtime_version, "
               "last_heartbeat_at, security_state) "
               "ON workers TO aik_worker_gateway")
    op.execute("GRANT UPDATE (used_at) ON worker_enrollment_tokens "
               "TO aik_worker_gateway")
    op.execute("GRANT UPDATE (status, assigned_worker_id, lease_id, fencing_token, "
               "lease_expires_at, last_sequence, last_operation_hash, updated_at) "
               "ON worker_execution_jobs TO aik_worker_gateway")
    op.execute(
        "GRANT INSERT (id, token_hash, worker_id, execution_id, lease_id, fencing_token, "
        "artifact_sha256, submission_version_id, operation, expires_at, max_uses, use_count) "
        "ON worker_artifact_capabilities TO aik_worker_gateway"
    )
    op.execute("GRANT UPDATE (use_count, revoked_at) ON worker_artifact_capabilities "
               "TO aik_worker_gateway")
    op.execute("GRANT INSERT ON worker_evidence, worker_security_events TO aik_worker_gateway")

    # At most one unused, unrevoked rotation authorization. Expired rows are
    # revoked transactionally by issue_certificate_rotation_token before reuse.
    op.execute("CREATE UNIQUE INDEX uq_worker_one_open_rotation_token "
               "ON worker_enrollment_tokens (worker_id) "
               "WHERE purpose = 'ROTATE' AND used_at IS NULL AND revoked_at IS NULL")

    op.execute("ALTER TABLE worker_execution_jobs DROP CONSTRAINT ck_worker_job_status")
    op.execute("ALTER TABLE worker_execution_jobs ADD CONSTRAINT ck_worker_job_status "
               "CHECK (status IN ('QUEUED','LEASED','ACKNOWLEDGED','CLEANUP_REPORTED',"
               "'COMPLETED','FAILED','CANCELED','EXPIRED'))")
    op.execute("""
    CREATE FUNCTION aik_worker_completion_authority_guard() RETURNS trigger AS $$
    BEGIN
      IF NEW.status = 'COMPLETED'
         AND OLD.status IS DISTINCT FROM NEW.status
         AND pg_has_role(session_user, 'aik_worker_gateway', 'member') THEN
        RAISE EXCEPTION 'worker principal cannot finalize execution jobs'
          USING ERRCODE = 'insufficient_privilege';
      END IF;
      RETURN NEW;
    END $$ LANGUAGE plpgsql
    """)
    op.execute("CREATE TRIGGER worker_completion_authority_guard "
               "BEFORE UPDATE OF status ON worker_execution_jobs "
               "FOR EACH ROW EXECUTE FUNCTION aik_worker_completion_authority_guard()")


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS worker_completion_authority_guard ON worker_execution_jobs")
    op.execute("DROP FUNCTION IF EXISTS aik_worker_completion_authority_guard()")
    op.execute("ALTER TABLE worker_execution_jobs DROP CONSTRAINT IF EXISTS ck_worker_job_status")
    op.execute("ALTER TABLE worker_execution_jobs ADD CONSTRAINT ck_worker_job_status "
               "CHECK (status IN ('QUEUED','LEASED','ACKNOWLEDGED','COMPLETED','FAILED',"
               "'CANCELED','EXPIRED'))")
    op.execute("DROP INDEX IF EXISTS uq_worker_one_open_rotation_token")
    op.execute("REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA public FROM aik_worker_gateway")
    op.execute("REVOKE USAGE ON SCHEMA public FROM aik_worker_gateway")
    op.execute("DROP ROLE IF EXISTS aik_worker_gateway")
