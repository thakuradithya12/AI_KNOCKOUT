"""Unit tests for structured logging."""

from __future__ import annotations

import json
import logging
import sys

from app.core.logging import StructuredFormatter


class TestStructuredLogging:
    """Test structured JSON log formatting."""

    def test_basic_log_structure(self):
        """Log entries must contain required fields."""
        formatter = StructuredFormatter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Test message",
            args=(),
            exc_info=None,
        )
        output = formatter.format(record)
        parsed = json.loads(output)
        assert "timestamp" in parsed
        assert parsed["level"] == "INFO"
        assert parsed["service"] == "ai-knockout"
        assert parsed["message"] == "Test message"

    def test_request_id_included(self):
        """Request ID should be included when set on the record."""
        formatter = StructuredFormatter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Test",
            args=(),
            exc_info=None,
        )
        record.request_id = "req-abc-123"  # type: ignore[attr-defined]
        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["request_id"] == "req-abc-123"

    def test_sensitive_data_redacted(self):
        """Sensitive fields must be redacted in logs."""
        formatter = StructuredFormatter()
        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="Test",
            args=(),
            exc_info=None,
        )
        record.password = "super_secret"  # type: ignore[attr-defined]
        record.api_key = "key123"  # type: ignore[attr-defined]
        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed.get("password") == "[REDACTED]"
        assert parsed.get("api_key") == "[REDACTED]"

    def test_exception_info_captured(self):
        """Exception information should be captured in log entry."""
        formatter = StructuredFormatter()
        try:
            raise ValueError("test error")
        except ValueError:
            record = logging.LogRecord(
                name="test",
                level=logging.ERROR,
                pathname="",
                lineno=0,
                msg="Error occurred",
                args=(),
                exc_info=sys.exc_info(),
            )
        output = formatter.format(record)
        parsed = json.loads(output)
        assert parsed["error"]["type"] == "ValueError"
        assert "test error" in parsed["error"]["message"]
