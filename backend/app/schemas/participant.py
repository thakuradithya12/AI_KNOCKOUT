"""Pydantic schemas for participant identity, registration, and authentication."""

from __future__ import annotations

import re
from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


class ParticipantRegisterRequest(BaseModel):
    """Registration request payload."""

    model_config = ConfigDict(str_strip_whitespace=True)

    name: str = Field(
        ...,
        min_length=2,
        max_length=100,
        description="Unique participant or team display name.",
        examples=["DeepKnight AI"],
    )
    email: str | None = Field(
        default=None,
        max_length=255,
        description="Optional contact email. Normalized to lowercase.",
        examples=["team@college.edu"],
    )
    team_name: str | None = Field(
        default=None,
        min_length=1,
        max_length=100,
        description="Optional team designation.",
        examples=["Team Alpha"],
    )

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        """Enforce clean name without control characters or HTML tags."""
        v = v.strip()
        if not v:
            raise ValueError("Name cannot be empty or whitespace only.")
        if re.search(r"[\x00-\x1f<>]", v):
            raise ValueError("Name contains prohibited characters.")
        return v

    @field_validator("email")
    @classmethod
    def validate_and_normalize_email(cls, v: str | None) -> str | None:
        """Normalize email to lowercase and validate format if provided."""
        if v is None:
            return None
        v = v.strip().lower()
        if not v:
            return None
        # Standard RFC 5322 compatible email regex
        email_pattern = r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$"
        if not re.match(email_pattern, v):
            raise ValueError("Invalid email format.")
        return v


class ParticipantRegisterResponse(BaseModel):
    """Registration response.

    Contains the initial plaintext Access Key — issued ONCE.
    """

    model_config = ConfigDict(from_attributes=True)

    participant_id: str = Field(..., description="Server-assigned immutable participant ID.")
    name: str
    email: str | None = None
    team_name: str | None = None
    status: str
    access_key: str = Field(
        ...,
        description="Secret credential. Displayed once; never persisted in plaintext on the server.",
    )
    created_at: datetime


class ParticipantResponse(BaseModel):
    """Safe public participant view — never leaks access keys or hashes."""

    model_config = ConfigDict(from_attributes=True)

    participant_id: str
    name: str
    email: str | None = None
    team_name: str | None = None
    status: str
    created_at: datetime


class LoginRequest(BaseModel):
    """Authentication request credentials."""

    model_config = ConfigDict(str_strip_whitespace=True)

    participant_id: str = Field(
        ...,
        min_length=5,
        max_length=50,
        description="Assigned participant identifier.",
        examples=["part_1234567890abcdef"],
    )
    access_key: str = Field(
        ...,
        min_length=16,
        max_length=128,
        description="Assigned secret access key.",
    )


class TokenResponse(BaseModel):
    """Bearer session token response."""

    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(..., description="Token validity duration in seconds.")
    participant: ParticipantResponse
