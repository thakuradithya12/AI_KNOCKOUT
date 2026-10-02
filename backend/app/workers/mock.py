"""Protocol-only mock worker. It verifies allowlisted fixture bytes and never executes code."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass


class MockArtifactRejected(Exception):
    pass


@dataclass(frozen=True)
class MockArtifactObservation:
    artifact_sha256: str
    artifact_size: int
    digest_verified: bool
    security_label: str = "MOCKED_DOES_NOT_PROVE_SANDBOX_SECURITY"


class MockWorker:
    """Test-only actor. There is intentionally no method that launches an artifact."""

    def __init__(self, fixtures: dict[str, bytes]) -> None:
        self._fixtures = dict(fixtures)
        if any(hashlib.sha256(data).hexdigest() != digest for digest, data in fixtures.items()):
            raise ValueError("mock fixture key must be its exact SHA-256")

    def verify_fixture(self, *, expected_digest: str, artifact_bytes: bytes) -> MockArtifactObservation:
        fixture = self._fixtures.get(expected_digest)
        if fixture is None:
            raise MockArtifactRejected("MOCK_FIXTURE_NOT_ALLOWLISTED")
        observed = hashlib.sha256(artifact_bytes).hexdigest()
        if observed != expected_digest or artifact_bytes != fixture:
            raise MockArtifactRejected("ARTIFACT_INTEGRITY_FAILURE")
        return MockArtifactObservation(
            artifact_sha256=observed,
            artifact_size=len(artifact_bytes),
            digest_verified=True,
        )
