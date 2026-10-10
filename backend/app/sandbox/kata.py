"""Fail-closed Linux containerd/Kata adapter.

Participant packages are revalidated and added as an immutable OCI layer. No host
directory is mounted in a participant guest. This adapter deliberately requires a
digest-pinned, preloaded Python base image and an operator-selected containerd
socket/namespace; it never downloads images or changes containerd configuration.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import queue
import re
import shutil
import subprocess
import tarfile
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from app.sandbox.contract import (
    _TERMINAL,
    AIKMessage,
    AIKProtocolError,
    CleanupResult,
    ExecutionIdentity,
    ExecutionState,
    ExecutionSubjectKind,
    ExpiredLeaseError,
    FailureClass,
    IdentityMismatchError,
    InvalidTransitionError,
    SandboxEvidence,
    SandboxPolicy,
    SandboxSnapshot,
    StagedArtifact,
    StaleFenceError,
)
from app.services.submission_validation import PackageRejected, validate_submission_package


class KataRuntimeError(RuntimeError):
    """Runtime operations failed; message text is stable and contains no secrets."""


@dataclass(frozen=True)
class KataRuntimeConfig:
    ctr_binary: Path
    containerd_address: Path
    namespace: str
    base_image: str
    base_manifest_digest: str
    runtime: str = "io.containerd.kata.v2"
    runtime_version: str = "4.2.0"
    ctr_version: str = "2.2.1"
    cgroup_parent: Path | None = None
    command_timeout_seconds: float = 30.0
    staging_parent: Path | None = None

    def __post_init__(self) -> None:
        if not self.namespace or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", self.namespace):
            raise ValueError("KATA_NAMESPACE_INVALID")
        if not re.fullmatch(r"[A-Za-z0-9._/:+-]+@sha256:[0-9a-f]{64}", self.base_image):
            raise ValueError("KATA_BASE_IMAGE_MUST_BE_DIGEST_PINNED")
        if not re.fullmatch(r"[0-9a-f]{64}", self.base_manifest_digest):
            raise ValueError("KATA_BASE_MANIFEST_DIGEST_INVALID")
        if self.runtime != "io.containerd.kata.v2" or self.runtime_version != "4.2.0":
            raise ValueError("KATA_RUNTIME_IDENTITY_UNSUPPORTED")
        if self.ctr_version != "2.2.1" or not 0 < self.command_timeout_seconds <= 60:
            raise ValueError("KATA_RUNTIME_CONFIG_INVALID")


@dataclass
class _Execution:
    identity: ExecutionIdentity
    policy: SandboxPolicy
    state: ExecutionState = ExecutionState.CREATED
    staged_digest: str | None = None
    image_ref: str | None = None
    temp_dir: Path | None = None
    process: subprocess.Popen | None = None
    cleanup: CleanupResult | None = None
    failure: FailureClass | None = None
    termination_reason: str | None = None
    created_at: datetime | None = None
    started_at: datetime | None = None
    ended_at: datetime | None = None
    started_mono: float | None = None
    stdout_total: int = 0
    stderr_total: int = 0
    stdout_evidence: bytearray | None = None
    stderr_evidence: bytearray | None = None
    output: queue.Queue | None = None
    lock: threading.RLock | None = None
    reader_threads: list[threading.Thread] | None = None
    stdin_total: int = 0
    last_input_at: float | None = None
    completion_code: int | None = None
    participant_ready: bool = False
    cgroup_path: Path | None = None


_LOCKED_POLICY = {
    "cpu_millicores": 1000,
    "memory_bytes": 268_435_456,
    "pid_limit": 16,
    "filesystem_bytes": 33_554_432,
    "filesystem_inodes": 128,
    "file_descriptors": 64,
    "stdout_bytes": 1_048_576,
    "stderr_bytes": 262_144,
    "startup_timeout_ms": 5000,
    "move_timeout_ms": 2000,
    "total_timeout_ms": 900_000,
}

_LINUX_CAPABILITIES = (
    "AUDIT_CONTROL",
    "AUDIT_READ",
    "AUDIT_WRITE",
    "BLOCK_SUSPEND",
    "CHOWN",
    "DAC_OVERRIDE",
    "DAC_READ_SEARCH",
    "FOWNER",
    "FSETID",
    "IPC_LOCK",
    "IPC_OWNER",
    "KILL",
    "LEASE",
    "LINUX_IMMUTABLE",
    "MAC_ADMIN",
    "MAC_OVERRIDE",
    "MKNOD",
    "NET_ADMIN",
    "NET_BIND_SERVICE",
    "NET_BROADCAST",
    "NET_RAW",
    "SETFCAP",
    "SETGID",
    "SETPCAP",
    "SETUID",
    "SYS_ADMIN",
    "SYS_BOOT",
    "SYS_CHROOT",
    "SYS_MODULE",
    "SYS_NICE",
    "SYS_PACCT",
    "SYS_PTRACE",
    "SYS_RAWIO",
    "SYS_RESOURCE",
    "SYS_TIME",
    "SYS_TTY_CONFIG",
    "SYSLOG",
    "WAKE_ALARM",
    "PERFMON",
    "BPF",
    "CHECKPOINT_RESTORE",
)


class KataSandboxAdapter:
    """Production adapter using ctr's foreground task I/O and Kata runtime.

    A controlling service must provide a real, current FenceAuthority and must
    keep the containerd socket available only to the trusted worker process.
    """

    def __init__(
        self,
        authority,
        config: KataRuntimeConfig,
        *,
        eligibility_check: Callable[[ExecutionIdentity], bool] | None = None,
        wall_clock=lambda: datetime.now(timezone.utc),
        monotonic=time.monotonic,
    ) -> None:
        self._authority = authority
        self._config = config
        self._eligibility_check = eligibility_check
        self._wall_clock = wall_clock
        self._monotonic = monotonic
        self._records: dict[uuid.UUID, _Execution] = {}
        self._lock = threading.RLock()

    def _run(
        self, args: list[str], *, input_bytes: bytes | None = None
    ) -> subprocess.CompletedProcess:
        if os.name != "posix" or not Path("/proc/sys/kernel/osrelease").exists():
            raise KataRuntimeError("KATA_REQUIRES_LINUX_WORKER")
        command = [
            str(self._config.ctr_binary),
            "--address",
            str(self._config.containerd_address),
            "--namespace",
            self._config.namespace,
            *args,
        ]
        try:
            result = subprocess.run(
                command,
                input=input_bytes,
                capture_output=True,
                timeout=self._config.command_timeout_seconds,
                check=False,
                shell=False,
                close_fds=True,
                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": "/nonexistent"},
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise KataRuntimeError("CONTAINERD_COMMAND_FAILED") from exc
        if result.returncode:
            # Never surface raw ctr stderr: it may contain host paths/configuration.
            raise KataRuntimeError(f"CONTAINERD_COMMAND_EXIT_{result.returncode}")
        return result

    @staticmethod
    def _locked_policy(policy: SandboxPolicy) -> bool:
        values = policy.model_dump(mode="python")
        return all(values.get(key) == value for key, value in _LOCKED_POLICY.items()) and (
            values.get("network_mode") == "DENY_ALL"
            and values.get("writable_storage") == "PRIVATE_TMPFS"
        )

    def _record(self, execution_id: uuid.UUID) -> _Execution:
        try:
            return self._records[execution_id]
        except KeyError as exc:
            raise IdentityMismatchError("EXECUTION_NOT_FOUND") from exc

    def _guard(self, record: _Execution) -> None:
        self._authority.assert_current(record.identity, self._wall_clock())

    @staticmethod
    def _snapshot(record: _Execution) -> SandboxSnapshot:
        return SandboxSnapshot(
            record.identity, record.state, record.staged_digest, record.cleanup, record.failure
        )

    def create_execution(
        self, identity: ExecutionIdentity, policy: SandboxPolicy
    ) -> SandboxSnapshot:
        self._authority.assert_current(identity, self._wall_clock())
        if identity.match_id is None or identity.policy_digest is None:
            raise IdentityMismatchError("MATCH_AND_POLICY_DIGEST_REQUIRED")
        if not self._locked_policy(policy):
            raise IdentityMismatchError("LOCKED_EXECUTION_POLICY_MISMATCH")
        policy_document = json.dumps(
            policy.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
        ).encode()
        expected_policy_digest = identity.sandbox_policy_digest or identity.policy_digest
        if hashlib.sha256(policy_document).hexdigest() != expected_policy_digest:
            raise IdentityMismatchError("POLICY_DIGEST_MISMATCH")
        if identity.protocol_version != "aik-jsonl/1" or identity.runtime_profile != "python-3.12":
            raise IdentityMismatchError("RUNTIME_OR_PROTOCOL_PROFILE_MISMATCH")
        with self._lock:
            if identity.execution_id in self._records:
                raise IdentityMismatchError("EXECUTION_ID_ALREADY_EXISTS")
            record = _Execution(
                identity=identity,
                policy=policy,
                created_at=self._wall_clock(),
                stdout_evidence=bytearray(),
                stderr_evidence=bytearray(),
                output=queue.Queue(maxsize=4096),
                lock=threading.RLock(),
                reader_threads=[],
            )
            self._records[identity.execution_id] = record
        return self._snapshot(record)

    @staticmethod
    def _sha(data: bytes) -> str:
        return hashlib.sha256(data).hexdigest()

    def stage_artifact(
        self,
        execution_id: uuid.UUID,
        artifact_bytes: bytes,
        submission_version_id: uuid.UUID | None,
        expected_digest: str,
        *,
        house_ai_id: str | None = None,
        house_ai_version: int | None = None,
    ) -> StagedArtifact:
        record = self._record(execution_id)
        self._guard(record)
        identity = record.identity
        if identity.subject_kind == ExecutionSubjectKind.PARTICIPANT:
            if (
                submission_version_id != identity.submission_version_id
                or house_ai_id is not None
                or house_ai_version is not None
            ):
                raise IdentityMismatchError("SUBMISSION_VERSION_MISMATCH")
        elif (
            submission_version_id is not None
            or house_ai_id != identity.house_ai_id
            or house_ai_version != identity.house_ai_version
        ):
            raise IdentityMismatchError("HOUSE_AI_VERSION_MISMATCH")
        if (
            expected_digest != identity.artifact_digest
            or self._sha(artifact_bytes) != identity.artifact_digest
        ):
            raise IdentityMismatchError("ARTIFACT_DIGEST_MISMATCH")
        if record.state != ExecutionState.CREATED:
            raise InvalidTransitionError("ARTIFACT_STAGE_STATE_INVALID")
        try:
            package = validate_submission_package(artifact_bytes)
        except PackageRejected as exc:
            raise IdentityMismatchError(f"ARTIFACT_PACKAGE_REJECTED:{exc.code}") from exc
        if (
            package.manifest["runtime"] != identity.runtime_profile
            or package.manifest["protocol_version"] != identity.protocol_version
        ):
            raise IdentityMismatchError("ARTIFACT_MANIFEST_IDENTITY_MISMATCH")
        work = Path(
            tempfile.mkdtemp(prefix=f"aik-{execution_id.hex}-", dir=self._config.staging_parent)
        )
        image_ref = f"localhost/aik-execution:{execution_id.hex}"
        try:
            source_copy = work / "accepted-artifact.ustar"
            fd = os.open(source_copy, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(artifact_bytes)
                stream.flush()
                os.fsync(stream.fileno())
            base_tar = work / "base.oci.tar"
            image_tar = work / "execution.oci.tar"
            self._run(
                [
                    "images",
                    "export",
                    "--platform",
                    "linux/amd64",
                    str(base_tar),
                    self._config.base_image,
                ]
            )
            self._build_derived_image(
                base_tar, image_tar, package.files, package.manifest["entrypoint"], image_ref
            )
            if self._sha(artifact_bytes) != identity.artifact_digest:
                raise IdentityMismatchError("ARTIFACT_DIGEST_CHANGED_DURING_STAGE")
            self._run(["images", "import", "--base-name", image_ref.split(":")[0], str(image_tar)])
        except Exception:
            try:
                self._run(["images", "rm", image_ref])
            except Exception:
                pass
            shutil.rmtree(work, ignore_errors=True)
            raise
        record.temp_dir = work
        record.image_ref = image_ref
        record.staged_digest = identity.artifact_digest
        record.state = ExecutionState.STAGED
        return StagedArtifact(
            execution_id,
            submission_version_id,
            identity.artifact_digest,
            len(artifact_bytes),
            bytes(artifact_bytes),
            subject_kind=identity.subject_kind,
            house_ai_id=identity.house_ai_id,
            house_ai_version=identity.house_ai_version,
        )

    def _build_derived_image(
        self,
        base_tar: Path,
        output_tar: Path,
        files: dict[str, bytes],
        entrypoint: str,
        image_ref: str,
    ) -> None:
        """Append a deterministic source-only layer to the digest-pinned Python image."""
        max_base_bytes = 1024 * 1024 * 1024
        if base_tar.stat().st_size > max_base_bytes:
            raise KataRuntimeError("BASE_IMAGE_EXPORT_TOO_LARGE")
        with tarfile.open(base_tar, "r:") as source:
            members = {m.name: m for m in source.getmembers() if m.isfile()}
            index_raw = self._read_oci_member(source, members, "index.json", 1_000_000)
            index = json.loads(index_raw)
            manifests = index.get("manifests")
            if not isinstance(manifests, list):
                raise KataRuntimeError("OCI_INDEX_INVALID")
            chosen = None
            for descriptor in manifests:
                if descriptor.get("platform") == {"architecture": "amd64", "os": "linux"}:
                    chosen = descriptor
                    break
            if chosen is None and len(manifests) == 1:
                chosen = manifests[0]
            if chosen is None:
                raise KataRuntimeError("OCI_PLATFORM_AMBIGUOUS")
            manifest_raw = self._read_oci_member(
                source, members, self._blob_path(chosen["digest"]), 4_000_000
            )
            if f"sha256:{self._sha(manifest_raw)}" != chosen.get("digest"):
                raise KataRuntimeError("BASE_MANIFEST_DIGEST_MISMATCH")
            if chosen["digest"] != f"sha256:{self._config.base_manifest_digest}":
                raise KataRuntimeError("PINNED_BASE_MANIFEST_MISMATCH")
            manifest = json.loads(manifest_raw)
            config_desc = manifest.get("config")
            config_raw = self._read_oci_member(
                source, members, self._blob_path(config_desc["digest"]), 8_000_000
            )
            if f"sha256:{self._sha(config_raw)}" != config_desc.get("digest"):
                raise KataRuntimeError("BASE_CONFIG_DIGEST_MISMATCH")
            config = json.loads(config_raw)
            if config.get("architecture") != "amd64" or config.get("os") != "linux":
                raise KataRuntimeError("BASE_IMAGE_PLATFORM_INVALID")

            layer = self._artifact_layer(files, entrypoint)
            diff_id = f"sha256:{self._sha(layer)}"
            layer_digest = self._sha(layer)
            config.setdefault("rootfs", {}).setdefault("diff_ids", []).append(diff_id)
            config.setdefault("history", []).append({"created_by": "AIK bounded artifact layer"})
            runtime_config = config.setdefault("config", {})
            runtime_config["User"] = "65532:65532"
            runtime_config["WorkingDir"] = "/opt/aik/run"
            runtime_config["Entrypoint"] = ["/usr/local/bin/python3", "/opt/aik/launcher.py"]
            runtime_config["Cmd"] = [entrypoint]
            runtime_config["Env"] = [
                "PATH=/usr/local/bin:/usr/bin:/bin",
                "LANG=C.UTF-8",
                "TZ=UTC",
                "PYTHONHASHSEED=0",
                "PYTHONDONTWRITEBYTECODE=1",
            ]
            runtime_config["Volumes"] = {}
            runtime_config["WorkingDir"] = "/opt/aik/run"
            config_blob = json.dumps(config, separators=(",", ":"), sort_keys=True).encode()
            config_digest = self._sha(config_blob)
            new_manifest = dict(manifest)
            new_manifest["config"] = {
                "mediaType": config_desc["mediaType"],
                "digest": f"sha256:{config_digest}",
                "size": len(config_blob),
            }
            new_manifest["layers"] = list(manifest["layers"]) + [
                {
                    "mediaType": "application/vnd.oci.image.layer.v1.tar",
                    "digest": f"sha256:{layer_digest}",
                    "size": len(layer),
                }
            ]
            manifest_blob = json.dumps(new_manifest, separators=(",", ":"), sort_keys=True).encode()
            manifest_digest = self._sha(manifest_blob)
            new_index = {
                "schemaVersion": 2,
                "mediaType": "application/vnd.oci.image.index.v1+json",
                "manifests": [
                    {
                        "mediaType": "application/vnd.oci.image.manifest.v1+json",
                        "digest": f"sha256:{manifest_digest}",
                        "size": len(manifest_blob),
                        "platform": {"architecture": "amd64", "os": "linux"},
                        "annotations": {"org.opencontainers.image.ref.name": image_ref},
                    }
                ],
            }
            with tarfile.open(output_tar, "w:") as target:
                self._tar_bytes(target, "oci-layout", b'{"imageLayoutVersion":"1.0.0"}')
                self._tar_bytes(
                    target,
                    "index.json",
                    json.dumps(new_index, separators=(",", ":"), sort_keys=True).encode(),
                )
                self._tar_bytes(target, f"blobs/sha256/{config_digest}", config_blob)
                self._tar_bytes(target, f"blobs/sha256/{manifest_digest}", manifest_blob)
                self._tar_bytes(target, f"blobs/sha256/{layer_digest}", layer)
                copied: set[str] = set()
                for desc in [*manifest.get("layers", [])]:
                    blob = self._blob_path(desc["digest"])
                    if blob in copied:
                        continue
                    copied.add(blob)
                    member = members.get(blob)
                    if member is None:
                        raise KataRuntimeError("BASE_LAYER_MISSING")
                    stream = source.extractfile(member)
                    if stream is None:
                        raise KataRuntimeError("BASE_LAYER_UNREADABLE")
                    target.addfile(self._tar_info(blob, member.size), stream)

    @staticmethod
    def _read_oci_member(archive: tarfile.TarFile, members: dict, name: str, limit: int) -> bytes:
        member = members.get(name)
        if member is None or member.size > limit:
            raise KataRuntimeError("OCI_MEMBER_MISSING_OR_OVERSIZED")
        stream = archive.extractfile(member)
        data = stream.read(limit + 1) if stream else b""
        if len(data) != member.size:
            raise KataRuntimeError("OCI_MEMBER_TRUNCATED")
        return data

    @staticmethod
    def _blob_path(digest: str) -> str:
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
            raise KataRuntimeError("OCI_DIGEST_INVALID")
        return f"blobs/sha256/{digest[7:]}"

    @staticmethod
    def _tar_info(name: str, size: int) -> tarfile.TarInfo:
        info = tarfile.TarInfo(name)
        info.size, info.mode, info.uid, info.gid, info.mtime = size, 0o444, 0, 0, 0
        info.uname = info.gname = ""
        return info

    @classmethod
    def _tar_bytes(cls, archive: tarfile.TarFile, name: str, data: bytes) -> None:
        archive.addfile(cls._tar_info(name, len(data)), io.BytesIO(data))

    @staticmethod
    def _artifact_layer(files: dict[str, bytes], entrypoint: str) -> bytes:
        layer = io.BytesIO()
        for path in files:
            parts = path.split("/")
            if (
                not re.fullmatch(
                    r"(?:manifest\.json|README\.md|engine/(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_.-]+\.py)",
                    path,
                )
                or any(part in {"", ".", ".."} for part in parts)
                or len(path.encode("ascii", "strict")) > 255
                or len(parts[-1].encode("ascii")) > 100
                or (len(parts) > 1 and len("/".join(parts[:-1]).encode("ascii")) > 155)
            ):
                raise IdentityMismatchError("ARTIFACT_PATH_REJECTED")
        with tarfile.open(fileobj=layer, mode="w", format=tarfile.USTAR_FORMAT) as archive:
            dirs = {"opt", "opt/aik", "opt/aik/artifact", "opt/aik/run", "tmp"}
            for path in files:
                parent = Path(path).parent
                while str(parent) != ".":
                    dirs.add(f"opt/aik/artifact/{parent.as_posix()}")
                    parent = parent.parent
            for dirname in sorted(dirs):
                info = tarfile.TarInfo(dirname)
                info.type, info.mode, info.uid, info.gid, info.mtime = (
                    tarfile.DIRTYPE,
                    0o555,
                    0,
                    0,
                    0,
                )
                archive.addfile(info)
            for path, body in sorted(files.items()):
                info = tarfile.TarInfo(f"opt/aik/artifact/{path}")
                info.size, info.mode, info.uid, info.gid, info.mtime = len(body), 0o444, 0, 0, 0
                archive.addfile(info, io.BytesIO(body))
            launcher = (
                "import os,resource,sys\n"
                "resource.setrlimit(resource.RLIMIT_CORE,(0,0))\n"
                "resource.setrlimit(resource.RLIMIT_NOFILE,(64,64))\n"
                "resource.setrlimit(resource.RLIMIT_FSIZE,(33554432,33554432))\n"
                "resource.setrlimit(resource.RLIMIT_NPROC,(16,16))\n"
                "resource.setrlimit(resource.RLIMIT_CPU,(900,900))\n"
                "p='/opt/aik/artifact/'+sys.argv[1]\n"
                "os.execv('/usr/local/bin/python3',['/usr/local/bin/python3',p])\n"
            ).encode("ascii")
            info = tarfile.TarInfo("opt/aik/launcher.py")
            info.size, info.mode, info.uid, info.gid, info.mtime = len(launcher), 0o444, 0, 0, 0
            archive.addfile(info, io.BytesIO(launcher))
        return layer.getvalue()

    def start(self, execution_id: uuid.UUID) -> SandboxSnapshot:
        record = self._record(execution_id)
        self._guard(record)
        if (
            record.state != ExecutionState.STAGED
            or record.staged_digest != record.identity.artifact_digest
            or not record.image_ref
        ):
            raise IdentityMismatchError("START_REQUIRES_VERIFIED_STAGED_ARTIFACT")
        accepted = record.temp_dir / "accepted-artifact.ustar"
        if not accepted.is_file() or self._hash_file(accepted) != record.identity.artifact_digest:
            raise IdentityMismatchError("STAGED_ARTIFACT_DIGEST_MISMATCH")
        if self._eligibility_check is None:
            raise IdentityMismatchError("EXECUTION_ELIGIBILITY_RECHECK_REQUIRED")
        try:
            eligible = self._eligibility_check(record.identity)
        except Exception as exc:
            raise IdentityMismatchError("EXECUTION_ELIGIBILITY_CHECK_FAILED") from exc
        if eligible is not True:
            raise IdentityMismatchError("ARTIFACT_NOT_ELIGIBLE_AT_LAUNCH")
        container_id = f"aik-{execution_id.hex}"
        # A previous worker process may have died while leaving containerd state
        # behind. Never let ctr reuse an execution name across worker lifetimes.
        if self._listed("containers", container_id) or self._listed("tasks", container_id):
            raise IdentityMismatchError("EXECUTION_RUNTIME_ID_ALREADY_EXISTS")
        args = [
            "run",
            "--runtime",
            self._config.runtime,
            "--read-only",
            "--memory-limit",
            str(record.policy.memory_bytes),
            "--mount",
            f"type=tmpfs,dst=/tmp,options=size={record.policy.filesystem_bytes}:nr_inodes={record.policy.filesystem_inodes}:nodev:nosuid:noexec",
            "--user",
            self._sandbox_uid(execution_id),
            "--cgroup",
            self._create_cgroup(record),
        ]
        for capability in _LINUX_CAPABILITIES:
            args.extend(("--cap-drop", f"CAP_{capability}"))
        args.append("--seccomp")
        args.extend((record.image_ref, f"aik-{execution_id.hex}"))
        try:
            record.process = subprocess.Popen(
                [
                    str(self._config.ctr_binary),
                    "--address",
                    str(self._config.containerd_address),
                    "--namespace",
                    self._config.namespace,
                    *args,
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                shell=False,
                close_fds=True,
                bufsize=0,
                env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8", "HOME": "/nonexistent"},
            )
        except OSError as exc:
            record.state = ExecutionState.CRASHED
            record.failure = FailureClass.INFRASTRUCTURE_FAILURE
            record.termination_reason = "KATA_START_FAILED"
            record.ended_at = self._wall_clock()
            if record.cgroup_path is not None:
                try:
                    record.cgroup_path.rmdir()
                    record.cgroup_path = None
                except OSError:
                    record.cleanup = CleanupResult.FAILED
                    record.state = ExecutionState.CLEANUP_FAILED
            raise KataRuntimeError("KATA_START_FAILED") from exc
        record.started_mono = self._monotonic()
        record.started_at = self._wall_clock()
        record.state = ExecutionState.STARTED
        for stream, target, is_stdout in (
            (record.process.stdout, record.stdout_evidence, True),
            (record.process.stderr, record.stderr_evidence, False),
        ):
            thread = threading.Thread(
                target=self._drain, args=(record, stream, is_stdout), daemon=True
            )
            record.reader_threads.append(thread)
            thread.start()
        threading.Thread(target=self._watch_exit, args=(record,), daemon=True).start()
        threading.Thread(target=self._watch_fence_and_deadline, args=(record,), daemon=True).start()
        return self._snapshot(record)

    @staticmethod
    def _sandbox_uid(execution_id: uuid.UUID) -> str:
        # Unique unprivileged guest UID makes RLIMIT_NPROC execution-scoped.
        uid = 100_000 + (int(execution_id.hex[:6], 16) % 10_000_000)
        return f"{uid}:{uid}"

    @staticmethod
    def _hash_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    def _create_cgroup(self, record: _Execution) -> str:
        parent = self._config.cgroup_parent
        if parent is None:
            raise KataRuntimeError("DELEGATED_CGROUP_PARENT_REQUIRED")
        if not parent.is_absolute() or not (
            Path("/sys/fs/cgroup") in parent.parents or parent == Path("/sys/fs/cgroup")
        ):
            raise KataRuntimeError("CGROUP_PARENT_MUST_BE_IN_CGROUP_V2")
        if not (parent / "cgroup.controllers").exists():
            raise KataRuntimeError("CGROUP_V2_UNAVAILABLE")
        available = set((parent / "cgroup.controllers").read_text(encoding="ascii").split())
        if not {"cpu", "pids"}.issubset(available):
            raise KataRuntimeError("REQUIRED_CGROUP_CONTROLLERS_UNAVAILABLE")
        target = parent / f"aik-{record.identity.execution_id.hex}"
        try:
            target.mkdir(mode=0o700)
            # Configure every limit before ctr creates or starts the task.
            (target / "pids.max").write_text(str(record.policy.pid_limit), encoding="ascii")
            (target / "cpu.max").write_text(
                f"{record.policy.cpu_millicores * 100} 100000", encoding="ascii"
            )
            if (target / "pids.max").read_text(encoding="ascii").strip() != str(
                record.policy.pid_limit
            ):
                raise OSError("PID limit readback mismatch")
            if (target / "cpu.max").read_text(encoding="ascii").split() != [
                str(record.policy.cpu_millicores * 100),
                "100000",
            ]:
                raise OSError("CPU limit readback mismatch")
            record.cgroup_path = target
        except OSError as exc:
            # cgroupfs is not a normal filesystem: remove only the empty cgroup
            # directory, and retain a failure if the kernel says it is busy.
            try:
                target.rmdir()
            except OSError:
                pass
            raise KataRuntimeError("CGROUP_POLICY_SETUP_FAILED") from exc
        return str(target)

    def _drain(self, record: _Execution, stream, is_stdout: bool) -> None:
        pending = bytearray()
        try:
            while True:
                chunk = stream.read(4096)
                if not chunk:
                    break
                with record.lock:
                    if is_stdout:
                        record.stdout_total += len(chunk)
                        record.stdout_evidence.extend(
                            chunk[: max(0, 4096 - len(record.stdout_evidence))]
                        )
                        if record.stdout_total > record.policy.stdout_bytes:
                            self._fail_and_kill(
                                record, FailureClass.PROTOCOL_VIOLATION, "STDOUT_LIMIT_EXCEEDED"
                            )
                            return
                        pending.extend(chunk)
                        while b"\n" in pending:
                            line, _, remainder = pending.partition(b"\n")
                            pending = bytearray(remainder)
                            raw = bytes(line) + b"\n"
                            if len(raw) > 65_536:
                                self._fail_and_kill(
                                    record, FailureClass.PROTOCOL_VIOLATION, "STDOUT_LINE_TOO_LARGE"
                                )
                                return
                            try:
                                parsed = json.loads(line.decode("utf-8", "strict"))
                                message_id = (
                                    parsed.get("request_id", "") if isinstance(parsed, dict) else ""
                                )
                                message = AIKMessage(message_id, raw)
                                record.output.put_nowait(message)
                            except (UnicodeDecodeError, json.JSONDecodeError, queue.Full):
                                self._fail_and_kill(
                                    record,
                                    FailureClass.PROTOCOL_VIOLATION,
                                    "STDOUT_MESSAGE_INVALID_OR_OVERFLOW",
                                )
                                return
                        if len(pending) > 65_536:
                            self._fail_and_kill(
                                record, FailureClass.PROTOCOL_VIOLATION, "STDOUT_LINE_TOO_LARGE"
                            )
                            return
                    else:
                        record.stderr_total += len(chunk)
                        record.stderr_evidence.extend(
                            chunk[: max(0, 4096 - len(record.stderr_evidence))]
                        )
                        if record.stderr_total > record.policy.stderr_bytes:
                            self._fail_and_kill(
                                record, FailureClass.RESOURCE_EXHAUSTION, "STDERR_LIMIT_EXCEEDED"
                            )
                            return
        except (OSError, ValueError):
            self._fail_and_kill(record, FailureClass.INFRASTRUCTURE_FAILURE, "PIPE_READ_FAILED")
        finally:
            if is_stdout and pending:
                self._fail_and_kill(record, FailureClass.PROTOCOL_VIOLATION, "STDOUT_PARTIAL_LINE")

    def _watch_exit(self, record: _Execution) -> None:
        code = record.process.wait()
        with record.lock:
            record.completion_code = code
            if record.state not in _TERMINAL | {
                ExecutionState.TERMINATED,
                ExecutionState.CLEANUP_FAILED,
                ExecutionState.CLEANUP_UNKNOWN,
                ExecutionState.DESTROYED,
            }:
                record.state = ExecutionState.COMPLETED if code == 0 else ExecutionState.CRASHED
                record.failure = None if code == 0 else FailureClass.PARTICIPANT_PROCESS_FAILURE
                record.termination_reason = "PROCESS_EXITED" if code == 0 else "PROCESS_CRASHED"
                record.ended_at = self._wall_clock()

    def _watch_fence_and_deadline(self, record: _Execution) -> None:
        while record.process is not None and record.process.poll() is None:
            try:
                self._authority.assert_current(record.identity, self._wall_clock())
            except StaleFenceError:
                self._fail_and_kill(record, FailureClass.STALE_EXECUTION, "FENCING_TOKEN_STALE")
                return
            except ExpiredLeaseError:
                self._fail_and_kill(record, FailureClass.STALE_EXECUTION, "LEASE_EXPIRED")
                return
            except Exception:
                self._fail_and_kill(
                    record, FailureClass.INFRASTRUCTURE_FAILURE, "FENCE_AUTHORITY_UNAVAILABLE"
                )
                return
            if (
                not record.participant_ready
                and record.started_mono is not None
                and self._monotonic() - record.started_mono
                >= record.policy.startup_timeout_ms / 1000
            ):
                self._fail_and_kill(record, FailureClass.STARTUP_FAILURE, "STARTUP_TIMEOUT")
                return
            if (
                record.started_mono is not None
                and self._monotonic() - record.started_mono >= record.policy.total_timeout_ms / 1000
            ):
                self._fail_and_kill(record, FailureClass.EXECUTION_TIMEOUT, "LIFETIME_TIMEOUT")
                return
            if (
                record.last_input_at is not None
                and self._monotonic() - record.last_input_at >= record.policy.move_timeout_ms / 1000
            ):
                self._fail_and_kill(record, FailureClass.MOVE_TIMEOUT, "MOVE_TIMEOUT")
                return
            time.sleep(0.05)

    def send_input(self, execution_id: uuid.UUID, message: AIKMessage) -> None:
        record = self._record(execution_id)
        self._guard(record)
        if (
            record.state not in {ExecutionState.STARTED, ExecutionState.RUNNING}
            or record.process.stdin is None
        ):
            raise InvalidTransitionError("INPUT_REQUIRES_ACTIVE_PROCESS")
        if (
            len(message.payload) > 65_536
            or not message.payload.endswith(b"\n")
            or message.payload.count(b"\n") != 1
        ):
            self._fail_and_kill(record, FailureClass.PROTOCOL_VIOLATION, "STDIN_MESSAGE_INVALID")
            raise AIKProtocolError("JSONL_LINE_INVALID")
        if (
            record.started_mono is not None
            and self._monotonic() - record.started_mono > record.policy.total_timeout_ms / 1000
        ):
            self._fail_and_kill(record, FailureClass.EXECUTION_TIMEOUT, "LIFETIME_TIMEOUT")
            raise AIKProtocolError("LIFETIME_TIMEOUT")
        try:
            payload_type = json.loads(message.payload[:-1].decode("utf-8")).get("type")
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            payload_type = None
        record.stdin_total += len(message.payload)
        if record.stdin_total > record.policy.stdout_bytes:
            self._fail_and_kill(record, FailureClass.PROTOCOL_VIOLATION, "STDIN_LIMIT_EXCEEDED")
            raise AIKProtocolError("STDIN_LIMIT_EXCEEDED")
        try:
            record.process.stdin.write(message.payload)
            record.process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            self._fail_and_kill(record, FailureClass.PARTICIPANT_PROCESS_FAILURE, "PARTICIPANT_EOF")
            raise AIKProtocolError("PARTICIPANT_EOF") from exc
        record.state = ExecutionState.RUNNING
        record.last_input_at = self._monotonic() if payload_type == "go" else None

    def read_output(self, execution_id: uuid.UUID) -> AIKMessage | None:
        record = self._record(execution_id)
        self._guard(record)
        if record.state in _TERMINAL | {ExecutionState.TERMINATED}:
            try:
                return record.output.get_nowait()
            except queue.Empty:
                return None
        if record.state not in {ExecutionState.STARTED, ExecutionState.RUNNING}:
            raise InvalidTransitionError("OUTPUT_REQUIRES_ACTIVE_PROCESS")
        if (
            record.started_mono is not None
            and self._monotonic() - record.started_mono > record.policy.total_timeout_ms / 1000
        ):
            self._fail_and_kill(record, FailureClass.EXECUTION_TIMEOUT, "LIFETIME_TIMEOUT")
            return None
        if (
            record.state == ExecutionState.STARTED
            and record.started_mono is not None
            and self._monotonic() - record.started_mono > record.policy.startup_timeout_ms / 1000
        ):
            self._fail_and_kill(record, FailureClass.STARTUP_FAILURE, "STARTUP_TIMEOUT")
            return None
        if (
            record.last_input_at is not None
            and self._monotonic() - record.last_input_at > record.policy.move_timeout_ms / 1000
        ):
            self._fail_and_kill(record, FailureClass.MOVE_TIMEOUT, "MOVE_TIMEOUT")
            return None
        try:
            message = record.output.get_nowait()
        except queue.Empty:
            return None
        try:
            parsed = json.loads(message.payload[:-1].decode("utf-8"))
            if parsed.get("type") == "ready":
                record.participant_ready = True
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            pass
        record.last_input_at = None
        return message

    def _fail_and_kill(self, record: _Execution, failure: FailureClass, reason: str) -> None:
        if record.state in _TERMINAL:
            return
        record.failure = failure
        record.termination_reason = reason
        record.state = (
            ExecutionState.TIMED_OUT
            if failure
            in {
                FailureClass.MOVE_TIMEOUT,
                FailureClass.STARTUP_FAILURE,
                FailureClass.EXECUTION_TIMEOUT,
            }
            else (
                ExecutionState.PROTOCOL_FAILED
                if failure == FailureClass.PROTOCOL_VIOLATION
                else ExecutionState.RESOURCE_EXCEEDED
            )
        )
        record.ended_at = self._wall_clock()
        self._kill_task(record)

    def _kill_task(self, record: _Execution) -> None:
        if record.process and record.process.poll() is None:
            try:
                self._run(
                    [
                        "tasks",
                        "kill",
                        "--signal",
                        "SIGKILL",
                        f"aik-{record.identity.execution_id.hex}",
                    ]
                )
            except KataRuntimeError:
                pass
            try:
                record.process.kill()
            except OSError:
                pass

    def _listed(self, resource: str, container_id: str) -> bool:
        result = self._run([resource, "ls"])
        return any(
            row.split() and row.split()[0] == container_id
            for row in result.stdout.decode("utf-8", "replace").splitlines()[1:]
        )

    def inspect(self, execution_id: uuid.UUID) -> SandboxSnapshot:
        record = self._record(execution_id)
        self._guard(record)
        if (
            record.state in {ExecutionState.STARTED, ExecutionState.RUNNING}
            and record.started_mono is not None
        ):
            elapsed = self._monotonic() - record.started_mono
            if (
                elapsed >= record.policy.startup_timeout_ms / 1000
                and record.state == ExecutionState.STARTED
            ):
                self._fail_and_kill(record, FailureClass.STARTUP_FAILURE, "STARTUP_TIMEOUT")
            elif elapsed >= record.policy.total_timeout_ms / 1000:
                self._fail_and_kill(record, FailureClass.EXECUTION_TIMEOUT, "LIFETIME_TIMEOUT")
        return self._snapshot(record)

    def terminate(self, execution_id: uuid.UUID) -> SandboxSnapshot:
        record = self._record(execution_id)
        if record.state not in _TERMINAL and record.state not in {
            ExecutionState.TERMINATED,
            ExecutionState.CLEANUP_FAILED,
            ExecutionState.CLEANUP_UNKNOWN,
            ExecutionState.DESTROYED,
        }:
            record.state = ExecutionState.TERMINATED
            record.termination_reason = "SUPERVISOR_TERMINATED"
            record.ended_at = self._wall_clock()
        self._kill_task(record)
        return self._snapshot(record)

    def apply_supervisor_timeout(
        self, execution_id: uuid.UUID, failure_class: FailureClass
    ) -> SandboxSnapshot:
        record = self._record(execution_id)
        if record.state not in {ExecutionState.STARTED, ExecutionState.RUNNING}:
            raise InvalidTransitionError("TIMEOUT_REQUIRES_ACTIVE_STATE")
        self._fail_and_kill(record, failure_class, failure_class.value)
        return self._snapshot(record)

    def collect_evidence(self, execution_id: uuid.UUID) -> SandboxEvidence:
        record = self._record(execution_id)
        return SandboxEvidence(
            execution_id=execution_id,
            artifact_digest=record.identity.artifact_digest,
            submission_version_id=record.identity.submission_version_id,
            state=record.state,
            failure_class=record.failure,
            stdout_bytes=record.stdout_total,
            stderr_bytes=record.stderr_total,
            process_count=None,
            memory_bytes=None,
            filesystem_bytes=None,
            filesystem_inodes=None,
            file_descriptors=None,
            cpu_millicores=None,
            started_monotonic=record.started_mono,
            observed_monotonic=self._monotonic(),
            cleanup=record.cleanup,
            subject_kind=record.identity.subject_kind,
            house_ai_id=record.identity.house_ai_id,
            house_ai_version=record.identity.house_ai_version,
            match_id=record.identity.match_id,
            worker_id=record.identity.worker_id,
            lease_id=record.identity.lease_id,
            fencing_token=record.identity.fencing_token,
            policy_digest=record.identity.policy_digest,
            runtime_identity=f"{self._config.runtime}/{self._config.runtime_version};containerd/{self._config.ctr_version}",
            container_id=f"aik-{execution_id.hex}",
            started_at=record.started_at,
            ended_at=record.ended_at,
            termination_reason=record.termination_reason,
            stdout_evidence=bytes(record.stdout_evidence),
            stderr_evidence=bytes(record.stderr_evidence),
        )

    def verify_cleanup(self, execution_id: uuid.UUID) -> CleanupResult:
        record = self._record(execution_id)
        if record.state not in _TERMINAL | {
            ExecutionState.TERMINATED,
            ExecutionState.CLEANUP_FAILED,
            ExecutionState.CLEANUP_UNKNOWN,
        }:
            raise InvalidTransitionError("CLEANUP_REQUIRES_TERMINAL_STATE")
        container_id = f"aik-{execution_id.hex}"
        try:
            self._kill_task(record)
            if record.process is not None and record.process.poll() is None:
                try:
                    record.process.wait(timeout=5)
                except subprocess.TimeoutExpired as exc:
                    raise KataRuntimeError("KATA_TASK_DID_NOT_EXIT") from exc
            for reader in record.reader_threads or ():
                reader.join(timeout=1)
                if reader.is_alive():
                    raise KataRuntimeError("KATA_OUTPUT_PIPE_DID_NOT_CLOSE")
            if self._listed("tasks", container_id):
                self._run(["tasks", "delete", container_id])
            if self._listed("containers", container_id):
                self._run(["containers", "delete", "--snapshot-cleanup", container_id])
            if record.cgroup_path is not None:
                # Removal succeeds only after every sandbox process has exited.
                record.cgroup_path.rmdir()
                record.cgroup_path = None
            record.cleanup = CleanupResult.VERIFIED
        except Exception:
            record.cleanup = CleanupResult.FAILED
            record.state = ExecutionState.CLEANUP_FAILED
            return record.cleanup
        if record.state in _TERMINAL:
            record.state = (
                ExecutionState.CLEANUP_FAILED
                if record.cleanup != CleanupResult.VERIFIED
                else record.state
            )
        return record.cleanup

    def destroy(self, execution_id: uuid.UUID) -> SandboxSnapshot:
        record = self._record(execution_id)
        if record.cleanup != CleanupResult.VERIFIED:
            raise InvalidTransitionError("CLEANUP_NOT_VERIFIED")
        try:
            if record.image_ref:
                self._run(["images", "rm", record.image_ref])
            if record.temp_dir:
                shutil.rmtree(record.temp_dir)
            record.state = ExecutionState.DESTROYED
            record.staged_digest = None
            record.image_ref = None
            record.temp_dir = None
        except Exception as exc:
            record.cleanup = CleanupResult.FAILED
            record.state = ExecutionState.CLEANUP_FAILED
            raise KataRuntimeError("KATA_DESTROY_FAILED") from exc
        return self._snapshot(record)
