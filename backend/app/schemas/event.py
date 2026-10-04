"""Participant-safe event lifecycle response contracts."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class SelectOfficialSubmissionRequest(BaseModel):
    submission_id: uuid.UUID
    version: int = Field(ge=1)


class LatestSubmissionStatus(BaseModel):
    submission_id: uuid.UUID
    version: int
    submitted_at: datetime
    build_duration_seconds: int | None
    state: str
    validation_outcome: str | None


class OfficialSubmissionStatus(BaseModel):
    version: int
    selected_at: datetime


class ParticipantEventStatus(BaseModel):
    server_now: datetime
    build_started_at: datetime
    build_elapsed_seconds: int
    latest_submission: LatestSubmissionStatus | None
    official_submission: OfficialSubmissionStatus | None
    official_frozen_at: datetime | None
