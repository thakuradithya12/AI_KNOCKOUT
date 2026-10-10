"""Fail-closed TLS configuration and worker certificate identity validation.

The listener must enforce TLS 1.2+ and client-certificate verification against the
configured CA. The ASGI application independently checks the supplied leaf
certificate and its worker URI SAN before mapping it to a database identity.
"""

from __future__ import annotations

import hashlib
import ipaddress
import os
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.serialization import Encoding
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID


class WorkerTLSConfigurationError(ValueError):
    """Raised when TLS inputs are absent, invalid, expired, or misbound."""


@dataclass(frozen=True)
class VerifiedWorkerCertificate:
    fingerprint: str
    subject: str
    common_name: str
    worker_id: str
    serial: str
    not_before: datetime
    not_after: datetime


@dataclass(frozen=True)
class VerifiedServiceCertificate:
    fingerprint: str
    service_name: str
    not_before: datetime
    not_after: datetime


GATEWAY_SERVICE_ID = "urn:aik:service:worker-gateway"
COORDINATOR_SERVICE_ID = "urn:aik:service:match-coordinator"


@dataclass(frozen=True)
class WorkerGatewayTLSConfig:
    ca_certificate: Path
    server_certificate: Path
    server_private_key: Path
    server_name: str
    bind_host: str
    port: int

    @classmethod
    def from_env(cls) -> WorkerGatewayTLSConfig:
        enabled = os.environ.get("WORKER_TLS_ENABLED", "").strip().lower()
        if enabled != "true":
            raise WorkerTLSConfigurationError("WORKER_TLS_ENABLED must be true")
        required = {
            "WORKER_TLS_CA_CERT": os.environ.get("WORKER_TLS_CA_CERT"),
            "WORKER_TLS_SERVER_CERT": os.environ.get("WORKER_TLS_SERVER_CERT"),
            "WORKER_TLS_SERVER_KEY": os.environ.get("WORKER_TLS_SERVER_KEY"),
            "WORKER_TLS_SERVER_NAME": os.environ.get("WORKER_TLS_SERVER_NAME"),
            "WORKER_TLS_BIND_HOST": os.environ.get("WORKER_TLS_BIND_HOST"),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise WorkerTLSConfigurationError(
                "required worker TLS configuration is missing: " + ", ".join(missing)
            )
        try:
            host = ipaddress.ip_address(required["WORKER_TLS_BIND_HOST"])
        except ValueError as exc:
            raise WorkerTLSConfigurationError(
                "WORKER_TLS_BIND_HOST must be a specific IP address"
            ) from exc
        if host.is_unspecified or not (host.is_private or host.is_loopback):
            raise WorkerTLSConfigurationError(
                "worker gateway must bind to loopback or a private IP"
            )
        try:
            port = int(os.environ.get("WORKER_TLS_PORT", "8443"))
        except ValueError as exc:
            raise WorkerTLSConfigurationError("WORKER_TLS_PORT must be an integer") from exc
        if not 1 <= port <= 65535:
            raise WorkerTLSConfigurationError("WORKER_TLS_PORT is outside the valid port range")
        config = cls(
            ca_certificate=Path(required["WORKER_TLS_CA_CERT"]),
            server_certificate=Path(required["WORKER_TLS_SERVER_CERT"]),
            server_private_key=Path(required["WORKER_TLS_SERVER_KEY"]),
            server_name=required["WORKER_TLS_SERVER_NAME"],
            bind_host=str(host),
            port=port,
        )
        config.validate_server_material()
        return config

    def validate_server_material(self) -> tuple[x509.Certificate, ...]:
        for name, path in (
            ("CA certificate", self.ca_certificate),
            ("server certificate", self.server_certificate),
            ("server private key", self.server_private_key),
        ):
            if not path.is_file():
                raise WorkerTLSConfigurationError(f"configured {name} file is unavailable")
        _check_private_key_permissions(self.server_private_key)
        roots = _load_certificates(self.ca_certificate)
        server = _load_certificates(self.server_certificate)[0]
        _require_valid_dates(server)
        _require_signed_by_trusted_ca(server, roots)
        _require_key_matches(server, self.server_private_key)
        try:
            names = server.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            if self.server_name not in names.get_values_for_type(x509.DNSName):
                raise WorkerTLSConfigurationError(
                    "server certificate SAN does not match configured name"
                )
        except x509.ExtensionNotFound as exc:
            raise WorkerTLSConfigurationError("server certificate requires a DNS SAN") from exc
        try:
            usages = server.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
            if ExtendedKeyUsageOID.SERVER_AUTH not in usages:
                raise WorkerTLSConfigurationError("server certificate lacks serverAuth usage")
        except x509.ExtensionNotFound as exc:
            raise WorkerTLSConfigurationError(
                "server certificate requires extended key usage"
            ) from exc
        return roots

    def build_server_ssl_context(self) -> ssl.SSLContext:
        roots = self.validate_server_material()
        context = ssl.create_default_context(
            ssl.Purpose.CLIENT_AUTH, cafile=str(self.ca_certificate)
        )
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.verify_mode = ssl.CERT_REQUIRED
        context.options |= ssl.OP_NO_COMPRESSION
        context.load_cert_chain(
            certfile=str(self.server_certificate), keyfile=str(self.server_private_key)
        )
        if not roots:
            raise WorkerTLSConfigurationError("worker CA bundle contains no certificates")
        return context


@dataclass(frozen=True)
class WorkerClientTLSConfig:
    ca_certificate: Path
    worker_certificate: Path
    worker_private_key: Path
    worker_id: str
    server_name: str

    def build_ssl_context(self) -> ssl.SSLContext:
        for name, path in (
            ("CA certificate", self.ca_certificate),
            ("worker certificate", self.worker_certificate),
            ("worker private key", self.worker_private_key),
        ):
            if not path.is_file():
                raise WorkerTLSConfigurationError(f"configured {name} file is unavailable")
        _check_private_key_permissions(self.worker_private_key)
        roots = _load_certificates(self.ca_certificate)
        worker = _load_certificates(self.worker_certificate)[0]
        _require_valid_dates(worker)
        _require_signed_by_trusted_ca(worker, roots)
        _require_key_matches(worker, self.worker_private_key)
        _require_worker_san(worker, self.worker_id)
        _require_client_auth(worker)
        context = ssl.create_default_context(
            ssl.Purpose.SERVER_AUTH, cafile=str(self.ca_certificate)
        )
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        context.options |= ssl.OP_NO_COMPRESSION
        context.load_cert_chain(
            certfile=str(self.worker_certificate), keyfile=str(self.worker_private_key)
        )
        return context


@dataclass(frozen=True)
class GatewayServiceClientTLSConfig:
    """Gateway identity for gateway→coordinator mTLS, separate from worker TLS."""

    ca_certificate: Path
    gateway_certificate: Path
    gateway_private_key: Path
    coordinator_server_name: str

    @classmethod
    def from_env(cls) -> GatewayServiceClientTLSConfig:
        names = {
            "MATCH_SERVICE_CA_CERT": os.environ.get("MATCH_SERVICE_CA_CERT"),
            "GATEWAY_SERVICE_CERT": os.environ.get("GATEWAY_SERVICE_CERT"),
            "GATEWAY_SERVICE_KEY": os.environ.get("GATEWAY_SERVICE_KEY"),
            "COORDINATOR_SERVER_NAME": os.environ.get("COORDINATOR_SERVER_NAME"),
        }
        missing = [key for key, value in names.items() if not value]
        if missing:
            raise WorkerTLSConfigurationError(
                "required gateway service TLS configuration is missing: " + ", ".join(missing)
            )
        return cls(
            ca_certificate=Path(names["MATCH_SERVICE_CA_CERT"]),
            gateway_certificate=Path(names["GATEWAY_SERVICE_CERT"]),
            gateway_private_key=Path(names["GATEWAY_SERVICE_KEY"]),
            coordinator_server_name=names["COORDINATOR_SERVER_NAME"],
        )

    def build_ssl_context(self) -> ssl.SSLContext:
        for name, path in (
            ("service CA certificate", self.ca_certificate),
            ("gateway service certificate", self.gateway_certificate),
            ("gateway service private key", self.gateway_private_key),
        ):
            if not path.is_file():
                raise WorkerTLSConfigurationError(f"configured {name} file is unavailable")
        _check_private_key_permissions(self.gateway_private_key)
        roots = _load_certificates(self.ca_certificate)
        leaf = _load_certificates(self.gateway_certificate)[0]
        _require_valid_dates(leaf)
        _require_signed_by_trusted_ca(leaf, roots)
        _require_key_matches(leaf, self.gateway_private_key)
        _require_service_identity(leaf, GATEWAY_SERVICE_ID, ExtendedKeyUsageOID.CLIENT_AUTH)
        context = ssl.create_default_context(
            ssl.Purpose.SERVER_AUTH, cafile=str(self.ca_certificate)
        )
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.check_hostname = True
        context.verify_mode = ssl.CERT_REQUIRED
        context.options |= ssl.OP_NO_COMPRESSION
        context.load_cert_chain(str(self.gateway_certificate), str(self.gateway_private_key))
        return context


@dataclass(frozen=True)
class MatchCoordinatorServiceTLSConfig:
    """Private coordinator listener identity and dedicated gateway trust root."""

    ca_certificate: Path
    server_certificate: Path
    server_private_key: Path
    server_name: str
    bind_host: str
    port: int

    @classmethod
    def from_env(cls) -> MatchCoordinatorServiceTLSConfig:
        enabled = os.environ.get("MATCH_CONTROL_TLS_ENABLED", "").strip().lower()
        if enabled != "true":
            raise WorkerTLSConfigurationError("MATCH_CONTROL_TLS_ENABLED must be true")
        names = {
            "MATCH_SERVICE_CA_CERT": os.environ.get("MATCH_SERVICE_CA_CERT"),
            "COORDINATOR_SERVER_CERT": os.environ.get("COORDINATOR_SERVER_CERT"),
            "COORDINATOR_SERVER_KEY": os.environ.get("COORDINATOR_SERVER_KEY"),
            "COORDINATOR_SERVER_NAME": os.environ.get("COORDINATOR_SERVER_NAME"),
            "MATCH_CONTROL_BIND_HOST": os.environ.get("MATCH_CONTROL_BIND_HOST"),
        }
        missing = [key for key, value in names.items() if not value]
        if missing:
            raise WorkerTLSConfigurationError(
                "required match control TLS configuration is missing: " + ", ".join(missing)
            )
        try:
            host = str(ipaddress.ip_address(names["MATCH_CONTROL_BIND_HOST"]))
            port = int(os.environ.get("MATCH_CONTROL_PORT", "8444"))
        except ValueError as exc:
            raise WorkerTLSConfigurationError("match control bind address/port is invalid") from exc
        config = cls(
            ca_certificate=Path(names["MATCH_SERVICE_CA_CERT"]),
            server_certificate=Path(names["COORDINATOR_SERVER_CERT"]),
            server_private_key=Path(names["COORDINATOR_SERVER_KEY"]),
            server_name=names["COORDINATOR_SERVER_NAME"],
            bind_host=host,
            port=port,
        )
        config.validate_server_material()
        return config

    def validate_server_material(self) -> tuple[x509.Certificate, ...]:
        host = ipaddress.ip_address(self.bind_host)
        if host.is_unspecified or not (host.is_private or host.is_loopback):
            raise WorkerTLSConfigurationError("coordinator listener must bind privately")
        if not 1 <= self.port <= 65535:
            raise WorkerTLSConfigurationError("coordinator port is invalid")
        for name, path in (
            ("service CA certificate", self.ca_certificate),
            ("coordinator server certificate", self.server_certificate),
            ("coordinator private key", self.server_private_key),
        ):
            if not path.is_file():
                raise WorkerTLSConfigurationError(f"configured {name} file is unavailable")
        _check_private_key_permissions(self.server_private_key)
        roots = _load_certificates(self.ca_certificate)
        leaf = _load_certificates(self.server_certificate)[0]
        _require_valid_dates(leaf)
        _require_signed_by_trusted_ca(leaf, roots)
        _require_key_matches(leaf, self.server_private_key)
        _require_service_identity(leaf, COORDINATOR_SERVICE_ID, ExtendedKeyUsageOID.SERVER_AUTH)
        try:
            names = leaf.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            if self.server_name not in names.get_values_for_type(x509.DNSName):
                raise WorkerTLSConfigurationError("coordinator server SAN mismatch")
        except x509.ExtensionNotFound as exc:
            raise WorkerTLSConfigurationError("coordinator certificate requires DNS SAN") from exc
        return roots

    def build_server_ssl_context(self) -> ssl.SSLContext:
        roots = self.validate_server_material()
        context = ssl.create_default_context(
            ssl.Purpose.CLIENT_AUTH, cafile=str(self.ca_certificate)
        )
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.verify_mode = ssl.CERT_REQUIRED
        context.options |= ssl.OP_NO_COMPRESSION
        context.load_cert_chain(str(self.server_certificate), str(self.server_private_key))
        if not roots:
            raise WorkerTLSConfigurationError("service CA bundle is empty")
        return context


def _require_service_identity(
    certificate: x509.Certificate, expected: str, eku: x509.ObjectIdentifier
) -> None:
    try:
        names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        uris = names.get_values_for_type(x509.UniformResourceIdentifier)
        if uris != [expected] or any(uri.startswith("urn:aik:worker:") for uri in uris):
            raise WorkerTLSConfigurationError("service certificate URI SAN mismatch")
        usages = certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        if eku not in usages:
            raise WorkerTLSConfigurationError("service certificate EKU mismatch")
    except x509.ExtensionNotFound as exc:
        raise WorkerTLSConfigurationError("service certificate requires SAN and EKU") from exc


def verified_gateway_service_from_scope(
    scope: dict, trusted_ca_certificates: tuple[x509.Certificate, ...] | None
) -> VerifiedServiceCertificate | None:
    """Validate the coordinator's peer as the dedicated gateway service identity."""
    extensions = scope.get("extensions")
    tls = extensions.get("tls") if isinstance(extensions, dict) else None
    if not isinstance(tls, dict) or tls.get("client_cert_error"):
        return None
    chain = tls.get("client_cert_chain")
    if not isinstance(chain, (list, tuple)) or not chain or not isinstance(chain[0], str):
        return None
    if not trusted_ca_certificates:
        return None
    try:
        leaf = x509.load_pem_x509_certificate(chain[0].encode("ascii"))
        _require_valid_dates(leaf)
        _require_signed_by_trusted_ca(leaf, trusted_ca_certificates)
        _require_service_identity(leaf, GATEWAY_SERVICE_ID, ExtendedKeyUsageOID.CLIENT_AUTH)
        der = leaf.public_bytes(Encoding.DER)
    except (ValueError, UnicodeEncodeError, AttributeError, WorkerTLSConfigurationError):
        return None
    return VerifiedServiceCertificate(
        fingerprint=hashlib.sha256(der).hexdigest(),
        service_name=GATEWAY_SERVICE_ID,
        not_before=leaf.not_valid_before_utc,
        not_after=leaf.not_valid_after_utc,
    )


def _load_certificates(path: Path) -> tuple[x509.Certificate, ...]:
    try:
        data = path.read_bytes()
        certs: list[x509.Certificate] = []
        marker = b"-----END CERTIFICATE-----"
        for block in data.split(marker):
            if b"-----BEGIN CERTIFICATE-----" in block:
                certs.append(x509.load_pem_x509_certificate(block + marker + b"\n"))
        if not certs:
            raise WorkerTLSConfigurationError("certificate file contains no PEM certificates")
        return tuple(certs)
    except (OSError, ValueError) as exc:
        raise WorkerTLSConfigurationError("certificate file could not be parsed") from exc


def _require_valid_dates(certificate: x509.Certificate) -> None:
    now = datetime.now(timezone.utc)
    if now < certificate.not_valid_before_utc or now >= certificate.not_valid_after_utc:
        raise WorkerTLSConfigurationError("certificate is not currently valid")


def _require_signed_by_trusted_ca(
    certificate: x509.Certificate, roots: tuple[x509.Certificate, ...]
) -> None:
    for root in roots:
        try:
            _require_valid_dates(root)
            constraints = root.extensions.get_extension_for_class(x509.BasicConstraints).value
            if not constraints.ca or certificate.issuer != root.subject:
                continue
            certificate.verify_directly_issued_by(root)
            return
        except (x509.ExtensionNotFound, InvalidSignature, ValueError):
            continue
    raise WorkerTLSConfigurationError("certificate chain is not issued by the configured CA")


def _require_key_matches(certificate: x509.Certificate, key_path: Path) -> None:
    try:
        private_key = serialization.load_pem_private_key(key_path.read_bytes(), password=None)
    except (OSError, ValueError, TypeError) as exc:
        raise WorkerTLSConfigurationError(
            "private key could not be loaded without a password"
        ) from exc
    public_format = serialization.PublicFormat.SubjectPublicKeyInfo
    if certificate.public_key().public_bytes(
        Encoding.DER, public_format
    ) != private_key.public_key().public_bytes(Encoding.DER, public_format):
        raise WorkerTLSConfigurationError("certificate and private key do not match")


def _check_private_key_permissions(path: Path) -> None:
    if os.name != "nt" and path.stat().st_mode & 0o077:
        raise WorkerTLSConfigurationError(
            "private key file must not be accessible to group or others"
        )


def _require_worker_san(certificate: x509.Certificate, worker_id: str) -> None:
    try:
        names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    except x509.ExtensionNotFound as exc:
        raise WorkerTLSConfigurationError("worker certificate requires a URI SAN") from exc
    expected = f"urn:aik:worker:{worker_id}"
    uris = names.get_values_for_type(x509.UniformResourceIdentifier)
    if uris.count(expected) != 1 or len(uris) != 1:
        raise WorkerTLSConfigurationError("worker certificate URI SAN does not match worker_id")


def _require_client_auth(certificate: x509.Certificate) -> None:
    try:
        usages = certificate.extensions.get_extension_for_class(x509.ExtendedKeyUsage).value
        if ExtendedKeyUsageOID.CLIENT_AUTH not in usages:
            raise WorkerTLSConfigurationError("worker certificate lacks clientAuth usage")
    except x509.ExtensionNotFound as exc:
        raise WorkerTLSConfigurationError("worker certificate requires extended key usage") from exc


def verified_peer_from_scope(
    scope: dict, trusted_ca_certificates: tuple[x509.Certificate, ...] | None = None
) -> VerifiedWorkerCertificate | None:
    """Parse and validate a peer cert from the ASGI TLS extension; never trust headers."""
    extensions = scope.get("extensions")
    tls = extensions.get("tls") if isinstance(extensions, dict) else None
    if not isinstance(tls, dict) or tls.get("client_cert_error"):
        return None
    spec_version = tls.get("spec_version")
    if spec_version is not None and spec_version not in {"0.2", "0.3"}:
        return None
    chain = tls.get("client_cert_chain")
    if not isinstance(chain, (list, tuple)) or not chain or not isinstance(chain[0], str):
        return None
    # ASGI TLS extension 0.3 removed tls_version; TLS strength is enforced by
    # the configured listener. Validate an explicit version when older servers supply it.
    tls_version = tls.get("tls_version")
    if tls_version is not None and (not isinstance(tls_version, int) or tls_version < 0x0303):
        return None
    if not trusted_ca_certificates:
        return None
    try:
        leaf = x509.load_pem_x509_certificate(chain[0].encode("ascii"))
        _require_valid_dates(leaf)
        _require_signed_by_trusted_ca(leaf, trusted_ca_certificates)
        worker_id = _worker_id_from_certificate(leaf)
        _require_worker_san(leaf, worker_id)
        _require_client_auth(leaf)
        der = leaf.public_bytes(Encoding.DER)
        common_names = leaf.subject.get_attributes_for_oid(NameOID.COMMON_NAME)
    except (ValueError, UnicodeEncodeError, AttributeError, WorkerTLSConfigurationError):
        return None
    return VerifiedWorkerCertificate(
        fingerprint=hashlib.sha256(der).hexdigest(),
        subject=leaf.subject.rfc4514_string()[:512],
        common_name=common_names[0].value[:128] if common_names else "",
        worker_id=worker_id,
        serial=format(leaf.serial_number, "x"),
        not_before=leaf.not_valid_before_utc,
        not_after=leaf.not_valid_after_utc,
    )


def _worker_id_from_certificate(certificate: x509.Certificate) -> str:
    names = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    uris = names.get_values_for_type(x509.UniformResourceIdentifier)
    prefix = "urn:aik:worker:"
    if len(uris) != 1 or not uris[0].startswith(prefix):
        raise WorkerTLSConfigurationError("worker certificate requires exactly one worker URI SAN")
    worker_id = uris[0][len(prefix) :]
    if not worker_id:
        raise WorkerTLSConfigurationError("worker certificate URI SAN has no worker ID")
    return worker_id


# Backward-compatible helper names for callers configuring a listener/client.
def server_ssl_context(
    ca_bundle: str | Path,
    server_cert: str | Path,
    server_key: str | Path,
    *,
    server_name: str,
) -> ssl.SSLContext:
    return WorkerGatewayTLSConfig(
        Path(ca_bundle), Path(server_cert), Path(server_key), server_name, "127.0.0.1", 8443
    ).build_server_ssl_context()


def worker_client_ssl_context(
    ca_bundle: str | Path,
    client_cert: str | Path,
    client_key: str | Path,
    *,
    worker_id: str,
    server_name: str,
) -> ssl.SSLContext:
    return WorkerClientTLSConfig(
        Path(ca_bundle), Path(client_cert), Path(client_key), worker_id, server_name
    ).build_ssl_context()
