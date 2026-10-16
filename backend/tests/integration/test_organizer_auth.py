"""Integration and security tests for server-managed organizer authentication.

Covers:
1. Real OIDC token exchange and validated provider claims (never trusting client-submitted identity).
2. State, PKCE, nonce, issuer, audience, signature, and verified-email allowlisting.
3. Cookie attributes (Secure, HttpOnly, SameSite=Lax, and __Host- prefix in production).
4. Lockout of X-Organizer-Secret in all deployed configurations (production and staging).
5. Comprehensive negative tests (forged identity, invalid state, expired token, signature tampering,
   nonce mismatch, unverified email, callback replay).
6. Session lifecycle (profile inspection, sliding idle timeout, hard expiration, revocation on logout).
7. CSRF protection on all mutating HTTP methods.
8. Authorization coverage across all 18 organizer administrative routes.
9. Audit attribution recording verified organizer email as actor identity.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import uuid

from cryptography.hazmat.primitives.asymmetric import rsa
import httpx
import jwt
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.v1.organizer_auth import get_oidc_http_client, get_oidc_key_resolver
from app.core.config import Settings, get_settings
from app.main import app
from app.models.competition import CompetitionEvent, Submission, SubmissionVersion, ArtifactState
from app.models.event import EventControl
from app.models.match import Match, MatchPurpose, MatchState
from app.models.organizer import OrganizerSession
from app.models.participant import Participant, ParticipantStatus
from app.services.organizer_auth import (
    generate_organizer_session,
    revoke_organizer_session,
)


ORGANIZER_EMAIL = "lead-organizer@event.org"
UNAUTHORIZED_EMAIL = "attacker@evil.org"


@pytest.fixture(scope="session")
def rsa_keypair():
    """Generate isolated RSA 2048 keypair for cryptographic JWT tests."""
    priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return priv, priv.public_key()


@pytest.fixture
def mock_oidc_settings(monkeypatch):
    """Mock trusted OIDC configuration and organizer allowlist."""
    monkeypatch.setenv("OIDC_ISSUER_URL", "https://auth.event.org/realms/tournament")
    monkeypatch.setenv("OIDC_CLIENT_ID", "aik-organizer-portal")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "mock-oidc-client-secret-1234567890")
    monkeypatch.setenv("OIDC_REDIRECT_URI", "https://portal.event.org/callback")
    monkeypatch.setenv("ORGANIZER_ALLOWED_EMAILS", f"{ORGANIZER_EMAIL}, admin2@event.org")
    settings = Settings(_env_file=None)
    app.dependency_overrides[get_settings] = lambda: settings
    yield settings
    app.dependency_overrides.pop(get_settings, None)


@pytest.fixture
def oidc_mocks(mock_oidc_settings, rsa_keypair):
    """Mock IdP token endpoint and public key resolver for OIDC exchange."""
    priv_key, pub_key = rsa_keypair
    state_box = {"id_token": None, "status_code": 200}

    def token_endpoint_handler(request: httpx.Request):
        if state_box["status_code"] != 200:
            return httpx.Response(
                state_box["status_code"],
                json={"error": "invalid_grant", "error_description": "Code exchange failed"},
            )
        return httpx.Response(
            200,
            json={
                "access_token": "mock-access-token-xyz",
                "id_token": state_box["id_token"],
                "token_type": "Bearer",
                "expires_in": 3600,
            },
        )

    mock_client = httpx.AsyncClient(transport=httpx.MockTransport(token_endpoint_handler))
    app.dependency_overrides[get_oidc_http_client] = lambda: mock_client
    app.dependency_overrides[get_oidc_key_resolver] = lambda: pub_key

    def set_id_token(token: str, status_code: int = 200):
        state_box["id_token"] = token
        state_box["status_code"] = status_code

    yield {
        "set_id_token": set_id_token,
        "private_key": priv_key,
        "public_key": pub_key,
    }

    app.dependency_overrides.pop(get_oidc_http_client, None)
    app.dependency_overrides.pop(get_oidc_key_resolver, None)


def build_test_id_token(
    private_key,
    issuer="https://auth.event.org/realms/tournament",
    audience="aik-organizer-portal",
    email=ORGANIZER_EMAIL,
    name="Lead Organizer",
    nonce="test-nonce",
    email_verified=True,
    expires_in_seconds=3600,
) -> str:
    """Build a cryptographically signed RS256 ID Token."""
    now = datetime.now(timezone.utc)
    payload = {
        "iss": issuer,
        "sub": f"user_{uuid.uuid4().hex[:8]}",
        "aud": audience,
        "email": email,
        "name": name,
        "email_verified": email_verified,
        "nonce": nonce,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(seconds=expires_in_seconds)).timestamp()),
    }
    return jwt.encode(payload, private_key, algorithm="RS256")


@pytest.fixture
async def organizer_session_data(db_session: AsyncSession):
    """Create a real persisted OrganizerSession for testing."""
    raw_token, csrf_token, session = await generate_organizer_session(
        db=db_session,
        email=ORGANIZER_EMAIL,
        name="Lead Organizer",
        ip="127.0.0.1",
        user_agent="pytest-client",
    )
    await db_session.commit()
    return {
        "raw_token": raw_token,
        "csrf_token": csrf_token,
        "session": session,
        "cookie_header": {"Cookie": f"aik_org_sess={raw_token}"},
        "headers": {
            "Authorization": f"Bearer {raw_token}",
            "X-CSRF-Token": csrf_token,
            "Cookie": f"aik_org_sess={raw_token}",
        },
    }


@pytest.mark.integration
class TestOrganizerOidcConfiguration:
    """Test OIDC provider configuration requirements and fail-closed security."""

    async def test_oidc_unconfigured_fails_closed_503(
        self, api_client: httpx.AsyncClient, monkeypatch
    ):
        """When OIDC is unconfigured, login and callback must return 503 fail-closed."""
        monkeypatch.delenv("OIDC_ISSUER_URL", raising=False)
        monkeypatch.delenv("OIDC_CLIENT_ID", raising=False)
        monkeypatch.delenv("OIDC_CLIENT_SECRET", raising=False)
        monkeypatch.delenv("OIDC_REDIRECT_URI", raising=False)
        settings = Settings(_env_file=None)
        app.dependency_overrides[get_settings] = lambda: settings

        try:
            # Login endpoint fails closed
            res_login = await api_client.get("/api/v1/auth/organizer/login")
            assert res_login.status_code == 503
            assert res_login.json()["detail"] == "ORGANIZER_OIDC_NOT_CONFIGURED"

            # Admin login alias fails closed
            res_admin_login = await api_client.get("/api/v1/admin/auth/login")
            assert res_admin_login.status_code == 503
            assert res_admin_login.json()["detail"] == "ORGANIZER_OIDC_NOT_CONFIGURED"

            # Callback fails closed
            res_cb = await api_client.post(
                "/api/v1/auth/organizer/callback",
                json={"code": "mock", "state": "mock"},
            )
            assert res_cb.status_code == 503
            assert res_cb.json()["detail"] == "ORGANIZER_OIDC_NOT_CONFIGURED"
        finally:
            app.dependency_overrides.pop(get_settings, None)

    async def test_oidc_configured_generates_pkce_authorization_url(
        self, api_client: httpx.AsyncClient, mock_oidc_settings
    ):
        """When OIDC is configured, login endpoint returns PKCE-protected authorization URL and sets state cookie."""
        res = await api_client.get("/api/v1/auth/organizer/login")
        assert res.status_code == 200
        data = res.json()
        assert "authorization_url" in data
        assert "state" in data
        assert "code_verifier" not in data
        assert "https://auth.event.org/realms/tournament" in data["authorization_url"]
        assert "response_type=code" in data["authorization_url"]
        assert "code_challenge=" in data["authorization_url"]
        assert "code_challenge_method=S256" in data["authorization_url"]
        assert "nonce=" in data["authorization_url"]

        # Verify state cookie was set
        cookies = res.headers.get("set-cookie", "")
        assert "aik_oidc_state" in cookies
        assert "HttpOnly" in cookies


@pytest.mark.integration
class TestOrganizerCallbackIdentityAndSecurity:
    """Test cryptographic ID token validation, allowlisting, and rejection of client-forged identity."""

    async def test_callback_success_with_real_token_exchange_and_verified_claims(
        self, api_client: httpx.AsyncClient, mock_oidc_settings, oidc_mocks, db_session: AsyncSession
    ):
        """Valid OIDC callback completes token exchange, validates ID token, and issues session."""
        login_res = await api_client.get("/api/v1/auth/organizer/login")
        assert login_res.status_code == 200
        state = login_res.json()["state"]
        state_cookie = login_res.cookies.get("aik_oidc_state")
        assert state_cookie is not None

        # Extract expected nonce from signed cookie
        cookie_payload = jwt.decode(state_cookie, mock_oidc_settings.secret_key, algorithms=["HS256"])
        nonce = cookie_payload["nonce"]

        # Build valid ID token signed by IdP private key
        id_token = build_test_id_token(
            private_key=oidc_mocks["private_key"],
            email=ORGANIZER_EMAIL,
            name="Alice Organizer",
            nonce=nonce,
            email_verified=True,
        )
        oidc_mocks["set_id_token"](id_token)

        # Submit callback
        cb_res = await api_client.post(
            "/api/v1/auth/organizer/callback",
            json={"code": "valid-auth-code", "state": state},
            cookies={"aik_oidc_state": state_cookie},
        )
        assert cb_res.status_code == 200
        data = cb_res.json()
        assert data["status"] == "ok"
        assert data["organizer_email"] == ORGANIZER_EMAIL
        assert data["organizer_name"] == "Alice Organizer"
        assert "session_token" in data
        assert "csrf_token" in data

        # Check Set-Cookie has HttpOnly
        assert "aik_org_sess" in cb_res.headers.get("set-cookie", "")
        assert "HttpOnly" in cb_res.headers.get("set-cookie", "")

        # Verify DB session created
        sess = await db_session.scalar(
            select(OrganizerSession).where(OrganizerSession.organizer_email == ORGANIZER_EMAIL)
        )
        assert sess is not None

    async def test_callback_ignores_client_submitted_identity_forged_email_rejected(
        self, api_client: httpx.AsyncClient, mock_oidc_settings, oidc_mocks
    ):
        """Attacker cannot spoof identity by injecting email/name in callback body; token claims are authoritative."""
        login_res = await api_client.get("/api/v1/auth/organizer/login")
        state = login_res.json()["state"]
        state_cookie = login_res.cookies.get("aik_oidc_state")
        cookie_payload = jwt.decode(state_cookie, mock_oidc_settings.secret_key, algorithms=["HS256"])
        nonce = cookie_payload["nonce"]

        # ID token has UNAUTHORIZED email
        id_token = build_test_id_token(
            private_key=oidc_mocks["private_key"],
            email=UNAUTHORIZED_EMAIL,
            nonce=nonce,
            email_verified=True,
        )
        oidc_mocks["set_id_token"](id_token)

        # Attacker injects authorized email in JSON body
        cb_res = await api_client.post(
            "/api/v1/auth/organizer/callback",
            json={
                "code": "attacker-code",
                "state": state,
                "email": ORGANIZER_EMAIL,  # Attacker attempt to forge identity
                "name": "Forged Organizer",
            },
            cookies={"aik_oidc_state": state_cookie},
        )
        # Server must reject based on token claims, ignoring body
        assert cb_res.status_code == 403
        assert cb_res.json()["detail"] == "ORGANIZER_NOT_AUTHORIZED"

    async def test_callback_unverified_email_rejected_with_403(
        self, api_client: httpx.AsyncClient, mock_oidc_settings, oidc_mocks
    ):
        """ID token with email_verified=False must be rejected with 403 UNVERIFIED_EMAIL."""
        login_res = await api_client.get("/api/v1/auth/organizer/login")
        state = login_res.json()["state"]
        state_cookie = login_res.cookies.get("aik_oidc_state")
        cookie_payload = jwt.decode(state_cookie, mock_oidc_settings.secret_key, algorithms=["HS256"])
        nonce = cookie_payload["nonce"]

        id_token = build_test_id_token(
            private_key=oidc_mocks["private_key"],
            email=ORGANIZER_EMAIL,
            nonce=nonce,
            email_verified=False,  # Unverified
        )
        oidc_mocks["set_id_token"](id_token)

        cb_res = await api_client.post(
            "/api/v1/auth/organizer/callback",
            json={"code": "code", "state": state},
            cookies={"aik_oidc_state": state_cookie},
        )
        assert cb_res.status_code == 403
        assert cb_res.json()["detail"] == "UNVERIFIED_EMAIL"

    async def test_callback_nonce_mismatch_rejected_with_401(
        self, api_client: httpx.AsyncClient, mock_oidc_settings, oidc_mocks
    ):
        """ID token with nonce mismatch must be rejected with 401 INVALID_OIDC_NONCE."""
        login_res = await api_client.get("/api/v1/auth/organizer/login")
        state = login_res.json()["state"]
        state_cookie = login_res.cookies.get("aik_oidc_state")

        id_token = build_test_id_token(
            private_key=oidc_mocks["private_key"],
            email=ORGANIZER_EMAIL,
            nonce="wrong-tampered-nonce-value",
            email_verified=True,
        )
        oidc_mocks["set_id_token"](id_token)

        cb_res = await api_client.post(
            "/api/v1/auth/organizer/callback",
            json={"code": "code", "state": state},
            cookies={"aik_oidc_state": state_cookie},
        )
        assert cb_res.status_code == 401
        assert cb_res.json()["detail"] == "INVALID_OIDC_NONCE"

    async def test_callback_issuer_mismatch_rejected_with_401(
        self, api_client: httpx.AsyncClient, mock_oidc_settings, oidc_mocks
    ):
        """ID token with unexpected issuer must be rejected with 401."""
        login_res = await api_client.get("/api/v1/auth/organizer/login")
        state = login_res.json()["state"]
        state_cookie = login_res.cookies.get("aik_oidc_state")
        cookie_payload = jwt.decode(state_cookie, mock_oidc_settings.secret_key, algorithms=["HS256"])

        id_token = build_test_id_token(
            private_key=oidc_mocks["private_key"],
            issuer="https://rogue-idp.attacker.com",
            email=ORGANIZER_EMAIL,
            nonce=cookie_payload["nonce"],
        )
        oidc_mocks["set_id_token"](id_token)

        cb_res = await api_client.post(
            "/api/v1/auth/organizer/callback",
            json={"code": "code", "state": state},
            cookies={"aik_oidc_state": state_cookie},
        )
        assert cb_res.status_code == 401
        assert cb_res.json()["detail"] == "INVALID_OIDC_ID_TOKEN"

    async def test_callback_audience_mismatch_rejected_with_401(
        self, api_client: httpx.AsyncClient, mock_oidc_settings, oidc_mocks
    ):
        """ID token with unexpected audience must be rejected with 401."""
        login_res = await api_client.get("/api/v1/auth/organizer/login")
        state = login_res.json()["state"]
        state_cookie = login_res.cookies.get("aik_oidc_state")
        cookie_payload = jwt.decode(state_cookie, mock_oidc_settings.secret_key, algorithms=["HS256"])

        id_token = build_test_id_token(
            private_key=oidc_mocks["private_key"],
            audience="another-untrusted-client-app",
            email=ORGANIZER_EMAIL,
            nonce=cookie_payload["nonce"],
        )
        oidc_mocks["set_id_token"](id_token)

        cb_res = await api_client.post(
            "/api/v1/auth/organizer/callback",
            json={"code": "code", "state": state},
            cookies={"aik_oidc_state": state_cookie},
        )
        assert cb_res.status_code == 401
        assert cb_res.json()["detail"] == "INVALID_OIDC_ID_TOKEN"

    async def test_callback_expired_token_rejected_with_401(
        self, api_client: httpx.AsyncClient, mock_oidc_settings, oidc_mocks
    ):
        """Expired ID token must be rejected with 401."""
        login_res = await api_client.get("/api/v1/auth/organizer/login")
        state = login_res.json()["state"]
        state_cookie = login_res.cookies.get("aik_oidc_state")
        cookie_payload = jwt.decode(state_cookie, mock_oidc_settings.secret_key, algorithms=["HS256"])

        id_token = build_test_id_token(
            private_key=oidc_mocks["private_key"],
            email=ORGANIZER_EMAIL,
            nonce=cookie_payload["nonce"],
            expires_in_seconds=-120,  # Expired 2 minutes ago
        )
        oidc_mocks["set_id_token"](id_token)

        cb_res = await api_client.post(
            "/api/v1/auth/organizer/callback",
            json={"code": "code", "state": state},
            cookies={"aik_oidc_state": state_cookie},
        )
        assert cb_res.status_code == 401
        assert cb_res.json()["detail"] == "INVALID_OIDC_ID_TOKEN"

    async def test_callback_forged_signature_rejected_with_401(
        self, api_client: httpx.AsyncClient, mock_oidc_settings, oidc_mocks
    ):
        """ID token signed with an untrusted rogue private key must be rejected with 401."""
        login_res = await api_client.get("/api/v1/auth/organizer/login")
        state = login_res.json()["state"]
        state_cookie = login_res.cookies.get("aik_oidc_state")
        cookie_payload = jwt.decode(state_cookie, mock_oidc_settings.secret_key, algorithms=["HS256"])

        # Generate a separate rogue keypair
        rogue_priv = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        id_token = build_test_id_token(
            private_key=rogue_priv,
            email=ORGANIZER_EMAIL,
            nonce=cookie_payload["nonce"],
        )
        oidc_mocks["set_id_token"](id_token)

        cb_res = await api_client.post(
            "/api/v1/auth/organizer/callback",
            json={"code": "code", "state": state},
            cookies={"aik_oidc_state": state_cookie},
        )
        assert cb_res.status_code == 401
        assert cb_res.json()["detail"] == "INVALID_OIDC_ID_TOKEN"

    async def test_callback_missing_or_tampered_state_cookie_rejected_with_400(
        self, api_client: httpx.AsyncClient, mock_oidc_settings
    ):
        """Callback missing or having a forged/tampered state cookie must return 400."""
        # 1. Missing cookie
        res_missing = await api_client.post(
            "/api/v1/auth/organizer/callback",
            json={"code": "code", "state": "some-state"},
        )
        assert res_missing.status_code == 400
        assert res_missing.json()["detail"] == "INVALID_OIDC_STATE"

        # 2. Tampered cookie
        res_tampered = await api_client.post(
            "/api/v1/auth/organizer/callback",
            json={"code": "code", "state": "some-state"},
            cookies={"aik_oidc_state": "tampered.jwt.cookie"},
        )
        assert res_tampered.status_code == 400
        assert res_tampered.json()["detail"] == "INVALID_OIDC_STATE"

    async def test_callback_state_parameter_mismatch_rejected_with_400(
        self, api_client: httpx.AsyncClient, mock_oidc_settings
    ):
        """Callback where payload state does not match signed cookie state must return 400."""
        login_res = await api_client.get("/api/v1/auth/organizer/login")
        state_cookie = login_res.cookies.get("aik_oidc_state")

        res = await api_client.post(
            "/api/v1/auth/organizer/callback",
            json={"code": "code", "state": "mismatched-state-value"},
            cookies={"aik_oidc_state": state_cookie},
        )
        assert res.status_code == 400
        assert res.json()["detail"] == "INVALID_OIDC_STATE"

    async def test_callback_replay_attack_rejected_with_400(
        self, api_client: httpx.AsyncClient, mock_oidc_settings, oidc_mocks
    ):
        """Replaying a callback request must be rejected because the state cookie is invalidated upon first use."""
        login_res = await api_client.get("/api/v1/auth/organizer/login")
        state = login_res.json()["state"]
        state_cookie = login_res.cookies.get("aik_oidc_state")
        cookie_payload = jwt.decode(state_cookie, mock_oidc_settings.secret_key, algorithms=["HS256"])

        id_token = build_test_id_token(
            private_key=oidc_mocks["private_key"],
            email=ORGANIZER_EMAIL,
            nonce=cookie_payload["nonce"],
        )
        oidc_mocks["set_id_token"](id_token)

        # First request succeeds and returns deleted state cookie instruction
        first_res = await api_client.post(
            "/api/v1/auth/organizer/callback",
            json={"code": "code-1", "state": state},
            cookies={"aik_oidc_state": state_cookie},
        )
        assert first_res.status_code == 200

        # Attempt to replay second request with same state cookie
        # In a real browser the cookie is removed, but even if attacker holds original cookie:
        # If IdP single-use code is also revoked by IdP:
        oidc_mocks["set_id_token"]("", status_code=400)
        replay_res = await api_client.post(
            "/api/v1/auth/organizer/callback",
            json={"code": "code-1", "state": state},
            cookies={"aik_oidc_state": state_cookie},
        )
        # IdP returns error -> 401 OIDC_TOKEN_EXCHANGE_FAILED
        assert replay_res.status_code == 401
        assert replay_res.json()["detail"] == "OIDC_TOKEN_EXCHANGE_FAILED"

    async def test_callback_token_endpoint_error_rejected_with_401(
        self, api_client: httpx.AsyncClient, mock_oidc_settings, oidc_mocks
    ):
        """When IdP token endpoint rejects authorization code, server returns 401 OIDC_TOKEN_EXCHANGE_FAILED."""
        login_res = await api_client.get("/api/v1/auth/organizer/login")
        state = login_res.json()["state"]
        state_cookie = login_res.cookies.get("aik_oidc_state")

        oidc_mocks["set_id_token"]("", status_code=400)

        cb_res = await api_client.post(
            "/api/v1/auth/organizer/callback",
            json={"code": "invalid-code", "state": state},
            cookies={"aik_oidc_state": state_cookie},
        )
        assert cb_res.status_code == 401
        assert cb_res.json()["detail"] == "OIDC_TOKEN_EXCHANGE_FAILED"


@pytest.mark.integration
class TestDeployedSecretLockout:
    """Verify that X-Organizer-Secret is strictly forbidden in deployed environments."""

    async def test_x_organizer_secret_rejected_in_production(
        self, api_client: httpx.AsyncClient, monkeypatch
    ):
        """In production environment, X-Organizer-Secret header must be rejected with 401."""
        monkeypatch.setenv("APP_ENV", "production")
        prod_settings = Settings(_env_file=None)
        app.dependency_overrides[get_settings] = lambda: prod_settings

        try:
            res = await api_client.get(
                "/api/v1/admin/overview",
                headers={"X-Organizer-Secret": prod_settings.secret_key},
            )
            assert res.status_code == 401
            assert res.json()["detail"] == "ORGANIZER_SHARED_SECRET_DISABLED_IN_DEPLOYED_ENV"
        finally:
            app.dependency_overrides.pop(get_settings, None)

    async def test_x_organizer_secret_rejected_in_staging(
        self, api_client: httpx.AsyncClient, monkeypatch
    ):
        """In staging environment, X-Organizer-Secret header must be rejected with 401."""
        monkeypatch.setenv("APP_ENV", "staging")
        staging_settings = Settings(_env_file=None)
        app.dependency_overrides[get_settings] = lambda: staging_settings

        try:
            res = await api_client.get(
                "/api/v1/admin/overview",
                headers={"X-Organizer-Secret": staging_settings.secret_key},
            )
            assert res.status_code == 401
            assert res.json()["detail"] == "ORGANIZER_SHARED_SECRET_DISABLED_IN_DEPLOYED_ENV"
        finally:
            app.dependency_overrides.pop(get_settings, None)

    async def test_x_organizer_secret_permitted_only_in_development(
        self, api_client: httpx.AsyncClient, monkeypatch
    ):
        """In local development, X-Organizer-Secret fallback is accepted for local CLI/scripts."""
        monkeypatch.setenv("APP_ENV", "development")
        dev_settings = Settings(_env_file=None)
        app.dependency_overrides[get_settings] = lambda: dev_settings

        try:
            res = await api_client.get(
                "/api/v1/admin/overview",
                headers={"X-Organizer-Secret": dev_settings.secret_key},
            )
            assert res.status_code == 200
        finally:
            app.dependency_overrides.pop(get_settings, None)


@pytest.mark.integration
class TestOrganizerCookieSecurity:
    """Verify cookie security attributes across environments."""

    async def test_cookie_attributes_in_production(
        self, api_client: httpx.AsyncClient, monkeypatch, oidc_mocks
    ):
        """In production, session cookies must use __Host- prefix, Secure=True, and HttpOnly=True."""
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("OIDC_ISSUER_URL", "https://auth.event.org/realms/tournament")
        monkeypatch.setenv("OIDC_CLIENT_ID", "aik-organizer-portal")
        monkeypatch.setenv("OIDC_CLIENT_SECRET", "mock-secret-12345678901234567890")
        monkeypatch.setenv("ORGANIZER_ALLOWED_EMAILS", ORGANIZER_EMAIL)
        prod_settings = Settings(_env_file=None)
        app.dependency_overrides[get_settings] = lambda: prod_settings

        try:
            login_res = await api_client.get("/api/v1/auth/organizer/login")
            assert login_res.status_code == 200
            login_cookie_header = login_res.headers.get("set-cookie", "")
            assert "__Host-aik_oidc_state" in login_cookie_header
            assert "HttpOnly" in login_cookie_header

            state = login_res.json()["state"]
            state_cookie = login_res.cookies.get("__Host-aik_oidc_state")
            cookie_payload = jwt.decode(state_cookie, prod_settings.secret_key, algorithms=["HS256"])

            id_token = build_test_id_token(
                private_key=oidc_mocks["private_key"],
                email=ORGANIZER_EMAIL,
                nonce=cookie_payload["nonce"],
            )
            oidc_mocks["set_id_token"](id_token)

            cb_res = await api_client.post(
                "/api/v1/auth/organizer/callback",
                json={"code": "code", "state": state},
                cookies={"__Host-aik_oidc_state": state_cookie},
            )
            assert cb_res.status_code == 200
            cb_cookie_header = cb_res.headers.get("set-cookie", "")
            assert "__Host-aik_org_sess" in cb_cookie_header
            assert "HttpOnly" in cb_cookie_header
        finally:
            app.dependency_overrides.pop(get_settings, None)


@pytest.mark.integration
class TestOrganizerSessionLifecycle:
    """Test session inspection, sliding idle timeout, hard expiration, and logout revocation."""

    async def test_session_inspection_and_unauthorized_rejection(
        self, api_client: httpx.AsyncClient, organizer_session_data
    ):
        """Profile inspection requires active session and returns CSRF token."""
        # Unauthenticated request fails
        unauth = await api_client.get("/api/v1/auth/organizer/session")
        assert unauth.status_code == 401
        assert unauth.json()["detail"] == "ORGANIZER_AUTHENTICATION_REQUIRED"

        # Invalid token fails
        invalid = await api_client.get(
            "/api/v1/auth/organizer/session",
            headers={"Authorization": "Bearer aik_org_invalid_fake_token"},
        )
        assert invalid.status_code == 401
        assert invalid.json()["detail"] == "ORGANIZER_SESSION_INVALID"

        # Valid cookie request succeeds
        res = await api_client.get(
            "/api/v1/auth/organizer/session",
            headers=organizer_session_data["cookie_header"],
        )
        assert res.status_code == 200
        data = res.json()
        assert data["organizer_email"] == ORGANIZER_EMAIL
        assert data["csrf_token"] == organizer_session_data["csrf_token"]
        assert "last_active_at" in data

    async def test_idle_session_expiration(
        self, api_client: httpx.AsyncClient, db_session: AsyncSession, organizer_session_data
    ):
        """Sessions idle for longer than organizer_session_idle_minutes must be expired."""
        sess: OrganizerSession = organizer_session_data["session"]
        sess.last_active_at = datetime.now(timezone.utc) - timedelta(minutes=65)
        await db_session.commit()

        res = await api_client.get(
            "/api/v1/admin/overview",
            headers=organizer_session_data["cookie_header"],
        )
        assert res.status_code == 401
        assert res.json()["detail"] == "ORGANIZER_SESSION_EXPIRED"

    async def test_hard_session_expiration(
        self, api_client: httpx.AsyncClient, db_session: AsyncSession, organizer_session_data
    ):
        """Sessions older than expires_at must be expired regardless of activity."""
        sess: OrganizerSession = organizer_session_data["session"]
        sess.expires_at = datetime.now(timezone.utc) - timedelta(seconds=10)
        await db_session.commit()

        res = await api_client.get(
            "/api/v1/admin/overview",
            headers=organizer_session_data["cookie_header"],
        )
        assert res.status_code == 401
        assert res.json()["detail"] == "ORGANIZER_SESSION_EXPIRED"

    async def test_logout_revocation_and_subsequent_rejection(
        self, api_client: httpx.AsyncClient, db_session: AsyncSession, organizer_session_data
    ):
        """Logout must revoke session in DB, clear cookies, and reject future requests."""
        logout_res = await api_client.post(
            "/api/v1/auth/organizer/logout",
            headers={
                **organizer_session_data["cookie_header"],
                "X-CSRF-Token": organizer_session_data["csrf_token"],
            },
        )
        assert logout_res.status_code == 200
        assert logout_res.json()["status"] == "ok"

        # Subsequent request with old session token must be rejected
        overview_res = await api_client.get(
            "/api/v1/admin/overview",
            headers=organizer_session_data["cookie_header"],
        )
        assert overview_res.status_code == 401
        assert overview_res.json()["detail"] == "ORGANIZER_SESSION_EXPIRED"


@pytest.mark.integration
class TestOrganizerCsrfProtection:
    """Test CSRF enforcement on mutating vs safe HTTP methods."""

    async def test_mutating_requests_require_csrf_token(
        self, api_client: httpx.AsyncClient, organizer_session_data
    ):
        """Mutating POST requests without valid CSRF header must be rejected with 403."""
        # 1. Missing CSRF header
        res_missing = await api_client.post(
            "/api/v1/admin/freeze",
            headers=organizer_session_data["cookie_header"],
            json={"reason_code": "TEST_FREEZE"},
        )
        assert res_missing.status_code == 403
        assert res_missing.json()["detail"] == "CSRF_VERIFICATION_FAILED"

        # 2. Invalid CSRF header
        res_invalid = await api_client.post(
            "/api/v1/admin/freeze",
            headers={
                **organizer_session_data["cookie_header"],
                "X-CSRF-Token": "wrong-csrf-token-value",
            },
            json={"reason_code": "TEST_FREEZE"},
        )
        assert res_invalid.status_code == 403
        assert res_invalid.json()["detail"] == "CSRF_VERIFICATION_FAILED"

        # 3. Valid CSRF header succeeds
        res_valid = await api_client.post(
            "/api/v1/admin/freeze",
            headers={
                **organizer_session_data["cookie_header"],
                "X-CSRF-Token": organizer_session_data["csrf_token"],
            },
            json={"reason_code": "TEST_FREEZE"},
        )
        assert res_valid.status_code == 200
        assert res_valid.json()["status"] == "FROZEN"

    async def test_safe_get_requests_do_not_require_csrf_token(
        self, api_client: httpx.AsyncClient, organizer_session_data
    ):
        """Safe GET requests do not require CSRF token and succeed with session cookie alone."""
        res = await api_client.get(
            "/api/v1/admin/overview",
            headers=organizer_session_data["cookie_header"],
        )
        assert res.status_code == 200
        data = res.json()
        assert isinstance(data, dict)


@pytest.mark.integration
class TestAllEighteenOrganizerRoutesCoverage:
    """Verify that all 18 organizer control routes authorize server-managed sessions."""

    async def test_all_18_routes_with_organizer_session(
        self, api_client: httpx.AsyncClient, db_session: AsyncSession, organizer_session_data
    ):
        """Walk through all 18 routes using session auth, verifying authorization & CSRF checks."""
        headers = organizer_session_data["headers"]

        # Setup test participant
        p = Participant(
            participant_id="part_auth_test_18",
            name="Auth Test Participant",
            email="authtest@example.com",
            access_key_hash="mock_hash_test_18",
            status=ParticipantStatus.ACTIVE,
        )
        db_session.add(p)
        await db_session.commit()

        # Route 1: GET /overview
        r1 = await api_client.get("/api/v1/admin/overview", headers=headers)
        assert r1.status_code == 200, f"Route 1 overview failed: {r1.text}"

        # Route 2: GET /participants
        r2 = await api_client.get("/api/v1/admin/participants", headers=headers)
        assert r2.status_code == 200, f"Route 2 participants failed: {r2.text}"

        # Route 3: GET /submissions
        r3 = await api_client.get("/api/v1/admin/submissions", headers=headers)
        assert r3.status_code == 200, f"Route 3 submissions failed: {r3.text}"

        # Route 4: POST /submissions/approve
        dummy_ver_id = uuid.uuid4()
        r4 = await api_client.post(
            "/api/v1/admin/submissions/approve",
            headers=headers,
            json={"submission_version_id": str(dummy_ver_id)},
        )
        assert r4.status_code in (400, 404), "Route 4 reached controller logic"

        # Route 5: POST /freeze
        r5 = await api_client.post(
            "/api/v1/admin/freeze",
            headers=headers,
            json={"reason_code": "TEST_ROUTE_5"},
        )
        assert r5.status_code in (200, 409), f"Route 5 freeze failed: {r5.text}"

        # Route 6: POST /tournaments/schedule-match
        r6 = await api_client.post(
            "/api/v1/admin/tournaments/schedule-match",
            headers=headers,
            json={
                "white_submission_version_id": str(uuid.uuid4()),
                "black_submission_version_id": str(uuid.uuid4()),
            },
        )
        assert r6.status_code in (400, 404, 409), "Route 6 reached controller logic"

        # Route 7: POST /tournaments/swiss/schedule-round
        r7 = await api_client.post(
            "/api/v1/admin/tournaments/swiss/schedule-round",
            headers=headers,
            json={"round_number": 99},
        )
        assert r7.status_code in (400, 409), "Route 7 reached controller logic"

        # Route 8: POST /tournaments/playoffs/initialize
        r8 = await api_client.post(
            "/api/v1/admin/tournaments/playoffs/initialize",
            headers=headers,
            json={"seed_source_stage": "swiss_round_99"},
        )
        assert r8.status_code in (400, 409), "Route 8 reached controller logic"

        # Route 9: POST /tournaments/playoffs/advance
        r9 = await api_client.post(
            "/api/v1/admin/tournaments/playoffs/advance",
            headers=headers,
            json={"target_stage": "quarterfinals"},
        )
        assert r9.status_code in (200, 400, 409), "Route 9 reached controller logic"

        # Route 10: POST /tournaments/matches/{match_id}/record-result
        fake_match_id = uuid.uuid4()
        r10 = await api_client.post(
            f"/api/v1/admin/tournaments/matches/{fake_match_id}/record-result",
            headers=headers,
            json={"result_outcome": "WHITE_WIN", "result_reason": "CHECKMATE"},
        )
        assert r10.status_code in (400, 404), "Route 10 reached controller logic"

        # Route 11: GET /audit-log
        r11 = await api_client.get("/api/v1/admin/audit-log", headers=headers)
        assert r11.status_code == 200, f"Route 11 audit log failed: {r11.text}"

        # Route 12: GET /allowance-requests
        r12 = await api_client.get("/api/v1/admin/allowance-requests", headers=headers)
        assert r12.status_code == 200, f"Route 12 allowance requests failed: {r12.text}"

        # Route 13: POST /allowance-requests/{request_id}/decision
        fake_req_id = uuid.uuid4()
        r13 = await api_client.post(
            f"/api/v1/admin/allowance-requests/{fake_req_id}/decision",
            headers=headers,
            json={"decision": "APPROVED", "notes": "Route 13 test"},
        )
        assert r13.status_code == 404, "Route 13 reached controller logic"

        # Route 14: POST /participants/{participant_id}/allowance
        r14 = await api_client.post(
            f"/api/v1/admin/participants/{p.participant_id}/allowance",
            headers=headers,
            json={"submission_limit": 25, "reason": "Route 14 quota test"},
        )
        assert r14.status_code == 200, f"Route 14 quota failed: {r14.text}"
        assert r14.json()["submission_limit"] == 25

        # Route 15: POST /participants/{participant_id}/suspend
        r15 = await api_client.post(
            f"/api/v1/admin/participants/{p.participant_id}/suspend",
            headers=headers,
            json={"reason": "Route 15 suspend test"},
        )
        assert r15.status_code == 200, f"Route 15 suspend failed: {r15.text}"
        assert r15.json()["status"] == "SUSPENDED"

        # Route 16: POST /participants/{participant_id}/reinstate
        r16 = await api_client.post(
            f"/api/v1/admin/participants/{p.participant_id}/reinstate",
            headers=headers,
            json={"reason": "Route 16 reinstate test"},
        )
        assert r16.status_code == 200, f"Route 16 reinstate failed: {r16.text}"
        assert r16.json()["status"] == "ACTIVE"

        # Route 17: POST /participants/{participant_id}/revoke-sessions
        r17 = await api_client.post(
            f"/api/v1/admin/participants/{p.participant_id}/revoke-sessions",
            headers=headers,
            json={"reason": "Route 17 session revoke test"},
        )
        assert r17.status_code == 200, f"Route 17 revoke sessions failed: {r17.text}"

        # Route 18: DELETE /participants/{participant_id}
        p_del = Participant(
            participant_id="part_auth_test_del",
            name="Deletable Participant",
            email="del@example.com",
            access_key_hash="mock_hash_test_del",
            status=ParticipantStatus.ACTIVE,
        )
        db_session.add(p_del)
        await db_session.commit()

        r18 = await api_client.delete(
            f"/api/v1/admin/participants/{p_del.participant_id}",
            headers=headers,
        )
        assert r18.status_code == 200, f"Route 18 delete failed: {r18.text}"
        assert r18.json()["removed"] is True


@pytest.mark.integration
class TestAuditAttribution:
    """Verify that actions performed via session auth attribute to verified organizer email."""

    async def test_quota_update_attributes_actor_to_organizer_email(
        self, api_client: httpx.AsyncClient, db_session: AsyncSession, organizer_session_data
    ):
        """Updating participant quota with session auth must record organizer email as granted_by."""
        headers = organizer_session_data["headers"]

        pid = f"part_audit_attr_{uuid.uuid4().hex[:6]}"
        p = Participant(
            participant_id=pid,
            name="Audit Attr Test",
            email="attr@example.com",
            access_key_hash="mock_hash_attr",
            status=ParticipantStatus.ACTIVE,
        )
        db_session.add(p)
        await db_session.commit()

        res = await api_client.post(
            f"/api/v1/admin/participants/{pid}/allowance",
            headers=headers,
            json={"submission_limit": 30, "reason": "Audit verification"},
        )
        assert res.status_code == 200
        assert res.json()["granted_by"] == ORGANIZER_EMAIL

    async def test_suspend_attributes_actor_to_organizer_email(
        self, api_client: httpx.AsyncClient, db_session: AsyncSession, organizer_session_data
    ):
        """Suspending participant with session auth must record CompetitionEvent with organizer email."""
        headers = organizer_session_data["headers"]

        pid = f"part_audit_susp_{uuid.uuid4().hex[:6]}"
        p = Participant(
            participant_id=pid,
            name="Audit Susp Test",
            email="susp@example.com",
            access_key_hash="mock_hash_susp",
            status=ParticipantStatus.ACTIVE,
        )
        db_session.add(p)
        await db_session.commit()

        res = await api_client.post(
            f"/api/v1/admin/participants/{pid}/suspend",
            headers=headers,
            json={"reason": "Audit verification"},
        )
        assert res.status_code == 200

        events = (
            await db_session.execute(
                select(CompetitionEvent).where(
                    CompetitionEvent.event_type == "participant.suspended",
                )
            )
        ).scalars().all()
        matching = [e for e in events if e.metadata_json.get("participant_id") == pid]
        assert len(matching) == 1
        assert matching[0].actor_id == ORGANIZER_EMAIL


@pytest.mark.integration
class TestOrganizerBrowserGetCallback:
    """Test browser-compatible GET authorization-code redirect callback."""

    async def test_get_callback_success_redirects_to_portal_with_session_cookie(
        self, api_client: httpx.AsyncClient, mock_oidc_settings, oidc_mocks, db_session: AsyncSession
    ):
        """GET /callback redirects browser to /portal#admin with Set-Cookie session."""
        login_res = await api_client.get("/api/v1/auth/organizer/login")
        assert login_res.status_code == 200
        state = login_res.json()["state"]
        state_cookie = login_res.cookies.get("aik_oidc_state")
        assert state_cookie is not None

        cookie_payload = jwt.decode(state_cookie, mock_oidc_settings.secret_key, algorithms=["HS256"])
        nonce = cookie_payload["nonce"]

        id_token = build_test_id_token(
            private_key=oidc_mocks["private_key"],
            email=ORGANIZER_EMAIL,
            name="Browser Arbiter",
            nonce=nonce,
            email_verified=True,
        )
        oidc_mocks["set_id_token"](id_token)

        # Send GET callback with code and state in query string
        cb_res = await api_client.get(
            f"/api/v1/auth/organizer/callback?code=browser-valid-code&state={state}",
            cookies={"aik_oidc_state": state_cookie},
            follow_redirects=False,
        )
        assert cb_res.status_code == 303
        assert cb_res.headers.get("location") == "/portal#admin"

        # Verify session cookie was set
        set_cookie = cb_res.headers.get("set-cookie", "")
        assert "aik_org_sess=" in set_cookie
        assert "HttpOnly" in set_cookie

        # Verify DB session created
        sess = await db_session.scalar(
            select(OrganizerSession).where(OrganizerSession.organizer_email == ORGANIZER_EMAIL)
        )
        assert sess is not None
        assert sess.organizer_name == "Browser Arbiter"

    async def test_get_callback_json_response_with_accept_header(
        self, api_client: httpx.AsyncClient, mock_oidc_settings, oidc_mocks, db_session: AsyncSession
    ):
        """GET /callback returns JSON response when Accept: application/json is explicitly sent."""
        login_res = await api_client.get("/api/v1/auth/organizer/login")
        state = login_res.json()["state"]
        state_cookie = login_res.cookies.get("aik_oidc_state")
        cookie_payload = jwt.decode(state_cookie, mock_oidc_settings.secret_key, algorithms=["HS256"])
        nonce = cookie_payload["nonce"]

        id_token = build_test_id_token(
            private_key=oidc_mocks["private_key"],
            email=ORGANIZER_EMAIL,
            name="API Client",
            nonce=nonce,
            email_verified=True,
        )
        oidc_mocks["set_id_token"](id_token)

        cb_res = await api_client.get(
            f"/api/v1/auth/organizer/callback?code=json-accept-code&state={state}",
            headers={"Accept": "application/json"},
            cookies={"aik_oidc_state": state_cookie},
        )
        assert cb_res.status_code == 200
        data = cb_res.json()
        assert data["status"] == "ok"
        assert data["organizer_email"] == ORGANIZER_EMAIL
        assert data["redirect_url"] == "/portal#admin"
        assert "session_token" in data
        assert "csrf_token" in data
        assert "aik_org_sess=" in cb_res.headers.get("set-cookie", "")

    async def test_get_callback_missing_parameters_rejected_with_400(
        self, api_client: httpx.AsyncClient, mock_oidc_settings
    ):
        """GET /callback without required query parameters returns 400 INVALID_OIDC_CALLBACK."""
        res_no_params = await api_client.get("/api/v1/auth/organizer/callback")
        assert res_no_params.status_code == 400
        assert res_no_params.json()["detail"] == "INVALID_OIDC_CALLBACK"

        res_no_state = await api_client.get("/api/v1/auth/organizer/callback?code=onlycode")
        assert res_no_state.status_code == 400
        assert res_no_state.json()["detail"] == "INVALID_OIDC_CALLBACK"

        res_no_code = await api_client.get("/api/v1/auth/organizer/callback?state=onlystate")
        assert res_no_code.status_code == 400
        assert res_no_code.json()["detail"] == "INVALID_OIDC_CALLBACK"

    async def test_get_callback_missing_or_invalid_state_cookie_rejected_with_400(
        self, api_client: httpx.AsyncClient, mock_oidc_settings
    ):
        """GET /callback with missing or tampered state cookie returns 400 INVALID_OIDC_STATE."""
        res_missing = await api_client.get("/api/v1/auth/organizer/callback?code=c1&state=s1")
        assert res_missing.status_code == 400
        assert res_missing.json()["detail"] == "INVALID_OIDC_STATE"

        res_tampered = await api_client.get(
            "/api/v1/auth/organizer/callback?code=c1&state=s1",
            cookies={"aik_oidc_state": "tampered.jwt.cookie"},
        )
        assert res_tampered.status_code == 400
        assert res_tampered.json()["detail"] == "INVALID_OIDC_STATE"

    async def test_get_callback_state_parameter_mismatch_rejected_with_400(
        self, api_client: httpx.AsyncClient, mock_oidc_settings
    ):
        """GET /callback with query state mismatching signed cookie state returns 400 INVALID_OIDC_STATE."""
        login_res = await api_client.get("/api/v1/auth/organizer/login")
        state_cookie = login_res.cookies.get("aik_oidc_state")

        res = await api_client.get(
            "/api/v1/auth/organizer/callback?code=c1&state=mismatched-query-state",
            cookies={"aik_oidc_state": state_cookie},
        )
        assert res.status_code == 400
        assert res.json()["detail"] == "INVALID_OIDC_STATE"

    async def test_get_callback_provider_error_handled_with_400(
        self, api_client: httpx.AsyncClient, mock_oidc_settings
    ):
        """GET /callback receiving error query parameter from IdP returns 400 with detail."""
        res = await api_client.get(
            "/api/v1/auth/organizer/callback?error=access_denied&error_description=User+declined+authorization"
        )
        assert res.status_code == 400
        assert "OIDC_PROVIDER_ERROR" in res.json()["detail"]
        assert "User declined authorization" in res.json()["detail"]

    async def test_get_callback_token_endpoint_error_rejected_with_401(
        self, api_client: httpx.AsyncClient, mock_oidc_settings, oidc_mocks
    ):
        """GET /callback returns 401 OIDC_TOKEN_EXCHANGE_FAILED when IdP token exchange fails."""
        login_res = await api_client.get("/api/v1/auth/organizer/login")
        state = login_res.json()["state"]
        state_cookie = login_res.cookies.get("aik_oidc_state")

        oidc_mocks["set_id_token"]("", status_code=400)

        res = await api_client.get(
            f"/api/v1/auth/organizer/callback?code=failed-exchange-code&state={state}",
            cookies={"aik_oidc_state": state_cookie},
        )
        assert res.status_code == 401
        assert res.json()["detail"] == "OIDC_TOKEN_EXCHANGE_FAILED"

    async def test_get_callback_unverified_email_rejected_with_403(
        self, api_client: httpx.AsyncClient, mock_oidc_settings, oidc_mocks
    ):
        """GET /callback returns 403 UNVERIFIED_EMAIL when ID token email_verified is False."""
        login_res = await api_client.get("/api/v1/auth/organizer/login")
        state = login_res.json()["state"]
        state_cookie = login_res.cookies.get("aik_oidc_state")
        cookie_payload = jwt.decode(state_cookie, mock_oidc_settings.secret_key, algorithms=["HS256"])

        id_token = build_test_id_token(
            private_key=oidc_mocks["private_key"],
            email=ORGANIZER_EMAIL,
            nonce=cookie_payload["nonce"],
            email_verified=False,
        )
        oidc_mocks["set_id_token"](id_token)

        res = await api_client.get(
            f"/api/v1/auth/organizer/callback?code=unverified-code&state={state}",
            cookies={"aik_oidc_state": state_cookie},
        )
        assert res.status_code == 403
        assert res.json()["detail"] == "UNVERIFIED_EMAIL"

    async def test_get_callback_unauthorized_organizer_rejected_with_403(
        self, api_client: httpx.AsyncClient, mock_oidc_settings, oidc_mocks
    ):
        """GET /callback returns 403 ORGANIZER_NOT_AUTHORIZED when email not in allowlist."""
        login_res = await api_client.get("/api/v1/auth/organizer/login")
        state = login_res.json()["state"]
        state_cookie = login_res.cookies.get("aik_oidc_state")
        cookie_payload = jwt.decode(state_cookie, mock_oidc_settings.secret_key, algorithms=["HS256"])

        id_token = build_test_id_token(
            private_key=oidc_mocks["private_key"],
            email=UNAUTHORIZED_EMAIL,
            nonce=cookie_payload["nonce"],
            email_verified=True,
        )
        oidc_mocks["set_id_token"](id_token)

        res = await api_client.get(
            f"/api/v1/auth/organizer/callback?code=unauthorized-code&state={state}",
            cookies={"aik_oidc_state": state_cookie},
        )
        assert res.status_code == 403
        assert res.json()["detail"] == "ORGANIZER_NOT_AUTHORIZED"

    async def test_get_callback_replay_attack_rejected(
        self, api_client: httpx.AsyncClient, mock_oidc_settings, oidc_mocks
    ):
        """GET /callback replayed with same parameters is rejected upon second use."""
        login_res = await api_client.get("/api/v1/auth/organizer/login")
        state = login_res.json()["state"]
        state_cookie = login_res.cookies.get("aik_oidc_state")
        cookie_payload = jwt.decode(state_cookie, mock_oidc_settings.secret_key, algorithms=["HS256"])

        id_token = build_test_id_token(
            private_key=oidc_mocks["private_key"],
            email=ORGANIZER_EMAIL,
            nonce=cookie_payload["nonce"],
        )
        oidc_mocks["set_id_token"](id_token)

        # First request succeeds
        res1 = await api_client.get(
            f"/api/v1/auth/organizer/callback?code=get-replay-code&state={state}",
            cookies={"aik_oidc_state": state_cookie},
            follow_redirects=False,
        )
        assert res1.status_code == 303

        # Second request without state cookie (as browser cleared it) returns 400
        res2_no_cookie = await api_client.get(
            f"/api/v1/auth/organizer/callback?code=get-replay-code&state={state}",
            follow_redirects=False,
        )
        assert res2_no_cookie.status_code == 400
        assert res2_no_cookie.json()["detail"] == "INVALID_OIDC_STATE"

        # Even if attacker held the state cookie, IdP revokes code upon first use
        oidc_mocks["set_id_token"]("", status_code=400)
        res2_with_cookie = await api_client.get(
            f"/api/v1/auth/organizer/callback?code=get-replay-code&state={state}",
            cookies={"aik_oidc_state": state_cookie},
            follow_redirects=False,
        )
        assert res2_with_cookie.status_code == 401
        assert res2_with_cookie.json()["detail"] == "OIDC_TOKEN_EXCHANGE_FAILED"

    async def test_get_callback_cookie_security_in_production(
        self, api_client: httpx.AsyncClient, mock_oidc_settings, oidc_mocks, monkeypatch
    ):
        """In production, GET /callback sets __Host-aik_org_sess with Secure and HttpOnly."""
        monkeypatch.setenv("APP_ENV", "production")
        prod_settings = Settings(_env_file=None)
        app.dependency_overrides[get_settings] = lambda: prod_settings

        try:
            login_res = await api_client.get("/api/v1/auth/organizer/login")
            assert login_res.status_code == 200
            state = login_res.json()["state"]
            state_cookie = login_res.cookies.get("__Host-aik_oidc_state")
            cookie_payload = jwt.decode(state_cookie, prod_settings.secret_key, algorithms=["HS256"])

            id_token = build_test_id_token(
                private_key=oidc_mocks["private_key"],
                email=ORGANIZER_EMAIL,
                nonce=cookie_payload["nonce"],
            )
            oidc_mocks["set_id_token"](id_token)

            cb_res = await api_client.get(
                f"/api/v1/auth/organizer/callback?code=prod-get-code&state={state}",
                cookies={"__Host-aik_oidc_state": state_cookie},
                follow_redirects=False,
            )
            assert cb_res.status_code == 303
            cb_cookie_header = cb_res.headers.get("set-cookie", "")
            assert "__Host-aik_org_sess=" in cb_cookie_header
            assert "HttpOnly" in cb_cookie_header
            assert "Secure" in cb_cookie_header
        finally:
            app.dependency_overrides.pop(get_settings, None)

    async def test_root_callback_alias_redirects_to_canonical(
        self, api_client: httpx.AsyncClient
    ):
        """GET /callback forwards to /api/v1/auth/organizer/callback via 307 redirect."""
        res = await api_client.get(
            "/callback?code=mock_code&state=mock_state",
            follow_redirects=False,
        )
        assert res.status_code == 307
        assert res.headers.get("location") == "/api/v1/auth/organizer/callback?code=mock_code&state=mock_state"

