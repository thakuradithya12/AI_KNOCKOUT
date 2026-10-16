"""Application configuration with validation.

All configuration is loaded from environment variables and validated at startup.
Missing required configuration causes immediate failure with clear error messages.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Optional

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.db.url import normalize_async_database_url, normalize_sync_database_url


class Settings(BaseSettings):
    """Application settings loaded from environment variables.

    Required secrets (SECRET_KEY, DATABASE_URL) must be explicitly set.
    The application will refuse to start with placeholder or missing values.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Application
    app_env: str = "development"
    app_host: str = "0.0.0.0"
    app_port: int = 8000
    app_name: str = "AI Knockout"
    app_version: str = "0.1.0"

    # Security
    secret_key: str

    # Database
    database_url: str
    database_url_sync: str = ""
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_pool_timeout: int = 30
    db_pool_recycle: int = 1800
    db_echo: bool = False

    # Redis
    redis_url: str = "redis://localhost:6379/0"
    redis_socket_timeout: int = 5
    redis_socket_connect_timeout: int = 5
    redis_max_connections: int = 20

    # Logging
    log_level: str = "info"

    # CORS
    cors_allowed_origins: str = "http://localhost:3000"

    # Phase 2: Session & Auth
    session_expire_minutes: int = 120
    rate_limit_login_attempts: int = 50
    rate_limit_window_seconds: int = 60
    rate_limit_register_attempts: int = 100

    # Organizer OIDC & Server-Managed Session Settings
    oidc_issuer_url: Optional[str] = None
    oidc_client_id: Optional[str] = None
    oidc_client_secret: Optional[str] = None
    oidc_redirect_uri: Optional[str] = None
    organizer_allowed_emails: str = ""
    organizer_session_expire_minutes: int = 480
    organizer_session_idle_minutes: int = 60

    # Approved technical ceilings from AI Knockout Rules (10 MiB maximum archive size).
    submission_max_archive_bytes: int = 10 * 1024 * 1024
    submission_max_members: int = 128
    submission_max_member_bytes: int = 4 * 1024 * 1024
    submission_max_total_bytes: int = 10 * 1024 * 1024
    submission_max_manifest_bytes: int = 16 * 1024

    @field_validator("app_env")
    @classmethod
    def normalize_app_env(cls, value: str) -> str:
        """Normalize and validate the deployment environment name."""
        normalized = value.strip().lower()
        if normalized not in {"development", "test", "staging", "production"}:
            raise ValueError("APP_ENV must be development, test, staging, or production.")
        return normalized

    @field_validator("secret_key")
    @classmethod
    def validate_secret_key(cls, v: str) -> str:
        """Reject placeholder and weak secret keys."""
        forbidden = {
            "CHANGE_ME_GENERATE_A_REAL_SECRET_KEY",
            "changeme",
            "secret",
            "your-secret-key",
            "",
        }
        if v in forbidden:
            raise ValueError(
                "SECRET_KEY must be set to a strong random value. "
                'Generate one with: python -c "import secrets; print(secrets.token_urlsafe(64))"'
            )
        if len(v) < 32:
            raise ValueError("SECRET_KEY must be at least 32 characters long.")
        return v

    @field_validator("database_url")
    @classmethod
    def validate_database_url(cls, v: str) -> str:
        """Ensure database URL is set, valid, and normalized for async driver."""
        if not v or "CHANGE_ME" in v:
            raise ValueError("DATABASE_URL must be configured with valid credentials.")
        return normalize_async_database_url(v)

    @field_validator("database_url_sync")
    @classmethod
    def validate_database_url_sync(cls, v: str) -> str:
        """Ensure sync database URL is normalized if configured."""
        if not v:
            return ""
        if "CHANGE_ME" in v:
            raise ValueError("DATABASE_URL_SYNC must be configured with valid credentials.")
        return normalize_sync_database_url(v)

    @property
    def sync_database_url(self) -> str:
        """Return explicit sync URL if configured, otherwise derive from database_url."""
        if self.database_url_sync:
            return self.database_url_sync
        return normalize_sync_database_url(self.database_url)

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """Normalize and validate log level."""
        v = v.lower()
        valid_levels = {"debug", "info", "warning", "error", "critical"}
        if v not in valid_levels:
            raise ValueError(f"LOG_LEVEL must be one of: {', '.join(sorted(valid_levels))}")
        return v

    @property
    def cors_origins_list(self) -> list[str]:
        """Parse comma-separated CORS origins into a list."""
        return [o.strip() for o in self.cors_allowed_origins.split(",") if o.strip()]

    @property
    def is_development(self) -> bool:
        """Check if running in development mode."""
        return self.app_env == "development"

    @property
    def is_production(self) -> bool:
        """Check if running in production mode."""
        return self.app_env == "production"

    @property
    def is_deployed(self) -> bool:
        """Check if running in a remotely deployed environment."""
        return self.app_env in {"staging", "production"}

    @property
    def organizer_allowed_emails_list(self) -> list[str]:
        """Parse comma-separated allowlist of organizer emails into normalized lowercase list."""
        if not self.organizer_allowed_emails:
            return []
        return [e.strip().lower() for e in self.organizer_allowed_emails.split(",") if e.strip()]

    @property
    def is_oidc_configured(self) -> bool:
        """Check if minimum trusted OIDC identity provider configuration is available."""
        return bool(self.oidc_issuer_url and self.oidc_client_id and self.oidc_client_secret)

    @model_validator(mode="after")
    def validate_production_settings(self) -> Settings:
        """Enforce stricter validation in production mode."""
        if self.is_deployed:
            if "*" in self.cors_allowed_origins:
                raise ValueError("Wildcard CORS origins are not allowed in deployed environments.")
            if self.db_echo:
                raise ValueError("Database SQL echo must be disabled in deployed environments.")
        return self

    @model_validator(mode="after")
    def validate_submission_limits(self) -> Settings:
        """Never let runtime configuration exceed the Core Contract V1 caps."""
        ceilings = {
            "submission_max_archive_bytes": 16 * 1024 * 1024,
            "submission_max_members": 128,
            "submission_max_member_bytes": 4 * 1024 * 1024,
            "submission_max_total_bytes": 16 * 1024 * 1024,
            "submission_max_manifest_bytes": 16 * 1024,
        }
        for name, ceiling in ceilings.items():
            value = getattr(self, name)
            if value < 1 or value > ceiling:
                raise ValueError(f"{name} must be between 1 and the Core Contract V1 ceiling ({ceiling}).")
        return self


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Create and return validated settings. Cached after first call.

    Raises ValidationError with clear messages if configuration is invalid.
    """
    return Settings()  # type: ignore[call-arg]
