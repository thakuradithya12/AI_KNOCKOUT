"""Trusted worker-side orchestration for explicitly leased Kata match jobs.

The agent is deliberately not an eligibility or chess authority. It accepts only
the immutable JobOut returned over the mTLS gateway, obtains the exact artifact
through its scoped capability, and delegates process isolation to KataSandboxAdapter.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from app.matches.kata_participant import KataMatchParticipant
from app.models.match import MatchPurpose
from app.models.worker import ExecutionFailureClass
from app.sandbox.contract import (
    CleanupResult,
    ExecutionIdentity,
    ExecutionState,
    ExecutionSubjectKind,
    ExpiredLeaseError,
    NetworkMode,
    SandboxAdapter,
    SandboxPolicy,
    StaleFenceError,
    WritableStoragePolicy,
)
from app.sandbox.kata import KataRuntimeError, KataSandboxAdapter
from app.workers.schemas import EvidenceEvent, JobOut
from app.workers.tls import WorkerClientTLSConfig

LEASE_RENEW_SECONDS = 8
LEASE_SAFETY_SECONDS = 2
MAX_ARTIFACT_BYTES = 32 * 1024 * 1024


class WorkerAgentError(RuntimeError):
    """Stable, non-sensitive worker-agent failure."""


class WorkerGatewayClient:
    """Small strict mTLS client for the existing worker protocol."""

    def __init__(self, base_url: str, tls: WorkerClientTLSConfig, *, timeout: float = 5.0):
        parsed = urllib.parse.urlsplit(base_url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.hostname != tls.server_name
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or not 0 < timeout <= 30
        ):
            raise ValueError("WORKER_GATEWAY_ENDPOINT_INVALID")
        self.base_url = base_url.rstrip("/")
        self.worker_id = tls.worker_id
        context = tls.build_ssl_context()
        self._opener = urllib.request.build_opener(
            urllib.request.HTTPSHandler(context=context)
        )
        self._timeout = timeout

    def _request(self, method: str, path: str, *, body: dict | None = None,
                 headers: dict[str, str] | None = None) -> tuple[bytes, dict[str, str]]:
        data = None if body is None else json.dumps(
            body, separators=(",", ":"), allow_nan=False
        ).encode("utf-8")
        request_headers = {"Accept": "application/json", **(headers or {})}
        if data is not None:
            request_headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            self.base_url + path, data=data, headers=request_headers, method=method
        )
        try:
            with self._opener.open(request, timeout=self._timeout) as response:
                payload = response.read(MAX_ARTIFACT_BYTES + 1)
                if len(payload) > MAX_ARTIFACT_BYTES:
                    raise WorkerAgentError("ARTIFACT_RESPONSE_TOO_LARGE")
                return payload, {key.lower(): value for key, value in response.headers.items()}
        except urllib.error.HTTPError as exc:
            # Do not include server response bodies; they may evolve to contain secrets.
            raise WorkerAgentError(f"WORKER_GATEWAY_HTTP_{exc.code}") from None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise WorkerAgentError("WORKER_GATEWAY_UNAVAILABLE") from exc

    def _json(self, method: str, path: str, body: dict | None = None) -> dict:
        raw, _ = self._request(method, path, body=body)
        try:
            value = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise WorkerAgentError("WORKER_GATEWAY_RESPONSE_INVALID") from exc
        if not isinstance(value, dict):
            raise WorkerAgentError("WORKER_GATEWAY_RESPONSE_INVALID")
        return value

    def claim(self) -> JobOut | None:
        raw, _ = self._request("POST", "/worker/v1/jobs/claim")
        try:
            data = json.loads(raw)
            return None if data is None else JobOut.model_validate(data)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
            raise WorkerAgentError("WORKER_JOB_RESPONSE_INVALID") from exc

    def operation(self, path: str, job: JobOut, sequence: int, **extra) -> dict:
        return self._json("POST", path, {
            "lease_id": str(job.lease.lease_id),
            "fencing_token": job.lease.fencing_token,
            "sequence": sequence,
            **extra,
        })

    def capability(self, job: JobOut, sequence: int) -> str:
        value = self.operation(
            f"/worker/v1/jobs/{job.execution_id}/artifact-capability", job, sequence
        )
        token = value.get("capability")
        if not isinstance(token, str) or len(token) < 32:
            raise WorkerAgentError("ARTIFACT_CAPABILITY_RESPONSE_INVALID")
        return token

    def artifact(self, job: JobOut, sequence: int, capability: str) -> bytes:
        query = urllib.parse.urlencode({
            "lease_id": str(job.lease.lease_id),
            "fencing_token": job.lease.fencing_token,
            "sequence": sequence,
        })
        data, headers = self._request(
            "GET", f"/worker/v1/jobs/{job.execution_id}/artifact?{query}",
            headers={"X-Artifact-Capability": capability},
        )
        if headers.get("x-artifact-sha256") != job.artifact_sha256:
            raise WorkerAgentError("ARTIFACT_DIGEST_HEADER_MISMATCH")
        if hashlib.sha256(data).hexdigest() != job.artifact_sha256:
            raise WorkerAgentError("ARTIFACT_DIGEST_MISMATCH")
        return data

    def next_match_command(self, job: JobOut) -> dict | None:
        """Fetch opaque AIK work from the authenticated server control path."""
        return self._json("POST", f"/worker/v1/jobs/{job.execution_id}/match/next", {
            "lease_id": str(job.lease.lease_id),
            "fencing_token": job.lease.fencing_token,
            "attempt": job.attempt_number,
        })

    def submit_match_response(self, job: JobOut, command: dict, payload: bytes) -> dict:
        return self._json("POST", f"/worker/v1/jobs/{job.execution_id}/match/response", {
            "lease_id": str(job.lease.lease_id),
            "fencing_token": job.lease.fencing_token,
            "attempt": job.attempt_number,
            "command_id": command["command_id"],
            "payload": payload.decode("utf-8", errors="strict"),
        })


@dataclass
class _LeaseState:
    expires_monotonic: float
    sequence: int
    revoked: bool = False


class RenewedLeaseFenceAuthority:
    """Fence guard backed by successful serialized server lease renewals."""

    def __init__(self, identity: ExecutionIdentity, state: _LeaseState, lock: threading.RLock,
                 monotonic: Callable[[], float] = time.monotonic):
        self.identity = identity
        self.state = state
        self.lock = lock
        self.monotonic = monotonic

    def assert_current(self, identity: ExecutionIdentity, now: datetime) -> None:
        with self.lock:
            if identity != self.identity or self.state.revoked:
                raise StaleFenceError("WORKER_FENCE_STALE")
            if self.monotonic() >= self.state.expires_monotonic - LEASE_SAFETY_SECONDS:
                raise ExpiredLeaseError("WORKER_LEASE_EXPIRED_OR_UNCONFIRMED")


class WorkerAgent:
    """Claim and run one KATA_AI_EXECUTION through the existing production adapter."""

    def __init__(
        self,
        *,
        client: WorkerGatewayClient,
        adapter_factory: Callable[[RenewedLeaseFenceAuthority], SandboxAdapter],
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        self.client = client
        self.adapter_factory = adapter_factory
        self.monotonic = monotonic
        self.wall_clock = wall_clock

    def run_once(self) -> bool:
        job = self.client.claim()
        if job is None:
            return False
        self.execute(job)
        return True

    def execute(self, job: JobOut) -> None:
        if job.worker_id != self.client.worker_id:
            raise WorkerAgentError("JOB_WORKER_IDENTITY_MISMATCH")
        if job.execution_mode not in {"KATA_AI_EXECUTION", "MODAL_AI_EXECUTION"} or job.match_id is None:
            raise WorkerAgentError("JOB_MODE_UNSUPPORTED")
        if job.match_side_id is None:
            raise WorkerAgentError("JOB_MATCH_BINDING_INCOMPLETE")
        if job.match_purpose not in {
            MatchPurpose.DEVELOPMENT.value,
            MatchPurpose.OFFICIAL.value,
            MatchPurpose.PEER_TEST.value,
        }:
            raise WorkerAgentError("JOB_PURPOSE_UNSUPPORTED")
        if (
            (job.match_purpose == MatchPurpose.DEVELOPMENT.value and job.policy_digest is None)
            or job.protocol_version != "aik-jsonl/1"
        ):
            raise WorkerAgentError("JOB_POLICY_OR_PROTOCOL_INVALID")
        if job.runtime_profile != "python-3.12":
            raise WorkerAgentError("JOB_RUNTIME_PROFILE_UNSUPPORTED")
        self._check_subject(job)
        sequence = job.lease.next_sequence
        base = f"/worker/v1/jobs/{job.execution_id}"
        self.client.operation(
            f"{base}/acknowledgement", job, sequence, accepted=True
        )
        sequence += 1
        token = self.client.capability(job, sequence)
        sequence += 1
        artifact = self.client.artifact(job, sequence, token)
        sequence += 1
        if len(artifact) > MAX_ARTIFACT_BYTES:
            raise WorkerAgentError("ARTIFACT_RESPONSE_TOO_LARGE")

        identity = ExecutionIdentity(
            execution_id=job.execution_id,
            submission_version_id=job.submission_version_id,
            artifact_digest=job.artifact_sha256,
            policy_profile=job.policy_digest,
            runtime_profile=job.runtime_profile,
            worker_id=job.worker_id,
            lease_id=job.lease.lease_id,
            fencing_token=job.lease.fencing_token,
            protocol_version=job.protocol_version,
            lease_expires_at=job.lease.expires_at,
            subject_kind=ExecutionSubjectKind(job.execution_kind),
            house_ai_id=job.house_ai_id,
            house_ai_version=job.house_ai_version,
            match_id=job.match_id,
            policy_digest=job.policy_digest,
        )
        # Public resource schema names are translated once at the adapter boundary.
        resource = job.resource_policy.model_dump(mode="python")
        policy = SandboxPolicy(
            cpu_millicores=resource["cpu_millicores"],
            memory_bytes=resource["memory_bytes"],
            pid_limit=resource["pid_limit"],
            filesystem_bytes=resource["tmpfs_bytes"],
            filesystem_inodes=resource["inode_limit"],
            file_descriptors=resource["nofile_limit"],
            stdout_bytes=resource["stdout_total_bytes"],
            stderr_bytes=resource["stderr_total_bytes"],
            startup_timeout_ms=resource["startup_timeout_ms"],
            move_timeout_ms=resource["move_timeout_ms"],
            total_timeout_ms=resource["process_lifetime_ms"],
            network_mode=NetworkMode.DENY_ALL,
            writable_storage=WritableStoragePolicy.PRIVATE_TMPFS,
        )
        sandbox_policy_digest = hashlib.sha256(
            json.dumps(
                policy.model_dump(mode="json"), sort_keys=True, separators=(",", ":")
            ).encode("utf-8")
        ).hexdigest()
        identity = replace(
            identity,
            sandbox_policy_digest=sandbox_policy_digest,
            policy_digest=job.policy_digest or sandbox_policy_digest,
        )
        lease_state = _LeaseState(
            expires_monotonic=self.monotonic() + max(
                0, (job.lease.expires_at - self.wall_clock()).total_seconds()
            ),
            sequence=sequence,
        )
        operation_lock = threading.RLock()
        fence = RenewedLeaseFenceAuthority(identity, lease_state, operation_lock, self.monotonic)
        adapter = self.adapter_factory(fence)
        bridge = None
        terminal_error: Exception | None = None
        renew_stop = threading.Event()
        renew_thread = threading.Thread(
            target=self._renew_loop,
            args=(job, lease_state, operation_lock, renew_stop),
            name=f"aik-lease-{job.execution_id.hex[:8]}",
            daemon=True,
        )
        cleanup_result = CleanupResult.UNKNOWN
        failure_class: ExecutionFailureClass | None = None
        resource_evidence = None
        renew_thread.start()
        try:
            adapter.create_execution(identity, policy)
            staged = adapter.stage_artifact(
                job.execution_id,
                artifact,
                job.submission_version_id,
                job.artifact_sha256,
                house_ai_id=job.house_ai_id,
                house_ai_version=job.house_ai_version,
            )
            if staged.digest != job.artifact_sha256:
                raise WorkerAgentError("STAGED_ARTIFACT_DIGEST_MISMATCH")
            adapter.start(job.execution_id)
            bridge = KataMatchParticipant(
                adapter=adapter,
                execution_id=job.execution_id,
                monotonic=self.monotonic,
            )
            deadline = self.monotonic() + policy.total_timeout_ms / 1000
            while self.monotonic() < deadline:
                fence.assert_current(identity, self.wall_clock())
                command = self.client.next_match_command(job)
                if command is None or command.get("status") in {"IDLE", "TERMINAL"}:
                    if command and command.get("status") == "TERMINAL":
                        break
                    time.sleep(0.01)
                    continue
                if command.get("status") != "ACCEPTED" or not isinstance(
                    command.get("message"), str
                ):
                    raise WorkerAgentError("MATCH_CONTROL_COMMAND_INVALID")
                response = bridge.exchange(
                    command["command_id"], command["message"].encode("utf-8"),
                    timeout=max(0.1, min(300.0, command.get("timeout_seconds", 30.0))),
                )
                self.client.submit_match_response(job, command, response.payload)
            else:
                raise WorkerAgentError("MATCH_EXECUTION_DEADLINE_EXCEEDED")
        except Exception as exc:
            terminal_error = exc
            failure_class = failure_class or self._failure_class(exc)
            if bridge is not None:
                try:
                    bridge.terminate()
                except Exception:
                    pass
            else:
                try:
                    adapter.terminate(job.execution_id)
                except Exception:
                    pass
        finally:
            renew_stop.set()
            if renew_thread.ident is not None:
                renew_thread.join(timeout=2)
            try:
                snapshot = adapter.inspect(job.execution_id)
                if snapshot.state not in {
                    ExecutionState.COMPLETED, ExecutionState.TIMED_OUT,
                    ExecutionState.RESOURCE_EXCEEDED, ExecutionState.PROTOCOL_FAILED,
                    ExecutionState.CRASHED, ExecutionState.TERMINATED,
                    ExecutionState.CLEANUP_FAILED, ExecutionState.CLEANUP_UNKNOWN,
                    ExecutionState.DESTROYED,
                }:
                    adapter.terminate(job.execution_id)
                resource_verifier = getattr(adapter, "verify_resource_evidence", None)
                if callable(resource_verifier):
                    resource_evidence = adapter.collect_evidence(job.execution_id)
                    try:
                        resource_verifier(job.execution_id)
                    except Exception:
                        if terminal_error is None:
                            terminal_error = WorkerAgentError(
                                "REQUIRED_RESOURCE_EVIDENCE_UNAVAILABLE"
                            )
                        failure_class = (
                            failure_class or ExecutionFailureClass.INFRASTRUCTURE_FAILURE
                        )
                cleanup_result = adapter.verify_cleanup(job.execution_id)
                if cleanup_result == CleanupResult.VERIFIED:
                    adapter.destroy(job.execution_id)
            except Exception:
                cleanup_result = CleanupResult.FAILED
                if terminal_error is None:
                    terminal_error = WorkerAgentError("SANDBOX_CLEANUP_FAILED")
                    failure_class = ExecutionFailureClass.INFRASTRUCTURE_FAILURE
            try:
                self._report_evidence(
                    job, lease_state, operation_lock, adapter, cleanup_result,
                    sequence, terminal_error, failure_class, resource_evidence,
                )
                with operation_lock:
                    sequence = lease_state.sequence
                    self.client.operation(
                        f"{base}/cleanup",
                        job,
                        sequence,
                        cleanup_verified=cleanup_result == CleanupResult.VERIFIED,
                        final_status=(
                            "COMPLETED"
                            if terminal_error is None and cleanup_result == CleanupResult.VERIFIED
                            else "FAILED"
                        ),
                        failure_class=failure_class.value if failure_class else None,
                    )
            except Exception as report_error:
                if terminal_error is None:
                    terminal_error = WorkerAgentError("WORKER_TERMINAL_REPORT_FAILED")
                else:
                    terminal_error.add_note(type(report_error).__name__)
        if terminal_error is not None:
            raise WorkerAgentError(self._safe_error(terminal_error)) from terminal_error

    def _renew_loop(self, job: JobOut, state: _LeaseState, lock: threading.RLock,
                    stop: threading.Event) -> None:
        base = f"/worker/v1/jobs/{job.execution_id}/lease/renewal"
        while not stop.wait(LEASE_RENEW_SECONDS):
            try:
                with lock:
                    result = self.client.operation(base, job, state.sequence)
                    expires = datetime.fromisoformat(result["expires_at"].replace("Z", "+00:00"))
                    ttl = (expires - self.wall_clock()).total_seconds()
                    if ttl <= LEASE_SAFETY_SECONDS:
                        raise WorkerAgentError("WORKER_LEASE_RENEWAL_TOO_SHORT")
                    state.expires_monotonic = self.monotonic() + ttl
                    state.sequence = int(result["next_sequence"])
            except Exception:
                with lock:
                    state.revoked = True
                return

    def _report_evidence(self, job: JobOut, state: _LeaseState, lock: threading.RLock,
                         adapter: SandboxAdapter, cleanup: CleanupResult,
                         initial_sequence: int, error: Exception | None,
                         failure: ExecutionFailureClass | None,
                         evidence=None) -> None:
        try:
            evidence = evidence or adapter.collect_evidence(job.execution_id)
        except Exception:
            return
        event = EvidenceEvent.FAILED if error else EvidenceEvent.ARTIFACT_VERIFIED
        data = {
            "event_type": event.value,
            "started_at": evidence.started_at.isoformat() if evidence.started_at else None,
            "ended_at": evidence.ended_at.isoformat() if evidence.ended_at else None,
            "exit_code": None,
            "failure_class": failure.value if failure else None,
            "termination_reason": (evidence.termination_reason or "UNKNOWN")[:64],
            "resource_usage": evidence.resource_usage,
            "resource_measurement_source": evidence.measurement_source,
            "resource_measurement_scope": evidence.measurement_scope,
            "resource_measurement_container_id": evidence.measurement_container_id,
            "resource_measurement_started_monotonic": evidence.measurement_started_monotonic,
            "resource_measurement_ended_monotonic": evidence.measurement_ended_monotonic,
            "unavailable_measurements": list(evidence.unavailable_measurements),
            "measurement_error": evidence.measurement_error,
            "artifact_digest_verified": True,
            "stdout_bytes": min(evidence.stdout_bytes, 1_048_576),
            "stdout_sha256": hashlib.sha256(evidence.stdout_evidence).hexdigest(),
            "stderr_bytes": min(evidence.stderr_bytes, 262_144),
            "stderr_sha256": hashlib.sha256(evidence.stderr_evidence).hexdigest(),
            "protocol_violation": None,
            "sandbox_violation": None,
            "lease_id": str(job.lease.lease_id),
            "fencing_token": job.lease.fencing_token,
        }
        with lock:
            sequence = state.sequence
            self.client.operation(
                f"/worker/v1/jobs/{job.execution_id}/evidence", job, sequence, **{
                    key: value for key, value in data.items()
                    if key not in {"lease_id", "fencing_token"}
                }
            )
            state.sequence += 1

    @staticmethod
    def _check_subject(job: JobOut) -> None:
        participant = (
            job.execution_kind == "PARTICIPANT"
            and job.submission_version_id is not None
            and job.participant_id is not None
            and job.submission_id is not None
            and job.house_ai_id is None
            and job.house_ai_version is None
        )
        house = (
            job.execution_kind == "HOUSE_AI"
            and job.submission_version_id is None
            and job.participant_id is None
            and job.submission_id is None
            and bool(job.house_ai_id)
            and (job.house_ai_version or 0) > 0
        )
        if not participant and not house:
            raise WorkerAgentError("JOB_SUBJECT_IDENTITY_INVALID")

    @staticmethod
    def _failure_class(exc: Exception) -> ExecutionFailureClass:
        from app.sandbox.contract import (
            AIKProtocolError,
            ExpiredLeaseError,
            IdentityMismatchError,
            StaleFenceError,
        )

        if isinstance(exc, (StaleFenceError, ExpiredLeaseError)):
            return ExecutionFailureClass.STALE_EXECUTION
        if isinstance(exc, AIKProtocolError):
            return ExecutionFailureClass.PROTOCOL_VIOLATION
        if isinstance(exc, (KataRuntimeError, IdentityMismatchError)):
            return ExecutionFailureClass.INFRASTRUCTURE_FAILURE
        return ExecutionFailureClass.PARTICIPANT_PROCESS_FAILURE

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        if isinstance(exc, WorkerAgentError):
            return str(exc)[:96]
        return "WORKER_EXECUTION_FAILED"


def build_sandbox_adapter_factory(
    backend: str | None = None,
) -> Callable[[RenewedLeaseFenceAuthority], SandboxAdapter]:
    """Return an adapter factory for the configured sandbox backend ('modal', 'kata', 'mock')."""
    import os

    selected = (backend or os.environ.get("WORKER_SANDBOX_BACKEND", "mock")).strip().lower()
    if selected == "modal":
        from app.sandbox.modal import ModalRuntimeConfig, ModalSandboxAdapter

        cfg = ModalRuntimeConfig.from_env()

        def _modal_factory(fence: RenewedLeaseFenceAuthority) -> SandboxAdapter:
            return ModalSandboxAdapter(authority=fence, config=cfg)

        return _modal_factory
    elif selected == "kata":
        from pathlib import Path
        from app.sandbox.kata import KataRuntimeConfig, KataSandboxAdapter

        cfg = KataRuntimeConfig(
            ctr_binary=Path(os.environ.get("CTR_BINARY", "/usr/bin/ctr")),
            containerd_address=Path(os.environ.get("CONTAINERD_ADDRESS", "/run/containerd/containerd.sock")),
            namespace=os.environ.get("KATA_NAMESPACE", "aik-workers"),
            base_image=os.environ["KATA_BASE_IMAGE"],
            base_manifest_digest=os.environ["KATA_BASE_MANIFEST_DIGEST"],
            runtime=os.environ.get("KATA_RUNTIME", "io.containerd.kata.v2"),
            runtime_version=os.environ.get("KATA_RUNTIME_VERSION", "4.2.0"),
            ctr_version=os.environ.get("CTR_VERSION", "2.2.1"),
            cgroup_parent=Path(os.environ["KATA_CGROUP_PARENT"]) if os.environ.get("KATA_CGROUP_PARENT") else None,
            staging_parent=Path(os.environ["KATA_STAGING_PARENT"]) if os.environ.get("KATA_STAGING_PARENT") else None,
        )

        def _kata_factory(fence: RenewedLeaseFenceAuthority) -> SandboxAdapter:
            return KataSandboxAdapter(authority=fence, config=cfg)

        return _kata_factory
    elif selected == "mock":
        from app.sandbox.modal import ModalRuntimeConfig, ModalSandboxAdapter

        mock_cfg = ModalRuntimeConfig(mock_mode=True, sandbox_enabled=False, execution_mode="mock")

        def _mock_factory(fence: RenewedLeaseFenceAuthority) -> SandboxAdapter:
            return ModalSandboxAdapter(authority=fence, config=mock_cfg)

        return _mock_factory
    else:
        raise WorkerAgentError(f"UNKNOWN_SANDBOX_BACKEND: {selected}")


def run_agent(
    *,
    client: WorkerGatewayClient | None = None,
    adapter_factory: Callable[[RenewedLeaseFenceAuthority], SandboxAdapter] | None = None,
    poll_interval: float = 1.0,
    stop_event: threading.Event | None = None,
    max_jobs: int | None = None,
) -> int:
    """Run worker agent polling loop until stopped or max_jobs processed."""
    import os

    # Execution-mode fail-closed guard (Gap 3).
    # Production backends (kata, modal) require SANDBOX_ENABLED=true to be
    # explicitly set. The mock backend requires SANDBOX_ENABLED=false (or unset).
    # Any contradictory combination is rejected here — before a job is claimed —
    # so the process exits with a clear error rather than silently misbehaving.
    sandbox_backend = (os.environ.get("WORKER_SANDBOX_BACKEND", "mock")).strip().lower()
    sandbox_enabled_raw = os.environ.get("SANDBOX_ENABLED", "false").strip().lower()
    sandbox_enabled = sandbox_enabled_raw in {"1", "true", "yes"}

    if sandbox_backend == "mock" and sandbox_enabled:
        raise WorkerAgentError(
            "EXECUTION_MODE_CONFLICT: WORKER_SANDBOX_BACKEND=mock but SANDBOX_ENABLED=true. "
            "Mock mode must not run with sandbox enabled — this would produce results that "
            "appear genuine. Set SANDBOX_ENABLED=false for mock, or use a real backend."
        )
    if sandbox_backend in {"kata", "modal"} and not sandbox_enabled:
        raise WorkerAgentError(
            f"EXECUTION_MODE_CONFLICT: WORKER_SANDBOX_BACKEND={sandbox_backend} but "
            "SANDBOX_ENABLED is not 'true'. Real execution backends require an explicit "
            "SANDBOX_ENABLED=true acknowledgement after all sandbox release gates pass."
        )

    if client is None:
        gateway_url = os.environ.get("WORKER_GATEWAY_URL")
        if not gateway_url:
            raise WorkerAgentError("WORKER_GATEWAY_URL_REQUIRED")
        tls_config = WorkerClientTLSConfig.from_env()
        client = WorkerGatewayClient(base_url=gateway_url, tls=tls_config)

    if adapter_factory is None:
        adapter_factory = build_sandbox_adapter_factory()

    agent = WorkerAgent(client=client, adapter_factory=adapter_factory)
    processed = 0
    stop = stop_event or threading.Event()

    while not stop.is_set():
        if max_jobs is not None and processed >= max_jobs:
            break
        try:
            executed = agent.run_once()
            if executed:
                processed += 1
            else:
                stop.wait(poll_interval)
        except Exception:
            stop.wait(poll_interval)

    return processed


def main() -> None:
    import signal
    import sys

    stop_event = threading.Event()

    def _sig_handler(signum, frame):
        stop_event.set()

    signal.signal(signal.SIGINT, _sig_handler)
    signal.signal(signal.SIGTERM, _sig_handler)

    try:
        run_agent(stop_event=stop_event)
    except Exception as exc:
        sys.stderr.write(f"Worker agent terminated: {exc}\n")
        sys.exit(1)


if __name__ == "__main__":
    main()
