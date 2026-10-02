"""Health, readiness, and version endpoints.

GET /health  — Liveness: Is the API process alive?
GET /ready   — Readiness: Are all dependencies reachable?
GET /version — Version and environment information.

These are mounted at both root level (for infrastructure probes)
and under /api/v1/ (for versioned access).
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from app.core.config import get_settings
from app.db.session import check_database_health
from app.schemas.health import (
    DependencyStatus,
    HealthResponse,
    ReadinessResponse,
    VersionResponse,
)
from app.services.redis import check_redis_health

router = APIRouter()


@router.get("/health", response_model=HealthResponse)
async def health_check() -> HealthResponse:
    """Liveness probe. Returns OK if the API process is running."""
    return HealthResponse(status="ok", service="ai-knockout")


@router.get("/ready", response_model=ReadinessResponse)
async def readiness_check(response: Response) -> ReadinessResponse:
    """Readiness probe. Checks all critical dependencies."""
    db_status = await check_database_health()
    redis_status = await check_redis_health()

    all_ok = db_status["status"] == "ok" and redis_status["status"] == "ok"
    if not all_ok:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return ReadinessResponse(
        status="ok" if all_ok else "degraded",
        service="ai-knockout",
        dependencies={
            "postgresql": DependencyStatus(**db_status),
            "redis": DependencyStatus(**redis_status),
        },
    )


@router.get("/version", response_model=VersionResponse)
async def version_info() -> VersionResponse:
    """Return service version and environment."""
    settings = get_settings()
    return VersionResponse(
        service=settings.app_name,
        version=settings.app_version,
        environment=settings.app_env,
    )
