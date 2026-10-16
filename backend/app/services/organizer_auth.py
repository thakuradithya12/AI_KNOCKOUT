"""Organizer authentication service for server-managed OIDC sessions."""

from __future__ import annotations

import base64
from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import secrets
import time
from typing import Any, Optional, Union

import httpx
import jwt
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.requests import Request
from starlette.responses import Response

from app.core.config import Settings, get_settings
from app.models.organizer import OrganizerSession


class OrganizerAuthError(Exception):
    """Base exception for organizer authentication errors."""


class OrganizerNotAuthorizedError(OrganizerAuthError):
    """Raised when an identity is not in the organizer allowlist."""


class OrganizerOidcNotConfiguredError(OrganizerAuthError):
    """Raised when trusted OIDC provider configuration is missing."""


class OrganizerSessionExpiredError(OrganizerAuthError):
    """Raised when an organizer session is expired or inactive."""


class OrganizerCsrfError(OrganizerAuthError):
    """Raised when CSRF token verification fails."""


class OrganizerStateError(OrganizerAuthError):
    """Raised when OIDC state is invalid, expired, or tampered."""


class OrganizerUnverifiedEmailError(OrganizerAuthError):
    """Raised when provider ID token claims indicate email is not verified."""


class OrganizerTokenValidationError(OrganizerAuthError):
    """Raised when ID token signature, claims, or nonce validation fails."""


def hash_session_token(token: str) -> str:
    """Return SHA-256 hex digest of raw session token."""
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def generate_tokens() -> tuple[str, str, str]:
    """Generate (raw_session_token, session_token_hash, csrf_token)."""
    raw_token = f"aik_org_{secrets.token_urlsafe(32)}"
    token_hash = hash_session_token(raw_token)
    csrf_token = secrets.token_urlsafe(32)
    return raw_token, token_hash, csrf_token


def get_session_cookie_name(settings: Settings) -> str:
    """Return consistent session cookie name adhering to RFC 6265bis prefixes in production."""
    return "__Host-aik_org_sess" if settings.is_deployed else "aik_org_sess"


def get_state_cookie_name(settings: Settings) -> str:
    """Return consistent OIDC state cookie name adhering to RFC 6265bis prefixes in production."""
    return "__Host-aik_oidc_state" if settings.is_deployed else "aik_oidc_state"


def create_signed_oidc_state(
    state: str,
    code_verifier: str,
    nonce: str,
    secret_key: str,
) -> str:
    """Generate an HMAC-SHA256 signed tamper-proof state token for OIDC initiation."""
    payload = {
        "state": state,
        "code_verifier": code_verifier,
        "nonce": nonce,
        "exp": int(datetime.now(timezone.utc).timestamp()) + 600,  # 10 minutes
    }
    return jwt.encode(payload, secret_key, algorithm="HS256")


def verify_signed_oidc_state(
    signed_state: str,
    expected_state: str,
    secret_key: str,
) -> tuple[str, str]:
    """Verify signed state token against expected state.

    Returns (code_verifier, nonce) or raises OrganizerStateError.
    """
    try:
        payload = jwt.decode(signed_state, secret_key, algorithms=["HS256"])
    except jwt.ExpiredSignatureError as exc:
        raise OrganizerStateError("OIDC state expired") from exc
    except jwt.PyJWTError as exc:
        raise OrganizerStateError("Invalid OIDC state signature") from exc

    stored_state = payload.get("state")
    if not stored_state or not hmac.compare_digest(stored_state, expected_state):
        raise OrganizerStateError("OIDC state mismatch")

    code_verifier = payload.get("code_verifier")
    nonce = payload.get("nonce")
    if not code_verifier or not nonce:
        raise OrganizerStateError("Malformed OIDC state payload")

    return code_verifier, nonce


def apply_session_and_clear_state_cookies(
    response: Response,
    raw_token: str,
    settings: Settings,
) -> None:
    """Apply session cookie and clear state cookies on outgoing response."""
    # 1. Clear state cookies
    response.delete_cookie(key=get_state_cookie_name(settings), path="/")
    response.delete_cookie(key="aik_oidc_state", path="/")
    response.delete_cookie(key="__Host-aik_oidc_state", path="/")

    # 2. Set session cookie
    session_cookie_name = get_session_cookie_name(settings)
    response.set_cookie(
        key=session_cookie_name,
        value=raw_token,
        httponly=True,
        samesite="lax",
        secure=settings.is_deployed,
        max_age=settings.organizer_session_expire_minutes * 60,
        path="/",
    )
    if not settings.is_deployed:
        response.set_cookie(
            key="aik_org_sess",
            value=raw_token,
            httponly=True,
            samesite="lax",
            secure=False,
            max_age=settings.organizer_session_expire_minutes * 60,
            path="/",
        )


def build_oidc_authorization_url(
    state: str,
    code_challenge: str,
    nonce: str,
    settings: Optional[Settings] = None,
) -> str:
    """Construct full OIDC authorization redirect URL with PKCE and nonce."""
    cfg = settings or get_settings()
    if not cfg.is_oidc_configured:
        raise OrganizerOidcNotConfiguredError(
            "Trusted OIDC identity provider is not configured. Contact event infrastructure owner."
        )

    base = cfg.oidc_issuer_url.rstrip("/")
    if "accounts.google.com" in base or "google" in base:
        auth_endpoint = "https://accounts.google.com/o/oauth2/v2/auth"
    elif "keycloak" in base:
        auth_endpoint = f"{base}/protocol/openid-connect/auth"
    else:
        auth_endpoint = f"{base}/authorize"
    redirect_uri = cfg.oidc_redirect_uri or "/api/v1/auth/organizer/callback"
    client_id = cfg.oidc_client_id or ""

    return (
        f"{auth_endpoint}?response_type=code"
        f"&client_id={client_id}"
        f"&redirect_uri={redirect_uri}"
        f"&scope=openid%20email%20profile"
        f"&state={state}"
        f"&code_challenge={code_challenge}"
        f"&code_challenge_method=S256"
        f"&nonce={nonce}"
    )


async def exchange_oidc_code(
    code: str,
    code_verifier: str,
    settings: Settings,
    http_client: Optional[httpx.AsyncClient] = None,
) -> dict:
    """Exchange authorization code and PKCE code_verifier with OIDC provider token endpoint."""
    if not settings.is_oidc_configured:
        raise OrganizerOidcNotConfiguredError("Trusted OIDC provider is not configured.")

    base = settings.oidc_issuer_url.rstrip("/")
    if "accounts.google.com" in base or "google" in base:
        token_endpoint = "https://oauth2.googleapis.com/token"
    elif "keycloak" in base:
        token_endpoint = f"{base}/protocol/openid-connect/token"
    else:
        token_endpoint = f"{base}/token"

    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": settings.oidc_redirect_uri or "/api/v1/auth/organizer/callback",
        "client_id": settings.oidc_client_id or "",
        "client_secret": settings.oidc_client_secret or "",
        "code_verifier": code_verifier,
    }

    if http_client is not None:
        resp = await http_client.post(
            token_endpoint,
            data=payload,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    else:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(
                token_endpoint,
                data=payload,
                headers={"Content-Type": "application/x-www-form-urlencoded"},
            )

    if resp.status_code != 200:
        raise OrganizerAuthError(f"OIDC token endpoint error {resp.status_code}: {resp.text}")

    try:
        return resp.json()
    except Exception as exc:
        raise OrganizerAuthError("Invalid JSON from OIDC token endpoint") from exc


def validate_oidc_id_token(
    id_token: str,
    expected_nonce: str,
    settings: Settings,
    verification_key: Optional[Union[str, bytes, Any]] = None,
    allowed_algorithms: Optional[list[str]] = None,
    jwks_client: Optional[jwt.PyJWKClient] = None,
) -> dict:
    """Validate OIDC ID Token signature, claims, and verified email status.

    Validates:
    - Signature: RS256 using JWKS or verification_key, or HS256 if configured
    - Issuer: claims['iss'] == settings.oidc_issuer_url.rstrip('/')
    - Audience: claims['aud'] == settings.oidc_client_id
    - Expiration: claims['exp'] > now
    - Nonce: claims['nonce'] == expected_nonce
    - Email verified: claims.get('email_verified') is True
    - Email presence: claims.get('email') is present and non-empty
    """
    issuer_str = settings.oidc_issuer_url.rstrip("/") if settings.oidc_issuer_url else ""
    if "accounts.google.com" in issuer_str or "google" in issuer_str:
        issuer_val: Union[str, list[str]] = ["https://accounts.google.com", "accounts.google.com"]
    else:
        issuer_val = issuer_str
    audience = settings.oidc_client_id

    algs = allowed_algorithms or ["RS256", "HS256"]
    key = verification_key

    try:
        if key is None and jwks_client is not None:
            signing_key = jwks_client.get_signing_key_from_jwt(id_token)
            key = signing_key.key

        if key is None:
            unverified_header = jwt.get_unverified_header(id_token)
            token_alg = unverified_header.get("alg")
            if token_alg == "HS256" and settings.oidc_client_secret:
                key = settings.oidc_client_secret
                algs = ["HS256"]
            elif token_alg == "RS256":
                if "accounts.google.com" in issuer_str or "google" in issuer_str:
                    jwks_url = "https://www.googleapis.com/oauth2/v3/certs"
                elif "keycloak" in issuer_str:
                    jwks_url = f"{issuer_str}/protocol/openid-connect/certs"
                else:
                    jwks_url = f"{issuer_str}/jwks"
                client = jwt.PyJWKClient(jwks_url)
                signing_key = client.get_signing_key_from_jwt(id_token)
                key = signing_key.key
                algs = ["RS256"]
            else:
                raise OrganizerTokenValidationError(f"Unsupported ID token algorithm: {token_alg}")

        claims = jwt.decode(
            id_token,
            key,
            algorithms=algs,
            audience=audience,
            issuer=issuer_val,
            options={"require": ["exp", "iss", "aud", "nonce", "email"]},
        )
    except jwt.ExpiredSignatureError as exc:
        raise OrganizerTokenValidationError("ID token has expired") from exc
    except jwt.InvalidIssuerError as exc:
        raise OrganizerTokenValidationError("ID token issuer mismatch") from exc
    except jwt.InvalidAudienceError as exc:
        raise OrganizerTokenValidationError("ID token audience mismatch") from exc
    except jwt.PyJWTError as exc:
        raise OrganizerTokenValidationError(f"Invalid ID token signature or claims: {exc}") from exc

    # Nonce verification
    token_nonce = claims.get("nonce")
    if not token_nonce or not hmac.compare_digest(str(token_nonce), expected_nonce):
        raise OrganizerTokenValidationError("OIDC_NONCE_MISMATCH")

    # Email verified requirement
    email_verified = claims.get("email_verified")
    if email_verified is not True:
        raise OrganizerUnverifiedEmailError("UNVERIFIED_EMAIL")

    # Email claim requirement
    email = claims.get("email")
    if not email or not isinstance(email, str) or not email.strip():
        raise OrganizerTokenValidationError("MISSING_ORGANIZER_EMAIL")

    return claims


async def generate_organizer_session(
    db: AsyncSession,
    email: str,
    name: Optional[str] = None,
    ip: Optional[str] = None,
    user_agent: Optional[str] = None,
    settings: Optional[Settings] = None,
    bypass_allowlist_check: bool = False,
) -> tuple[str, str, OrganizerSession]:
    """Create and persist a server-managed organizer session.

    Validates that the email is on the organizer allowlist before issuing tokens,
    unless bypass_allowlist_check is True (e.g. for approved database accounts).
    Returns (raw_session_token, csrf_token, session).
    """
    cfg = settings or get_settings()
    normalized_email = email.strip().lower()

    if not bypass_allowlist_check and cfg.organizer_allowed_emails_list and normalized_email not in cfg.organizer_allowed_emails_list:
        raise OrganizerNotAuthorizedError(f"Email {normalized_email!r} is not in the organizer allowlist.")

    raw_token, token_hash, csrf_token = generate_tokens()
    now = datetime.now(timezone.utc)
    expires_at = now + timedelta(minutes=cfg.organizer_session_expire_minutes)

    session = OrganizerSession(
        session_token_hash=token_hash,
        organizer_email=normalized_email,
        organizer_name=name,
        csrf_token=csrf_token,
        expires_at=expires_at,
        last_active_at=now,
        revoked_at=None,
        created_ip=ip,
        user_agent=user_agent,
    )
    db.add(session)
    await db.flush()
    return raw_token, csrf_token, session


async def validate_organizer_session(
    db: AsyncSession,
    raw_token: str,
    settings: Optional[Settings] = None,
) -> Optional[OrganizerSession]:
    """Validate raw session token and update last_active_at if valid."""
    cfg = settings or get_settings()
    token_hash = hash_session_token(raw_token)

    session = await db.scalar(
        select(OrganizerSession).where(OrganizerSession.session_token_hash == token_hash)
    )
    if session is None:
        return None

    if not session.is_active(cfg.organizer_session_idle_minutes):
        return None

    session.last_active_at = datetime.now(timezone.utc)
    await db.flush()
    return session


async def revoke_organizer_session(
    db: AsyncSession,
    raw_token: str,
) -> bool:
    """Revoke a specific active organizer session."""
    token_hash = hash_session_token(raw_token)
    session = await db.scalar(
        select(OrganizerSession).where(OrganizerSession.session_token_hash == token_hash)
    )
    if session is not None and session.revoked_at is None:
        session.revoked_at = datetime.now(timezone.utc)
        await db.flush()
        return True
    return False


async def revoke_all_organizer_sessions(
    db: AsyncSession,
    email: Optional[str] = None,
) -> int:
    """Revoke all active organizer sessions, optionally filtered by email."""
    now = datetime.now(timezone.utc)
    query = update(OrganizerSession).where(OrganizerSession.revoked_at.is_(None)).values(revoked_at=now)
    if email:
        query = query.where(OrganizerSession.organizer_email == email.strip().lower())
    result = await db.execute(query)
    await db.flush()
    return result.rowcount or 0


def verify_csrf_token(session: OrganizerSession, header_csrf: Optional[str]) -> bool:
    """Validate CSRF token header against session's stored CSRF token in constant time."""
    if not header_csrf:
        return False
    return hmac.compare_digest(session.csrf_token, header_csrf)


def extract_organizer_token(request: Request) -> Optional[str]:
    """Extract raw organizer session token from cookies or Authorization header."""
    cookie_token = request.cookies.get("__Host-aik_org_sess") or request.cookies.get("aik_org_sess")
    if cookie_token:
        return cookie_token
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        return auth_header[7:].strip()
    return None


def extract_state_cookie(request: Request) -> Optional[str]:
    """Extract OIDC state cookie from request."""
    return request.cookies.get("__Host-aik_oidc_state") or request.cookies.get("aik_oidc_state")
