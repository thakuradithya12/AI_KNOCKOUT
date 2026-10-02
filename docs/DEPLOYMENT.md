# AI Knockout — Deployment Guide (Phase 1)

This guide provides step-by-step instructions to deploy, run, and verify the AI Knockout Phase 1 server foundation from a clean environment.

---

## 1. Prerequisites

Before deploying, ensure the host machine meets the following requirements:

- **Operating System**: Windows 11 with WSL2 enabled, or Linux (Ubuntu 22.04+ recommended)
- **Docker**: Docker Engine 24+ / Docker Desktop with WSL2 backend
- **Docker Compose**: v2.20+ (included with Docker Desktop)
- **Git**: 2.30+
- **Python**: 3.10+ (for local host tooling/testing; backend runs inside Docker container)
- **Hardware Minimum**: 2 CPU cores, 4 GB RAM, 10 GB free disk space

---

## 2. Step-by-Step Deployment

### Step 2.1: Clone or Navigate to Repository

```bash
cd c:\TECH_EVENT\AI_KNOCKOUT
```

### Step 2.2: Configure Environment

Copy the template environment file to `.env`:

```bash
cp .env.example .env
```

Generate secure, random secrets using Python:

```bash
# Generate 64-byte URL-safe secret key
python -c "import secrets; print(secrets.token_urlsafe(64))"

# Generate 32-byte URL-safe database password
python -c "import secrets; print(secrets.token_urlsafe(32))"
```

Edit `.env` and set:
- `SECRET_KEY`: Set to the generated 64-byte key.
- `POSTGRES_PASSWORD`: Set to the generated 32-byte password.
- `POSTGRES_PORT`: Default is `5433` (to avoid conflict with any host Postgres running on 5432).
- `API_PORT`: Default is `8000`.
- `REDIS_PORT`: Default is `6379`.
- `APP_ENV`: Set to `development` (or `production` for production deployments).
- `CORS_ALLOWED_ORIGINS`: Comma-separated list of allowed origins (e.g. `http://localhost:3000`).

> [!IMPORTANT]
> Never commit `.env` to Git. The `.gitignore` file is pre-configured to exclude it.

### Step 2.3: Build and Start Services

Launch the entire stack using Docker Compose:

```bash
docker compose up -d --build
```

This starts three services connected via the private `aik-internal` network:
1. `aik-postgres` (PostgreSQL 16 Alpine)
2. `aik-redis` (Redis 7 Alpine)
3. `aik-api` (FastAPI on Python 3.10-slim)

### Step 2.4: Verify Container Health

Wait for the healthchecks to report `healthy`:

```bash
docker compose ps
```

Expected output:
```text
NAME           IMAGE                      STATUS                    PORTS
aik-api        ai-knockout-api            Up (healthy)              0.0.0.0:8000->8000/tcp
aik-postgres   postgres:16-alpine         Up (healthy)              0.0.0.0:5433->5432/tcp
aik-redis      redis:7-alpine             Up (healthy)              0.0.0.0:6379->6379/tcp
```

### Step 2.5: Apply Database Migrations

Run database migrations to initialize tables:

```bash
docker compose exec api alembic upgrade head
```

Verify migration status:

```bash
docker compose exec api alembic current
```

### Step 2.6: Smoke Test the API

Verify API liveness and readiness:

```bash
# 1. Check Liveness
curl -i http://localhost:8000/health

# 2. Check Dependency Readiness (PostgreSQL + Redis)
curl -i http://localhost:8000/ready

# 3. Check Version Info
curl -i http://localhost:8000/version
```

Expected readiness response:
```json
{
  "status": "ok",
  "service": "ai-knockout",
  "dependencies": {
    "postgresql": {"status": "ok", "detail": null},
    "redis": {"status": "ok", "detail": null}
  }
}
```

---

## 3. Stopping and Restarting

### Stop the stack (preserving data volumes):
```bash
docker compose down
```

### Restart the stack:
```bash
docker compose up -d
```

### Stop and remove data volumes (DESTRUCTIVE - resets DB and Redis):
```bash
docker compose down -v
```

---

## 4. College LAN Deployment Considerations (Future Phase)

When deploying on a local college network for competitions:
1. Bind only the API port (`8000`) to the host interface reachable by LAN clients.
2. Keep PostgreSQL (`5433`) and Redis (`6379`) bound to `127.0.0.1` or completely unmapped to the host in `compose.yml`.
3. Configure host firewall (`wf.msc` or `ufw`) to restrict port access.
4. Set `APP_ENV=production` in `.env` to enforce strict CORS and suppress stack traces.
