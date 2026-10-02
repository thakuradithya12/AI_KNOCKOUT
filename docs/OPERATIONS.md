# AI Knockout — Operations Guide (Phase 1)

This document provides day-to-day operational procedures for managing, monitoring, inspecting, and troubleshooting the AI Knockout Phase 1 server foundation.

---

## 1. Service Lifecycle Management

All commands should be executed from the project root: `c:\TECH_EVENT\AI_KNOCKOUT`.

| Operation | Command |
|---|---|
| View service status | `docker compose ps` |
| View real-time resource usage | `docker stats --no-stream` |
| Tail all service logs | `docker compose logs -f` |
| Tail API logs only | `docker compose logs -f api` |
| Tail PostgreSQL logs | `docker compose logs -f postgres` |
| Tail Redis logs | `docker compose logs -f redis` |
| Restart API service | `docker compose restart api` |
| Restart PostgreSQL | `docker compose restart postgres` |
| Restart Redis | `docker compose restart redis` |
| Full stack restart | `docker compose restart` |

---

## 2. Health & Readiness Monitoring

### Liveness Probe (`GET /health`)
- Indicates whether the FastAPI process is alive and receiving HTTP requests.
- Returns `HTTP 200` with `{"status": "ok", "service": "ai-knockout"}`.
- Used by orchestrators and load balancers to detect process hangs.

### Readiness Probe (`GET /ready`)
- Checks active connectivity to PostgreSQL and Redis.
- If all dependencies respond: `HTTP 200` with `"status": "ok"`.
- If any dependency fails: returns `"status": "degraded"` with details per dependency.

```bash
# Query readiness endpoint with headers
curl -s -D - http://localhost:8000/ready
```

---

## 3. Database Administration

### Connect to Database via `psql` (inside container)
```bash
docker compose exec postgres psql -U aik_user -d aik_db
```

### Connect to Database from Host (using host-mapped port 5433)
```bash
psql -h localhost -p 5433 -U aik_user -d aik_db
```

### Migration Management (Alembic)
```bash
# Check current revision
docker compose exec api alembic current

# View revision history
docker compose exec api alembic history --verbose

# Apply all pending migrations
docker compose exec api alembic upgrade head

# Rollback last migration
docker compose exec api alembic downgrade -1
```

---

## 4. Redis Administration

### Connect to Redis CLI (inside container)
```bash
docker compose exec redis redis-cli
```

### Redis Health and Info
```bash
docker compose exec redis redis-cli ping
docker compose exec redis redis-cli info memory
docker compose exec redis redis-cli info persistence
```

---

## 5. Structured Log Inspection

All application logs are formatted as structured JSON for easy parsing with tools like `jq` or log collectors:

```bash
docker compose logs api --tail 50 | grep "^{" | jq .
```

Each log entry includes:
- `timestamp`: UTC ISO-8601 string
- `level`: `INFO`, `WARNING`, `ERROR`, etc.
- `service`: `ai-knockout`
- `logger`: Subsystem logger name (e.g., `ai-knockout.middleware`)
- `message`: Event description
- `request_id`: Correlation UUID for HTTP request tracing
- `error`: Structured exception details (when applicable)
- Automatic redaction of passwords, tokens, and secret keys.

---

## 6. Troubleshooting Common Incidents

### Issue: API fails to start with `ValidationError: SECRET_KEY`
- **Cause**: `.env` contains default placeholder or weak secret (< 32 chars).
- **Resolution**: Generate a real key via `python -c "import secrets; print(secrets.token_urlsafe(64))"` and update `.env`.

### Issue: Port 5433 or 8000 already in use
- **Cause**: Another service is bound to that host port.
- **Resolution**: Inspect with `netstat -ano | findstr <port>` and change `API_PORT` or `POSTGRES_PORT` in `.env`.

### Issue: Database connection timeout
- **Cause**: PostgreSQL container is initializing or restarting.
- **Resolution**: Check `docker compose logs postgres`. Ensure `aik-pgdata` permissions are valid.
