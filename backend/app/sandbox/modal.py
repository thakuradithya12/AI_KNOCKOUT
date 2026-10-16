"""Fail-closed Modal Sandbox adapter for isolated chess agent execution.

This adapter implements the SandboxAdapter contract (contract.py) using Modal
microVM/gVisor sandboxes with strict egress isolation (block_network=True),
bounded CPU and memory quotas, and deterministic lifecycle tracking.

Default behavior: Safe mock/offline mode unless explicitly enabled and configured.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import shutil
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.sandbox.contract import (
    _ALLOWED,
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
    NetworkMode,
    SandboxEvidence,
    SandboxPolicy,
    SandboxSnapshot,
    StagedArtifact,
    StaleFenceError,
    WritableStoragePolicy,
)
from app.sandbox.kata import KataRuntimeError
from app.services.submission_validation import PackageRejected, validate_submission_package


# Libraries promised to participants by the runtime contract (python-3.12 profile).
MODAL_RUNTIME_PACKAGES: tuple[str, ...] = ("chess==1.11.2", "numpy>=1.26.0")


class ModalRuntimeError(KataRuntimeError):
    """Runtime operations failed for Modal sandbox; non-sensitive error code."""


@dataclass(frozen=True)
class ModalRuntimeConfig:
    """Configuration for Modal Sandbox adapter."""

    app_name: str = "aik-sandbox-modal"
    python_version: str = "3.12"
    block_network: bool = True
    default_cpu_millicores: int = 1000
    default_memory_bytes: int = 268_435_456  # 256 MiB
    default_timeout_seconds: int = 30
    staging_parent: Path | None = None
    mock_mode: bool = True  # Default to safe mock mode
    sandbox_enabled: bool = False  # Keep disabled by default per safety requirement
    execution_mode: str = "mock"  # Keep 'mock' by default

    def __post_init__(self) -> None:
        if not self.block_network:
            raise ValueError("MODAL_NETWORK_ISOLATION_REQUIRED")

    @classmethod
    def from_env(cls) -> ModalRuntimeConfig:
        sandbox_enabled_str = os.environ.get("SANDBOX_ENABLED", "false").strip().lower()
        sandbox_enabled = sandbox_enabled_str in {"1", "true", "yes"}
        exec_mode = os.environ.get("WORKER_EXECUTION_MODE", "mock").strip().lower()

        # Mock mode defaults to True unless live execution is explicitly requested and authorized
        mock_mode_str = os.environ.get("AIK_MODAL_MOCK_MODE", "").strip().lower()
        if mock_mode_str in {"0", "false", "no"}:
            mock_mode = False
        else:
            mock_mode = not (sandbox_enabled and exec_mode == "modal")

        staging_dir = os.environ.get("AIK_MODAL_STAGING_DIR")
        block_network_str = os.environ.get("AIK_MODAL_BLOCK_NETWORK", "true").strip().lower()
        if block_network_str not in {"1", "true", "yes"}:
            raise ValueError("MODAL_NETWORK_ISOLATION_REQUIRED")
        return cls(
            app_name=os.environ.get("AIK_MODAL_APP_NAME", "aik-sandbox-modal"),
            python_version=os.environ.get("AIK_MODAL_PYTHON_VERSION", "3.12"),
            block_network=True,
            default_cpu_millicores=int(os.environ.get("AIK_MODAL_CPU_MILLICORES", "1000")),
            default_memory_bytes=int(os.environ.get("AIK_MODAL_MEMORY_BYTES", "268435456")),
            default_timeout_seconds=int(os.environ.get("AIK_MODAL_TIMEOUT_SECONDS", "30")),
            staging_parent=Path(staging_dir) if staging_dir else None,
            mock_mode=mock_mode,
            sandbox_enabled=sandbox_enabled,
            execution_mode=exec_mode,
        )


@dataclass
class _ModalExecution:
    identity: ExecutionIdentity
    policy: SandboxPolicy
    state: ExecutionState = ExecutionState.CREATED
    staged_digest: str | None = None
    temp_dir: Path | None = None
    files: dict[str, bytes] | None = None
    entrypoint: str | None = None
    sandbox: Any = None
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
    output: queue.Queue[AIKMessage] | None = None
    lock: threading.RLock | None = None
    reader_threads: list[threading.Thread] | None = None
    stdin_total: int = 0
    last_input_at: float | None = None
    completion_code: int | None = None
    participant_ready: bool = False
    hello_sent_mono: float | None = None
    mock_responses: dict[str, AIKMessage] | None = None


class ModalSandboxAdapter:
    """Sandbox adapter using Modal's microVM sandboxes for hostile agent isolation.

    Implements the SandboxAdapter protocol. In mock mode or when sandbox_enabled=False,
    it executes purely simulated/mock protocol exchanges without creating cloud resources
    or running participant code unsandboxed on the local host.
    """

    def __init__(
        self,
        authority: Any,
        config: ModalRuntimeConfig | None = None,
        *,
        eligibility_check: Callable[[ExecutionIdentity], bool] | None = None,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._authority = authority
        self._config = config or ModalRuntimeConfig()
        self._eligibility_check = eligibility_check
        self._wall_clock = wall_clock
        self._monotonic = monotonic
        self._records: dict[uuid.UUID, _ModalExecution] = {}
        self._lock = threading.RLock()

    def _record(self, execution_id: uuid.UUID) -> _ModalExecution:
        try:
            return self._records[execution_id]
        except KeyError as exc:
            raise IdentityMismatchError("EXECUTION_NOT_FOUND") from exc

    def _guard(self, record: _ModalExecution) -> None:
        self._authority.assert_current(record.identity, self._wall_clock())

    @staticmethod
    def _snapshot(record: _ModalExecution) -> SandboxSnapshot:
        return SandboxSnapshot(
            record.identity, record.state, record.staged_digest, record.cleanup, record.failure
        )

    @staticmethod
    def _move(record: _ModalExecution, new_state: ExecutionState) -> None:
        if new_state not in _ALLOWED[record.state]:
            raise InvalidTransitionError(f"{record.state.value}_TO_{new_state.value}_INVALID")
        record.state = new_state

    def create_execution(
        self, identity: ExecutionIdentity, policy: SandboxPolicy
    ) -> SandboxSnapshot:
        self._authority.assert_current(identity, self._wall_clock())
        expected_policy_digest = identity.sandbox_policy_digest or identity.policy_digest
        if identity.match_id is None or expected_policy_digest is None:
            raise IdentityMismatchError("MATCH_AND_POLICY_DIGEST_REQUIRED")

        # Verify policy bounds and network isolation
        if policy.network_mode != NetworkMode.DENY_ALL:
            raise IdentityMismatchError("MODAL_SANDBOX_REQUIRES_NETWORK_DENY_ALL")
        if policy.writable_storage != WritableStoragePolicy.PRIVATE_TMPFS:
            raise IdentityMismatchError("MODAL_SANDBOX_REQUIRES_PRIVATE_TMPFS")

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
            record = _ModalExecution(
                identity=identity,
                policy=policy,
                created_at=self._wall_clock(),
                stdout_evidence=bytearray(),
                stderr_evidence=bytearray(),
                output=queue.Queue(maxsize=4096),
                lock=threading.RLock(),
                reader_threads=[],
                mock_responses={},
            )
            self._records[identity.execution_id] = record
        return self._snapshot(record)

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
            or hashlib.sha256(artifact_bytes).hexdigest() != identity.artifact_digest
        ):
            raise IdentityMismatchError("ARTIFACT_DIGEST_MISMATCH")

        if record.state != ExecutionState.CREATED:
            raise InvalidTransitionError("ARTIFACT_STAGE_STATE_INVALID")

        # Validate canonical package archive structure
        try:
            package = validate_submission_package(artifact_bytes)
        except PackageRejected as exc:
            raise IdentityMismatchError(f"ARTIFACT_PACKAGE_REJECTED:{exc.code}") from exc

        if (
            package.manifest.get("runtime") != identity.runtime_profile
            or package.manifest.get("protocol_version") != identity.protocol_version
        ):
            raise IdentityMismatchError("ARTIFACT_MANIFEST_IDENTITY_MISMATCH")

        # Prepare staging directory
        work = Path(
            tempfile.mkdtemp(
                prefix=f"aik-modal-{execution_id.hex}-", dir=self._config.staging_parent
            )
        )
        record.temp_dir = work
        record.files = package.files
        record.entrypoint = package.manifest["entrypoint"]

        for rel_path, data in package.files.items():
            dest = work / rel_path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(data)

        # Generate guest confinement launcher enforcing RLIMITs inside Modal microVM
        policy = record.policy
        pid_limit = policy.pid_limit if policy else 16
        nofile_limit = policy.file_descriptors if policy else 64
        fsize_limit = policy.filesystem_bytes if policy else 33554432
        cpu_sec = (policy.total_timeout_ms // 1000) if policy else 900
        launcher_code = (
            "import os, pathlib, resource, sys\n\n"
            "try:\n"
            "    if len(sys.argv) < 2:\n"
            "        sys.stderr.write('LAUNCHER_ERROR: missing entrypoint argument\\n')\n"
            "        sys.exit(126)\n"
            "    root = pathlib.Path(os.environ.get('AIK_ARTIFACT_ROOT', '/opt/aik/artifact')).resolve()\n"
            "    target = (root / sys.argv[1]).resolve()\n"
            "    if not target.is_relative_to(root) or not target.is_file():\n"
            "        sys.stderr.write('LAUNCHER_ERROR: invalid entrypoint path\\n')\n"
            "        sys.exit(126)\n"
            "    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))\n"
            f"    resource.setrlimit(resource.RLIMIT_NOFILE, ({nofile_limit}, {nofile_limit}))\n"
            f"    resource.setrlimit(resource.RLIMIT_FSIZE, ({fsize_limit}, {fsize_limit}))\n"
            f"    resource.setrlimit(resource.RLIMIT_NPROC, ({pid_limit}, {pid_limit}))\n"
            f"    resource.setrlimit(resource.RLIMIT_CPU, ({cpu_sec}, {cpu_sec}))\n"
            "except SystemExit:\n"
            "    raise\n"
            "except Exception as exc:\n"
            "    sys.stderr.write(f'LAUNCHER_CONFINEMENT_FAILED: {exc}\\n')\n"
            "    sys.exit(127)\n\n"
            "os.execv(sys.executable, [sys.executable, str(target)] + sys.argv[2:])\n"
        ).encode("ascii")
        (work / "_launcher.py").write_bytes(launcher_code)

        record.staged_digest = expected_digest
        self._move(record, ExecutionState.STAGED)

        return StagedArtifact(
            execution_id=execution_id,
            submission_version_id=submission_version_id,
            digest=expected_digest,
            size=len(artifact_bytes),
            content=artifact_bytes,
            subject_kind=identity.subject_kind,
            house_ai_id=house_ai_id,
            house_ai_version=house_ai_version,
        )

    def start(self, execution_id: uuid.UUID) -> SandboxSnapshot:
        record = self._record(execution_id)
        self._guard(record)

        if (
            record.state != ExecutionState.STAGED
            or record.staged_digest != record.identity.artifact_digest
            or record.temp_dir is None
        ):
            raise IdentityMismatchError("START_REQUIRES_VERIFIED_STAGED_ARTIFACT")

        if self._eligibility_check is not None:
            try:
                eligible = self._eligibility_check(record.identity)
            except Exception as exc:
                raise IdentityMismatchError("EXECUTION_ELIGIBILITY_CHECK_FAILED") from exc
            if eligible is not True:
                raise IdentityMismatchError("ARTIFACT_NOT_ELIGIBLE_AT_LAUNCH")

        record.started_mono = self._monotonic()
        record.started_at = self._wall_clock()
        self._move(record, ExecutionState.STARTED)

        # Decide whether to execute live Modal sandbox or safe mock
        is_live = (
            self._config.sandbox_enabled
            and not self._config.mock_mode
            and self._config.execution_mode == "modal"
        )

        if not is_live:
            # Safe mock mode: no cloud calls, no remote resources, no local untrusted execution
            self._move(record, ExecutionState.RUNNING)
            return self._snapshot(record)

        # Enforce strict safety authorization gate before any remote cloud API call:
        if os.environ.get("AIK_MODAL_LIVE_EXECUTION_AUTHORIZED") != "1":
            self._fail_and_kill(
                record,
                FailureClass.INFRASTRUCTURE_FAILURE,
                "LIVE_MODAL_EXECUTION_NOT_AUTHORIZED",
            )
            raise ModalRuntimeError(
                "LIVE_MODAL_EXECUTION_NOT_AUTHORIZED: Remote Modal sandbox execution requires "
                "AIK_MODAL_LIVE_EXECUTION_AUTHORIZED=1. Keep SANDBOX_ENABLED=False and "
                "WORKER_EXECUTION_MODE=mock by default."
            )

        # Live Modal Sandbox Execution
        try:
            import modal  # noqa: F401
        except ImportError as exc:
            self._fail_and_kill(
                record, FailureClass.INFRASTRUCTURE_FAILURE, "MODAL_SDK_NOT_INSTALLED"
            )
            raise ModalRuntimeError("MODAL_SDK_NOT_INSTALLED") from exc

        try:
            app = modal.App.lookup(self._config.app_name, create_if_missing=True)
            # Build container image with the contracted runtime libraries and staged files.
            # The participant runtime contract promises python-chess and numpy; without
            # them every agent dies on `import chess` before it can answer `hello`.
            image = (
                modal.Image.debian_slim(python_version=self._config.python_version)
                .pip_install(*MODAL_RUNTIME_PACKAGES)
                .add_local_dir(str(record.temp_dir), remote_path="/opt/aik/artifact")
            )
            cpu_cores = max(0.1, min(4.0, record.policy.cpu_millicores / 1000.0))
            mem_mib = max(128, min(4096, record.policy.memory_bytes // (1024 * 1024)))
            timeout_sec = max(1, min(1800, record.policy.total_timeout_ms // 1000))

            record.sandbox = modal.Sandbox.create(
                "python3",
                "/opt/aik/artifact/_launcher.py",
                record.entrypoint,
                app=app,
                image=image,
                cpu=cpu_cores,
                memory=mem_mib,
                timeout=timeout_sec,
                block_network=self._config.block_network,
            )
            # The startup budget covers the agent answering `hello`, not Modal image
            # build / cold boot inside Sandbox.create(), so restart the clock here.
            record.started_mono = self._monotonic()
            # Enter RUNNING before the watcher threads start, so an agent that exits
            # instantly is recorded as CRASHED instead of racing this transition.
            self._move(record, ExecutionState.RUNNING)

            # Start continuous background drain threads for stdout and stderr
            if hasattr(record.sandbox, "stdout"):
                thread_out = threading.Thread(
                    target=self._drain_stream,
                    args=(record, record.sandbox.stdout, True),
                    daemon=True,
                )
                record.reader_threads.append(thread_out)
                thread_out.start()

            if hasattr(record.sandbox, "stderr"):
                thread_err = threading.Thread(
                    target=self._drain_stream,
                    args=(record, record.sandbox.stderr, False),
                    daemon=True,
                )
                record.reader_threads.append(thread_err)
                thread_err.start()

            threading.Thread(target=self._watch_modal_exit, args=(record,), daemon=True).start()
            threading.Thread(
                target=self._watch_fence_and_deadline, args=(record,), daemon=True
            ).start()

            return self._snapshot(record)

        except Exception as exc:
            self._fail_and_kill(
                record,
                FailureClass.INFRASTRUCTURE_FAILURE,
                f"MODAL_SPAWN_FAILED:{type(exc).__name__}",
            )
            raise ModalRuntimeError(f"MODAL_SPAWN_FAILED:{type(exc).__name__}") from exc

    def send_input(self, execution_id: uuid.UUID, message: AIKMessage) -> None:
        record = self._record(execution_id)
        self._guard(record)

        if record.state not in {ExecutionState.STARTED, ExecutionState.RUNNING}:
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
            parsed = json.loads(message.payload[:-1].decode("utf-8"))
            payload_type = parsed.get("type") if isinstance(parsed, dict) else None
        except (UnicodeDecodeError, json.JSONDecodeError, AttributeError):
            payload_type = None

        record.stdin_total += len(message.payload)
        if record.stdin_total > record.policy.stdout_bytes:
            self._fail_and_kill(record, FailureClass.PROTOCOL_VIOLATION, "STDIN_LIMIT_EXCEEDED")
            raise AIKProtocolError("STDIN_LIMIT_EXCEEDED")

        # Mock Mode handler: synthesize valid protocol responses
        if record.sandbox is None:
            self._handle_mock_input(record, message, payload_type)
            record.state = ExecutionState.RUNNING
            record.last_input_at = self._monotonic() if payload_type == "go" else None
            return

        # Live Modal Sandbox stdin write
        try:
            stdin = getattr(record.sandbox, "stdin", None)
            if stdin is None:
                raise RuntimeError("MODAL_STDIN_UNAVAILABLE")
            # Modal's StreamWriter.write() only buffers; drain() actually sends the bytes.
            stdin.write(message.payload)
            stdin.drain()
        except Exception as exc:
            self._fail_and_kill(record, FailureClass.PARTICIPANT_PROCESS_FAILURE, "PARTICIPANT_EOF")
            raise AIKProtocolError("PARTICIPANT_EOF") from exc

        record.state = ExecutionState.RUNNING
        record.last_input_at = self._monotonic() if payload_type == "go" else None
        if payload_type == "hello" and record.hello_sent_mono is None:
            # The ready deadline runs from `hello`, not from sandbox creation: the controller
            # sends `hello` only once BOTH sides' sandboxes are up, so measuring from creation
            # charges this agent for the opponent's (or a second worker's) cold start.
            record.hello_sent_mono = self._monotonic()

    def _handle_mock_input(
        self, record: _ModalExecution, message: AIKMessage, payload_type: str | None
    ) -> None:
        """Generate standard protocol replies in safe mock/offline mode."""
        req_id = message.message_id
        if record.mock_responses and req_id in record.mock_responses:
            record.output.put_nowait(record.mock_responses[req_id])
            return

        resp_dict: dict[str, Any] | None = None
        if payload_type == "hello":
            resp_dict = {"protocol_version": "aik-jsonl/1", "type": "ready", "request_id": req_id}
            record.participant_ready = True
        elif payload_type in {"new_game", "position"}:
            resp_dict = {"protocol_version": "aik-jsonl/1", "type": "ack", "request_id": req_id}
            if payload_type == "position":
                try:
                    msg_payload = json.loads(message.payload[:-1].decode("utf-8"))
                    fen = msg_payload.get("fen")
                    if fen:
                        record.current_fen = fen
                except Exception:
                    pass
        elif payload_type == "go":
            move = "e2e4"
            current_fen = getattr(record, "current_fen", None)
            if current_fen:
                try:
                    import chess
                    b = chess.Board(current_fen)
                    legal = sorted([m.uci() for m in b.legal_moves])
                    if legal:
                        move = legal[0]
                except Exception:
                    pass
            resp_dict = {
                "protocol_version": "aik-jsonl/1",
                "type": "bestmove",
                "request_id": req_id,
                "move": move,
            }
        elif payload_type in {"game_over", "shutdown"}:
            resp_dict = {"protocol_version": "aik-jsonl/1", "type": "ack", "request_id": req_id}

        if resp_dict is not None:
            payload = json.dumps(resp_dict, separators=(",", ":")).encode("utf-8") + b"\n"
            record.stdout_total += len(payload)
            if record.stdout_evidence is not None:
                record.stdout_evidence.extend(payload[: max(0, 4096 - len(record.stdout_evidence))])
            record.output.put_nowait(AIKMessage(req_id, payload))

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

        # Check timeouts
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
        self._kill_sandbox(record)
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
            resource_usage={},
            unavailable_measurements=(
                "cpu_millicores",
                "memory_bytes",
                "process_count",
                "filesystem_bytes",
                "filesystem_inodes",
                "file_descriptors",
            ),
            measurement_source="modal-sdk",
            measurement_scope="remote-cloud-sandbox",
            measurement_container_id=f"modal-{execution_id.hex}",
            measurement_started_monotonic=record.started_mono,
            measurement_ended_monotonic=self._monotonic() if record.ended_at else None,
            subject_kind=record.identity.subject_kind,
            house_ai_id=record.identity.house_ai_id,
            house_ai_version=record.identity.house_ai_version,
            match_id=record.identity.match_id,
            worker_id=record.identity.worker_id,
            lease_id=record.identity.lease_id,
            fencing_token=record.identity.fencing_token,
            policy_digest=record.identity.policy_digest,
            runtime_identity=f"modal/gvisor;python/{self._config.python_version}",
            container_id=f"modal-{execution_id.hex}",
            started_at=record.started_at,
            ended_at=record.ended_at,
            termination_reason=record.termination_reason,
            stdout_evidence=bytes(record.stdout_evidence or b""),
            stderr_evidence=bytes(record.stderr_evidence or b""),
        )

    def verify_cleanup(self, execution_id: uuid.UUID) -> CleanupResult:
        record = self._record(execution_id)
        if record.state not in _TERMINAL | {
            ExecutionState.TERMINATED,
            ExecutionState.CLEANUP_FAILED,
            ExecutionState.CLEANUP_UNKNOWN,
        }:
            raise InvalidTransitionError("CLEANUP_REQUIRES_TERMINAL_STATE")

        kill_ok = self._kill_sandbox(record)

        # Remove local staging directory
        staging_removed = True
        if record.temp_dir and record.temp_dir.exists():
            try:
                shutil.rmtree(record.temp_dir, ignore_errors=True)
            except Exception:
                staging_removed = False
            if record.temp_dir.exists():
                staging_removed = False

        if not kill_ok or not staging_removed:
            record.cleanup = CleanupResult.FAILED
            record.state = ExecutionState.CLEANUP_FAILED
            return CleanupResult.FAILED

        record.cleanup = CleanupResult.VERIFIED
        return CleanupResult.VERIFIED

    def destroy(self, execution_id: uuid.UUID) -> SandboxSnapshot:
        record = self._record(execution_id)
        if record.cleanup != CleanupResult.VERIFIED:
            raise InvalidTransitionError("CLEANUP_NOT_VERIFIED")

        self._move(record, ExecutionState.DESTROYED)
        with self._lock:
            self._records.pop(execution_id, None)
        return self._snapshot(record)

    def _fail_and_kill(self, record: _ModalExecution, failure: FailureClass, reason: str) -> None:
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
        self._kill_sandbox(record)

    def _kill_sandbox(self, record: _ModalExecution) -> bool:
        success = True
        if record.sandbox is not None:
            try:
                record.sandbox.terminate()
            except Exception:
                success = False
            record.sandbox = None
        return success

    def _drain_stream(self, record: _ModalExecution, stream: Any, is_stdout: bool) -> None:
        """Stream reader partitioner for Modal Sandbox stdout/stderr."""
        pending = bytearray()
        try:
            # Modal's StreamReader.read() takes no size and blocks until EOF, so it can
            # never deliver a reply mid-game. Iterating yields chunks as they arrive.
            for chunk in stream:
                if not chunk:
                    continue
                if isinstance(chunk, str):
                    chunk = chunk.encode("utf-8")

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
                                    parsed.get("request_id", "")
                                    if isinstance(parsed, dict)
                                    else ""
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
        except Exception:
            self._fail_and_kill(record, FailureClass.INFRASTRUCTURE_FAILURE, "STREAM_READ_FAILED")

    def _watch_modal_exit(self, record: _ModalExecution) -> None:
        try:
            wait_fn = getattr(record.sandbox, "wait", None)
            if wait_fn is not None:
                wait_fn()
            code = getattr(record.sandbox, "returncode", None)
            if code is None:
                # Unknown exit status must never be reported as a clean exit.
                code = -1
        except Exception:
            code = -1

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

    def _watch_fence_and_deadline(self, record: _ModalExecution) -> None:
        while record.sandbox is not None:
            try:
                poll_fn = getattr(record.sandbox, "poll", None)
                if poll_fn is not None and poll_fn() is not None:
                    break
            except Exception:
                break
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
                and record.hello_sent_mono is not None
                and self._monotonic() - record.hello_sent_mono
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
