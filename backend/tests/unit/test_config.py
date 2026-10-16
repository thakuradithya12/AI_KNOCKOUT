"""Unit tests for configuration validation."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.core.config import Settings


class TestConfigValidation:
    """Test configuration loading and validation."""

    def test_reject_placeholder_secret_key(self, monkeypatch):
        """SECRET_KEY must not be a known placeholder."""
        monkeypatch.setenv("SECRET_KEY", "CHANGE_ME_GENERATE_A_REAL_SECRET_KEY")
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:pass@localhost/db")
        with pytest.raises(ValidationError, match="SECRET_KEY"):
            Settings(_env_file=None)  # type: ignore[call-arg]

    def test_reject_short_secret_key(self, monkeypatch):
        """SECRET_KEY must be at least 32 characters."""
        monkeypatch.setenv("SECRET_KEY", "tooshort")
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:pass@localhost/db")
        with pytest.raises(ValidationError, match="32 characters"):
            Settings(_env_file=None)  # type: ignore[call-arg]

    def test_reject_empty_secret_key(self, monkeypatch):
        """Empty SECRET_KEY must be rejected."""
        monkeypatch.setenv("SECRET_KEY", "")
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:pass@localhost/db")
        with pytest.raises(ValidationError):
            Settings(_env_file=None)  # type: ignore[call-arg]

    def test_accept_valid_secret_key(self, monkeypatch):
        """A proper secret key should be accepted."""
        monkeypatch.setenv("SECRET_KEY", "a" * 64)
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:pass@localhost/db")
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert len(settings.secret_key) == 64

    def test_reject_invalid_database_url(self, monkeypatch):
        """DATABASE_URL with CHANGE_ME placeholder must be rejected."""
        monkeypatch.setenv("SECRET_KEY", "a" * 64)
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://CHANGE_ME@localhost/db")
        with pytest.raises(ValidationError, match="DATABASE_URL"):
            Settings(_env_file=None)  # type: ignore[call-arg]

    def test_reject_invalid_log_level(self, monkeypatch):
        """Invalid log level must be rejected."""
        monkeypatch.setenv("SECRET_KEY", "a" * 64)
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:pass@localhost/db")
        monkeypatch.setenv("LOG_LEVEL", "verbose")
        with pytest.raises(ValidationError, match="LOG_LEVEL"):
            Settings(_env_file=None)  # type: ignore[call-arg]

    def test_accept_valid_log_levels(self, monkeypatch):
        """All standard log levels should be accepted."""
        monkeypatch.setenv("SECRET_KEY", "a" * 64)
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:pass@localhost/db")
        for level in ["debug", "info", "warning", "error", "critical"]:
            monkeypatch.setenv("LOG_LEVEL", level)
            settings = Settings(_env_file=None)  # type: ignore[call-arg]
            assert settings.log_level == level

    def test_cors_origins_list_parsing(self, monkeypatch):
        """CORS origins should be parsed from comma-separated string."""
        monkeypatch.setenv("SECRET_KEY", "a" * 64)
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:pass@localhost/db")
        monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "http://localhost:3000,http://localhost:8080")
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings.cors_origins_list == ["http://localhost:3000", "http://localhost:8080"]

    def test_production_rejects_wildcard_cors(self, monkeypatch):
        """Production mode must reject wildcard CORS."""
        monkeypatch.setenv("SECRET_KEY", "a" * 64)
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:pass@localhost/db")
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "*")
        with pytest.raises(ValidationError, match="Wildcard"):
            Settings(_env_file=None)  # type: ignore[call-arg]

    def test_production_rejects_db_echo(self, monkeypatch):
        """Production mode must not allow SQL echo."""
        monkeypatch.setenv("SECRET_KEY", "a" * 64)
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:pass@localhost/db")
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("DB_ECHO", "true")
        monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "http://example.com")
        with pytest.raises(ValidationError, match="echo"):
            Settings(_env_file=None)  # type: ignore[call-arg]

    def test_staging_uses_deployed_security_defaults(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY", "a" * 64)
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:pass@localhost/db")
        monkeypatch.setenv("APP_ENV", "  STAGING  ")
        monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://event.example")
        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings.app_env == "staging"
        assert settings.is_deployed
        assert not settings.is_production

    def test_staging_rejects_wildcard_cors(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY", "a" * 64)
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:pass@localhost/db")
        monkeypatch.setenv("APP_ENV", "staging")
        monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "*")
        with pytest.raises(ValidationError, match="Wildcard"):
            Settings(_env_file=None)  # type: ignore[call-arg]

    def test_staging_rejects_db_echo(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY", "a" * 64)
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:pass@localhost/db")
        monkeypatch.setenv("APP_ENV", "staging")
        monkeypatch.setenv("DB_ECHO", "true")
        with pytest.raises(ValidationError, match="echo"):
            Settings(_env_file=None)  # type: ignore[call-arg]

    def test_missing_secret_key_fails(self, monkeypatch):
        """Missing SECRET_KEY must fail."""
        monkeypatch.delenv("SECRET_KEY", raising=False)
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:pass@localhost/db")
        with pytest.raises(ValidationError):
            Settings(_env_file=None)  # type: ignore[call-arg]
