"""Centralized error handling.

All API errors use a consistent response structure.
Internal details (tracebacks, SQL errors) are never exposed in production.
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

logger = logging.getLogger("ai-knockout.errors")


def create_error_response(
    status_code: int,
    error: str,
    detail: str | None = None,
    request_id: str | None = None,
) -> JSONResponse:
    """Create a consistent error JSON response."""
    body: dict[str, Any] = {"error": error}
    if detail:
        body["detail"] = detail
    if request_id:
        body["request_id"] = request_id
    return JSONResponse(status_code=status_code, content=body)


def register_error_handlers(app: FastAPI, is_development: bool = False) -> None:
    """Register global exception handlers on the FastAPI app."""

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        logger.warning(
            "Request validation error",
            extra={"request_id": request_id, "errors": str(exc.errors())},
        )
        return create_error_response(
            status_code=422,
            error="Validation Error",
            detail="Request validation failed.",
            request_id=request_id,
        )

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        request_id = getattr(request.state, "request_id", None)
        logger.error(
            f"Unhandled exception: {type(exc).__name__}: {exc}",
            extra={"request_id": request_id},
            exc_info=True,
        )
        return create_error_response(
            status_code=500,
            error="Internal Server Error",
            detail=None,
            request_id=request_id,
        )
