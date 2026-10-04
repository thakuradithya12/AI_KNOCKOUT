"""Server-authoritative match domain boundary; no process-launch integration."""

from app.matches.controller import (
    MatchClock,
    MatchConfiguration,
    MatchController,
    MatchEvent,
    MatchExecutionBinding,
    MatchResult,
    MatchSideIdentity,
)

__all__ = [
    "MatchClock",
    "MatchConfiguration",
    "MatchController",
    "MatchEvent",
    "MatchExecutionBinding",
    "MatchResult",
    "MatchSideIdentity",
]
