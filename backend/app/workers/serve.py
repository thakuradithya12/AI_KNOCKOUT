"""Private mTLS-only Hypercorn entry point for the worker gateway.

Run with ``python -m app.workers.serve``. This listener is separate from the
participant API process and binds only to the configured private/loopback IP.
"""

from __future__ import annotations

import asyncio
import ssl

from app.workers.gateway import create_worker_gateway_app
from app.workers.tls import WorkerGatewayTLSConfig


def build_hypercorn_config(tls: WorkerGatewayTLSConfig):
    """Build a TLS-only Hypercorn listener with mandatory client certificates."""
    from hypercorn.config import Config

    config = Config()
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
    return config


async def run() -> None:
    from hypercorn.asyncio import serve

    tls = WorkerGatewayTLSConfig.from_env()
    # Validate and construct the same policy the listener must enforce before
    # Hypercorn opens a socket. Hypercorn applies TLSv1.2 minimum and exposes
    # peer certificates via the ASGI TLS extension.
    tls.build_server_ssl_context()
    await serve(create_worker_gateway_app(), build_hypercorn_config(tls))


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()
