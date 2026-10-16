"""Organizer cookies must use deployed security attributes in staging and production."""

from starlette.responses import Response

from app.core.config import Settings
from app.services.organizer_auth import (
    apply_session_and_clear_state_cookies,
    get_session_cookie_name,
    get_state_cookie_name,
)


def _settings(monkeypatch, environment: str) -> Settings:
    monkeypatch.setenv("SECRET_KEY", "s" * 64)
    monkeypatch.setenv("DATABASE_URL", "postgresql+asyncpg://user:pass@localhost/db")
    monkeypatch.setenv("APP_ENV", environment)
    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://event.example")
    return Settings(_env_file=None)


def test_staging_organizer_cookies_are_secure_and_host_scoped(monkeypatch):
    settings = _settings(monkeypatch, "staging")
    assert get_session_cookie_name(settings) == "__Host-aik_org_sess"
    assert get_state_cookie_name(settings) == "__Host-aik_oidc_state"

    response = Response()
    apply_session_and_clear_state_cookies(response, "session-value", settings)
    cookie_headers = response.headers.getlist("set-cookie")
    session_cookie = next(value for value in cookie_headers if value.startswith("__Host-aik_org_sess="))
    assert "; Secure" in session_cookie
    assert "; HttpOnly" in session_cookie
    assert not any(value.startswith("aik_org_sess=") for value in cookie_headers)


def test_development_organizer_cookie_keeps_local_http_compatibility(monkeypatch):
    settings = _settings(monkeypatch, "development")
    assert get_session_cookie_name(settings) == "aik_org_sess"

    response = Response()
    apply_session_and_clear_state_cookies(response, "session-value", settings)
    cookie_headers = response.headers.getlist("set-cookie")
    session_cookie = next(value for value in cookie_headers if value.startswith("aik_org_sess="))
    assert "; Secure" not in session_cookie
    assert "; HttpOnly" in session_cookie
