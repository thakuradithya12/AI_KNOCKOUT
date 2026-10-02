"""API v1 router.

Aggregates all v1 route modules:
- Health, readiness, and version endpoints
- Participant identity and profile endpoints
- Authentication and session token endpoints
"""

from __future__ import annotations

from fastapi import APIRouter

from app.api.v1 import auth, health, participants, submissions

router = APIRouter(prefix="/api/v1")
router.include_router(health.router, tags=["health"])
router.include_router(participants.router, tags=["participants"])
router.include_router(auth.router, tags=["auth"])
router.include_router(submissions.router, tags=["submissions"])
