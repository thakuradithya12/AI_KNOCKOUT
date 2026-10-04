# Architecture

## System Overview

The event-level lifecycle and approval-gated policy decisions are specified in
[AI Knockout Event Master Plan V1](AI_KNOCKOUT_EVENT_MASTER_PLAN.md). Build starts,
submission receipt/duration, and official-submission freeze are server-owned;
House AI execution, official tournament policy, admin controls, and spectator mode
remain future work.

```
┌─────────────────────────────────────────────────────────┐
│ Host Machine (Windows 11)                                │
│                                                          │
│  ┌─────────────────────────────────────────────────────┐ │
│  │ Docker Engine (via WSL2)                            │ │
│  │                                                     │ │
│  │  ┌─ aik-internal network (bridge) ───────────────┐  │ │
│  │  │                                               │  │ │
│  │  │  ┌─────────────┐   port 5432   ┌──────────┐  │  │ │
│  │  │  │  aik-api    │──────────────▶│ aik-     │  │  │ │
│  │  │  │  (FastAPI)  │               │ postgres │  │  │ │
│  │  │  │  :8000      │               │ (PG 16)  │  │  │ │
│  │  │  └─────────────┘               └──────────┘  │  │ │
│  │  │         │                                     │  │ │
│  │  │         │ port 6379    ┌──────────┐           │  │ │
│  │  │         └─────────────▶│ aik-     │           │  │ │
│  │  │                        │ redis    │           │  │ │
│  │  │                        │ (Redis7) │           │  │ │
│  │  │                        └──────────┘           │  │ │
│  │  └───────────────────────────────────────────────┘  │ │
│  │                                                     │ │
│  │  Host Ports: 8000 (API), 5433 (PG), 6379 (Redis)  │ │
│  └─────────────────────────────────────────────────────┘ │
└─────────────────────────────────────────────────────────┘
```

## Components

### API Server (`aik-api`)
- **Technology**: Python 3.10, FastAPI, Uvicorn
- **Purpose**: HTTP API server providing health, readiness, and version endpoints
- **Container**: `python:3.10-slim`, non-root user (`appuser`)
- **Port**: 8000 (internal and host-mapped)

### PostgreSQL (`aik-postgres`)
- **Technology**: PostgreSQL 16 Alpine
- **Purpose**: Primary persistent database
- **Port**: 5432 (internal), 5433 (host) — uses 5433 to avoid conflict with host PostgreSQL
- **Storage**: Named volume `aik-pgdata`
- **Init**: UTC timezone, `uuid-ossp` and `pgcrypto` extensions

### Redis (`aik-redis`)
- **Technology**: Redis 7 Alpine
- **Purpose**: Infrastructure foundation for future caching/messaging
- **Port**: 6379 (internal and host-mapped)
- **Storage**: Named volume `aik-redisdata` with AOF persistence
- **Memory**: 256MB max with LRU eviction

## Network Architecture

All services communicate via the `aik-internal` Docker bridge network. Services refer to each other by container name (`postgres`, `redis`).

**Host port mappings** (for development only):
- `8000` → API server
- `5433` → PostgreSQL (not 5432, because host already has PostgreSQL)
- `6379` → Redis

**Future deployment**: Only port 8000 should be exposed to the LAN. PostgreSQL and Redis remain internal.

## Data Persistence

| Volume | Mount Point | Purpose |
|---|---|---|
| `aik-pgdata` | `/var/lib/postgresql/data` | PostgreSQL data directory |
| `aik-redisdata` | `/data` | Redis AOF persistence |

Data survives container restarts and rebuilds.

## Configuration Flow

```
Environment Variables → .env file → pydantic-settings → Settings class → Application
```

All configuration is validated at startup. Missing required values cause immediate failure.

## Startup Sequence

1. Load and validate configuration (fail fast on bad config)
2. Initialize structured logging
3. Create FastAPI application with middleware
4. Initialize database engine with connection pool
5. Verify database connectivity
6. Initialize Redis client
7. Verify Redis connectivity
8. Start accepting HTTP requests

## Shutdown Sequence

1. Stop accepting new HTTP requests
2. Close Redis client and connections
3. Dispose database engine and connection pool
4. Exit cleanly

## Health Monitoring

- **`GET /health`** — Liveness: Is the API process running? (always returns `ok` if reachable)
- **`GET /ready`** — Readiness: Are PostgreSQL and Redis reachable? Returns dependency-level status.

## Logging

- **Format**: Structured JSON to stdout
- **Fields**: timestamp (UTC), level, service, logger, message, request_id
- **Security**: Sensitive fields (password, secret, token, api_key, etc.) are automatically redacted
- **Correlation**: Every request gets a unique `X-Request-ID` (auto-generated or client-provided)

## Security Boundaries

- Non-root container user
- Internal Docker network isolation
- No Docker socket mounting
- CORS controlled by environment configuration
- No tracebacks in production error responses
- Security headers (X-Content-Type-Options, X-Frame-Options, X-XSS-Protection, Referrer-Policy)

## Database Migrations

Alembic manages schema versioning:
```bash
# Create a new migration
docker compose exec api alembic revision --autogenerate -m "description"

# Apply migrations
docker compose exec api alembic upgrade head

# Check current version
docker compose exec api alembic current

# Rollback one step
docker compose exec api alembic downgrade -1
```

## Timezone Policy

All timestamps stored in the database and emitted in logs use **UTC**. The PostgreSQL database timezone is set to UTC. Python code uses `datetime.now(timezone.utc)`.

## Current worker-control-plane status

Worker protocol code lives in a separate ASGI application and listener entry point (`backend/app/workers/gateway.py`, `backend/app/workers/serve.py`). It is not mounted in the participant API and is not a Compose service. Enrollment and certificate lifecycle logic, TLS configuration, execution-scoped artifact capabilities, and lease/fencing are implemented. The listener has not been deployed or tested with a real TLS handshake. The current protocol is mock-only; no participant package is executed. Dedicated Linux/KVM, Kata, sandbox isolation, chess execution and match/tournament services are not implemented or verified.

## Original Phase-1 Extension Points (historical)

Future phases will add:
- **Authentication middleware** on the FastAPI app
- **WebSocket endpoints** for live spectator
- **Worker services** as additional Docker containers
- **Sandboxed execution** with isolated Docker networks
- **Additional database models** via Alembic migrations
- **Redis queues** for match scheduling

This list records anticipated additions at the time of the Phase-1 architecture document. It is not a current inventory; use the status sections above and in `SECURE_EXECUTION_ENVIRONMENT_V1.md` for current implementation and verification state.
