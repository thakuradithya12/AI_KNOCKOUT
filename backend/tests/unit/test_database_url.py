"""Unit tests for centralized database URL normalization, SSL handling, and validation."""

from __future__ import annotations

import inspect

import asyncpg
import pytest
from pydantic import ValidationError
from sqlalchemy.dialects.postgresql.asyncpg import PGDialect_asyncpg
from sqlalchemy.engine import make_url

from app.core.config import Settings
from app.db import session
from app.db.url import (
    build_async_ssl_context,
    canonicalize_ssl_mode,
    get_asyncpg_connect_args,
    normalize_async_database_url,
    normalize_database_url,
    normalize_sync_database_url,
)
from migrations import env as alembic_env


class TestDatabaseUrlNormalization:
    """Test suite for URL normalization across drivers, schemes, and parameters."""

    # 1. postgres:// normalization
    def test_normalize_postgres_scheme_async(self):
        raw = "postgres://synth_user:synth_pass@db.staging.internal:5432/aik_staging"
        result = normalize_async_database_url(raw)
        assert result == "postgresql+asyncpg://synth_user:synth_pass@db.staging.internal:5432/aik_staging"

    def test_normalize_postgres_scheme_sync(self):
        raw = "postgres://synth_user:synth_pass@db.staging.internal:5432/aik_staging"
        result = normalize_sync_database_url(raw)
        assert result == "postgresql+psycopg2://synth_user:synth_pass@db.staging.internal:5432/aik_staging"

    # 2. postgresql:// normalization
    def test_normalize_postgresql_scheme_async(self):
        raw = "postgresql://synth_user:synth_pass@db.staging.internal:5432/aik_staging"
        result = normalize_async_database_url(raw)
        assert result == "postgresql+asyncpg://synth_user:synth_pass@db.staging.internal:5432/aik_staging"

    def test_normalize_postgresql_scheme_sync(self):
        raw = "postgresql://synth_user:synth_pass@db.staging.internal:5432/aik_staging"
        result = normalize_sync_database_url(raw)
        assert result == "postgresql+psycopg2://synth_user:synth_pass@db.staging.internal:5432/aik_staging"

    # 3. Already driver-qualified URLs (no double prefixing)
    def test_already_normalized_async_url_preserved(self):
        raw = "postgresql+asyncpg://synth_user:synth_pass@host:5432/aik_db"
        result = normalize_async_database_url(raw)
        assert result == raw
        assert "asyncpg+asyncpg" not in result

    def test_already_normalized_sync_url_preserved(self):
        raw = "postgresql+psycopg2://synth_user:synth_pass@host:5432/aik_db"
        result = normalize_sync_database_url(raw)
        assert result == raw
        assert "psycopg2+psycopg2" not in result

    def test_convert_between_driver_qualified_urls(self):
        async_url = "postgresql+asyncpg://synth_user:synth_pass@host:5432/aik_db"
        sync_url = "postgresql+psycopg2://synth_user:synth_pass@host:5432/aik_db"

        # asyncpg -> psycopg2
        assert normalize_sync_database_url(async_url) == sync_url
        # psycopg2 -> asyncpg
        assert normalize_async_database_url(sync_url) == async_url

    # 4. SSL Mode Conversions (all standard modes)
    @pytest.mark.parametrize(
        "mode,expected_async_mode,expected_sync_mode",
        [
            ("disable", "disable", "disable"),
            ("allow", "allow", "allow"),
            ("prefer", "prefer", "prefer"),
            ("require", "require", "require"),
            ("verify-ca", "verify-ca", "verify-ca"),
            ("verify-full", "verify-full", "verify-full"),
            ("verify_ca", "verify-ca", "verify-ca"),
            ("verify_full", "verify-full", "verify-full"),
        ],
    )
    def test_ssl_mode_conversion_all_modes(
        self, mode: str, expected_async_mode: str, expected_sync_mode: str
    ):
        raw_sslmode = f"postgres://synth_user:synth_pass@db:5432/aik_db?sslmode={mode}"
        async_res = normalize_async_database_url(raw_sslmode)
        sync_res = normalize_sync_database_url(raw_sslmode)

        assert f"ssl={expected_async_mode}" in async_res
        assert "sslmode" not in async_res

        assert f"sslmode={expected_sync_mode}" in sync_res
        assert "ssl=" not in sync_res

    # 5. Boolean provider SSL parameters (ssl=true, sslmode=true, etc.)
    @pytest.mark.parametrize(
        "query_param,expected_async,expected_sync",
        [
            ("ssl=true", "ssl=require", "sslmode=require"),
            ("ssl=false", "ssl=disable", "sslmode=disable"),
            ("ssl=1", "ssl=require", "sslmode=require"),
            ("ssl=0", "ssl=disable", "sslmode=disable"),
            ("sslmode=true", "ssl=require", "sslmode=require"),
            ("sslmode=false", "ssl=disable", "sslmode=disable"),
        ],
    )
    def test_boolean_ssl_modes(self, query_param: str, expected_async: str, expected_sync: str):
        raw = f"postgres://synth_user:synth_pass@db:5432/aik_db?{query_param}"
        async_res = normalize_async_database_url(raw)
        sync_res = normalize_sync_database_url(raw)

        assert expected_async in async_res
        assert "sslmode" not in async_res

        assert expected_sync in sync_res
        assert "ssl=" not in sync_res

    # 6. Invalid SSL modes rejection (no silent weakening or bypass)
    @pytest.mark.parametrize(
        "invalid_mode",
        [
            "invalid",
            "none",
            "unknown",
            "super_secure",
            "trust",
        ],
    )
    def test_invalid_ssl_modes_rejected(self, invalid_mode: str):
        raw = f"postgres://synth_user:synth_pass@db:5432/aik_db?sslmode={invalid_mode}"
        with pytest.raises(ValueError) as exc_info:
            normalize_async_database_url(raw)
        msg = str(exc_info.value)
        assert f"Invalid SSL mode '{invalid_mode}'" in msg
        assert "synth_pass" not in msg

    def test_canonicalize_ssl_mode_direct(self):
        assert canonicalize_ssl_mode("require") == "require"
        assert canonicalize_ssl_mode("VERIFY_FULL") == "verify-full"
        assert canonicalize_ssl_mode("TRUE") == "require"
        assert canonicalize_ssl_mode("false") == "disable"

    # 7. Regression Test: sslmode must NEVER be passed as kwarg to asyncpg.connect
    def test_regression_sslmode_not_passed_as_asyncpg_kwarg(self):
        raw = "postgres://synth_user:synth_pass@stage.cloud.io:5432/aik_db?sslmode=require"
        normalized = normalize_async_database_url(raw)

        dialect = PGDialect_asyncpg()
        parsed_url = make_url(normalized)
        _, connect_opts = dialect.create_connect_args(parsed_url)

        # Proves sslmode was removed and will not crash asyncpg.connect()
        assert "sslmode" not in connect_opts
        assert connect_opts.get("ssl") == "require"

        # Verify against actual asyncpg.connect signature
        sig = inspect.signature(asyncpg.connect)
        allowed_params = set(sig.parameters.keys()) | {"prepared_statement_cache_size"}
        unexpected_kwargs = set(connect_opts.keys()) - allowed_params
        assert not unexpected_kwargs, f"Found unexpected kwargs for asyncpg: {unexpected_kwargs}"

    # 8. Certificate parameters handling
    def test_certificate_parameters_handling(self):
        raw = (
            "postgres://synth_user:synth_pass@db.staging.internal:5432/aik_db"
            "?sslmode=verify-full&sslrootcert=%2Ftmp%2Fca.crt"
            "&sslcert=%2Ftmp%2Fclient.crt&sslkey=%2Ftmp%2Fclient.key"
        )
        async_res = normalize_async_database_url(raw)
        sync_res = normalize_sync_database_url(raw)

        # In async URL: unsupported kwargs stripped from query string
        assert "ssl=verify-full" in async_res
        assert "sslmode" not in async_res
        assert "sslrootcert" not in async_res
        assert "sslcert" not in async_res
        assert "sslkey" not in async_res

        # In sync URL: full libpq query parameters preserved
        assert "sslmode=verify-full" in sync_res
        assert "sslrootcert=%2Ftmp%2Fca.crt" in sync_res
        assert "sslcert=%2Ftmp%2Fclient.crt" in sync_res
        assert "sslkey=%2Ftmp%2Fclient.key" in sync_res

    # 9. Credentials with special characters preservation
    def test_credentials_with_encoded_special_chars_preserved(self):
        raw = "postgres://aik_user:p%40ss%23word%21@db-host:5433/aik_db?sslmode=require"
        async_res = normalize_async_database_url(raw)
        sync_res = normalize_sync_database_url(raw)

        assert async_res == "postgresql+asyncpg://aik_user:p%40ss%23word%21@db-host:5433/aik_db?ssl=require"
        assert sync_res == "postgresql+psycopg2://aik_user:p%40ss%23word%21@db-host:5433/aik_db?sslmode=require"

    # 10. URLs without credentials or port
    def test_urls_without_credentials_or_port(self):
        raw = "postgresql://localhost/aik_db"
        assert normalize_async_database_url(raw) == "postgresql+asyncpg://localhost/aik_db"
        assert normalize_sync_database_url(raw) == "postgresql+psycopg2://localhost/aik_db"

    # 11. SQLite test URLs
    def test_sqlite_urls_preserved_for_tests(self):
        mem = "sqlite:///:memory:"
        assert normalize_async_database_url(mem) == "sqlite+aiosqlite:///:memory:"
        assert normalize_sync_database_url(mem) == "sqlite:///:memory:"

        aiosqlite = "sqlite+aiosqlite:///:memory:"
        assert normalize_async_database_url(aiosqlite) == aiosqlite
        assert normalize_sync_database_url(aiosqlite) == "sqlite:///:memory:"

        pysqlite = "sqlite+pysqlite:///:memory:"
        assert normalize_async_database_url(pysqlite) == "sqlite+aiosqlite:///:memory:"
        assert normalize_sync_database_url(pysqlite) == pysqlite

    # 12. Unsupported schemes rejection
    @pytest.mark.parametrize(
        "scheme_url,expected_scheme",
        [
            ("mysql://synth_user:synth_secret@localhost:3306/db", "mysql"),
            ("mongodb://synth_user:synth_secret@localhost:27017/db", "mongodb"),
            ("redis://localhost:6379/0", "redis"),
            ("http://api.staging.internal/db", "http"),
            ("oracle://synth_user:synth_secret@localhost:1521/db", "oracle"),
        ],
    )
    def test_unsupported_schemes_rejected_safely(self, scheme_url: str, expected_scheme: str):
        with pytest.raises(ValueError) as exc_info:
            normalize_database_url(scheme_url)

        msg = str(exc_info.value)
        assert f"Unsupported database scheme '{expected_scheme}'" in msg
        # Ensure credentials or hosts are never leaked in the exception message
        assert "synth_secret" not in msg
        assert "synth_user" not in msg

    # 13. Invalid URL formats rejection
    @pytest.mark.parametrize(
        "invalid_input",
        [
            "",
            "   ",
            "://",
            "not_a_valid_url",
            "missing-scheme-host:5432/db",
        ],
    )
    def test_invalid_url_rejected_safely(self, invalid_input: str):
        with pytest.raises(ValueError) as exc_info:
            normalize_database_url(invalid_input)
        err = str(exc_info.value)
        assert "Invalid database URL" in err or "must not be empty" in err

    # 14. Strict idempotence
    def test_repeated_normalization_is_strictly_idempotent(self):
        url = "postgres://synth_user:p%40ss@host:5432/db?sslmode=require"
        normalized = url
        for _ in range(5):
            normalized = normalize_async_database_url(normalized)
            assert normalized == "postgresql+asyncpg://synth_user:p%40ss@host:5432/db?ssl=require"

        sync_norm = url
        for _ in range(5):
            sync_norm = normalize_sync_database_url(sync_norm)
            assert sync_norm == "postgresql+psycopg2://synth_user:p%40ss@host:5432/db?sslmode=require"

    # 15. Helper: get_asyncpg_connect_args with application_name
    def test_get_asyncpg_connect_args_application_name(self):
        url = "postgres://synth_user:synth_pass@host:5432/db?sslmode=require&application_name=ai_knockout"
        connect_args = get_asyncpg_connect_args(url)
        assert connect_args.get("server_settings") == {"application_name": "ai_knockout"}

    # 16. Helper: build_async_ssl_context
    def test_build_async_ssl_context_returns_none_for_standard_mode(self):
        url = "postgres://synth_user:synth_pass@host:5432/db?sslmode=require"
        # Standard require mode lets asyncpg construct its own context natively
        ctx = build_async_ssl_context(url)
        assert ctx is None


class TestConfigAndConsumerIntegration:
    """Test configuration validation and consumer integration with URL normalization."""

    def test_settings_normalizes_postgres_database_url(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY", "s" * 64)
        monkeypatch.setenv("DATABASE_URL", "postgres://synth_user:synth_pass@db:5432/aik_db?sslmode=require")
        monkeypatch.delenv("DATABASE_URL_SYNC", raising=False)

        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings.database_url == "postgresql+asyncpg://synth_user:synth_pass@db:5432/aik_db?ssl=require"
        assert settings.sync_database_url == "postgresql+psycopg2://synth_user:synth_pass@db:5432/aik_db?sslmode=require"

    def test_settings_normalizes_database_url_sync(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY", "s" * 64)
        monkeypatch.setenv("DATABASE_URL", "postgres://synth_user:synth_pass@db:5432/aik_db")
        monkeypatch.setenv("DATABASE_URL_SYNC", "postgres://synth_user:synth_pass@db:5432/aik_db?sslmode=require")

        settings = Settings(_env_file=None)  # type: ignore[call-arg]
        assert settings.database_url == "postgresql+asyncpg://synth_user:synth_pass@db:5432/aik_db"
        assert settings.database_url_sync == "postgresql+psycopg2://synth_user:synth_pass@db:5432/aik_db?sslmode=require"
        assert settings.sync_database_url == "postgresql+psycopg2://synth_user:synth_pass@db:5432/aik_db?sslmode=require"

    def test_settings_rejects_unsupported_scheme(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY", "s" * 64)
        monkeypatch.setenv("DATABASE_URL", "mysql://synth_user:super_secret_pw@localhost:3306/aik_db")

        with pytest.raises(ValidationError) as exc_info:
            Settings(_env_file=None)  # type: ignore[call-arg]

        err_str = str(exc_info.value)
        assert "Unsupported database scheme 'mysql'" in err_str
        assert "super_secret_pw" not in err_str

    def test_settings_rejects_placeholder_sync_database_url(self, monkeypatch):
        monkeypatch.setenv("SECRET_KEY", "s" * 64)
        monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://synth_user:synth_pass@localhost:5432/db")
        monkeypatch.setenv("DATABASE_URL_SYNC", "postgresql://CHANGE_ME@localhost/db")

        with pytest.raises(ValidationError) as exc_info:
            Settings(_env_file=None)  # type: ignore[call-arg]
        assert "DATABASE_URL_SYNC" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_session_init_engine_normalizes_postgres_url_and_ssl(self):
        # init_engine should normalize postgres:// to asyncpg engine with ssl=require
        session.init_engine("postgres://synth_user:synth_pass@localhost:5432/aik_db?sslmode=require")
        try:
            assert session._engine is not None
            assert session._engine.dialect.name == "postgresql"
            assert session._engine.dialect.driver == "asyncpg"
        finally:
            await session.close_engine()

    def test_alembic_env_get_database_url_from_sync_env(self, monkeypatch):
        monkeypatch.setenv("DATABASE_URL_SYNC", "postgres://synth_user:synth_pass@stage-db:5432/aik_db?ssl=require")
        url = alembic_env.get_database_url()
        assert url == "postgresql+psycopg2://synth_user:synth_pass@stage-db:5432/aik_db?sslmode=require"

    def test_alembic_env_get_database_url_fallback_to_database_url(self, monkeypatch):
        monkeypatch.delenv("DATABASE_URL_SYNC", raising=False)
        monkeypatch.setenv("DATABASE_URL", "postgres://synth_user:synth_pass@stage-db:5432/aik_db?sslmode=verify-full")
        url = alembic_env.get_database_url()
        assert url == "postgresql+psycopg2://synth_user:synth_pass@stage-db:5432/aik_db?sslmode=verify-full"
