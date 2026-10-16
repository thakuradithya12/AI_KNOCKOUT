from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.sandbox.cgroup_v2 import (
    CgroupEvidenceError,
    collect_cgroup_v2_sample,
    verify_cgroup_v2_limits,
)
from app.sandbox.contract import (
    CleanupResult,
    ExecutionIdentity,
    ExecutionState,
    NetworkMode,
    SandboxPolicy,
    WritableStoragePolicy,
)
from app.sandbox.kata import KataRuntimeConfig, KataRuntimeError, KataSandboxAdapter, _Execution
from app.workers.schemas import EvidenceIn


def _files(path: Path, **overrides: str) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    values = {
        "cpu.max": "100000 100000\n",
        "cpu.stat": "usage_usec 125000\nuser_usec 100000\nsystem_usec 25000\n",
        "memory.max": "268435456\n",
        "memory.current": "4096\n",
        "memory.peak": "8192\n",
        "pids.max": "16\n",
        "pids.current": "2\n",
        "pids.peak": "3\n",
    }
    values.update(overrides)
    for name, content in values.items():
        (path / name).write_text(content, encoding="ascii")
    return path


def _policy() -> SandboxPolicy:
    return SandboxPolicy(
        cpu_millicores=1000,
        memory_bytes=268_435_456,
        pid_limit=16,
        filesystem_bytes=33_554_432,
        filesystem_inodes=128,
        file_descriptors=64,
        stdout_bytes=1_048_576,
        stderr_bytes=262_144,
        startup_timeout_ms=5000,
        move_timeout_ms=2000,
        total_timeout_ms=900_000,
        network_mode=NetworkMode.DENY_ALL,
        writable_storage=WritableStoragePolicy.PRIVATE_TMPFS,
    )


def _identity(execution_id: uuid.UUID) -> ExecutionIdentity:
    return ExecutionIdentity(
        execution_id=execution_id,
        submission_version_id=uuid.uuid4(),
        artifact_digest="a" * 64,
        policy_profile="participant-v1",
        runtime_profile="python-3.12",
        worker_id="worker-evidence-test",
        lease_id=uuid.uuid4(),
        fencing_token=1,
        protocol_version="aik-jsonl/1",
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        match_id=uuid.uuid4(),
        policy_digest="b" * 64,
    )


def _adapter(tmp_path: Path, clock: list[float]) -> KataSandboxAdapter:
    config = KataRuntimeConfig(
        ctr_binary=tmp_path / "ctr",
        containerd_address=tmp_path / "containerd.sock",
        namespace="kata-test",
        base_image=f"registry.invalid/python@sha256:{'c' * 64}",
        base_manifest_digest="d" * 64,
        cgroup_parent=tmp_path / "delegated",
        staging_parent=tmp_path,
    )
    return KataSandboxAdapter(object(), config, monotonic=lambda: clock[0])


def _record(
    adapter: KataSandboxAdapter,
    path: Path,
    clock: list[float],
    execution_id: uuid.UUID | None = None,
) -> tuple[uuid.UUID, str]:
    execution_id = execution_id or uuid.uuid4()
    identity = _identity(execution_id)
    container_id = f"aik-{execution_id.hex}"
    record = _Execution(
        identity=identity,
        policy=_policy(),
        state=ExecutionState.TERMINATED,
        cleanup=CleanupResult.VERIFIED,
        started_mono=1.0,
        started_at=datetime.now(timezone.utc),
        ended_at=datetime.now(timezone.utc),
        termination_reason="TEST_TERMINATED",
        stdout_evidence=bytearray(),
        stderr_evidence=bytearray(),
        cgroup_path=path,
        container_id=container_id,
        cgroup_start_sample=collect_cgroup_v2_sample(
            path,
            execution_id=execution_id,
            container_id=container_id,
            sampled_monotonic=clock[0] - 1.0,
            cpu_millicores=1000,
            memory_bytes=268_435_456,
            pid_limit=16,
        ),
        measurement_started_monotonic=clock[0] - 1.0,
    )
    adapter._records[execution_id] = record
    return execution_id, container_id


def _sample(path: Path, execution_id: uuid.UUID, container_id: str):
    return collect_cgroup_v2_sample(
        path,
        execution_id=execution_id,
        container_id=container_id,
        sampled_monotonic=12.0,
        cpu_millicores=1000,
        memory_bytes=268_435_456,
        pid_limit=16,
    )


def test_cgroup_sample_parses_usage_limits_and_binds_execution(tmp_path: Path) -> None:
    execution_id = uuid.uuid4()
    container_id = f"aik-{execution_id.hex}"
    sample = _sample(_files(tmp_path / container_id), execution_id, container_id)

    assert sample.execution_id == execution_id
    assert sample.container_id == container_id
    assert sample.cpu_usage_usec == 125_000
    assert sample.memory_current_bytes == 4096
    assert sample.memory_peak_bytes == 8192
    assert sample.pids_current == 2
    assert sample.pids_peak == 3


@pytest.mark.parametrize(
    "filename", ["cpu.stat", "memory.current", "memory.max", "pids.current", "pids.max"]
)
def test_missing_required_cgroup_metric_is_unavailable_not_zero(
    tmp_path: Path, filename: str
) -> None:
    execution_id = uuid.uuid4()
    container_id = f"aik-{execution_id.hex}"
    path = _files(tmp_path / container_id)
    (path / filename).unlink()

    with pytest.raises(CgroupEvidenceError) as error:
        _sample(path, execution_id, container_id)

    assert error.value.code in {"CGROUP_METRIC_UNAVAILABLE", "CGROUP_PATH_UNAVAILABLE"}


@pytest.mark.parametrize(
    ("filename", "content"),
    [
        ("cpu.stat", "usage_usec nope\n"),
        ("cpu.stat", "usage_usec 10\nusage_usec 11\n"),
        ("memory.current", "-1\n"),
        ("pids.current", "two\n"),
    ],
)
def test_malformed_cgroup_metrics_fail_closed(
    tmp_path: Path, filename: str, content: str
) -> None:
    execution_id = uuid.uuid4()
    container_id = f"aik-{execution_id.hex}"
    path = _files(tmp_path / container_id, **{filename: content})

    with pytest.raises(CgroupEvidenceError, match="CGROUP_METRIC_MALFORMED"):
        _sample(path, execution_id, container_id)


@pytest.mark.parametrize(
    ("filename", "content"),
    [("cpu.max", "max 100000\n"), ("memory.max", "max\n"), ("pids.max", "max\n")],
)
def test_limit_readback_rejects_unbounded_or_changed_policy(
    tmp_path: Path, filename: str, content: str
) -> None:
    path = _files(tmp_path / "limits", **{filename: content})

    with pytest.raises(CgroupEvidenceError, match="CGROUP_LIMIT_MISMATCH"):
        verify_cgroup_v2_limits(
            path, cpu_millicores=1000, memory_bytes=268_435_456, pid_limit=16
        )


def test_limit_readback_accepts_exact_configured_limits(tmp_path: Path) -> None:
    path = _files(tmp_path / "limits")

    verify_cgroup_v2_limits(
        path, cpu_millicores=1000, memory_bytes=268_435_456, pid_limit=16
    )


def test_terminated_execution_records_measured_zero_and_explicitly_missing_fields(
    tmp_path: Path,
) -> None:
    clock = [12.0]
    adapter = _adapter(tmp_path, clock)
    execution_id = uuid.uuid4()
    container_id = f"aik-{execution_id.hex}"
    path = _files(
        tmp_path / container_id,
        **{
            "cpu.stat": "usage_usec 125000\n",
            "memory.current": "0\n",
            "memory.peak": "0\n",
            "pids.current": "0\n",
            "pids.peak": "0\n",
        },
    )
    _record(adapter, path, clock, execution_id)

    evidence = adapter.collect_evidence(execution_id)

    assert evidence.measurement_error is None
    assert evidence.measurement_source == "cgroup-v2"
    assert evidence.measurement_container_id == container_id
    assert evidence.measurement_started_monotonic == 11.0
    assert evidence.measurement_ended_monotonic == 12.0
    assert evidence.process_count == 0
    assert evidence.memory_bytes == 0
    assert evidence.cpu_millicores == 0
    assert evidence.resource_usage == {
        "cpu_usec": 0,
        "cpu_millicores": 0,
        "memory_current_bytes": 0,
        "memory_peak_bytes": 0,
        "pids_current": 0,
        "pids_peak": 0,
    }
    assert evidence.unavailable_measurements == (
        "filesystem_bytes",
        "filesystem_inodes",
        "file_descriptors",
    )
    with pytest.raises(KataRuntimeError, match="REQUIRED_RESOURCE_EVIDENCE_UNAVAILABLE"):
        adapter.verify_resource_evidence(execution_id)


def test_evidence_is_cached_before_cleanup_removes_cgroup(tmp_path: Path) -> None:
    clock = [12.0]
    adapter = _adapter(tmp_path, clock)
    execution_id = uuid.uuid4()
    container_id = f"aik-{execution_id.hex}"
    path = _files(tmp_path / container_id)
    _record(adapter, path, clock, execution_id)

    before_cleanup = adapter.collect_evidence(execution_id)
    for child in path.iterdir():
        child.unlink()
    path.rmdir()

    assert adapter.collect_evidence(execution_id) is before_cleanup


def test_collection_failure_is_explicit_and_verification_fails(tmp_path: Path) -> None:
    clock = [12.0]
    adapter = _adapter(tmp_path, clock)
    execution_id = uuid.uuid4()
    container_id = f"aik-{execution_id.hex}"
    path = _files(tmp_path / container_id)
    _record(adapter, path, clock, execution_id)
    (path / "memory.current").unlink()

    evidence = adapter.collect_evidence(execution_id)

    assert evidence.measurement_error == "CGROUP_METRIC_UNAVAILABLE"
    assert evidence.measurement_container_id is None
    assert all(
        field in evidence.unavailable_measurements
        for field in ("process_count", "memory_bytes", "cpu_millicores")
    )
    with pytest.raises(KataRuntimeError, match="REQUIRED_RESOURCE_EVIDENCE_UNAVAILABLE"):
        adapter.verify_resource_evidence(execution_id)


def test_missing_high_water_metrics_are_not_reported_as_zero(tmp_path: Path) -> None:
    clock = [12.0]
    adapter = _adapter(tmp_path, clock)
    execution_id = uuid.uuid4()
    container_id = f"aik-{execution_id.hex}"
    path = _files(tmp_path / container_id)
    (path / "memory.peak").unlink()
    (path / "pids.peak").unlink()
    _record(adapter, path, clock, execution_id)

    evidence = adapter.collect_evidence(execution_id)

    assert "memory_peak_bytes" not in evidence.resource_usage
    assert "pids_peak" not in evidence.resource_usage
    assert "memory_peak_bytes" in evidence.unavailable_measurements
    assert "pids_peak" in evidence.unavailable_measurements
    with pytest.raises(KataRuntimeError, match="REQUIRED_RESOURCE_EVIDENCE_UNAVAILABLE"):
        adapter.verify_resource_evidence(execution_id)


def test_stale_or_reversed_sample_interval_is_discarded(tmp_path: Path) -> None:
    clock = [12.0]
    adapter = _adapter(tmp_path, clock)
    execution_id = uuid.uuid4()
    container_id = f"aik-{execution_id.hex}"
    path = _files(tmp_path / container_id)
    _record(adapter, path, clock, execution_id)
    clock[0] = 10.0

    evidence = adapter.collect_evidence(execution_id)

    assert evidence.measurement_error == "CGROUP_METRIC_INTERVAL_INVALID"
    assert evidence.measurement_source is None
    assert evidence.measurement_container_id is None
    assert evidence.resource_usage == {}
    assert set(evidence.unavailable_measurements) >= {
        "process_count",
        "memory_bytes",
        "cpu_millicores",
    }
    with pytest.raises(KataRuntimeError, match="REQUIRED_RESOURCE_EVIDENCE_UNAVAILABLE"):
        adapter.verify_resource_evidence(execution_id)


def test_wrong_execution_container_binding_is_rejected(tmp_path: Path) -> None:
    execution_id = uuid.uuid4()
    other_execution_id = uuid.uuid4()
    path = _files(tmp_path / f"aik-{execution_id.hex}")

    with pytest.raises(CgroupEvidenceError, match="CGROUP_EVIDENCE_IDENTITY_MISMATCH"):
        _sample(path, other_execution_id, f"aik-{execution_id.hex}")


def test_worker_evidence_schema_preserves_zero_and_measurement_provenance() -> None:
    execution_id = uuid.uuid4()
    evidence = EvidenceIn(
        lease_id=uuid.uuid4(),
        fencing_token=1,
        sequence=1,
        event_type="FAILED",
        resource_usage={
            "cpu_usec": 0,
            "cpu_millicores": 0,
            "memory_current_bytes": 0,
            "pids_current": 0,
        },
        resource_measurement_source="cgroup-v2",
        resource_measurement_scope="execution-cgroup-host-tasks",
        resource_measurement_container_id=f"aik-{execution_id.hex}",
        resource_measurement_started_monotonic=1.0,
        resource_measurement_ended_monotonic=2.0,
        unavailable_measurements=["filesystem_bytes", "filesystem_inodes", "file_descriptors"],
    )

    assert evidence.resource_usage["memory_current_bytes"] == 0
    assert evidence.unavailable_measurements == [
        "filesystem_bytes",
        "filesystem_inodes",
        "file_descriptors",
    ]


def test_worker_evidence_schema_accepts_collection_failure_without_fake_provenance() -> None:
    evidence = EvidenceIn(
        lease_id=uuid.uuid4(),
        fencing_token=1,
        sequence=1,
        event_type="FAILED",
        unavailable_measurements=["process_count", "memory_bytes", "cpu_millicores"],
        measurement_error="CGROUP_METRIC_UNAVAILABLE",
    )

    assert evidence.measurement_error == "CGROUP_METRIC_UNAVAILABLE"
    assert evidence.resource_measurement_container_id is None


def test_worker_evidence_schema_rejects_missing_provenance_and_bad_interval() -> None:
    common = {
        "lease_id": uuid.uuid4(),
        "fencing_token": 1,
        "sequence": 1,
        "event_type": "FAILED",
    }
    with pytest.raises(ValueError, match="explicit measurement provenance"):
        EvidenceIn(**common, resource_usage={"memory_current_bytes": 0})
    with pytest.raises(ValueError, match="interval must be ordered"):
        EvidenceIn(
            **common,
            resource_measurement_source="cgroup-v2",
            resource_measurement_scope="execution-cgroup-host-tasks",
            resource_measurement_container_id=f"aik-{uuid.uuid4().hex}",
            resource_measurement_started_monotonic=2.0,
            resource_measurement_ended_monotonic=1.0,
        )


def test_worker_evidence_schema_rejects_host_measurements_with_guest_scope_label() -> None:
    execution_id = uuid.uuid4()
    with pytest.raises(ValueError):
        EvidenceIn(
            lease_id=uuid.uuid4(),
            fencing_token=1,
            sequence=1,
            event_type="FAILED",
            resource_measurement_source="cgroup-v2",
            resource_measurement_scope="participant-guest",
            resource_measurement_container_id=f"aik-{execution_id.hex}",
            resource_measurement_started_monotonic=1.0,
            resource_measurement_ended_monotonic=2.0,
        )
