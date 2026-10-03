"""Private mTLS-only Hypercorn entry point for the worker gateway.

Run with ``python -m app.workers.serve``. This listener is separate from the
participant API process and binds only to the configured private/loopback IP.
"""

from __future__ import annotations

import asyncio
import ssl
from ssl import TLSVersion

from app.workers.gateway import create_worker_gateway_app
from app.workers.tls import WorkerGatewayTLSConfig


def build_hypercorn_config(tls: WorkerGatewayTLSConfig):
    """Build a TLS-only Hypercorn listener with mandatory client certificates."""
    from hypercorn.config import Config
    from hypercorn.logging import Logger

    class BoundedWorkerLogger(Logger):
        """Emit only bounded categories; never serialize raw TLS/request details."""

        def error(self, message, *args, **kwargs):
            super().error("WORKER_LISTENER_ERROR")

        def warning(self, message, *args, **kwargs):
            super().warning("WORKER_LISTENER_WARNING")

        def exception(self, message, *args, **kwargs):
            super().error("WORKER_LISTENER_EXCEPTION")

    class WorkerTLSConfig(Config):
        """Pin transport policy on the SSL context Hypercorn actually creates."""

        def create_ssl_context(self):
            context = super().create_ssl_context()
            if context is None:
                raise RuntimeError("worker listener TLS context was not created")
            context.minimum_version = TLSVersion.TLSv1_2
            context.verify_mode = ssl.CERT_REQUIRED
            context.options |= ssl.OP_NO_COMPRESSION
            return context

    config = WorkerTLSConfig()
    address = (
        f"[{tls.bind_host}]:{tls.port}" if ":" in tls.bind_host else f"{tls.bind_host}:{tls.port}"
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
    config.logger_class = BoundedWorkerLogger
    return config


async def run() -> None:
    from hypercorn.asyncio import serve

    tls = WorkerGatewayTLSConfig.from_env()
    # Fail before opening a socket if server material or key permissions fail.
    # Hypercorn creates its own context via WorkerTLSConfig above; that context
    # receives the explicit TLS minimum and mandatory-client-cert policy.
    tls.build_server_ssl_context()
    await serve(create_worker_gateway_app(), build_hypercorn_config(tls))


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
