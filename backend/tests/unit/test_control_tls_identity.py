"""Control-plane gateway identity checks at the certificate boundary."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from pydantic import ValidationError

from app.workers.schemas import MatchCommandRequest
from app.workers.tls import (
    GATEWAY_SERVICE_ID,
    verified_gateway_service_from_scope,
)


def _certificates(service_uri: str = GATEWAY_SERVICE_ID):
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "control-test-ca")])
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=2))
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True, content_commitment=False, key_encipherment=False,
                data_encipherment=False, key_agreement=False, key_cert_sign=True,
                crl_sign=True, encipher_only=None, decipher_only=None,
            ),
            critical=True,
        )
        .sign(ca_key, hashes.SHA256())
    )
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "gateway")]))
        .issuer_name(ca.subject)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(hours=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.UniformResourceIdentifier(service_uri)]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False)
        .sign(ca_key, hashes.SHA256())
    )
    return ca, leaf


def _scope(certificate: x509.Certificate) -> dict:
    return {
        "extensions": {
            "tls": {
                "spec_version": "0.3",
                "client_cert_chain": [certificate.public_bytes(Encoding.PEM).decode("ascii")],
            }
        }
    }


def test_valid_gateway_identity_is_accepted():
    ca, gateway = _certificates()
    verified = verified_gateway_service_from_scope(_scope(gateway), (ca,))
    assert verified is not None
    assert verified.service_name == GATEWAY_SERVICE_ID


def test_missing_peer_identity_fails_closed():
    ca, _gateway = _certificates()
    assert verified_gateway_service_from_scope({"extensions": {}}, (ca,)) is None


def test_untrusted_certificate_fails_closed():
    _trusted_ca, gateway = _certificates()
    other_ca, _other_leaf = _certificates()
    assert verified_gateway_service_from_scope(_scope(gateway), (other_ca,)) is None


def test_wrong_service_identity_fails_closed():
    ca, other_service = _certificates("urn:aik:service:other")
    assert verified_gateway_service_from_scope(_scope(other_service), (ca,)) is None


def test_worker_cannot_override_certificate_derived_identity_in_request_body():
    with pytest.raises(ValidationError):
        MatchCommandRequest.model_validate({
            "lease_id": "00000000-0000-0000-0000-000000000001",
            "fencing_token": 1,
            "attempt": 1,
            "worker_id": "another-worker",
        })


@pytest.mark.asyncio
async def test_unauthorized_control_request_fails_closed():
    import httpx
    from app.matches.control_api import create_control_plane_app

    class DummyControlService:
        async def command_issue(self, payload, gateway):
            return {"command_id": "dummy"}
        async def submit_response(self, payload, gateway):
            return {}
        async def fence_execution(self, payload, gateway):
            return {}
        async def abort_match(self, payload, gateway):
            return {}
        async def acknowledge(self, payload, gateway):
            return {}

    ca, gateway = _certificates()
    app = create_control_plane_app(DummyControlService(), (ca,))

    # 1. Missing peer identity fails closed with 401
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://control.local") as client:
        res = await client.post("/internal/v1/commands/issue", json={
            "match_id": "00000000-0000-0000-0000-000000000001",
            "execution_id": "00000000-0000-0000-0000-000000000002",
            "side_id": "00000000-0000-0000-0000-000000000003",
            "worker_id": "w1",
            "worker_certificate_fingerprint": "0" * 64,
            "side": "WHITE",
            "lease_id": "00000000-0000-0000-0000-000000000004",
            "fence_token": 1,
            "attempt": 1,
        })
        assert res.status_code == 401

    # 2. Untrusted client certificate fails closed with 401
    _other_ca, other_leaf = _certificates()

    class CustomScopeTransport(httpx.ASGITransport):
        def __init__(self, app, scope_patch):
            super().__init__(app=app)
            self.scope_patch = scope_patch

        async def handle_async_request(self, request):
            return await super().handle_async_request(request)

    class UntrustedApp:
        def __init__(self, inner, cert):
            self.inner = inner
            self.cert = cert

        async def __call__(self, scope, receive, send):
            if scope.get("type") == "http":
                scope = dict(scope)
                scope["extensions"] = _scope(self.cert)["extensions"]
            await self.inner(scope, receive, send)

    untrusted_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=UntrustedApp(app, other_leaf)),
        base_url="https://control.local",
    )
    untrusted_res = await untrusted_client.post("/internal/v1/commands/issue", json={
        "match_id": "00000000-0000-0000-0000-000000000001",
        "execution_id": "00000000-0000-0000-0000-000000000002",
        "side_id": "00000000-0000-0000-0000-000000000003",
        "worker_id": "w1",
        "worker_certificate_fingerprint": "0" * 64,
        "side": "WHITE",
        "lease_id": "00000000-0000-0000-0000-000000000004",
        "fence_token": 1,
        "attempt": 1,
    })
    assert untrusted_res.status_code == 401
    await untrusted_client.aclose()

