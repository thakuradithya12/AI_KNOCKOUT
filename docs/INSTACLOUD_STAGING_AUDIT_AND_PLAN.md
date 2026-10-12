# AI Knockout — InstaCloud Staging Compatibility Audit & Deployment Plan
**Target Environment:** InstaCloud (InsForge) Free Plan  
**Repository Branch:** `workstream/tournament-stabilization`  
**Reference Commit:** `668d2c8`  
**Baseline Checkpoint:** `checkpoint-baseline-head` (`f02d49f`)  
**Status:** Pre-Deployment Audit & Plan (Pending User Approval — ₹0 Expenditure)

---

## 1. Executive Summary

This document outlines the compatibility audit, resource boundaries, security policies, and step-by-step deployment runbook for staging AI Knockout on InstaCloud's Free tier.

* **Primary Objective:** Validate the web application, participant portal, API routes, database migrations, tournament bracket progression, and admin workflows in a real-world cloud staging environment without incurring any monetary charges.
* **Cost Constraint:** Strict ₹0 / $0 spend. Utilize only the included Free Plan usage credits ($10/month platform credit). Do not upgrade, bind billing cards, or purchase custom domains.
* **Critical Finding (Kata Sandboxing):** InstaCloud is a containerized PaaS without nested Linux KVM virtualization (`/dev/kvm`). Consequently, **untrusted student model execution via Kata microVMs is INCOMPATIBLE and BLOCKED on InstaCloud**. Staging will operate in synthetic/mock execution mode; untrusted code execution remains isolated on the dedicated KVM-capable cloud host.

---

## 2. InstaCloud Free Plan Limits & Sizing Audit

| Parameter | InstaCloud Free Plan Limit | AI Knockout Staging Requirement | Compatibility Status |
| :--- | :--- | :--- | :--- |
| **Monthly Cost** | $0/month | $0 (₹0) | **PASS** |
| **Included Usage Credit**| $10 monthly compute credits | Rehearsal usage estimated at < $1.50 | **PASS** |
| **Max Services** | 5 services per environment | 3 services (`api`, `postgres`, `redis`) | **PASS** (3 of 5 utilized) |
| **Compute per Service** | Up to 4 vCPUs, 4 GB RAM | API: 1 vCPU, 1 GB RAM<br>Postgres: 1 vCPU, 1 GB RAM<br>Redis: 0.5 vCPU, 256 MB RAM | **PASS** (Total 2.5 vCPUs, 2.25 GB RAM) |
| **Storage per Service** | 1 GB persistent volume | Postgres: 1 GB volume (adequate for ~50MB staging DB) | **PASS (with volume limit caveat)** |
| **Inactivity Policy** | Projects auto-pause after 7 days | Explicitly stopped post-rehearsal | **PASS** |
| **Nested Virtualization**| Not supported (no `/dev/kvm`) | Untrusted model execution requires KVM | **BLOCKED (Kata sandboxing disabled)** |
| **Public Networking** | Subdomain (`*.insforge.app`) | Default HTTPS provided; no domain purchase needed | **PASS** |

---

## 3. Service Architecture & Sizing Breakdown

### Service 1: `aik-staging-api` (FastAPI ASGI)
* **Image / Runtime:** Dockerfile built from `backend/Dockerfile` (Python 3.10-slim).
* **Resource Allocation:** 1 vCPU, 1024 MB RAM.
* **Ports Exposed:** 8000 (mapped to public HTTPS route by platform).
* **Role:** Serves web UI static assets, participant REST endpoints, tournament engine, and admin portal.

### Service 2: `aik-staging-postgres` (PostgreSQL 16)
* **Image:** `postgres:16-alpine`.
* **Resource Allocation:** 1 vCPU, 1024 MB RAM.
* **Volume:** 1 GB persistent volume mounted at `/var/lib/postgresql/data`.
* **Ports:** Private internal port 5432 (not exposed to the public internet).
* **Capacity Note:** 1 GB provides ample headroom for staging schema and hundreds of synthetic match records (~50 MB). Heavy binary model artifact blobs must not be stored in the database.

### Service 3: `aik-staging-redis` (Redis 7)
* **Image:** `redis:7-alpine`.
* **Resource Allocation:** 0.5 vCPU, 256 MB RAM.
* **Volume:** Ephemeral / in-memory.
* **Ports:** Private internal port 6379 (not exposed to the public internet).
* **Role:** Fast token bucket rate limiting and pub/sub job queuing.

---

## 4. Sandbox & Untrusted Execution Policy

> [!CAUTION]
> **KATA CONTAINERS MICROVM EXECUTION IS STRICTLY INCOMPATIBLE WITH INSTACLOUD FREE TIER.**
> InstaCloud's serverless infrastructure does not provide direct access to `/dev/kvm` hardware virtualization extensions. Attempting to start Kata Containers microVMs will fail at runtime.

### Staging Operating Posture:
1. **Disabled Live Untrusted Code Execution:** The worker execution engine on InstaCloud staging will run in **Mock / Verification Mode** using pre-verified reference house models (`BaselineBot`, `RandomBot`).
2. **Untrusted Code Isolation:** Real participant `.tar.gz` model archives containing arbitrary Python code will **never** be executed on InstaCloud.
3. **Full System Verification:** Staging verifies 100% of the tournament lifecycle:
   - Registration, login, and JWT credential rotation.
   - Model upload, size/checksum verification, AST safety linting, and metadata parsing.
   - Bracket generation, single/double elimination progression, bye handling, and tie-breakers.
   - Score persistence, tournament standings, and admin overrides.

---

## 5. Security & Data Isolation Protocols

1. **Zero Production Data:**
   - Production participant database records will NOT be transferred to InstaCloud.
   - Staging begins with a clean schema generated by `alembic upgrade head`.
   - Seed data will consist exclusively of synthetic test users (`test_user_01` to `test_user_08`).
2. **Disposable Staging Secrets:**
   - Production `.env` secrets remain untouched on the local host.
   - Fresh random secrets will be generated for staging:
     - `SECRET_KEY`: 64-byte random URL-safe token.
     - `POSTGRES_PASSWORD`: 32-byte random URL-safe token.
     - `ORGANIZER_SECRET`: 32-byte random URL-safe token.
3. **Network Isolation:**
   - PostgreSQL (`5432`) and Redis (`6379`) will be bound only to the private internal network.
   - Only the FastAPI ASGI service will have an external HTTPS endpoint.

---

## 6. Exact Step-by-Step Staging Deployment Procedure

### Prerequisites (Local Host)
* Ensure repository is at commit `668d2c8`.
* Install InsForge CLI if deploying via CLI:
  ```bash
  npm install -g @insforge/cli
  ```

### Step 1: Generate Disposable Staging Secrets
```bash
# Generate staging secrets (run locally, never commit to git):
python -c "import secrets; print('STAGING_SECRET_KEY=' + secrets.token_urlsafe(64))"
python -c "import secrets; print('STAGING_POSTGRES_PASSWORD=' + secrets.token_urlsafe(32))"
```

### Step 2: Provision Services via InstaCloud
```bash
# 1. Deploy Redis Cache
npx @insforge/cli compute deploy \
  --name aik-staging-redis \
  --image redis:7-alpine \
  --port 6379 \
  --protocol tcp \
  --memory 256 \
  --cpu 0.5

# 2. Deploy PostgreSQL Database
npx @insforge/cli compute deploy \
  --name aik-staging-postgres \
  --image postgres:16-alpine \
  --port 5432 \
  --protocol tcp \
  --memory 1024 \
  --cpu 1.0 \
  --env POSTGRES_DB=aik_staging_db \
  --env POSTGRES_USER=aik_staging_user \
  --env POSTGRES_PASSWORD=<GENERATED_STAGING_PASSWORD>

# 3. Deploy FastAPI API
npx @insforge/cli compute deploy \
  --name aik-staging-api \
  --build-context ./backend \
  --dockerfile ./backend/Dockerfile \
  --port 8000 \
  --memory 1024 \
  --cpu 1.0 \
  --env APP_ENV=staging \
  --env DATABASE_URL=postgresql+asyncpg://aik_staging_user:<PASSWORD>@aik-staging-postgres:5432/aik_staging_db \
  --env DATABASE_URL_SYNC=postgresql+psycopg2://aik_staging_user:<PASSWORD>@aik-staging-postgres:5432/aik_staging_db \
  --env REDIS_URL=redis://aik-staging-redis:6379/0 \
  --env SECRET_KEY=<GENERATED_STAGING_SECRET_KEY> \
  --env WORKER_EXECUTION_MODE=mock
```

### Step 3: Run Database Migrations
```bash
# Execute Alembic migrations to initialize schema up to 0011:
npx @insforge/cli compute exec aik-staging-api -- alembic upgrade head
```

### Step 4: Verification & Smoke Testing
```bash
# 1. Verify healthcheck and database readiness
curl -i https://<generated-staging-url>.insforge.app/health
curl -i https://<generated-staging-url>.insforge.app/ready
curl -i https://<generated-staging-url>.insforge.app/version

# 2. Register synthetic test team and log in
curl -X POST https://<generated-staging-url>.insforge.app/api/v1/auth/register \
  -H "Content-Type: application/json" \
  -d '{"username":"staging_tester","password":"StagingPassword123!","email":"test@mvsrec.edu.in"}'
```

### Step 5: Post-Rehearsal Hibernation (Preserve Free Credits)
```bash
# Pause services immediately after testing to prevent credit consumption:
npx @insforge/cli compute stop aik-staging-api
npx @insforge/cli compute stop aik-staging-postgres
npx @insforge/cli compute stop aik-staging-redis
```

---

## 7. Approval Gate

> [!IMPORTANT]
> **NO CLOUD SERVICES HAVE BEEN CREATED OR PROVISIONED.**  
> Execution of the deployment steps above is blocked pending explicit operator approval.
