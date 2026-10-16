"""Organizer OIDC authentication and session management routes."""

from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import secrets
from typing import Any, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response, status
from fastapi.responses import JSONResponse, RedirectResponse
import httpx
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import get_db, require_organizer
from app.core.config import Settings, get_settings
from app.services.rate_limiter import RateLimiter
from app.core.security import (
    async_hash_access_key,
    async_verify_access_key,
    async_verify_dummy_hash,
)
from app.models.organizer import OrganizerAccount, OrganizerSession
from app.services.organizer_auth import (
    OrganizerAuthError,
    OrganizerStateError,
    OrganizerTokenValidationError,
    OrganizerUnverifiedEmailError,
    apply_session_and_clear_state_cookies,
    build_oidc_authorization_url,
    create_signed_oidc_state,
    exchange_oidc_code,
    extract_organizer_token,
    extract_state_cookie,
    generate_organizer_session,
    get_state_cookie_name,
    hash_session_token,
    revoke_organizer_session,
    validate_oidc_id_token,
    verify_csrf_token,
    verify_signed_oidc_state,
)

router = APIRouter()


def get_oidc_http_client() -> Optional[httpx.AsyncClient]:
    """Dependency hook for OIDC token exchange HTTP client (overridable in tests)."""
    return None


def get_oidc_key_resolver() -> Optional[Any]:
    """Dependency hook for OIDC ID token verification key / JWKS (overridable in tests)."""
    return None


class OrganizerCallbackRequest(BaseModel):
    """Authoritative OIDC callback request model.

    Identity (email, name, roles) is never accepted from the client request body.
    Identity is strictly derived from the validated OIDC ID token returned by the IdP.
    """
    model_config = ConfigDict(extra="ignore")

    code: str
    state: str
    code_verifier: Optional[str] = None


@router.get(
    "/login",
    summary="Initiate OIDC organizer login flow",
    description=(
        "Generates PKCE challenge, state, and nonce, sets secure state cookie, "
        "and returns OIDC authorization URL. Fails closed with 503 if trusted OIDC provider is not configured."
    ),
)
async def organizer_login(
    response: Response,
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    if not settings.is_oidc_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ORGANIZER_OIDC_NOT_CONFIGURED",
        )

    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    nonce = secrets.token_urlsafe(32)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

    signed_state = create_signed_oidc_state(
        state=state,
        code_verifier=verifier,
        nonce=nonce,
        secret_key=settings.secret_key,
    )

    state_cookie_name = get_state_cookie_name(settings)
    response.set_cookie(
        key=state_cookie_name,
        value=signed_state,
        httponly=True,
        samesite="lax",
        secure=settings.is_deployed,
        max_age=600,  # 10 minutes
        path="/",
    )
    if not settings.is_deployed:
        response.set_cookie(
            key="aik_oidc_state",
            value=signed_state,
            httponly=True,
            samesite="lax",
            secure=False,
            max_age=600,
            path="/",
        )

    auth_url = build_oidc_authorization_url(
        state=state,
        code_challenge=challenge,
        nonce=nonce,
        settings=settings,
    )
    return {
        "authorization_url": auth_url,
        "state": state,
    }


async def _execute_oidc_callback_exchange(
    code: str,
    state: str,
    request: Request,
    db: AsyncSession,
    settings: Settings,
    http_client: Optional[httpx.AsyncClient] = None,
    key_resolver: Optional[Any] = None,
) -> tuple[str, str, OrganizerSession]:
    """Execute complete server-side OIDC authorization code exchange and validation.

    Enforces:
    1. Provider configuration requirement (503 fail-closed).
    2. Single-use code verification (anti-replay).
    3. Signed state cookie verification and constant-time state matching.
    4. Server-to-server authorization code exchange with IdP using stored PKCE verifier.
    5. Cryptographic ID token validation (issuer, audience, signature, expiration, nonce).
    6. Verified email requirement (email_verified == True).
    7. Organizer email allowlist enforcement.
    8. Server-managed session persistence.
    """
    if not settings.is_oidc_configured:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="ORGANIZER_OIDC_NOT_CONFIGURED",
        )

    # 1. State verification from signed cookie
    state_cookie = extract_state_cookie(request)
    if not state_cookie:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="INVALID_OIDC_STATE",
        )

    try:
        cookie_verifier, expected_nonce = verify_signed_oidc_state(
            signed_state=state_cookie,
            expected_state=state,
            secret_key=settings.secret_key,
        )
    except OrganizerStateError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="INVALID_OIDC_STATE",
        )

    # Authoritative code verifier comes from our verified signed cookie
    code_verifier = cookie_verifier

    # 2. Exchange code for tokens at IdP token endpoint
    try:
        token_data = await exchange_oidc_code(
            code=code,
            code_verifier=code_verifier,
            settings=settings,
            http_client=http_client,
        )
    except OrganizerAuthError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="OIDC_TOKEN_EXCHANGE_FAILED",
        )

    id_token = token_data.get("id_token")
    if not id_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="OIDC_MISSING_ID_TOKEN",
        )

    # 4. Cryptographic validation of ID token
    try:
        claims = validate_oidc_id_token(
            id_token=id_token,
            expected_nonce=expected_nonce,
            settings=settings,
            verification_key=key_resolver,
        )
    except OrganizerUnverifiedEmailError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="UNVERIFIED_EMAIL",
        )
    except OrganizerTokenValidationError as exc:
        err_msg = str(exc)
        if "nonce" in err_msg.lower():
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="INVALID_OIDC_NONCE",
            )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="INVALID_OIDC_ID_TOKEN",
        )

    # 5. Extract validated identity claims (never trusting client request body or query params)
    email = claims.get("email", "")
    norm_email = email.strip().lower()
    if not norm_email:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="MISSING_ORGANIZER_EMAIL",
        )

    # 6. Enforce organizer allowlist
    if settings.organizer_allowed_emails_list and norm_email not in settings.organizer_allowed_emails_list:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="ORGANIZER_NOT_AUTHORIZED",
        )

    client_ip = request.client.host if request.client else None
    user_agent = request.headers.get("User-Agent")

    # 7. Issue server-managed session
    raw_token, csrf_token, session = await generate_organizer_session(
        db=db,
        email=norm_email,
        name=claims.get("name"),
        ip=client_ip,
        user_agent=user_agent,
        settings=settings,
    )
    await db.commit()

    return raw_token, csrf_token, session


@router.get(
    "/callback",
    summary="Browser OIDC authorization-code redirect callback",
    description=(
        "Standard browser GET callback invoked by the identity provider redirect. "
        "Validates state from query and signed cookie, exchanges authorization code using stored PKCE verifier, "
        "validates ID token claims and allowlist, sets session cookie, and redirects browser to organizer console."
    ),
)
async def organizer_callback_get(
    request: Request,
    code: Optional[str] = Query(None),
    state: Optional[str] = Query(None),
    error: Optional[str] = Query(None),
    error_description: Optional[str] = Query(None),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    http_client: Optional[httpx.AsyncClient] = Depends(get_oidc_http_client),
    key_resolver: Optional[Any] = Depends(get_oidc_key_resolver),
) -> Response:
    if error:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"OIDC_PROVIDER_ERROR: {error_description or error}",
        )
    if not code or not state:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="INVALID_OIDC_CALLBACK",
        )

    raw_token, csrf_token, session = await _execute_oidc_callback_exchange(
        code=code,
        state=state,
        request=request,
        db=db,
        settings=settings,
        http_client=http_client,
        key_resolver=key_resolver,
    )

    # Check if client explicitly requests JSON (API client / test harness)
    accept = request.headers.get("accept", "")
    if "application/json" in accept and "text/html" not in accept:
        json_resp = JSONResponse(
            content={
                "status": "ok",
                "session_token": raw_token,
                "csrf_token": csrf_token,
                "organizer_email": session.organizer_email,
                "organizer_name": session.organizer_name,
                "expires_at": session.expires_at.isoformat(),
                "redirect_url": "/portal#admin",
            },
            status_code=status.HTTP_200_OK,
        )
        apply_session_and_clear_state_cookies(response=json_resp, raw_token=raw_token, settings=settings)
        return json_resp

    # Standard browser flow: 303 See Other redirect to /portal#admin
    redirect_resp = RedirectResponse(
        url="/portal#admin",
        status_code=status.HTTP_303_SEE_OTHER,
    )
    apply_session_and_clear_state_cookies(response=redirect_resp, raw_token=raw_token, settings=settings)
    return redirect_resp


@router.post(
    "/callback",
    summary="Exchange OIDC callback for organizer session (JSON API)",
    description=(
        "Validates OIDC state/PKCE/nonce, exchanges code for ID token via trusted provider, "
        "validates token claims and verified email, enforces allowlist, and issues server-managed session."
    ),
)
async def organizer_callback_post(
    payload: OrganizerCallbackRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    http_client: Optional[httpx.AsyncClient] = Depends(get_oidc_http_client),
    key_resolver: Optional[Any] = Depends(get_oidc_key_resolver),
) -> dict:
    raw_token, csrf_token, session = await _execute_oidc_callback_exchange(
        code=payload.code,
        state=payload.state,
        request=request,
        db=db,
        settings=settings,
        http_client=http_client,
        key_resolver=key_resolver,
    )
    apply_session_and_clear_state_cookies(response=response, raw_token=raw_token, settings=settings)

    return {
        "status": "ok",
        "session_token": raw_token,
        "csrf_token": csrf_token,
        "organizer_email": session.organizer_email,
        "organizer_name": session.organizer_name,
        "expires_at": session.expires_at.isoformat(),
    }


@router.get(
    "/session",
    summary="Inspect current organizer session profile",
    description="Returns verified organizer identity, CSRF token, and expiry details for active session.",
)
async def organizer_session_profile(
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    raw_token = extract_organizer_token(request)
    if not raw_token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="ORGANIZER_AUTHENTICATION_REQUIRED",
        )

    token_hash = hash_session_token(raw_token)
    session = await db.scalar(
        select(OrganizerSession).where(OrganizerSession.session_token_hash == token_hash)
    )
    if session is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="ORGANIZER_SESSION_INVALID",
        )
    if not session.is_active(settings.organizer_session_idle_minutes):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="ORGANIZER_SESSION_EXPIRED",
        )

    session.last_active_at = datetime.now(timezone.utc)
    await db.commit()

    return {
        "organizer_email": session.organizer_email,
        "organizer_name": session.organizer_name,
        "csrf_token": session.csrf_token,
        "expires_at": session.expires_at.isoformat(),
        "last_active_at": session.last_active_at.isoformat(),
    }


@router.post(
    "/logout",
    summary="Revoke organizer session and clear session cookies",
    description="Revokes server-managed session in database and clears browser cookies. Requires CSRF verification if session is active.",
)
async def organizer_logout(
    request: Request,
    response: Response,
    x_csrf_token: Optional[str] = Header(None, alias="X-CSRF-Token"),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict[str, str]:
    raw_token = extract_organizer_token(request)
    if raw_token:
        token_hash = hash_session_token(raw_token)
        session = await db.scalar(
            select(OrganizerSession).where(OrganizerSession.session_token_hash == token_hash)
        )
        if session is not None and session.is_active(settings.organizer_session_idle_minutes):
            if not verify_csrf_token(session, x_csrf_token):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail="CSRF_VERIFICATION_FAILED",
                )
            await revoke_organizer_session(db, raw_token)
            await db.commit()

    response.delete_cookie(key="aik_org_sess", path="/")
    response.delete_cookie(key="__Host-aik_org_sess", path="/")
    response.delete_cookie(key="aik_oidc_state", path="/")
    response.delete_cookie(key="__Host-aik_oidc_state", path="/")
    return {"status": "ok", "message": "Successfully logged out."}


EMAIL_REGEX = r"^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$"


class OrganizerRegisterRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(..., max_length=255, pattern=EMAIL_REGEX)
    name: str = Field(min_length=2, max_length=100)
    password: str = Field(min_length=12, max_length=128)


class OrganizerPasswordLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(..., max_length=255, pattern=EMAIL_REGEX)
    password: str = Field(min_length=1, max_length=128)


class OrganizerApproveRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    email: str = Field(..., max_length=255, pattern=EMAIL_REGEX)
    role: str = Field(default="ORGANIZER", pattern="^(ORGANIZER|ADMIN)$")


def _validate_password_complexity(password: str) -> None:
    if len(password) < 12:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="PASSWORD_TOO_SHORT: Password must be at least 12 characters",
        )
    has_alpha = any(c.isalpha() for c in password)
    has_digit = any(c.isdigit() for c in password)
    if not (has_alpha and has_digit):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="PASSWORD_TOO_WEAK: Password must contain letters and numbers",
        )


@router.post(
    "/register",
    status_code=status.HTTP_201_CREATED,
    summary="Register a new organizer account",
    description="Public registration for organizer accounts. Public registrations are created with PENDING_ORGANIZER status and require administrator approval unless on the explicit bootstrap allowlist.",
)
async def organizer_register(
    payload: OrganizerRegisterRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    # Rate limit by IP — prevents account farming and enumeration
    client_ip = request.client.host if request.client else "unknown"
    allowed, retry_after = await RateLimiter.check_rate_limit(
        key_prefix="org_register_ip",
        identifier=client_ip,
        max_attempts=settings.rate_limit_login_attempts,
        window_seconds=settings.rate_limit_window_seconds,
    )
    if not allowed:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="ORGANIZER_REGISTER_RATE_LIMIT_EXCEEDED: Too many registration attempts from this IP.",
            headers={"Retry-After": str(retry_after)},
        )
    _validate_password_complexity(payload.password)
    norm_email = payload.email.strip().lower()

    # Check for existing organizer account
    existing = await db.scalar(
        select(OrganizerAccount).where(OrganizerAccount.email == norm_email)
    )
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="ORGANIZER_EMAIL_ALREADY_REGISTERED",
        )

    # Determine initial approval status: only bootstrap emails in allowlist are auto-approved
    is_bootstrap = norm_email in settings.organizer_allowed_emails_list
    initial_role = "ORGANIZER" if is_bootstrap else "PENDING_ORGANIZER"
    is_approved = is_bootstrap
    approved_by = "bootstrap_allowlist" if is_bootstrap else None
    approved_at = datetime.now(timezone.utc) if is_bootstrap else None

    hashed_pw = await async_hash_access_key(payload.password)
    account = OrganizerAccount(
        email=norm_email,
        name=payload.name.strip(),
        password_hash=hashed_pw,
        role=initial_role,
        is_approved=is_approved,
        approved_by=approved_by,
        approved_at=approved_at,
    )
    db.add(account)
    await db.commit()
    await db.refresh(account)

    return {
        "status": "APPROVED" if is_approved else "PENDING_APPROVAL",
        "email": account.email,
        "name": account.name,
        "role": account.role,
        "is_approved": account.is_approved,
        "message": (
            "Organizer account created and approved via allowlist."
            if is_approved
            else "Organizer registration submitted. An authorized administrator must approve your account before access is granted."
        ),
    }


@router.post(
    "/login-password",
    summary="Sign in to organizer account using email and password",
    description="Authenticates approved organizers with credentials and issues secure server-managed session.",
)
async def organizer_login_password(
    payload: OrganizerPasswordLoginRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> dict:
    # Rate limit by IP — prevents brute-force against organizer accounts
    client_ip = request.client.host if request.client else "unknown"
    allowed_ip, retry_after_ip = await RateLimiter.check_rate_limit(
        key_prefix="org_login_ip",
        identifier=client_ip,
        max_attempts=settings.rate_limit_login_attempts,
        window_seconds=settings.rate_limit_window_seconds,
    )
    if not allowed_ip:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                "ORGANIZER_LOGIN_RATE_LIMIT_EXCEEDED: Too many login attempts from this IP. "
                f"Retry in {retry_after_ip} seconds."
            ),
            headers={"Retry-After": str(retry_after_ip)},
        )
    # Rate limit by email — prevents targeted account brute-force
    norm_email = payload.email.strip().lower()
    allowed_email, retry_after_email = await RateLimiter.check_rate_limit(
        key_prefix="org_login_email",
        identifier=norm_email,
        max_attempts=settings.rate_limit_login_attempts,
        window_seconds=settings.rate_limit_window_seconds,
    )
    if not allowed_email:
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                "ORGANIZER_LOGIN_RATE_LIMIT_EXCEEDED: Too many login attempts for this account. "
                f"Retry in {retry_after_email} seconds."
            ),
            headers={"Retry-After": str(retry_after_email)},
        )
    account = await db.scalar(
        select(OrganizerAccount).where(OrganizerAccount.email == norm_email)
    )
    if account is None:
        await async_verify_dummy_hash(payload.password)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="INVALID_CREDENTIALS",
        )

    valid_pw = await async_verify_access_key(payload.password, account.password_hash)
    if not valid_pw:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="INVALID_CREDENTIALS",
        )

    if not account.is_approved:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="ORGANIZER_ACCOUNT_PENDING_APPROVAL: Account has not been approved by an administrator",
        )

    client_ip = request.client.host if request.client else None
    user_agent = request.headers.get("User-Agent")

    raw_token, csrf_token, session = await generate_organizer_session(
        db=db,
        email=account.email,
        name=account.name,
        ip=client_ip,
        user_agent=user_agent,
        settings=settings,
        bypass_allowlist_check=True,
    )
    await db.commit()

    apply_session_and_clear_state_cookies(response=response, raw_token=raw_token, settings=settings)

    return {
        "status": "ok",
        "session_token": raw_token,
        "csrf_token": csrf_token,
        "organizer_email": session.organizer_email,
        "organizer_name": session.organizer_name,
        "role": account.role,
        "expires_at": session.expires_at.isoformat(),
    }


@router.post(
    "/approve",
    summary="Approve a pending organizer registration",
    description="Authorizes a pending organizer account. Accessible only by already authenticated organizers.",
)
async def organizer_approve_account(
    payload: OrganizerApproveRequest,
    _actor: str = Depends(require_organizer),
    db: AsyncSession = Depends(get_db),
) -> dict:
    norm_email = payload.email.strip().lower()
    account = await db.scalar(
        select(OrganizerAccount).where(OrganizerAccount.email == norm_email)
    )
    if account is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="ORGANIZER_ACCOUNT_NOT_FOUND",
        )

    account.is_approved = True
    account.role = payload.role
    account.approved_by = _actor
    account.approved_at = datetime.now(timezone.utc)
    await db.commit()
    await db.refresh(account)

    return {
        "status": "APPROVED",
        "email": account.email,
        "name": account.name,
        "role": account.role,
        "is_approved": account.is_approved,
        "approved_by": account.approved_by,
        "approved_at": account.approved_at.isoformat() if account.approved_at else None,
    }


@router.get(
    "/accounts",
    summary="List organizer accounts",
    description="Lists registered organizer accounts and their approval status. Accessible only by authorized organizers.",
)
async def list_organizer_accounts(
    _actor: str = Depends(require_organizer),
    db: AsyncSession = Depends(get_db),
) -> list[dict]:
    statement = select(OrganizerAccount).order_by(OrganizerAccount.created_at.desc())
    accounts = (await db.execute(statement)).scalars().all()
    return [
        {
            "id": str(a.id),
            "email": a.email,
            "name": a.name,
            "role": a.role,
            "is_approved": a.is_approved,
            "approved_by": a.approved_by,
            "approved_at": a.approved_at.isoformat() if a.approved_at else None,
            "created_at": a.created_at.isoformat(),
        }
        for a in accounts
    ]

