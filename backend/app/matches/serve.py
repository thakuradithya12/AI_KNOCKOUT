"""Private mTLS listener for MatchCoordinator control operations."""

from __future__ import annotations

import asyncio
import contextvars
import os
import ssl
from ssl import TLSVersion

from hypercorn.asyncio.tcp_server import TCPServer

from app.matches.control_api import create_control_plane_app
from app.matches.control_service import DatabaseControlService
from app.workers.tls import MatchCoordinatorServiceTLSConfig

_tls_peer_certificate: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "match_control_tls_peer_certificate", default=None
)


class _VerifiedPeerTCPServer(TCPServer):
    """Expose Hypercorn's already-verified TLS peer certificate to its ASGI task."""

    async def run(self) -> None:
        ssl_object = self.writer.get_extra_info("ssl_object")
        certificate = None
        if ssl_object is not None:
            der = ssl_object.getpeercert(binary_form=True)
            if der:
                certificate = ssl.DER_cert_to_PEM_cert(der)
        token = _tls_peer_certificate.set(certificate)
        try:
            await super().run()
        finally:
            _tls_peer_certificate.reset(token)


class _ASGITLSIdentity:
    """Adapt the verified connection certificate into the standard ASGI TLS extension."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send):
        certificate = _tls_peer_certificate.get()
        if scope.get("type") == "http" and scope.get("scheme") == "https" and certificate:
            scope = dict(scope)
            extensions = dict(scope.get("extensions", {}))
            tls = dict(extensions.get("tls", {}))
            # Use the certificate read from this connection's SSLObject, never a header
            # or a client-controlled scope value.
            tls.update(spec_version="0.3", client_cert_chain=[certificate])
            extensions["tls"] = tls
            scope["extensions"] = extensions
        await self.app(scope, receive, send)


async def serve_with_verified_peer_identity(app, config, *, shutdown_trigger=None) -> None:
    """Serve with the ASGI TLS extension populated from Hypercorn's TLS connection."""
    import hypercorn.asyncio.run as hypercorn_run
    from hypercorn.asyncio import serve

    original_server = hypercorn_run.TCPServer
    hypercorn_run.TCPServer = _VerifiedPeerTCPServer
    try:
        await serve(
            _ASGITLSIdentity(app), config, shutdown_trigger=shutdown_trigger, mode="asgi"
        )
    finally:
        hypercorn_run.TCPServer = original_server


def build_hypercorn_config(tls: MatchCoordinatorServiceTLSConfig):
    from hypercorn.config import Config
    from hypercorn.logging import Logger

    class BoundedControlLogger(Logger):
        def error(self, message, *args, **kwargs):
            super().error("MATCH_CONTROL_LISTENER_ERROR")

        def warning(self, message, *args, **kwargs):
            super().warning("MATCH_CONTROL_LISTENER_WARNING")

        def exception(self, message, *args, **kwargs):
            super().error("MATCH_CONTROL_LISTENER_EXCEPTION")

    class ControlTLSConfig(Config):
        def create_ssl_context(self):
            context = super().create_ssl_context()
            if context is None:
                raise RuntimeError("match control TLS context unavailable")
            context.minimum_version = TLSVersion.TLSv1_2
            context.verify_mode = ssl.CERT_REQUIRED
            context.options |= ssl.OP_NO_COMPRESSION
            return context

    config = ControlTLSConfig()
    address = (
        f"[{tls.bind_host}]:{tls.port}"
        if ":" in tls.bind_host
        else f"{tls.bind_host}:{tls.port}"
    )
    config.bind = [address]
    config.insecure_bind = []
    config.certfile = str(tls.server_certificate)
    config.keyfile = str(tls.server_private_key)
    config.ca_certs = str(tls.ca_certificate)
    config.verify_mode = ssl.CERT_REQUIRED
    config.workers = 1
    config.use_reloader = False
    config.accesslog = None
    config.errorlog = "-"
    config.loglevel = "WARNING"
    config.logger_class = BoundedControlLogger
    return config


async def run() -> None:
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from app.db.url import get_asyncpg_connect_args, normalize_async_database_url

    tls = MatchCoordinatorServiceTLSConfig.from_env()
    tls.build_server_ssl_context()
    database_url = os.environ.get("MATCH_DATABASE_URL")
    if not database_url:
        raise RuntimeError("MATCH_DATABASE_URL must be configured")
    normalized_db_url = normalize_async_database_url(database_url)
    connect_args = get_asyncpg_connect_args(database_url)
    engine = create_async_engine(normalized_db_url, connect_args=connect_args, pool_pre_ping=True)
    sessions = async_sessionmaker(engine, expire_on_commit=False)
    try:
        app = create_control_plane_app(
            DatabaseControlService(sessions), tls.validate_server_material()
        )
        await serve_with_verified_peer_identity(
            app,
            build_hypercorn_config(tls),
        )
    finally:
        await engine.dispose()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
