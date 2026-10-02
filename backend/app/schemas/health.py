"""Health and readiness response schemas."""

from __future__ import annotations

from pydantic import BaseModel


class HealthResponse(BaseModel):
    """Liveness check response."""

    status: str
    service: str


class DependencyStatus(BaseModel):
    """Individual dependency status."""

    status: str
    detail: str | None = None


class ReadinessResponse(BaseModel):
    """Readiness check response with dependency statuses."""

    status: str
    service: str
    dependencies: dict[str, DependencyStatus]


class VersionResponse(BaseModel):
    """Version information response."""

    service: str
    version: str
    environment: str
