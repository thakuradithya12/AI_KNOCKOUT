"""Safety checks for opt-in PostgreSQL integration test configuration."""

import pytest
from sqlalchemy.engine import make_url

from test_support.postgres_test_database import postgres_test_database_urls


def test_default_test_database_remains_isolated_sqlite() -> None:
    assert postgres_test_database_urls(None) is None


@pytest.mark.parametrize(
    "url",
    [
        "sqlite+aiosqlite:///:memory:",
        "postgresql+asyncpg://test:secret@staging.example/aik_test_staging",
        "postgresql+asyncpg://test:secret@localhost/aik_staging",
        "not a database url",
    ],
)
def test_rejects_nonlocal_or_nontest_database_urls(url: str) -> None:
    with pytest.raises(ValueError, match=r"local PostgreSQL database named aik_test_\*"):
        postgres_test_database_urls(url)


def test_local_dedicated_postgres_url_produces_driver_specific_urls() -> None:
    urls = postgres_test_database_urls(
        "postgresql://test-user:private-test-password@postgres/aik_test_integration"
    )

    assert urls is not None
    async_url, sync_url = map(make_url, urls)
    assert async_url.drivername == "postgresql+asyncpg"
    assert sync_url.drivername == "postgresql+psycopg2"
    assert async_url.database == sync_url.database == "aik_test_integration"
    assert async_url.password == sync_url.password == "private-test-password"
