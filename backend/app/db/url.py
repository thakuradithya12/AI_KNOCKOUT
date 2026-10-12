"""Database URL normalization and validation helper.

Centralizes database URL transformation for AI Knockout services:
- Normalizes PostgreSQL URLs (e.g. `postgres://`, `postgresql://`) to `postgresql+asyncpg://`
  for asynchronous SQLAlchemy engines (FastAPI app, worker gateway, match server).
- Normalizes PostgreSQL URLs to `postgresql+psycopg2://` for synchronous operations
  (Alembic migrations).
- Normalizes SSL parameters deliberately:
  - Translates `sslmode` to `ssl` for asyncpg
    (`require`, `prefer`, `allow`, `disable`, `verify-ca`, `verify-full`).
  - Strips unsupported keyword arguments (e.g. `sslmode`, `sslrootcert`, `sslcert`, `sslkey`)
    from asyncpg connection URL query strings to prevent `unexpected keyword argument` errors.
  - Preserves client-certificate and CA verification behavior via `build_async_ssl_context`
    and `get_asyncpg_connect_args`.
  - Preserves compatible `sslmode` and certificate query semantics for psycopg2.
- Preserves SQLite URLs for test suites (`sqlite+aiosqlite://`, `sqlite+pysqlite://`).
- Preserves credentials, hostname, port, database name, and supported query parameters.
- Rejects unsupported schemes and invalid SSL modes with safe, actionable errors.
- Never logs connection strings or credentials.
"""

from __future__ import annotations

import os
import ssl as ssl_module
from typing import Any

from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

# Supported dialects/backends
_POSTGRES_SCHEMES = frozenset({"postgres", "postgresql"})
_SQLITE_SCHEMES = frozenset({"sqlite"})

# Valid PostgreSQL SSL modes
VALID_SSL_MODES = frozenset(
    {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}
)

SSL_MODE_ALIASES: dict[str, str] = {
    "verify_ca": "verify-ca",
    "verify_full": "verify-full",
    "true": "require",
    "false": "disable",
    "1": "require",
    "0": "disable",
}


def canonicalize_ssl_mode(raw: str) -> str:
    """Validate and canonicalize SSL mode string.

    Accepts standard PostgreSQL SSL modes and boolean-like provider strings.
    Raises ValueError with safe error message on invalid modes without leaking credentials.
    """
    val = raw.strip().lower()
    if val in SSL_MODE_ALIASES:
        return SSL_MODE_ALIASES[val]
    if val in VALID_SSL_MODES:
        return val
    raise ValueError(
        f"Invalid SSL mode '{raw}'. Supported modes are: "
        "disable, allow, prefer, require, verify-ca, verify-full."
    )


def _normalize_postgres_query(query: dict[str, Any], *, is_async: bool) -> dict[str, Any]:
    """Normalize query parameters according to driver requirements.

    For asyncpg:
    - Translates sslmode to ssl.
    - Canonicalizes SSL modes without silently weakening certificate verification.
    - Strips unsupported keyword arguments (sslrootcert, sslcert, sslkey, sslcrl, sslpassword)
      so they are not forwarded blindly as kwargs to asyncpg.connect.
    - Translates connect_timeout to timeout.
    - Removes application_name from URL kwargs (handled via connect_args['server_settings']).

    For psycopg2:
    - Translates ssl to sslmode.
    - Preserves sslrootcert, sslcert, sslkey, sslcrl, application_name, etc.
    """
    q = dict(query)

    # 1. Determine and canonicalize SSL mode
    raw_ssl = q.pop("sslmode", None)
    if raw_ssl is None:
        raw_ssl = q.pop("ssl", None)
    else:
        # If both are present, pop both so we don't leave conflicting parameters
        q.pop("ssl", None)

    mode = canonicalize_ssl_mode(raw_ssl) if raw_ssl is not None else None

    if is_async:
        if mode is not None:
            q["ssl"] = mode

        # asyncpg.connect does not accept certificate file paths as kwargs.
        # Strip them from URL kwargs to prevent unexpected keyword argument errors.
        for param in (
            "sslrootcert", "sslcert", "sslkey", "sslcrl", "sslpassword",
            "sslnegotiation", "channel_binding"
        ):
            q.pop(param, None)

        # connect_timeout -> timeout in asyncpg
        if "connect_timeout" in q:
            ct = q.pop("connect_timeout")
            if "timeout" not in q:
                q["timeout"] = ct

        # application_name is not an asyncpg kwarg; remove from URL kwargs
        q.pop("application_name", None)

    else:
        if mode is not None:
            q["sslmode"] = mode

    return q


def normalize_database_url(url: str, *, is_async: bool = True) -> str:
    """Normalize a database URL for the required driver (async or sync).

    Args:
        url: Raw database URL from configuration or environment.
        is_async: True to normalize for async driver (asyncpg/aiosqlite),
                  False for sync driver (psycopg2/sqlite).

    Returns:
        The normalized database URL string.

    Raises:
        ValueError: If the URL is empty, malformed, has an unsupported scheme,
                    or contains an invalid SSL mode.
                    Exception messages never contain connection strings or credentials.
    """
    if not isinstance(url, str) or not url.strip():
        raise ValueError("Database URL must not be empty.")

    raw_url = url.strip()

    try:
        parsed = make_url(raw_url)
    except (ArgumentError, ValueError):
        raise ValueError(
            "Invalid database URL format. Scheme must be specified (e.g. postgresql:// or sqlite://)."
        ) from None

    drivername = parsed.drivername
    if not drivername:
        raise ValueError(
            "Invalid database URL format. Scheme must be specified (e.g. postgresql:// or sqlite://)."
        )

    backend = drivername.split("+")[0].lower()

    if backend in _POSTGRES_SCHEMES:
        target_driver = "postgresql+asyncpg" if is_async else "postgresql+psycopg2"
        normalized_query = _normalize_postgres_query(dict(parsed.query), is_async=is_async)
        return parsed.set(drivername=target_driver, query=normalized_query).render_as_string(
            hide_password=False
        )

    if backend in _SQLITE_SCHEMES:
        if is_async:
            target_driver = "sqlite+aiosqlite"
            if drivername == target_driver:
                return raw_url
            return parsed.set(drivername=target_driver).render_as_string(hide_password=False)
        else:
            # For sync SQLite: preserve sqlite/sqlite+pysqlite; convert aiosqlite to sqlite
            if drivername in ("sqlite", "sqlite+pysqlite"):
                return raw_url
            return parsed.set(drivername="sqlite").render_as_string(hide_password=False)

    raise ValueError(
        f"Unsupported database scheme '{backend}'. "
        "Supported schemes are PostgreSQL (postgres://, postgresql://) and SQLite (sqlite://)."
    )


def normalize_async_database_url(url: str) -> str:
    """Normalize a database URL for async execution (asyncpg/aiosqlite)."""
    return normalize_database_url(url, is_async=True)


def normalize_sync_database_url(url: str) -> str:
    """Normalize a database URL for sync execution (psycopg2/sqlite)."""
    return normalize_database_url(url, is_async=False)


def build_async_ssl_context(url: str) -> ssl_module.SSLContext | None:
    """Build an ssl.SSLContext from URL parameters if custom certificates are configured.

    Returns None for standard SSL modes (e.g. require, prefer, allow, disable)
    where asyncpg natively handles SSL negotiation without an explicit SSLContext.
    """
    if not isinstance(url, str) or not url.strip():
        return None

    try:
        parsed = make_url(url.strip())
    except Exception:
        return None

    query = dict(parsed.query)
    raw_ssl = query.get("sslmode") or query.get("ssl")

    cafile = query.get("sslrootcert")
    certfile = query.get("sslcert")
    keyfile = query.get("sslkey")

    # If no certificate files are specified, let asyncpg handle standard modes natively
    if not cafile and not certfile and not keyfile:
        return None

    mode = canonicalize_ssl_mode(raw_ssl) if raw_ssl else "verify-full"
    if mode == "disable":
        return None

    ctx = ssl_module.create_default_context()
    if mode in ("require", "allow", "prefer"):
        ctx.check_hostname = False
        ctx.verify_mode = ssl_module.CERT_NONE
    elif mode == "verify-ca":
        ctx.check_hostname = False
        ctx.verify_mode = ssl_module.CERT_REQUIRED
    elif mode == "verify-full":
        ctx.check_hostname = True
        ctx.verify_mode = ssl_module.CERT_REQUIRED

    if cafile:
        if not os.path.exists(cafile):
            raise FileNotFoundError(f"Root certificate file '{cafile}' does not exist.")
        ctx.load_verify_locations(cafile=cafile)

    if certfile:
        if not os.path.exists(certfile):
            raise FileNotFoundError(f"Client certificate file '{certfile}' does not exist.")
        if keyfile and not os.path.exists(keyfile):
            raise FileNotFoundError(f"Client key file '{keyfile}' does not exist.")
        ctx.load_cert_chain(certfile=certfile, keyfile=keyfile)

    return ctx


def get_asyncpg_connect_args(url: str) -> dict[str, Any]:
    """Return connect_args dictionary for create_async_engine.

    Includes explicit SSL context if custom certificates are configured,
    and server_settings (such as application_name) if specified.
    """
    connect_args: dict[str, Any] = {}
    ssl_ctx = build_async_ssl_context(url)
    if ssl_ctx is not None:
        connect_args["ssl"] = ssl_ctx

    try:
        parsed = make_url(url.strip())
        app_name = parsed.query.get("application_name")
        if app_name:
            connect_args["server_settings"] = {"application_name": app_name}
    except Exception:
        pass

    return connect_args
