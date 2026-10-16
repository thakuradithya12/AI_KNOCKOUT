"""Live-path tests for ModalSandboxAdapter against a Modal-faithful fake SDK.

Every other ModalSandboxAdapter test runs in mock mode, which never touches the
live stdin/stdout code. These tests install a fake ``modal`` module whose
streams follow the real Modal 1.x semantics, so the live path is exercised
without credentials or cloud calls:

* ``StreamWriter.write()`` only buffers; bytes are delivered on ``drain()``.
  There is no ``flush()``.
* ``StreamReader.read()`` takes no size argument and blocks until EOF.
  Iterating the reader yields chunks as they arrive.
"""

from __future__ import annotations

import hashlib
import json
import queue
import sys
import threading
import time
import types
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from app.matches.kata_participant import KataMatchParticipant
from app.sandbox.contract import (
    AIKMessage,
    ExecutionIdentity,
    ExecutionState,
    ExecutionSubjectKind,
    FailureClass,
    LeaseFenceAuthority,
    LeaseSnapshot,
    NetworkMode,
    SandboxPolicy,
    WritableStoragePolicy,
)
from app.sandbox.modal import MODAL_RUNTIME_PACKAGES, ModalRuntimeConfig, ModalSandboxAdapter
from tests.fixtures.trusted_engine.builder import build_trusted_engine_archive

_EOF = object()
STARTING_FEN = "rnbqkbnr/pppppppp/8/8/8/8/PPPPPPPP/RNBQKBNR w KQkq - 0 1"


class _FakeStreamReader:
    """Modal-like reader: read() blocks until EOF; iteration streams chunks."""

    def __init__(self) -> None:
        self._chunks: queue.Queue = queue.Queue()

    def feed(self, chunk: str) -> None:
        self._chunks.put(chunk)

    def close(self) -> None:
        self._chunks.put(_EOF)

    def read(self) -> str:  # real signature: no size argument
        parts = []
        while (item := self._chunks.get()) is not _EOF:
            parts.append(item)
        return "".join(parts)

    def __iter__(self):
        while (item := self._chunks.get()) is not _EOF:
            yield item


class _FakeStreamWriter:
    """Modal-like writer: write() buffers, drain() delivers. No flush()."""

    def __init__(self, on_line) -> None:
        self._buffer = bytearray()
        self._on_line = on_line
        self.delivered: list[bytes] = []

    def write(self, data: bytes | str) -> None:
        self._buffer.extend(data.encode() if isinstance(data, str) else data)

    def drain(self) -> None:
        data, self._buffer = bytes(self._buffer), bytearray()
        for line in data.splitlines(keepends=True):
            self.delivered.append(line)
            self._on_line(line)

    def write_eof(self) -> None:
        pass


class _FakeSandbox:
    """Answers the aik-jsonl/1 handshake the way a well-behaved agent would."""

    instances: list[_FakeSandbox] = []
    exit_immediately_with: int | None | str = "running"
    silent: bool = False

    def __init__(self, args: tuple, kwargs: dict) -> None:
        self.args = args
        self.kwargs = kwargs
        self.returncode = None
        self._done = threading.Event()
        self.stdout = _FakeStreamReader()
        self.stderr = _FakeStreamReader()
        self.stdin = _FakeStreamWriter(self._handle_line)
        if self.exit_immediately_with != "running":
            self.returncode = self.exit_immediately_with
            self._finish()

    @classmethod
    def create(cls, *args, **kwargs) -> _FakeSandbox:
        box = cls(args, kwargs)
        cls.instances.append(box)
        return box

    def _handle_line(self, line: bytes) -> None:
        if self.silent:
            return
        message = json.loads(line)
        reply_type = {"hello": "ready", "new_game": "ack", "position": "ack"}.get(message["type"])
        if message["type"] == "go":
            reply = {"type": "move", "move": "e2e4"}
        elif reply_type:
            reply = {"type": reply_type}
        else:
            return
        reply.update(protocol_version="aik-jsonl/1", request_id=message["request_id"])
        self.stdout.feed(json.dumps(reply, separators=(",", ":")) + "\n")

    def _finish(self) -> None:
        self._done.set()
        self.stdout.close()
        self.stderr.close()

    def poll(self):
        return self.returncode if self._done.is_set() else None

    def wait(self, raise_on_termination: bool = True) -> None:
        self._done.wait()

    def terminate(self) -> None:
        if not self._done.is_set():
            self.returncode = -9
            self._finish()


class _FakeImage:
    def __init__(self) -> None:
        self.packages: list[str] = []
        self.local_dirs: list[tuple[str, str]] = []

    @classmethod
    def debian_slim(cls, python_version: str | None = None) -> _FakeImage:
        return cls()

    def pip_install(self, *packages: str) -> _FakeImage:
        self.packages.extend(packages)
        return self

    def add_local_dir(self, local_path, remote_path: str, **_: object) -> _FakeImage:
        self.local_dirs.append((str(local_path), remote_path))
        return self


@pytest.fixture
def fake_modal(monkeypatch: pytest.MonkeyPatch):
    _FakeSandbox.instances = []
    _FakeSandbox.exit_immediately_with = "running"
    _FakeSandbox.silent = False
    module = types.ModuleType("modal")
    module.Sandbox = _FakeSandbox
    module.Image = _FakeImage
    module.App = types.SimpleNamespace(lookup=lambda *a, **k: object())
    monkeypatch.setitem(sys.modules, "modal", module)
    monkeypatch.setenv("AIK_MODAL_LIVE_EXECUTION_AUTHORIZED", "1")
    yield module
    for box in _FakeSandbox.instances:
        box.terminate()


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


def _started_live_adapter(monotonic=time.monotonic):
    policy = _policy()
    archive = build_trusted_engine_archive()
    digest = hashlib.sha256(archive).hexdigest()
    policy_digest = hashlib.sha256(
        json.dumps(policy.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    identity = ExecutionIdentity(
        execution_id=uuid.uuid4(),
        submission_version_id=uuid.uuid4(),
        artifact_digest=digest,
        policy_profile="development-v1",
        runtime_profile="python-3.12",
        worker_id="worker-001",
        lease_id=uuid.uuid4(),
        fencing_token=1,
        protocol_version="aik-jsonl/1",
        lease_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
        subject_kind=ExecutionSubjectKind.PARTICIPANT,
        match_id=uuid.uuid4(),
        policy_digest=policy_digest,
        sandbox_policy_digest=policy_digest,
    )
    authority = LeaseFenceAuthority()
    authority.set_current(
        LeaseSnapshot(
            worker_id=identity.worker_id,
            lease_id=identity.lease_id,
            fencing_token=identity.fencing_token,
            expires_at=identity.lease_expires_at,
        )
    )
    adapter = ModalSandboxAdapter(
        authority,
        ModalRuntimeConfig(mock_mode=False, sandbox_enabled=True, execution_mode="modal"),
        monotonic=monotonic,
    )
    adapter.create_execution(identity, policy)
    adapter.stage_artifact(identity.execution_id, archive, identity.submission_version_id, digest)
    adapter.start(identity.execution_id)
    return adapter, identity.execution_id


def _line(request_id: str, **fields) -> bytes:
    body = {"protocol_version": "aik-jsonl/1", "request_id": request_id, **fields}
    return json.dumps(body, separators=(",", ":")).encode() + b"\n"


def _wait_for(predicate, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError("condition not reached")


def test_live_modal_handshake_and_move_round_trip(fake_modal) -> None:
    adapter, execution_id = _started_live_adapter()
    bridge = KataMatchParticipant(adapter=adapter, execution_id=execution_id)

    replies = [
        bridge.exchange("r1", _line("r1", type="hello"), timeout=2),
        bridge.exchange("r2", _line("r2", type="new_game"), timeout=2),
        bridge.exchange("r3", _line("r3", type="position", fen=STARTING_FEN), timeout=2),
        bridge.exchange("r4", _line("r4", type="go", move_budget_ms=1500), timeout=2),
    ]

    assert [json.loads(r.payload)["type"] for r in replies] == ["ready", "ack", "ack", "move"]
    assert json.loads(replies[-1].payload)["move"] == "e2e4"
    assert adapter.inspect(execution_id).state == ExecutionState.RUNNING
    # Every controller message reached the agent because drain() was called.
    assert len(_FakeSandbox.instances[0].stdin.delivered) == 4
    bridge.terminate()


def test_live_modal_image_installs_contract_runtime_packages(fake_modal) -> None:
    adapter, execution_id = _started_live_adapter()
    image = _FakeSandbox.instances[0].kwargs["image"]
    assert set(MODAL_RUNTIME_PACKAGES) <= set(image.packages)
    assert any(p.startswith("chess==") for p in image.packages)
    assert image.local_dirs and image.local_dirs[0][1] == "/opt/aik/artifact"
    assert _FakeSandbox.instances[0].kwargs["block_network"] is True
    adapter.terminate(execution_id)


def test_live_modal_startup_budget_excludes_sandbox_creation(fake_modal) -> None:
    clock = {"now": 1000.0}

    original_create = _FakeSandbox.create

    def slow_create(*args, **kwargs):
        clock["now"] += 60.0  # simulated image build + cold boot
        return original_create(*args, **kwargs)

    fake_modal.Sandbox = types.SimpleNamespace(create=slow_create)
    adapter, execution_id = _started_live_adapter(monotonic=lambda: clock["now"])

    snapshot = adapter.inspect(execution_id)
    assert snapshot.state == ExecutionState.RUNNING
    assert snapshot.failure_class is None
    adapter.terminate(execution_id)


def test_live_modal_unknown_exit_code_is_not_reported_as_clean_exit(fake_modal) -> None:
    _FakeSandbox.exit_immediately_with = None
    adapter, execution_id = _started_live_adapter()

    _wait_for(lambda: adapter._records[execution_id].completion_code is not None)
    record = adapter._records[execution_id]
    assert record.completion_code == -1
    assert record.state == ExecutionState.CRASHED
    assert record.failure == FailureClass.PARTICIPANT_PROCESS_FAILURE


def test_live_modal_nonzero_exit_is_crash(fake_modal) -> None:
    _FakeSandbox.exit_immediately_with = 1
    adapter, execution_id = _started_live_adapter()

    _wait_for(lambda: adapter._records[execution_id].completion_code is not None)
    assert adapter._records[execution_id].state == ExecutionState.CRASHED


def test_live_modal_send_input_without_stdin_fails_closed(fake_modal) -> None:
    adapter, execution_id = _started_live_adapter()
    record = adapter._records[execution_id]
    sandbox = record.sandbox
    sandbox.stdin = None
    with pytest.raises(Exception, match="PARTICIPANT_EOF"):
        adapter.send_input(execution_id, AIKMessage("r1", _line("r1", type="hello")))
    assert record.failure == FailureClass.PARTICIPANT_PROCESS_FAILURE
    sandbox.terminate()


def test_live_modal_ready_deadline_starts_at_hello_not_at_sandbox_creation(fake_modal) -> None:
    clock = {"now": 1000.0}
    _FakeSandbox.silent = True  # an agent that never answers
    adapter, execution_id = _started_live_adapter(monotonic=lambda: clock["now"])
    record = adapter._records[execution_id]

    # Waiting for the opponent's sandbox (no hello yet) must not trip the ready deadline.
    clock["now"] += 30.0
    time.sleep(0.2)
    assert record.state == ExecutionState.RUNNING
    assert record.failure is None

    # Once hello is sent, an agent that stays silent past the deadline is a startup failure.
    adapter.send_input(execution_id, AIKMessage("r1", _line("r1", type="hello")))
    clock["now"] += 6.0
    _wait_for(lambda: record.state == ExecutionState.TIMED_OUT)
    assert record.failure == FailureClass.STARTUP_FAILURE
    assert record.termination_reason == "STARTUP_TIMEOUT"
