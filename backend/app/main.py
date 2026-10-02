"""AI Knockout — Application Entry Point.

Lifecycle:
1. Load and validate configuration
2. Initialize structured logging
3. Initialize database engine
4. Initialize Redis client
5. Verify critical dependencies
6. Start API server

Shutdown:
1. Stop accepting requests
2. Close Redis client
3. Close database engine
4. Exit cleanly
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api.v1.health import router as health_router
from app.api.v1.router import router as v1_router
from app.core.config import get_settings
from app.core.errors import register_error_handlers
from app.core.logging import get_logger, setup_logging
from app.core.middleware import RequestIdMiddleware, SecurityHeadersMiddleware
from app.db.session import check_database_health, close_engine, init_engine
from app.services.redis import check_redis_health, close_redis, init_redis

logger = get_logger("main")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan manager — startup and shutdown."""
    settings = get_settings()

    # --- STARTUP ---
    logger.info(
        f"Starting {settings.app_name} v{settings.app_version}",
        extra={"environment": settings.app_env},
    )

    # Initialize database
    init_engine(
        database_url=settings.database_url,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_timeout=settings.db_pool_timeout,
        pool_recycle=settings.db_pool_recycle,
        echo=settings.db_echo,
    )

    # Verify database with retry
    import asyncio

    max_retries = 5
    for attempt in range(max_retries):
        db_health = await check_database_health()
        if db_health["status"] == "ok":
            logger.info("Database connection verified")
            break
        logger.warning(
            f"Database not reachable (attempt {attempt + 1}/{max_retries})", extra=db_health
        )
        if attempt == max_retries - 1:
            logger.error("Database is not reachable at startup", extra=db_health)
            raise RuntimeError(f"Database verification failed: {db_health}")
        await asyncio.sleep(2)

    # Initialize Redis
    init_redis(
        redis_url=settings.redis_url,
        socket_timeout=settings.redis_socket_timeout,
        socket_connect_timeout=settings.redis_socket_connect_timeout,
        max_connections=settings.redis_max_connections,
    )

    # Verify Redis
    redis_health = await check_redis_health()
    if redis_health["status"] != "ok":
        logger.error("Redis is not reachable at startup", extra=redis_health)
        raise RuntimeError(f"Redis verification failed: {redis_health}")
    logger.info("Redis connection verified")

    logger.info("Application startup complete")

    yield

    # --- SHUTDOWN ---
    logger.info("Shutting down application...")
    await close_redis()
    await close_engine()
    logger.info("Application shutdown complete")


def create_app() -> FastAPI:
    """Application factory."""
    settings = get_settings()

    # Initialize logging first
    setup_logging(settings.log_level)

    app = FastAPI(
        title=settings.app_name,
        version=settings.app_version,
        docs_url="/docs" if settings.is_development else None,
        redoc_url="/redoc" if settings.is_development else None,
        openapi_url="/openapi.json" if settings.is_development else None,
        lifespan=lifespan,
    )

    # Middleware (order matters — outermost first)
    app.add_middleware(SecurityHeadersMiddleware)
    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["*"],
    )

    # Error handlers
    register_error_handlers(app, is_development=settings.is_development)

    # Mount root-level health endpoints (outside /api/v1 for infrastructure probes)
    app.include_router(health_router)

    # Mount versioned API
    app.include_router(v1_router)

    # Serve the participant portal from the same origin as the API. This keeps
    # LAN setup simple and avoids broadening the bearer-token CORS surface.
    static_dir = Path(__file__).parent / "static"
    app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    async def participant_portal() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    @app.get("/portal", include_in_schema=False)
    async def participant_portal_alias() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    return app


# Application instance
app = create_app()
