"""Unit tests for error handling."""

from __future__ import annotations

import json

from app.core.errors import create_error_response


class TestErrorResponses:
    """Test error response creation."""

    def test_error_response_structure(self):
        """Error responses must have consistent structure."""
        response = create_error_response(
            status_code=404,
            error="Not Found",
            detail="Resource not found",
            request_id="test-123",
        )
        assert response.status_code == 404
        body = json.loads(response.body)
        assert body["error"] == "Not Found"
        assert body["detail"] == "Resource not found"
        assert body["request_id"] == "test-123"

    def test_error_response_without_detail(self):
        """Error response without detail should omit the field."""
        response = create_error_response(status_code=500, error="Internal Server Error")
        body = json.loads(response.body)
        assert body["error"] == "Internal Server Error"
        assert "detail" not in body

    def test_error_response_without_request_id(self):
        """Error response without request_id should omit the field."""
        response = create_error_response(status_code=400, error="Bad Request")
        body = json.loads(response.body)
        assert "request_id" not in body
