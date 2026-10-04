"""Durable server-authoritative match records and immutable move transcript.

Revision ID: 0006_match_controller
Revises: 0005_worker_gateway_privileges
"""

# PostgreSQL PL/pgSQL trigger bodies are intentionally kept as readable SQL blocks.
# ruff: noqa: E501

import sqlalchemy as sa
from alembic import op

revision = "0006_match_controller"
down_revision = "0005_worker_gateway_privileges"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "matches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("state", sa.String(16), nullable=False),
        sa.Column("protocol_version", sa.String(64), nullable=False),
        sa.Column("runtime_profile", sa.String(64), nullable=False),
        sa.Column("policy_version", sa.String(64), nullable=False),
        sa.Column("rules_version", sa.String(64), nullable=False),
        sa.Column("match_seed", sa.String(64), nullable=False),
        sa.Column("initial_fen", sa.String(256), nullable=False),
        sa.Column("current_fen", sa.String(256), nullable=False),
        sa.Column("side_to_move", sa.String(8), nullable=False),
        sa.Column("current_ply", sa.Integer(), nullable=False),
        sa.Column("initial_clock_ms", sa.Integer(), nullable=False),
        sa.Column("increment_ms", sa.Integer(), nullable=False),
        sa.Column("move_timeout_ms", sa.Integer(), nullable=False),
        sa.Column("white_remaining_ms", sa.Integer(), nullable=False),
        sa.Column("black_remaining_ms", sa.Integer(), nullable=False),
        sa.Column("result_outcome", sa.String(16), nullable=True),
        sa.Column("result_reason", sa.String(64), nullable=True),
        sa.Column("failure_reason", sa.String(32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "state IN ('QUEUED','STARTING','RUNNING','COMPLETED','ABORTED')",
            name="ck_matches_state",
        ),
        sa.CheckConstraint("side_to_move IN ('WHITE','BLACK')", name="ck_matches_side_to_move"),
        sa.CheckConstraint("match_seed ~ '^[0-9a-f]{64}$'", name="ck_matches_seed_format"),
        sa.CheckConstraint("current_ply >= 0", name="ck_matches_ply_nonnegative"),
        sa.CheckConstraint("initial_clock_ms > 0", name="ck_matches_initial_clock_positive"),
        sa.CheckConstraint("increment_ms >= 0", name="ck_matches_increment_nonnegative"),
        sa.CheckConstraint("move_timeout_ms > 0", name="ck_matches_move_timeout_positive"),
        sa.CheckConstraint(
            "result_reason IS NULL OR result_reason IN "
            "('CHECKMATE','STALEMATE','DEAD_POSITION','FIVEFOLD_REPETITION',"
            "'SEVENTY_FIVE_MOVE_RULE','THREEFOLD_REPETITION','FIFTY_MOVE_RULE')",
            name="ck_matches_result_reason",
        ),
        sa.CheckConstraint(
            "result_outcome IS NULL OR result_outcome IN ('WHITE_WIN','BLACK_WIN','DRAW')",
            name="ck_matches_result_outcome",
        ),
        sa.CheckConstraint("white_remaining_ms >= 0", name="ck_matches_white_clock_nonnegative"),
        sa.CheckConstraint("black_remaining_ms >= 0", name="ck_matches_black_clock_nonnegative"),
        sa.CheckConstraint(
            "(state = 'QUEUED' AND started_at IS NULL AND completed_at IS NULL) OR "
            "(state = 'STARTING' AND completed_at IS NULL) OR "
            "(state = 'RUNNING' AND started_at IS NOT NULL AND completed_at IS NULL) OR "
            "(state IN ('COMPLETED','ABORTED') AND completed_at IS NOT NULL)",
            name="ck_matches_lifecycle_timestamps",
        ),
        sa.CheckConstraint(
            "(state = 'COMPLETED' AND result_outcome IN ('WHITE_WIN','BLACK_WIN','DRAW') "
            "AND result_reason IS NOT NULL AND failure_reason IS NULL) OR "
            "(state = 'ABORTED' AND result_outcome IS NULL AND result_reason IS NULL "
            "AND failure_reason IS NOT NULL) OR "
            "(state NOT IN ('COMPLETED','ABORTED') AND result_outcome IS NULL "
            "AND result_reason IS NULL AND failure_reason IS NULL)",
            name="ck_matches_terminal_authority",
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    op.create_table(
        "match_sides",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("match_id", sa.Uuid(), nullable=False),
        sa.Column("color", sa.String(8), nullable=False),
        sa.Column("participant_id", sa.String(32), nullable=False),
        sa.Column("submission_id", sa.Uuid(), nullable=False),
        sa.Column("submission_version_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_sha256", sa.String(64), nullable=False),
        sa.Column("protocol_version", sa.String(64), nullable=False),
        sa.Column("runtime_profile", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("color IN ('WHITE','BLACK')", name="ck_match_sides_color"),
        sa.CheckConstraint("artifact_sha256 ~ '^[0-9a-f]{64}$'", name="ck_match_side_sha256"),
        sa.ForeignKeyConstraint(["match_id"], ["matches.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["participant_id"], ["participants.participant_id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["submission_id"], ["submissions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["submission_version_id"], ["submission_versions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["artifact_sha256"], ["quarantine_objects.artifact_sha256"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("match_id", "color", name="uq_match_sides_color"),
        sa.UniqueConstraint("match_id", "participant_id", name="uq_match_sides_participant"),
    )
    op.create_index("ix_match_sides_match_id", "match_sides", ["match_id"])

    op.create_table(
        "match_execution_bindings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("match_id", sa.Uuid(), nullable=False),
        sa.Column("match_side_id", sa.Uuid(), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("execution_id", sa.Uuid(), nullable=False),
        sa.Column("submission_version_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_sha256", sa.String(64), nullable=False),
        sa.Column("worker_id", sa.String(64), nullable=False),
        sa.Column("lease_id", sa.Uuid(), nullable=False),
        sa.Column("fencing_token", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("attempt_number > 0", name="ck_match_execution_attempt_positive"),
        sa.CheckConstraint("fencing_token > 0", name="ck_match_execution_fence_positive"),
        sa.CheckConstraint("artifact_sha256 ~ '^[0-9a-f]{64}$'", name="ck_match_execution_sha256"),
        sa.ForeignKeyConstraint(["match_id"], ["matches.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["match_side_id"], ["match_sides.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["submission_version_id"], ["submission_versions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["worker_id"], ["workers.worker_id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("execution_id", name="uq_match_execution_id"),
        sa.UniqueConstraint("match_side_id", "attempt_number", name="uq_match_side_attempt"),
    )
    op.create_index(
        "ix_match_execution_bindings_match_id", "match_execution_bindings", ["match_id"]
    )

    op.create_table(
        "match_moves",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("match_id", sa.Uuid(), nullable=False),
        sa.Column("match_side_id", sa.Uuid(), nullable=False),
        sa.Column("execution_binding_id", sa.Uuid(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("request_id", sa.String(64), nullable=False),
        sa.Column("move_uci", sa.String(5), nullable=False),
        sa.Column("position_before_sha256", sa.String(64), nullable=False),
        sa.Column("position_after_sha256", sa.String(64), nullable=False),
        sa.Column("position_before_fen", sa.String(256), nullable=False),
        sa.Column("position_after_fen", sa.String(256), nullable=False),
        sa.Column("elapsed_ms", sa.Integer(), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("sequence > 0", name="ck_match_move_sequence_positive"),
        sa.CheckConstraint("elapsed_ms >= 0", name="ck_match_move_elapsed_nonnegative"),
        sa.CheckConstraint("length(move_uci) IN (4,5)", name="ck_match_move_uci_length"),
        sa.ForeignKeyConstraint(["match_id"], ["matches.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["match_side_id"], ["match_sides.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["execution_binding_id"], ["match_execution_bindings.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("match_id", "sequence", name="uq_match_move_sequence"),
        sa.UniqueConstraint("match_id", "request_id", name="uq_match_move_request"),
    )
    op.create_index("ix_match_moves_match_sequence", "match_moves", ["match_id", "sequence"])

    op.execute("""
    CREATE FUNCTION aik_match_immutable_record() RETURNS trigger AS $$
    BEGIN
      RAISE EXCEPTION 'match side, execution binding and move records are append-only'
        USING ERRCODE = 'check_violation';
    END;
    $$ LANGUAGE plpgsql;
    """)
    for table in ("match_sides", "match_execution_bindings", "match_moves"):
        op.execute(
            f"CREATE TRIGGER {table}_append_only BEFORE UPDATE OR DELETE ON {table} "
            "FOR EACH ROW EXECUTE FUNCTION aik_match_immutable_record()"
        )

    op.execute("""
    CREATE FUNCTION aik_match_side_insert_guard() RETURNS trigger AS $$
    BEGIN
      IF NOT EXISTS (
        SELECT 1 FROM matches m JOIN submission_versions v ON v.id = NEW.submission_version_id
        WHERE m.id = NEW.match_id AND m.state = 'QUEUED'
          AND v.state = 'ELIGIBLE' AND v.participant_id = NEW.participant_id
          AND v.submission_id = NEW.submission_id AND v.artifact_sha256 = NEW.artifact_sha256
          AND v.protocol_version = NEW.protocol_version AND v.runtime_profile = NEW.runtime_profile
          AND m.protocol_version = NEW.protocol_version AND m.runtime_profile = NEW.runtime_profile
      ) THEN
        RAISE EXCEPTION 'match side must pin an eligible exact submission version'
          USING ERRCODE = 'check_violation';
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """)
    op.execute(
        "CREATE TRIGGER match_sides_insert_guard BEFORE INSERT ON match_sides FOR EACH ROW EXECUTE FUNCTION aik_match_side_insert_guard()"
    )

    op.execute("""
    CREATE FUNCTION aik_match_execution_insert_guard() RETURNS trigger AS $$
    DECLARE expected_attempt integer;
    BEGIN
      SELECT COALESCE(MAX(attempt_number), 0) + 1 INTO expected_attempt
        FROM match_execution_bindings WHERE match_side_id = NEW.match_side_id;
      IF NOT EXISTS (
        SELECT 1 FROM matches m JOIN match_sides s ON s.id = NEW.match_side_id
        WHERE m.id = NEW.match_id AND m.state = 'STARTING' AND s.match_id = m.id
          AND s.submission_version_id = NEW.submission_version_id
          AND s.artifact_sha256 = NEW.artifact_sha256
          AND s.participant_id = (
            SELECT participant_id FROM worker_execution_jobs
            WHERE execution_id = NEW.execution_id
          )
          AND s.submission_id = (
            SELECT submission_id FROM worker_execution_jobs
            WHERE execution_id = NEW.execution_id
          )
      ) OR NEW.attempt_number <> expected_attempt OR NOT EXISTS (
        SELECT 1 FROM worker_execution_jobs j
        JOIN workers w ON w.worker_id = j.assigned_worker_id
        WHERE j.execution_id = NEW.execution_id AND j.match_id = NEW.match_id
          AND j.submission_version_id = NEW.submission_version_id
          AND j.artifact_sha256 = NEW.artifact_sha256
          AND j.assigned_worker_id = NEW.worker_id AND j.lease_id = NEW.lease_id
          AND j.fencing_token = NEW.fencing_token AND j.status = 'ACKNOWLEDGED'
          AND j.protocol_version = (SELECT protocol_version FROM match_sides
                                    WHERE id = NEW.match_side_id)
          AND j.runtime_profile = (SELECT runtime_profile FROM match_sides
                                   WHERE id = NEW.match_side_id)
          AND j.lease_expires_at > now() AND j.execution_expires_at > now()
          AND w.status = 'ACTIVE' AND w.revoked_at IS NULL
      ) THEN
        RAISE EXCEPTION 'execution binding must match side identity and next attempt'
          USING ERRCODE = 'check_violation';
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """)
    op.execute(
        "CREATE TRIGGER match_execution_insert_guard BEFORE INSERT ON match_execution_bindings FOR EACH ROW EXECUTE FUNCTION aik_match_execution_insert_guard()"
    )

    op.execute("""
    CREATE FUNCTION aik_match_move_insert_guard() RETURNS trigger AS $$
    DECLARE expected_ply integer;
    BEGIN
      SELECT current_ply + 1 INTO expected_ply FROM matches
        WHERE id = NEW.match_id AND state = 'RUNNING' FOR UPDATE;
      IF NOT FOUND OR NEW.sequence <> expected_ply OR NOT EXISTS (
        SELECT 1 FROM matches m JOIN match_sides s ON s.id = NEW.match_side_id
        JOIN match_execution_bindings b ON b.id = NEW.execution_binding_id
        WHERE m.id = NEW.match_id AND s.match_id = m.id AND s.color = m.side_to_move
          AND b.match_id = m.id AND b.match_side_id = s.id
          AND b.submission_version_id = s.submission_version_id
          AND b.artifact_sha256 = s.artifact_sha256
          AND b.attempt_number = (SELECT MAX(b2.attempt_number) FROM match_execution_bindings b2
                                  WHERE b2.match_side_id = s.id)
          AND EXISTS (
            SELECT 1 FROM worker_execution_jobs j
            JOIN workers w ON w.worker_id = j.assigned_worker_id
            WHERE j.execution_id = b.execution_id AND j.match_id = b.match_id
              AND j.participant_id = s.participant_id AND j.submission_id = s.submission_id
              AND j.submission_version_id = s.submission_version_id
              AND j.artifact_sha256 = s.artifact_sha256
              AND j.assigned_worker_id = b.worker_id AND j.lease_id = b.lease_id
              AND j.fencing_token = b.fencing_token AND j.status = 'ACKNOWLEDGED'
              AND j.lease_expires_at > now() AND j.execution_expires_at > now()
              AND w.status = 'ACTIVE' AND w.revoked_at IS NULL
          )
      ) THEN
        RAISE EXCEPTION 'move sequence, side or execution binding is stale'
          USING ERRCODE = 'check_violation';
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """)
    op.execute(
        "CREATE TRIGGER match_moves_insert_guard BEFORE INSERT ON match_moves FOR EACH ROW EXECUTE FUNCTION aik_match_move_insert_guard()"
    )

    op.execute("""
    CREATE FUNCTION aik_match_update_guard() RETURNS trigger AS $$
    BEGIN
      IF ROW(NEW.id, NEW.protocol_version, NEW.runtime_profile, NEW.policy_version,
             NEW.rules_version, NEW.match_seed, NEW.initial_fen, NEW.initial_clock_ms, NEW.increment_ms,
             NEW.move_timeout_ms, NEW.created_at) IS DISTINCT FROM
         ROW(OLD.id, OLD.protocol_version, OLD.runtime_profile, OLD.policy_version,
             OLD.rules_version, OLD.initial_fen, OLD.initial_clock_ms, OLD.increment_ms,
             OLD.move_timeout_ms, OLD.created_at) THEN
        RAISE EXCEPTION 'match identity and configuration are immutable' USING ERRCODE = 'check_violation';
      END IF;
      IF (OLD.started_at IS NOT NULL AND NEW.started_at IS DISTINCT FROM OLD.started_at) OR
         (OLD.completed_at IS NOT NULL AND NEW.completed_at IS DISTINCT FROM OLD.completed_at) THEN
        RAISE EXCEPTION 'match lifecycle timestamps are immutable once assigned'
          USING ERRCODE = 'check_violation';
      END IF;
      IF OLD.state IN ('COMPLETED','ABORTED') THEN
        RAISE EXCEPTION 'terminal match is immutable' USING ERRCODE = 'check_violation';
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
        (SELECT count(*) FROM match_sides WHERE match_id = NEW.id) <> 2
      ) THEN
        RAISE EXCEPTION 'initialization requires exactly two immutable sides'
          USING ERRCODE = 'check_violation';
      END IF;
      IF NEW.state = 'RUNNING' AND OLD.state <> 'RUNNING' AND (
        NEW.started_at IS NULL OR NEW.completed_at IS NOT NULL OR
        (SELECT count(*) FROM match_sides WHERE match_id = NEW.id) <> 2 OR
        (SELECT count(DISTINCT b.match_side_id)
         FROM match_execution_bindings b
         WHERE b.match_id = NEW.id
           AND b.attempt_number = (SELECT max(b2.attempt_number)
                                   FROM match_execution_bindings b2
                                   WHERE b2.match_side_id = b.match_side_id)
           AND EXISTS (
             SELECT 1 FROM competition_events e
             WHERE e.aggregate_type = 'match' AND e.aggregate_id = NEW.id
               AND e.event_type = 'EXECUTION_READY'
               AND e.metadata ->> 'binding_id' = b.id::text
           )) <> 2
      ) THEN
        RAISE EXCEPTION 'running requires both current executions ready'
          USING ERRCODE = 'check_violation';
      END IF;
      IF NEW.current_ply <> OLD.current_ply THEN
        IF NEW.current_ply <> OLD.current_ply + 1 OR NOT EXISTS (
          SELECT 1 FROM match_moves mv JOIN match_sides s ON s.id = mv.match_side_id
          WHERE mv.match_id = NEW.id AND mv.sequence = NEW.current_ply
            AND mv.position_after_fen = NEW.current_fen AND s.color = OLD.side_to_move
            AND NEW.side_to_move <> OLD.side_to_move
        ) THEN
          RAISE EXCEPTION 'match position must advance with one authoritative move'
            USING ERRCODE = 'check_violation';
        END IF;
      ELSIF ROW(NEW.current_fen, NEW.side_to_move, NEW.white_remaining_ms, NEW.black_remaining_ms)
            IS DISTINCT FROM
            ROW(OLD.current_fen, OLD.side_to_move, OLD.white_remaining_ms, OLD.black_remaining_ms) THEN
        RAISE EXCEPTION 'position and clocks require an accepted move'
          USING ERRCODE = 'check_violation';
      END IF;
      IF NEW.state = 'COMPLETED' AND (
        NEW.result_outcome IS NULL OR NEW.result_reason IS NULL OR NEW.failure_reason IS NOT NULL
      ) THEN
        RAISE EXCEPTION 'completed match requires server-derived result' USING ERRCODE = 'check_violation';
      END IF;
      IF NEW.state = 'ABORTED' AND (
        NEW.failure_reason IS NULL OR NEW.result_outcome IS NOT NULL OR NEW.result_reason IS NOT NULL
      ) THEN
        RAISE EXCEPTION 'aborted match cannot declare chess result' USING ERRCODE = 'check_violation';
      END IF;
      IF NEW.state IN ('COMPLETED','ABORTED') AND NEW.completed_at IS NULL THEN
        RAISE EXCEPTION 'terminal match requires completion timestamp'
          USING ERRCODE = 'check_violation';
      END IF;
      RETURN NEW;
    END;
    $$ LANGUAGE plpgsql;
    """)
    op.execute(
        "CREATE TRIGGER matches_update_guard BEFORE UPDATE ON matches FOR EACH ROW EXECUTE FUNCTION aik_match_update_guard()"
    )
    op.execute(
        "CREATE TRIGGER matches_delete_guard BEFORE DELETE ON matches FOR EACH ROW EXECUTE FUNCTION aik_match_immutable_record()"
    )


def downgrade() -> None:
    for table in ("matches", "match_sides", "match_execution_bindings", "match_moves"):
        op.execute(f"DROP TRIGGER IF EXISTS {table}_append_only ON {table}")
    op.execute("DROP TRIGGER IF EXISTS match_sides_insert_guard ON match_sides")
    op.execute("DROP TRIGGER IF EXISTS match_execution_insert_guard ON match_execution_bindings")
    op.execute("DROP TRIGGER IF EXISTS match_moves_insert_guard ON match_moves")
    op.execute("DROP TRIGGER IF EXISTS matches_update_guard ON matches")
    op.execute("DROP TRIGGER IF EXISTS matches_delete_guard ON matches")
    for function in (
        "aik_match_move_insert_guard",
        "aik_match_execution_insert_guard",
        "aik_match_side_insert_guard",
        "aik_match_update_guard",
        "aik_match_immutable_record",
    ):
        op.execute(f"DROP FUNCTION IF EXISTS {function}()")
    op.drop_index("ix_match_moves_match_sequence", table_name="match_moves")
    op.drop_table("match_moves")
    op.drop_index("ix_match_execution_bindings_match_id", table_name="match_execution_bindings")
    op.drop_table("match_execution_bindings")
    op.drop_index("ix_match_sides_match_id", table_name="match_sides")
    op.drop_table("match_sides")
    op.drop_table("matches")
