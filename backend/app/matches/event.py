"""Typed identities for non-authoritative development matches against House AI.

These contracts identify and bind future development matches only. They do not
launch House AI or participant artifacts and do not create official match results.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass
from enum import Enum


class MatchPurpose(str, Enum):
    DEVELOPMENT = "DEVELOPMENT"
    OFFICIAL = "OFFICIAL"


@dataclass(frozen=True, slots=True)
class HouseAIIdentity:
    house_ai_id: str
    version: int
    artifact_digest: str
    protocol_version: str
    runtime_profile: str
    rules_version: str
    policy_version: str
    runtime_identity: str

    def __post_init__(self) -> None:
        if not self.house_ai_id or len(self.house_ai_id) > 64 or self.version < 1:
            raise ValueError("HOUSE_AI_IDENTITY_INVALID")
        if re.fullmatch(r"[0-9a-f]{64}", self.artifact_digest) is None:
            raise ValueError("HOUSE_AI_DIGEST_INVALID")
        if any(
            not value or len(value) > 128
            for value in (
                self.protocol_version,
                self.runtime_profile,
                self.rules_version,
                self.policy_version,
                self.runtime_identity,
            )
        ):
            raise ValueError("HOUSE_AI_POLICY_IDENTITY_INVALID")


@dataclass(frozen=True, slots=True)
class DevelopmentMatchAssociation:
    """Exact participant version ↔ frozen House AI version identity for later execution."""

    development_match_id: uuid.UUID
    participant_id: str
    submission_version_id: uuid.UUID
    participant_artifact_digest: str
    house_ai: HouseAIIdentity
    purpose: MatchPurpose = MatchPurpose.DEVELOPMENT

    def __post_init__(self) -> None:
        if self.purpose is not MatchPurpose.DEVELOPMENT:
            raise ValueError("DEVELOPMENT_ASSOCIATION_PURPOSE_INVALID")
        if not self.participant_id or len(self.participant_id) > 32:
            raise ValueError("DEVELOPMENT_PARTICIPANT_ID_INVALID")
        if re.fullmatch(r"[0-9a-f]{64}", self.participant_artifact_digest) is None:
            raise ValueError("DEVELOPMENT_ARTIFACT_DIGEST_INVALID")
