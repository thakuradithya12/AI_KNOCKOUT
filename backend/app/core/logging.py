"""Structured logging configuration.

All logs use structured JSON format with consistent fields:
- timestamp (UTC ISO-8601)
- level
- service
- message
- request_id (when available)

Secrets are never logged.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from typing import Any


class StructuredFormatter(logging.Formatter):
    """JSON structured log formatter."""

    SENSITIVE_KEYS = frozenset(
        {
            "password",
            "secret",
            "token",
            "api_key",
            "access_key",
            "secret_key",
            "database_url",
            "redis_url",
            "authorization",
            "credential",
            "private_key",
        }
    )

    # Fields that are standard LogRecord attributes (not extra data)
    _BUILTIN_ATTRS = frozenset(
        {
            "args",
            "created",
            "exc_info",
            "exc_text",
            "filename",
            "funcName",
            "levelname",
            "levelno",
            "lineno",
            "module",
            "msecs",
            "message",
            "msg",
            "name",
            "pathname",
            "process",
            "processName",
            "relativeCreated",
            "stack_info",
            "thread",
            "threadName",
            "taskName",
        }
    )

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict[str, Any] = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "service": "ai-knockout",
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Add request_id if present
        if hasattr(record, "request_id"):
            log_entry["request_id"] = record.request_id

        # Add exception info if present
        if record.exc_info and record.exc_info[1]:
            log_entry["error"] = {
                "type": type(record.exc_info[1]).__name__,
                "message": str(record.exc_info[1]),
            }

        # Add extra fields, filtering sensitive data
        for key, value in record.__dict__.items():
            if key.startswith("_") or key in self._BUILTIN_ATTRS:
                continue
            if key in ("request_id",):
                continue
            if any(sensitive in key.lower() for sensitive in self.SENSITIVE_KEYS):
                log_entry[key] = "[REDACTED]"
            else:
                log_entry[key] = value

        return json.dumps(log_entry, default=str)


def setup_logging(log_level: str = "info") -> None:
    """Configure structured logging for the application."""
    level = getattr(logging, log_level.upper(), logging.INFO)

    # Root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(level)

    # Remove existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Structured JSON handler
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(StructuredFormatter())
    handler.setLevel(level)
    root_logger.addHandler(handler)

    # Quiet noisy third-party loggers
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.error").setLevel(level)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)


def get_logger(name: str) -> logging.Logger:
    """Get a named logger instance."""
    return logging.getLogger(f"ai-knockout.{name}")
