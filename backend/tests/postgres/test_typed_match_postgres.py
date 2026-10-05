"""PostgreSQL-only migration 0008 invariants.

Run through scripts/verify_typed_match_postgres.ps1. The URL is explicitly
provided for a disposable, loopback-only PostgreSQL server; this module never
loads project settings or reads a .env file.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone

import psycopg2
import pytest
from psycopg2.extensions import parse_dsn
from psycopg2.extras import Json, register_uuid

register_uuid()


def _dsn() -> str:
    value = os.environ.get("AIK_TEST_POSTGRES_URL")
    if not value:
        pytest.skip("AIK_TEST_POSTGRES_URL must point to the disposable verification database")
    parsed = parse_dsn(value)
    if parsed.get("host") not in {"127.0.0.1", "localhost", "::1"}:
        pytest.fail("PostgreSQL verification is restricted to a loopback test server")
    if not parsed.get("dbname", "").startswith("aik_verify"):
        pytest.fail("PostgreSQL verification requires an aik_verify* disposable database")
    return value


def _token() -> str:
    return uuid.uuid4().hex[:12]


def _participant(cur, participant_id: str | None = None) -> str:
    participant_id = participant_id or f"pg-{_token()}"
    now = datetime.now(timezone.utc)
    cur.execute(
        "INSERT INTO participants (participant_id,name,access_key_hash) VALUES (%s,%s,%s)",
        (participant_id, f"PostgreSQL fixture {participant_id}", "f" * 64),
    )
    cur.execute(
        "INSERT INTO build_sessions (participant_id,started_at,start_source) VALUES (%s,%s,'REGISTRATION')",
        (participant_id, now),
    )
    return participant_id


def _eligible_version(cur, participant_id: str | None = None) -> dict:
    participant_id = participant_id or _participant(cur)
    version_id, submission_id = uuid.uuid4(), uuid.uuid4()
    payload = f"inert PostgreSQL test fixture {version_id}".encode()
    digest = hashlib.sha256(payload).hexdigest()
    now = datetime.now(timezone.utc)
    cur.execute(
        "INSERT INTO submissions (id,participant_id,created_at) VALUES (%s,%s,%s)",
        (submission_id, participant_id, now),
    )
    cur.execute(
        "INSERT INTO quarantine_objects (artifact_sha256,artifact_size,archive_bytes) VALUES (%s,%s,%s)",
        (digest, len(payload), psycopg2.Binary(payload)),
    )
    cur.execute(
        """INSERT INTO submission_versions
           (id,submission_id,participant_id,version,artifact_sha256,artifact_size,storage_key,
            manifest,contract_version,protocol_version,runtime_profile,state,created_at,
            build_duration_seconds)
           VALUES (%s,%s,%s,1,%s,%s,%s,'{}','core-v1','aik-jsonl/1','runtime-v1',
                   'QUARANTINED',%s,0)""",
        (version_id, submission_id, participant_id, digest, len(payload), f"sha256:{digest}", now),
    )
    states = [(None, "QUARANTINED"), ("QUARANTINED", "INSPECTING"),
              ("INSPECTING", "VALIDATING"), ("VALIDATING", "APPROVED"),
              ("APPROVED", "ELIGIBLE")]
    for from_state, to_state in states:
        cur.execute(
            """INSERT INTO artifact_state_transitions
               (id,submission_version_id,from_state,to_state,actor_kind,actor_id,reason_code)
               VALUES (%s,%s,%s,%s,'test-fixture','pg-test','PG_VERIFICATION')""",
            (uuid.uuid4(), version_id, from_state, to_state),
        )
        if from_state is not None:
            cur.execute(
                "UPDATE submission_versions SET state=%s WHERE id=%s", (to_state, version_id)
            )
    return {
        "participant_id": participant_id,
        "submission_id": submission_id,
        "version_id": version_id,
        "digest": digest,
        "storage_ref": f"sha256:{digest}",
    }


def _house_version(cur, *, suffix: str | None = None, **overrides) -> dict:
    suffix = suffix or _token()
    house_id, digest = f"house-{suffix}", hashlib.sha256(suffix.encode()).hexdigest()
    now = datetime.now(timezone.utc)
    values = {
        "protocol": "aik-jsonl/1", "runtime": "runtime-v1", "rules": "rules-v1",
        "policy": "policy-v1", "runtime_digest": "b" * 64, "policy_digest": "c" * 64,
    }
    values.update(overrides)
    cur.execute("INSERT INTO house_ais (house_ai_id,created_at) VALUES (%s,%s)", (house_id, now))
    cur.execute(
        """INSERT INTO house_ai_artifacts
           (artifact_sha256,artifact_size,storage_ref,created_at) VALUES (%s,12,%s,%s)""",
        (digest, f"house-artifact-{suffix}", now),
    )
    cur.execute(
        """INSERT INTO house_ai_versions
           (house_ai_id,version,artifact_sha256,artifact_size,protocol_version,runtime_profile,
            runtime_identity_sha256,rules_version,policy_version,policy_digest,created_at,frozen_at)
           VALUES (%s,1,%s,12,%s,%s,%s,%s,%s,%s,%s,%s)""",
        (house_id, digest, values["protocol"], values["runtime"], values["runtime_digest"],
         values["rules"], values["policy"], values["policy_digest"], now, now),
    )
    return {"house_ai_id": house_id, "house_ai_version": 1, "digest": digest,
            "storage_ref": f"house-artifact-{suffix}"}


def _match(cur, purpose: str = "PEER_TEST", participant_id: str | None = None) -> uuid.UUID:
    match_id, now = uuid.uuid4(), datetime.now(timezone.utc)
    idem = (participant_id, f"pg-{_token()}", "d" * 64) if purpose == "DEVELOPMENT" else (None, None, None)
    cur.execute(
        """INSERT INTO matches
           (id,state,protocol_version,runtime_profile,policy_version,rules_version,match_seed,
            initial_fen,current_fen,side_to_move,current_ply,initial_clock_ms,increment_ms,
            move_timeout_ms,white_remaining_ms,black_remaining_ms,created_at,purpose,
            idempotency_participant_id,idempotency_key,request_hash)
           VALUES (%s,'QUEUED','aik-jsonl/1','runtime-v1','policy-v1','rules-v1',%s,
                   'initial-fen','initial-fen','WHITE',0,10000,0,1000,10000,10000,%s,%s,%s,%s,%s)""",
        (match_id, "e" * 64, now, purpose, *idem),
    )
    return match_id


def _side(cur, match_id: uuid.UUID, color: str, kind: str, subject: dict | None) -> uuid.UUID:
    side_id, now = uuid.uuid4(), datetime.now(timezone.utc)
    cur.execute(
        """INSERT INTO match_sides
           (id,match_id,color,kind,protocol_version,runtime_profile,created_at)
           VALUES (%s,%s,%s,%s,'aik-jsonl/1','runtime-v1',%s)""",
        (side_id, match_id, color, kind, now),
    )
    if subject is not None and kind == "PARTICIPANT":
        cur.execute(
            """INSERT INTO participant_match_side_bindings
               (match_side_id,participant_id,submission_id,submission_version_id,artifact_sha256)
               VALUES (%s,%s,%s,%s,%s)""",
            (side_id, subject["participant_id"], subject["submission_id"],
             subject["version_id"], subject["digest"]),
        )
    elif subject is not None:
        cur.execute(
            """INSERT INTO house_ai_match_side_bindings
               (match_side_id,house_ai_id,house_ai_version,artifact_sha256)
               VALUES (%s,%s,%s,%s)""",
            (side_id, subject["house_ai_id"], subject["house_ai_version"], subject["digest"]),
        )
    return side_id


def _start(cur, match_id: uuid.UUID) -> None:
    cur.execute("UPDATE matches SET state='STARTING' WHERE id=%s", (match_id,))


def _worker(cur) -> str:
    worker_id = f"pg-worker-{_token()}"
    cur.execute(
        """INSERT INTO workers
           (worker_id,status,security_state,pending_capabilities,capabilities)
           VALUES (%s,'ACTIVE','HEALTHY','{}','{}')""",
        (worker_id,),
    )
    return worker_id


def _job(cur, match_id: uuid.UUID, subject: dict, worker_id: str | None = None) -> dict:
    kind = "PARTICIPANT" if "participant_id" in subject else "HOUSE_AI"
    execution_id, lease_id = uuid.uuid4(), uuid.uuid4()
    worker_id = worker_id or _worker(cur)
    now = datetime.now(timezone.utc)
    participant = subject if kind == "PARTICIPANT" else {}
    house = subject if kind == "HOUSE_AI" else {}
    ref = subject["storage_ref"] if kind == "HOUSE_AI" else subject["storage_ref"]
    cur.execute(
        """INSERT INTO worker_execution_jobs
           (id,execution_id,match_id,execution_kind,participant_id,submission_id,submission_version,
            submission_version_id,house_ai_id,house_ai_version,artifact_sha256,immutable_artifact_ref,
            runtime_profile,protocol_version,execution_mode,initial_state,resource_policy,
            execution_expires_at,status,assigned_worker_id,lease_id,fencing_token,lease_expires_at,
            last_sequence)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'runtime-v1','aik-jsonl/1',
                   'MOCK_PROTOCOL_ONLY',%s,%s,%s,'ACKNOWLEDGED',%s,%s,1,%s,0)""",
        (uuid.uuid4(), execution_id, match_id, kind, participant.get("participant_id"),
         participant.get("submission_id"), 1 if kind == "PARTICIPANT" else None,
         participant.get("version_id"), house.get("house_ai_id"),
         house.get("house_ai_version"), subject["digest"], ref, Json({}), Json({}),
         now + timedelta(minutes=2), worker_id, lease_id, now + timedelta(minutes=1)),
    )
    return {"execution_id": execution_id, "worker_id": worker_id, "lease_id": lease_id,
            "fence": 1, "kind": kind, "subject": subject}


def _binding(cur, match_id: uuid.UUID, side_id: uuid.UUID, job: dict,
             *, attempt: int = 1, overrides: dict | None = None) -> uuid.UUID:
    values = {
        "match_id": match_id, "match_side_id": side_id, "attempt_number": attempt,
        "execution_id": job["execution_id"], "execution_kind": job["kind"],
        "submission_version_id": job["subject"].get("version_id"),
        "house_ai_id": job["subject"].get("house_ai_id"),
        "house_ai_version": job["subject"].get("house_ai_version"),
        "artifact_sha256": job["subject"]["digest"], "worker_id": job["worker_id"],
        "lease_id": job["lease_id"], "fencing_token": job["fence"],
    }
    values.update(overrides or {})
    binding_id = uuid.uuid4()
    cur.execute(
        """INSERT INTO match_execution_bindings
           (id,match_id,match_side_id,attempt_number,execution_id,execution_kind,
            submission_version_id,house_ai_id,house_ai_version,artifact_sha256,worker_id,
            lease_id,fencing_token,created_at)
           VALUES (%(id)s,%(match_id)s,%(match_side_id)s,%(attempt_number)s,%(execution_id)s,
                   %(execution_kind)s,%(submission_version_id)s,%(house_ai_id)s,
                   %(house_ai_version)s,%(artifact_sha256)s,%(worker_id)s,%(lease_id)s,
                   %(fencing_token)s,now())""",
        {"id": binding_id, **values},
    )
    return binding_id


def _event_ready(cur, match_id: uuid.UUID, binding_id: uuid.UUID, sequence: int) -> None:
    cur.execute(
        """INSERT INTO competition_events
           (event_id,event_type,actor_kind,actor_id,aggregate_type,aggregate_id,
            aggregate_sequence,schema_version,metadata,recorded_at)
           VALUES (%s,'EXECUTION_READY','test-fixture','pg-test','match',%s,%s,1,%s,now())""",
        (uuid.uuid4(), match_id, sequence, Json({"binding_id": str(binding_id)})),
    )


def _running_match(cur, *, stale_first: bool = False) -> tuple[uuid.UUID, list, list]:
    white, black = _eligible_version(cur), _eligible_version(cur)
    match_id = _match(cur)
    sides = [
        _side(cur, match_id, "WHITE", "PARTICIPANT", white),
        _side(cur, match_id, "BLACK", "PARTICIPANT", black),
    ]
    _start(cur, match_id)
    bindings = []
    jobs = []
    if stale_first:
        first = _job(cur, match_id, white)
        first_binding = _binding(cur, match_id, sides[0], first, attempt=1)
        latest = _job(cur, match_id, white)
        latest_binding = _binding(cur, match_id, sides[0], latest, attempt=2)
        jobs.extend([first, latest])
        bindings.extend([first_binding, latest_binding])
        _event_ready(cur, match_id, latest_binding, 1)
    else:
        white_job = _job(cur, match_id, white)
        white_binding = _binding(cur, match_id, sides[0], white_job)
        jobs.append(white_job)
        bindings.append(white_binding)
        _event_ready(cur, match_id, white_binding, 1)
    black_job = _job(cur, match_id, black)
    black_binding = _binding(cur, match_id, sides[1], black_job)
    jobs.append(black_job)
    bindings.append(black_binding)
    _event_ready(cur, match_id, black_binding, 2)
    cur.execute("UPDATE matches SET state='RUNNING',started_at=now() WHERE id=%s", (match_id,))
    return match_id, sides, bindings


def _starting_with_bindings(cur) -> tuple[uuid.UUID, list, list, list]:
    white, black = _eligible_version(cur), _eligible_version(cur)
    match_id = _match(cur)
    sides = [
        _side(cur, match_id, "WHITE", "PARTICIPANT", white),
        _side(cur, match_id, "BLACK", "PARTICIPANT", black),
    ]
    _start(cur, match_id)
    jobs = [_job(cur, match_id, white), _job(cur, match_id, black)]
    bindings = [
        _binding(cur, match_id, sides[0], jobs[0]),
        _binding(cur, match_id, sides[1], jobs[1]),
    ]
    _event_ready(cur, match_id, bindings[0], 1)
    _event_ready(cur, match_id, bindings[1], 2)
    return match_id, sides, jobs, bindings


def test_typed_match_side_postgres_matrix():
    """Exercise real PL/pgSQL, deferred commit, typed identity, lifecycle and catalog."""
    dsn = _dsn()
    report: dict[str, str] = {}

    # PG-03/04: valid typed sides must survive an actual transaction commit.
    with psycopg2.connect(dsn) as conn:
        cur = conn.cursor()
        part = _eligible_version(cur)
        mid = _match(cur)
        _side(cur, mid, "WHITE", "PARTICIPANT", part)
        house = _house_version(cur)
        hm = _match(cur, "DEVELOPMENT", part["participant_id"])
        _side(cur, hm, "WHITE", "HOUSE_AI", house)
        # Add the participant side required by development composition.
        _side(cur, hm, "BLACK", "PARTICIPANT", part)
    report["PG-03"] = "PASS"
    report["PG-04"] = "PASS"

    # PG-05: deferred missing-child validation must fail at COMMIT.
    conn = psycopg2.connect(dsn)
    cur = conn.cursor()
    mid = _match(cur)
    _side(cur, mid, "WHITE", "PARTICIPANT", None)
    with pytest.raises(psycopg2.Error):
        conn.commit()
    conn.rollback()
    conn.close()
    report["PG-05"] = "PASS"

    # PG-06/07: wrong-kind and dual children are rejected; duplicate child is PK-rejected.
    with psycopg2.connect(dsn) as conn:
        cur = conn.cursor()
        part = _eligible_version(cur)
        house = _house_version(cur)
        mid = _match(cur)
        side = _side(cur, mid, "WHITE", "PARTICIPANT", part)
        with pytest.raises(psycopg2.Error):
            cur.execute(
                "INSERT INTO house_ai_match_side_bindings VALUES (%s,%s,%s,%s)",
                (side, house["house_ai_id"], house["house_ai_version"], house["digest"]),
            )
        conn.rollback()
    with psycopg2.connect(dsn) as conn:
        cur = conn.cursor()
        house = _house_version(cur)
        mid = _match(cur, "DEVELOPMENT", _participant(cur))
        side = _side(cur, mid, "WHITE", "HOUSE_AI", house)
        part = _eligible_version(cur)
        with pytest.raises(psycopg2.Error):
            cur.execute(
                "INSERT INTO participant_match_side_bindings VALUES (%s,%s,%s,%s,%s)",
                (side, part["participant_id"], part["submission_id"],
                 part["version_id"], part["digest"]),
            )
        conn.rollback()
    with psycopg2.connect(dsn) as conn:
        cur = conn.cursor()
        part = _eligible_version(cur)
        mid = _match(cur)
        side = _side(cur, mid, "WHITE", "PARTICIPANT", part)
        with pytest.raises(psycopg2.Error):
            cur.execute(
                "INSERT INTO participant_match_side_bindings VALUES (%s,%s,%s,%s,%s)",
                (side, part["participant_id"], part["submission_id"],
                 part["version_id"], part["digest"]),
            )
        conn.rollback()
    report["PG-06"] = "PASS"
    report["PG-07"] = "PASS"

    # PG-08: append-only UPDATE/DELETE semantics are immediate and deterministic.
    with psycopg2.connect(dsn) as conn:
        cur = conn.cursor()
        part = _eligible_version(cur)
        mid = _match(cur)
        side = _side(cur, mid, "WHITE", "PARTICIPANT", part)
        with pytest.raises(psycopg2.Error):
            cur.execute("UPDATE match_sides SET kind='HOUSE_AI' WHERE id=%s", (side,))
        conn.rollback()
    with psycopg2.connect(dsn) as conn:
        cur = conn.cursor()
        part = _eligible_version(cur)
        mid = _match(cur)
        side = _side(cur, mid, "WHITE", "PARTICIPANT", part)
        with pytest.raises(psycopg2.Error):
            cur.execute("DELETE FROM participant_match_side_bindings WHERE match_side_id=%s", (side,))
        conn.rollback()
    with psycopg2.connect(dsn) as conn:
        cur = conn.cursor()
        house = _house_version(cur)
        mid = _match(cur, "DEVELOPMENT", _participant(cur))
        side = _side(cur, mid, "WHITE", "HOUSE_AI", house)
        with pytest.raises(psycopg2.Error):
            cur.execute("UPDATE house_ai_match_side_bindings SET artifact_sha256=%s WHERE match_side_id=%s",
                        ("d" * 64, side))
        conn.rollback()
    with psycopg2.connect(dsn) as conn:
        cur = conn.cursor()
        house = _house_version(cur)
        mid = _match(cur, "DEVELOPMENT", _participant(cur))
        side = _side(cur, mid, "WHITE", "HOUSE_AI", house)
        with pytest.raises(psycopg2.Error):
            cur.execute("DELETE FROM match_sides WHERE id=%s", (side,))
        conn.rollback()
    report["PG-08"] = "PASS"

    # PG-09: typed participant exact identity rejects a tuple from another actor/version.
    with psycopg2.connect(dsn) as conn:
        cur = conn.cursor()
        one, two = _eligible_version(cur), _eligible_version(cur)
        mid = _match(cur)
        side = _side(cur, mid, "WHITE", "PARTICIPANT", None)
        with pytest.raises(psycopg2.Error):
            cur.execute(
                "INSERT INTO participant_match_side_bindings VALUES (%s,%s,%s,%s,%s)",
                (side, one["participant_id"], two["submission_id"], one["version_id"], one["digest"]),
            )
        conn.rollback()
    report["PG-09"] = "PASS"

    # PG-10: House foreign-key and compatibility checks reject invalid versions/metadata.
    with psycopg2.connect(dsn) as conn:
        cur = conn.cursor()
        house = _house_version(cur)
        mid = _match(cur)
        side = _side(cur, mid, "WHITE", "HOUSE_AI", None)
        with pytest.raises(psycopg2.Error):
            cur.execute(
                "INSERT INTO house_ai_match_side_bindings VALUES (%s,%s,%s,%s)",
                (side, house["house_ai_id"], 99, house["digest"]),
            )
        conn.rollback()
    for field, value in (("protocol", "aik-jsonl/other"), ("runtime", "runtime-other"),
                         ("rules", "rules-other"), ("policy", "policy-other")):
        with psycopg2.connect(dsn) as conn:
            cur = conn.cursor()
            house = _house_version(cur, **{field: value})
            mid = _match(cur, "DEVELOPMENT", _participant(cur))
            side = _side(cur, mid, "WHITE", "HOUSE_AI", None)
            with pytest.raises(psycopg2.Error):
                cur.execute(
                    "INSERT INTO house_ai_match_side_bindings VALUES (%s,%s,%s,%s)",
                    (side, house["house_ai_id"], 1, house["digest"]),
                )
            conn.rollback()
    with psycopg2.connect(dsn) as conn:
        cur = conn.cursor()
        house = _house_version(cur)
        mid = _match(cur, "DEVELOPMENT", _participant(cur))
        side = _side(cur, mid, "WHITE", "HOUSE_AI", None)
        with pytest.raises(psycopg2.Error):
            cur.execute(
                "INSERT INTO house_ai_match_side_bindings VALUES (%s,%s,%s,%s)",
                (side, house["house_ai_id"], 1, "d" * 64),
            )
        conn.rollback()
    report["PG-10"] = "PASS"

    # PG-11: valid typed DEVELOPMENT composition can enter STARTING without execution.
    with psycopg2.connect(dsn) as conn:
        cur = conn.cursor()
        part = _eligible_version(cur)
        house = _house_version(cur)
        mid = _match(cur, "DEVELOPMENT", part["participant_id"])
        _side(cur, mid, "WHITE", "PARTICIPANT", part)
        _side(cur, mid, "BLACK", "HOUSE_AI", house)
        _start(cur, mid)
    report["PG-11"] = "PASS"

    # PG-12/13: peer tests require distinct participant actors at DB boundary.
    with psycopg2.connect(dsn) as conn:
        cur = conn.cursor()
        one, two = _eligible_version(cur), _eligible_version(cur)
        mid = _match(cur)
        _side(cur, mid, "WHITE", "PARTICIPANT", one)
        _side(cur, mid, "BLACK", "PARTICIPANT", two)
        _start(cur, mid)
    report["PG-12"] = "PASS"
    with psycopg2.connect(dsn) as conn:
        cur = conn.cursor()
        one = _eligible_version(cur)
        mid = _match(cur)
        _side(cur, mid, "WHITE", "PARTICIPANT", one)
        other = _eligible_version(cur, one["participant_id"])
        duplicate_side = uuid.uuid4()
        cur.execute(
            """INSERT INTO match_sides
               (id,match_id,color,kind,protocol_version,runtime_profile,created_at)
               VALUES (%s,%s,'BLACK','PARTICIPANT','aik-jsonl/1','runtime-v1',now())""",
            (duplicate_side, mid),
        )
        with pytest.raises(psycopg2.Error):
            cur.execute(
                "INSERT INTO participant_match_side_bindings VALUES (%s,%s,%s,%s,%s)",
                (duplicate_side, other["participant_id"], other["submission_id"],
                 other["version_id"], other["digest"]),
            )
        conn.rollback()
    report["PG-13"] = "PASS"

    # PG-14: official matches require the one-way freeze and exact frozen selections.
    with psycopg2.connect(dsn) as conn:
        cur = conn.cursor()
        p1, p2 = _eligible_version(cur), _eligible_version(cur)
        selected1 = _eligible_version(cur, p1["participant_id"])
        selected2 = _eligible_version(cur, p2["participant_id"])
        now = datetime.now(timezone.utc)
        for item in (selected1, selected2):
            cur.execute(
                "INSERT INTO official_submission_selections (id,participant_id,submission_version_id,selected_at) VALUES (%s,%s,%s,%s)",
                (uuid.uuid4(), item["participant_id"], item["version_id"], now),
            )
        cur.execute(
            "UPDATE event_control SET official_freeze_at=%s,official_freeze_actor_id='pg-test',official_freeze_reason='PG_VERIFICATION' WHERE id=1",
            (now,),
        )
    with psycopg2.connect(dsn) as conn:
        cur = conn.cursor()
        mid = _match(cur, "OFFICIAL")
        _side(cur, mid, "WHITE", "PARTICIPANT", p1)
        _side(cur, mid, "BLACK", "PARTICIPANT", p2)
        with pytest.raises(psycopg2.Error):
            _start(cur, mid)
        conn.rollback()
    with psycopg2.connect(dsn) as conn:
        cur = conn.cursor()
        mid = _match(cur, "OFFICIAL")
        _side(cur, mid, "WHITE", "PARTICIPANT", selected1)
        _side(cur, mid, "BLACK", "PARTICIPANT", selected2)
        _start(cur, mid)
    report["PG-14"] = "PASS"

    # PG-15: exact participant and House job subjects are accepted without running jobs.
    with psycopg2.connect(dsn) as conn:
        cur = conn.cursor()
        participant = _eligible_version(cur)
        house = _house_version(cur)
        _job(cur, None, participant)
        _job(cur, None, house)
    report["PG-15"] = "PASS"

    # PG-16: partial and unknown execution-kind identities fail database CHECKs.
    with psycopg2.connect(dsn) as conn:
        cur = conn.cursor()
        with pytest.raises(psycopg2.Error):
            cur.execute(
                """INSERT INTO worker_execution_jobs
                   (id,execution_id,execution_kind,artifact_sha256,immutable_artifact_ref,
                    runtime_profile,protocol_version,execution_mode,initial_state,resource_policy,
                    execution_expires_at,status,fencing_token,last_sequence)
                   VALUES (%s,%s,'UNKNOWN',%s,'ref','runtime-v1','aik-jsonl/1',
                           'MOCK_PROTOCOL_ONLY','{}','{}',now(),'QUEUED',0,0)""",
                (uuid.uuid4(), uuid.uuid4(), "a" * 64),
            )
        conn.rollback()
    report["PG-16"] = "PASS"

    # PG-17: each stale/mismatched execution-binding dimension is rejected.
    for invalid in ("match", "side", "artifact", "worker", "lease", "fence",
                    "status", "expiry", "attempt"):
        with psycopg2.connect(dsn) as conn:
            cur = conn.cursor()
            participant, opponent = _eligible_version(cur), _eligible_version(cur)
            match_id = _match(cur)
            side_id = _side(cur, match_id, "WHITE", "PARTICIPANT", participant)
            other_side_id = _side(cur, match_id, "BLACK", "PARTICIPANT", opponent)
            _start(cur, match_id)
            job = _job(cur, match_id, participant)
            overrides = {}
            if invalid == "match": overrides["match_id"] = uuid.uuid4()
            elif invalid == "side": side_id = other_side_id
            elif invalid == "artifact": overrides["artifact_sha256"] = "d" * 64
            elif invalid == "worker": overrides["worker_id"] = _worker(cur)
            elif invalid == "lease": overrides["lease_id"] = uuid.uuid4()
            elif invalid == "fence": overrides["fencing_token"] = 2
            elif invalid == "status":
                cur.execute("UPDATE worker_execution_jobs SET status='QUEUED' WHERE execution_id=%s",
                            (job["execution_id"],))
            elif invalid == "expiry":
                cur.execute("UPDATE worker_execution_jobs SET lease_expires_at=now()-interval '1 second' WHERE execution_id=%s",
                            (job["execution_id"],))
            elif invalid == "attempt":
                with pytest.raises(psycopg2.Error):
                    _binding(cur, match_id, side_id, job, attempt=2)
                conn.rollback()
                continue
            with pytest.raises(psycopg2.Error):
                _binding(cur, match_id, side_id, job, overrides=overrides)
            conn.rollback()
    report["PG-17"] = "PASS"

    # PG-18: concurrent attempts cannot commit duplicate attempt numbers.
    with psycopg2.connect(dsn) as conn:
        cur = conn.cursor()
        one, two = _eligible_version(cur), _eligible_version(cur)
        match_id = _match(cur)
        side_id = _side(cur, match_id, "WHITE", "PARTICIPANT", one)
        _side(cur, match_id, "BLACK", "PARTICIPANT", two)
        _start(cur, match_id)
        jobs = [_job(cur, match_id, one), _job(cur, match_id, one)]
    barrier = threading.Barrier(2)
    results: list[str] = []

    def create_attempt(job_value: dict) -> None:
        connection = psycopg2.connect(dsn)
        cursor = connection.cursor()
        barrier.wait(timeout=5)
        try:
            _binding(cursor, match_id, side_id, job_value, attempt=1)
            connection.commit()
            results.append("COMMIT")
        except psycopg2.Error:
            connection.rollback()
            results.append("REJECT")
        finally:
            connection.close()

    threads = [threading.Thread(target=create_attempt, args=(job_value,)) for job_value in jobs]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert all(not thread.is_alive() for thread in threads)
    assert sorted(results) in (["COMMIT", "REJECT"], ["COMMIT", "COMMIT"])
    with psycopg2.connect(dsn) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT count(*),count(DISTINCT attempt_number) FROM match_execution_bindings WHERE match_side_id=%s",
            (side_id,),
        )
        count, distinct_count = cur.fetchone()
        assert count == distinct_count <= 2
    report["PG-18"] = "PASS"

    # PG-19: row locking serializes eligibility changes with job insertion;
    # a later STARTING transition rejects the now-revoked version.
    with psycopg2.connect(dsn) as conn:
        cur = conn.cursor()
        participant = _eligible_version(cur)
        opponent = _eligible_version(cur)
        match_id = _match(cur)
        _side(cur, match_id, "WHITE", "PARTICIPANT", participant)
        _side(cur, match_id, "BLACK", "PARTICIPANT", opponent)
        _job(cur, match_id, participant)
    revoked_done = threading.Event()

    def revoke_version() -> None:
        with psycopg2.connect(dsn) as conn:
            cur = conn.cursor()
            cur.execute(
                """INSERT INTO artifact_state_transitions
                   (id,submission_version_id,from_state,to_state,actor_kind,actor_id,reason_code)
                   VALUES (%s,%s,'ELIGIBLE','REVOKED','test-fixture','pg-test','PG_RACE')""",
                (uuid.uuid4(), participant["version_id"]),
            )
            cur.execute(
                "UPDATE submission_versions SET state='REVOKED' WHERE id=%s",
                (participant["version_id"],),
            )
        revoked_done.set()

    blocker = psycopg2.connect(dsn)
    # Hold the exact version's row lock with an accepted, uncommitted job.
    cursor = blocker.cursor()
    _job(cursor, None, participant)
    revoker = threading.Thread(target=revoke_version)
    revoker.start()
    time.sleep(0.25)
    assert not revoked_done.is_set()
    blocker.commit()
    blocker.close()
    revoker.join(timeout=5)
    assert revoked_done.is_set()
    with psycopg2.connect(dsn) as conn:
        cur = conn.cursor()
        with pytest.raises(psycopg2.Error):
            _start(cur, match_id)
        conn.rollback()
    report["PG-19"] = "PASS"

    # PG-20: revoked workers and expired leases cannot be bound to a side.
    for revoke, expire in ((True, False), (False, True)):
        with psycopg2.connect(dsn) as conn:
            cur = conn.cursor()
            participant = _eligible_version(cur)
            opponent = _eligible_version(cur)
            match_id = _match(cur)
            side_id = _side(cur, match_id, "WHITE", "PARTICIPANT", participant)
            _side(cur, match_id, "BLACK", "PARTICIPANT", opponent)
            _start(cur, match_id)
            job = _job(cur, match_id, participant)
            if revoke:
                cur.execute(
                    "UPDATE workers SET status='REVOKED',revoked_at=now() WHERE worker_id=%s",
                    (job["worker_id"],),
                )
            if expire:
                cur.execute(
                    "UPDATE worker_execution_jobs SET lease_expires_at=now()-interval '1 second' WHERE execution_id=%s",
                    (job["execution_id"],),
                )
            with pytest.raises(psycopg2.Error):
                _binding(cur, match_id, side_id, job)
            conn.rollback()

    # Stale worker or lease state also blocks the authoritative STARTING ->
    # RUNNING transition even when readiness evidence was recorded earlier.
    for revoke, expire in ((True, False), (False, True)):
        with psycopg2.connect(dsn) as conn:
            cur = conn.cursor()
            match_id, _sides, jobs, _bindings = _starting_with_bindings(cur)
            job = jobs[0]
            if revoke:
                cur.execute(
                    "UPDATE workers SET status='REVOKED',revoked_at=now() WHERE worker_id=%s",
                    (job["worker_id"],),
                )
            if expire:
                cur.execute(
                    "UPDATE worker_execution_jobs SET lease_expires_at=now()-interval '1 second' WHERE execution_id=%s",
                    (job["execution_id"],),
                )
            with pytest.raises(psycopg2.Error):
                cur.execute(
                    "UPDATE matches SET state='RUNNING',started_at=now() WHERE id=%s",
                    (match_id,),
                )
            conn.rollback()

    # Concurrent revocation serializes behind RUNNING's worker lock. The
    # transition therefore precedes revocation, and the revoked worker cannot
    # subsequently write a move.
    with psycopg2.connect(dsn) as conn:
        cur = conn.cursor()
        match_id, sides, jobs, bindings = _starting_with_bindings(cur)
    transition = psycopg2.connect(dsn)
    transition.cursor().execute(
        "UPDATE matches SET state='RUNNING',started_at=now() WHERE id=%s", (match_id,)
    )
    revoke_finished = threading.Event()

    def revoke_bound_worker() -> None:
        with psycopg2.connect(dsn) as conn:
            conn.cursor().execute(
                "UPDATE workers SET status='REVOKED',revoked_at=now() WHERE worker_id=%s",
                (jobs[0]["worker_id"],),
            )
        revoke_finished.set()

    revoker = threading.Thread(target=revoke_bound_worker)
    revoker.start()
    time.sleep(0.2)
    assert not revoke_finished.is_set()
    transition.commit()
    transition.close()
    revoker.join(timeout=5)
    assert revoke_finished.is_set()
    with psycopg2.connect(dsn) as conn:
        cur = conn.cursor()
        with pytest.raises(psycopg2.Error):
            cur.execute(
                """INSERT INTO match_moves
                   (id,match_id,match_side_id,execution_binding_id,sequence,request_id,move_uci,
                    position_before_sha256,position_after_sha256,position_before_fen,
                    position_after_fen,elapsed_ms,recorded_at)
                   VALUES (%s,%s,%s,%s,1,%s,'e2e4',%s,%s,'initial-fen','after-fen',1,now())""",
                (uuid.uuid4(), match_id, sides[0], bindings[0], f"revoked-{_token()}",
                 "a" * 64, "b" * 64),
            )
        conn.rollback()
    report["PG-20"] = "PASS"

    # PG-21: a move from the wrong side or a non-latest attempt is rejected.
    with psycopg2.connect(dsn) as conn:
        cur = conn.cursor()
        match_id, sides, bindings = _running_match(cur)
        wrong_side_binding = bindings[1]
        with pytest.raises(psycopg2.Error):
            cur.execute(
                """INSERT INTO match_moves
                   (id,match_id,match_side_id,execution_binding_id,sequence,request_id,move_uci,
                    position_before_sha256,position_after_sha256,position_before_fen,
                    position_after_fen,elapsed_ms,recorded_at)
                   SELECT %s,%s,%s,id,1,%s,'e2e4',%s,%s,'initial-fen','after-fen',1,now()
                   FROM match_execution_bindings WHERE id=%s""",
                (uuid.uuid4(), match_id, sides[0], f"wrong-{_token()}", "a" * 64,
                 "b" * 64, wrong_side_binding),
            )
        conn.rollback()
    with psycopg2.connect(dsn) as conn:
        cur = conn.cursor()
        match_id, sides, bindings = _running_match(cur, stale_first=True)
        with pytest.raises(psycopg2.Error):
            cur.execute(
                """INSERT INTO match_moves
                   (id,match_id,match_side_id,execution_binding_id,sequence,request_id,move_uci,
                    position_before_sha256,position_after_sha256,position_before_fen,
                    position_after_fen,elapsed_ms,recorded_at)
                   VALUES (%s,%s,%s,%s,1,%s,'e2e4',%s,%s,'initial-fen','after-fen',1,now())""",
                (uuid.uuid4(), match_id, sides[0], bindings[0], f"stale-{_token()}",
                 "a" * 64, "b" * 64),
            )
        conn.rollback()
    report["PG-21"] = "PASS"

    # PG-22: direct position mutation and invalid terminal transition are rejected.
    with psycopg2.connect(dsn) as conn:
        cur = conn.cursor()
        part = _eligible_version(cur)
        match_id = _match(cur)
        _side(cur, match_id, "WHITE", "PARTICIPANT", part)
        with pytest.raises(psycopg2.Error):
            cur.execute("UPDATE matches SET current_fen='forged' WHERE id=%s", (match_id,))
        conn.rollback()
    with psycopg2.connect(dsn) as conn:
        cur = conn.cursor()
        part, opponent = _eligible_version(cur), _eligible_version(cur)
        match_id = _match(cur)
        _side(cur, match_id, "WHITE", "PARTICIPANT", part)
        _side(cur, match_id, "BLACK", "PARTICIPANT", opponent)
        with pytest.raises(psycopg2.Error):
            cur.execute(
                """UPDATE matches SET state='COMPLETED',result_outcome='DRAW',
                   result_reason='STALEMATE',completed_at=now() WHERE id=%s""",
                (match_id,),
            )
        conn.rollback()
    report["PG-22"] = "PASS"

    # PG-23 and PG-24 are independent catalog/format assertions.
    with psycopg2.connect(dsn) as conn:
        cur = conn.cursor()
        with pytest.raises(psycopg2.Error):
            cur.execute(
                "INSERT INTO house_ai_artifacts (artifact_sha256,artifact_size,storage_ref) VALUES (%s,0,%s)",
                ("z" * 64, f"bad-{_token()}"),
            )
        conn.rollback()
    report["PG-23"] = "PASS"
    with psycopg2.connect(dsn) as conn:
        cur = conn.cursor()
        cur.execute(
            """SELECT tgname,tgdeferrable,tginitdeferred FROM pg_trigger
               WHERE tgrelid='match_sides'::regclass AND NOT tgisinternal"""
        )
        side_triggers = {row[0]: row[1:] for row in cur.fetchall()}
        cur.execute(
            """SELECT c.relname,t.tgname,p.proname,t.tgdeferrable,t.tginitdeferred,
                      pg_get_triggerdef(t.oid)
               FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid
               JOIN pg_proc p ON p.oid=t.tgfoid
               WHERE c.relname IN ('match_sides','participant_match_side_bindings',
                                   'house_ai_match_side_bindings') AND NOT t.tgisinternal"""
        )
        typed_triggers = cur.fetchall()
        trigger_functions = {row[2] for row in typed_triggers}
        assert trigger_functions == {
            "aik_match_side_identity_from_side_guard",
            "aik_participant_side_identity_guard",
            "aik_house_side_identity_guard",
            "aik_match_side_insert_guard",
            "aik_participant_side_insert_guard",
            "aik_house_side_insert_guard",
            "aik_match_immutable_record",
        }
        deferred = [row for row in typed_triggers if row[1].endswith("typed_identity_check")]
        assert len(deferred) == 3 and all(row[3] and row[4] for row in deferred)
        cur.execute(
            "SELECT conname,contype FROM pg_constraint WHERE conrelid='participant_match_side_bindings'::regclass"
        )
        constraints = {row[0]: row[1] for row in cur.fetchall()}
        cur.execute(
            "SELECT conname,contype FROM pg_constraint WHERE conrelid='worker_execution_jobs'::regclass"
        )
        job_constraints = {row[0]: row[1] for row in cur.fetchall()}
        cur.execute("SELECT indexname FROM pg_indexes WHERE tablename='match_sides'")
        side_indexes = {row[0] for row in cur.fetchall()}
        assert side_triggers["match_sides_typed_identity_check"] == (True, True)
        assert "fk_participant_side_exact_version" in constraints
        assert "uq_match_sides_color" in side_indexes
        assert "uq_match_sides_participant" not in constraints
        assert "uq_match_sides_participant" not in side_indexes
        assert constraints["fk_participant_side_exact_version"] == "f"
        assert job_constraints["ck_worker_job_subject_kind"] == "c"
        assert job_constraints["fk_worker_job_participant_subject"] == "f"
    report["PG-24"] = "PASS"

    for case, status in report.items():
        print(f"{case}: {status}")
