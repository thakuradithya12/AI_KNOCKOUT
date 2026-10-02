# AI Knockout

**AI Knockout — Participant and Worker-Control-Plane Prototype**

## Status

**Current implementation status**

- **IMPLEMENTED:** participant identity/authentication; submission, immutable quarantine and static validation; mock-only worker control protocol; separate worker gateway and listener entry point; enrollment and certificate lifecycle logic; execution-scoped artifact capabilities; leases/fencing; TLS configuration.
- **NOT VERIFIED:** real TLS handshake/certificate transport; deployed worker listener; dedicated Linux/KVM worker; Kata; sandbox isolation.
- **PARTICIPANT EXECUTION: DISABLED.** No participant artifact has been executed.

This README began as the Phase 1 foundation guide. The status above reflects the current source tree; Compose still deploys only the participant API, PostgreSQL and Redis. See [Worker security status](docs/SECURE_EXECUTION_ENVIRONMENT_V1.md) for verification limits.

## What's Included

| Component | Technology | Purpose |
|---|---|---|
| API Server | FastAPI + Uvicorn | HTTP API with health/readiness/version endpoints |
| Database | PostgreSQL 16 Alpine | Persistent data storage |
| Cache/Messaging | Redis 7 Alpine | Infrastructure foundation for future phases |
| Migrations | Alembic + SQLAlchemy 2.0 | Schema version control |
| Configuration | Pydantic Settings | Validated, environment-driven config |
| Logging | Structured JSON | Request correlation, sensitive data redaction |
| Infrastructure | Docker Compose | Reproducible multi-service deployment |

## Quick Start

```bash
# 1. Enter project directory
cd c:\TECH_EVENT\AI_KNOCKOUT

# 2. Copy and configure environment
cp .env.example .env
# Edit .env — generate real secrets:
#   python -c "import secrets; print(secrets.token_urlsafe(64))"  # SECRET_KEY
#   python -c "import secrets; print(secrets.token_urlsafe(32))"  # POSTGRES_PASSWORD

# 3. Start infrastructure
docker compose up -d --build

# 4. Wait for services to be healthy
docker compose ps

# 5. Run database migrations
docker compose exec api alembic upgrade head

# 6. Verify
curl http://localhost:8000/health
curl http://localhost:8000/ready
curl http://localhost:8000/version
```

## API Endpoints

| Method | Path | Purpose |
|---|---|---|
| GET | `/health` | Liveness probe |
| GET | `/ready` | Readiness probe (checks PostgreSQL + Redis) |
| GET | `/version` | Service version and environment |
| GET | `/api/v1/health` | Versioned liveness |
| GET | `/api/v1/ready` | Versioned readiness |
| GET | `/api/v1/version` | Versioned version info |
| GET | `/docs` | OpenAPI docs (development only) |

## Architecture

```
Host (Windows 11) → Docker Engine → aik-internal network
                                      ├── aik-api     (FastAPI, port 8000)
                                      ├── aik-postgres (PostgreSQL 16, port 5432 internal / 5433 host)
                                      └── aik-redis    (Redis 7, port 6379)
```

## Documentation

- [Architecture](docs/ARCHITECTURE.md)
- [Deployment](docs/DEPLOYMENT.md)
- [Operations](docs/OPERATIONS.md)
- [Testing](docs/TESTING.md)
- [Security](docs/SECURITY.md)
- [Backup & Restore](docs/BACKUP_AND_RESTORE.md)

## Original Phase Roadmap (historical)

This early roadmap predates the implementation milestones listed above and is retained as historical planning context. Sandbox execution and later competition features remain gated and are not implemented.

## License

TBD
