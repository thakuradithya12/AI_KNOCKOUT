"""Strict readers for metrics and limits on an execution-owned cgroup v2."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from uuid import UUID


class CgroupEvidenceError(RuntimeError):
    """Stable, path-free failure codes for cgroup evidence collection."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class CgroupV2Sample:
    execution_id: UUID
    container_id: str
    sampled_monotonic: float
    cpu_usage_usec: int
    memory_current_bytes: int
    pids_current: int
    memory_peak_bytes: int | None
    pids_peak: int | None


def _read(path: Path, name: str) -> str:
    try:
        return (path / name).read_text(encoding="ascii").strip()
    except OSError:
        raise CgroupEvidenceError("CGROUP_METRIC_UNAVAILABLE") from None
    except UnicodeError:
        raise CgroupEvidenceError("CGROUP_METRIC_MALFORMED") from None


def _integer(raw: str, *, code: str = "CGROUP_METRIC_MALFORMED") -> int:
    if not raw or not raw.isascii() or not raw.isdecimal():
        raise CgroupEvidenceError(code)
    value = int(raw)
    if value < 0:
        raise CgroupEvidenceError(code)
    return value


def _optional_integer(path: Path, name: str) -> int | None:
    try:
        raw = (path / name).read_text(encoding="ascii").strip()
    except FileNotFoundError:
        return None
    except OSError:
        raise CgroupEvidenceError("CGROUP_METRIC_UNAVAILABLE") from None
    except UnicodeError:
        raise CgroupEvidenceError("CGROUP_METRIC_MALFORMED") from None
    return _integer(raw)


def _cpu_usage_usec(raw: str) -> int:
    found: dict[str, int] = {}
    try:
        for line in raw.splitlines():
            parts = line.split()
            if len(parts) != 2 or parts[0] in found:
                raise CgroupEvidenceError("CGROUP_METRIC_MALFORMED")
            found[parts[0]] = _integer(parts[1])
    except CgroupEvidenceError:
        raise
    if "usage_usec" not in found:
        raise CgroupEvidenceError("CGROUP_METRIC_UNAVAILABLE")
    return found["usage_usec"]


def _limit(path: Path, name: str, expected: int) -> None:
    raw = _read(path, name)
    if raw == "max":
        raise CgroupEvidenceError("CGROUP_LIMIT_MISMATCH")
    if _integer(raw) != expected:
        raise CgroupEvidenceError("CGROUP_LIMIT_MISMATCH")


def verify_cgroup_v2_limits(
    path: Path, *, cpu_millicores: int, memory_bytes: int, pid_limit: int
) -> None:
    """Read back configured limits independently from usage measurements."""
    if not path.is_absolute() or not path.is_dir():
        raise CgroupEvidenceError("CGROUP_PATH_UNAVAILABLE")
    cpu_max = _read(path, "cpu.max").split()
    expected_cpu_quota = cpu_millicores * 100
    if len(cpu_max) != 2 or cpu_max[0] == "max":
        raise CgroupEvidenceError("CGROUP_LIMIT_MISMATCH")
    if _integer(cpu_max[0]) != expected_cpu_quota or _integer(cpu_max[1]) != 100_000:
        raise CgroupEvidenceError("CGROUP_LIMIT_MISMATCH")
    _limit(path, "memory.max", memory_bytes)
    _limit(path, "pids.max", pid_limit)


def collect_cgroup_v2_sample(
    path: Path,
    *,
    execution_id: UUID,
    container_id: str,
    sampled_monotonic: float,
    cpu_millicores: int,
    memory_bytes: int,
    pid_limit: int,
) -> CgroupV2Sample:
    """Read one sample only from the exact cgroup allocated to this execution."""
    expected_id = f"aik-{execution_id.hex}"
    if (
        container_id != expected_id
        or path.name != expected_id
        or not math.isfinite(sampled_monotonic)
        or sampled_monotonic < 0
    ):
        raise CgroupEvidenceError("CGROUP_EVIDENCE_IDENTITY_MISMATCH")
    if not path.is_absolute() or not path.is_dir():
        raise CgroupEvidenceError("CGROUP_PATH_UNAVAILABLE")

    verify_cgroup_v2_limits(
        path,
        cpu_millicores=cpu_millicores,
        memory_bytes=memory_bytes,
        pid_limit=pid_limit,
    )
    cpu_usage = _cpu_usage_usec(_read(path, "cpu.stat"))
    memory_current = _integer(_read(path, "memory.current"))
    pids_current = _integer(_read(path, "pids.current"))
    return CgroupV2Sample(
        execution_id=execution_id,
        container_id=container_id,
        sampled_monotonic=sampled_monotonic,
        cpu_usage_usec=cpu_usage,
        memory_current_bytes=memory_current,
        pids_current=pids_current,
        memory_peak_bytes=_optional_integer(path, "memory.peak"),
        pids_peak=_optional_integer(path, "pids.peak"),
    )
