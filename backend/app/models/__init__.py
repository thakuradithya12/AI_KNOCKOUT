"""Database models.

Phase 1: System infrastructure verification model.
Phase 2: Participant identity, sessions, and security audit log.
"""

from app.models.competition import (
    ArtifactState,
    ArtifactStateTransition,
    CompetitionEvent,
    QuarantineObject,
    Submission,
    SubmissionVersion,
    ValidationAttempt,
    ValidationOutcome,
)
from app.models.event import BuildSession, EventControl, OfficialSubmissionSelection
from app.models.house_ai import HouseAI, HouseAIArtifact, HouseAIVersion
from app.models.match import (
    HouseAIMatchSideBinding,
    Match,
    MatchExecutionBinding,
    MatchFailure,
    MatchMove,
    MatchOutcome,
    MatchPurpose,
    MatchSide,
    MatchSideColor,
    MatchSideKind,
    MatchState,
    ParticipantMatchSideBinding,
)
from app.models.participant import (
    AuthEvent,
    AuthEventType,
    Participant,
    ParticipantSession,
    ParticipantStatus,
)
from app.models.system import SystemInfo
from app.models.worker import (
    ExecutionFailureClass,
    ExecutionJobStatus,
    ExecutionKind,
    Worker,
    WorkerArtifactCapability,
    WorkerEnrollmentToken,
    WorkerEvidence,
    WorkerExecutionJob,
    WorkerSecurityEvent,
    WorkerStatus,
)

__all__ = [
    "SystemInfo",
    "Submission",
    "SubmissionVersion",
    "QuarantineObject",
    "ArtifactState",
    "ArtifactStateTransition",
    "ValidationAttempt",
    "ValidationOutcome",
    "CompetitionEvent",
    "Participant",
    "ParticipantSession",
    "ParticipantStatus",
    "AuthEvent",
    "AuthEventType",
    "Worker",
    "WorkerStatus",
    "WorkerEnrollmentToken",
    "WorkerExecutionJob",
    "ExecutionJobStatus",
    "ExecutionKind",
    "ExecutionFailureClass",
    "WorkerArtifactCapability",
    "WorkerEvidence",
    "WorkerSecurityEvent",
    "Match",
    "MatchSide",
    "MatchSideColor",
    "MatchSideKind",
    "MatchPurpose",
    "ParticipantMatchSideBinding",
    "HouseAIMatchSideBinding",
    "MatchState",
    "MatchOutcome",
    "MatchFailure",
    "MatchExecutionBinding",
    "MatchMove",
    "BuildSession",
    "EventControl",
    "OfficialSubmissionSelection",
    "HouseAI",
    "HouseAIArtifact",
    "HouseAIVersion",
]
