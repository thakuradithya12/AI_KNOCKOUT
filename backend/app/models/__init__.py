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
from app.models.match import (
    Match,
    MatchExecutionBinding,
    MatchFailure,
    MatchMove,
    MatchOutcome,
    MatchSide,
    MatchSideColor,
    MatchState,
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
    "ExecutionFailureClass",
    "WorkerArtifactCapability",
    "WorkerEvidence",
    "WorkerSecurityEvent",
    "Match",
    "MatchSide",
    "MatchSideColor",
    "MatchState",
    "MatchOutcome",
    "MatchFailure",
    "MatchExecutionBinding",
    "MatchMove",
]
