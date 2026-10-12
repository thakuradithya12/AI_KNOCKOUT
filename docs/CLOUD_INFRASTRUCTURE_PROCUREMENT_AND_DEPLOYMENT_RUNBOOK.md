# AI Knockout — Cloud Infrastructure Procurement Checklist & Deployment Runbook
**Event:** AI Knockout — MVSR Engineering College  
**Target Audience:** Infrastructure Engineers, Cloud Architects, Event Sponsors  
**Document Version:** 1.0  
**Status:** Pre-Procurement Technical Guide

---

## 1. Cloud Procurement & Architecture Specifications

### 1.1 Architecture Topology
```
                     +---------------------------------------+
                     |         Internet / Participants        |
                     +---------------------------------------+
                                         |
                                         v (HTTPS / 443)
                     +---------------------------------------+
                     |        Reverse Proxy (Caddy / Nginx)  |
                     +---------------------------------------+
                                         |
                     +---------------------------------------+
                     |        AI Knockout API & Controller   |
                     |             (FastAPI / ASGI)          |
                     +---------------------------------------+
                         |                 |               |
                         v                 v               v
                +----------------+ +---------------+ +---------------+
                | PostgreSQL 16  | |    Redis 7    | | mTLS Gateway  |
                |   (aik_db)     | | (Rate limits) | | (Port 8443)   |
                +----------------+ +---------------+ +---------------+
                                                           |
                                               (mTLS Peer Auth / gRPC)
                                                           |
                                                           v
                                            +------------------------------+
                                            |   Execution Worker Node(s)   |
                                            | Ubuntu 24.04 + Kata Runtime  |
                                            |  Isolated MicroVMs (QEMU)    |
                                            +------------------------------+
```

### 1.2 Recommended Cloud Compute Instances
To execute the competition for 30–50 participants with parallel matches:

| Component | Recommended Sizing | Cloud Provider Equivalent | Notes |
| :--- | :--- | :--- | :--- |
| **API Coordinator & Database** | 4 vCPU, 16 GB RAM, 100 GB NVMe SSD | GCP: `e2-standard-4`<br>AWS: `t4g.xlarge` / `c6g.xlarge`<br>DigitalOcean: `General Purpose 16GB` | Hosts FastAPI, PostgreSQL 16, Redis 7, Static assets. |
| **Execution Worker Host (Kata)** | 8 vCPU, 32 GB RAM, Nested Virtualization enabled | GCP: `c2-standard-8` (Intel Xeon)<br>AWS: `c5.2xlarge` (bare metal or `.metal`)<br>Hetzner: `AX52` / `CCX33` | **Crucial:** Must support Hardware Virtualization (VT-x/AMD-v) for Kata Containers / QEMU microVM isolation. |

### 1.3 Estimated Budget
- Pre-event testing & rehearsal (3 days): ~$20
- Event day runtime (8 hours peak): ~$15
- Bandwidth, domain, SSL/TLS, backups: ~$10
- **Total Infrastructure Cost Estimate: $45 – $60 USD (~₹3,800 – ₹5,000 INR)**

---

## 2. Environment Variables & Secret-Free Configuration Template

Create `/etc/aiknockout/production.env` (permissions `0600`, owned by service user):
```bash
# ==============================================================================
# AI KNOCKOUT PRODUCTION ENVIRONMENT CONFIGURATION
# ==============================================================================

# Application Environment
ENVIRONMENT=production
DEBUG=false
LOG_LEVEL=INFO

# Application Core Secret (Must be 32+ bytes cryptographic random)
# Generate with: openssl rand -hex 32
SECRET_KEY=<GENERATE_RANDOM_SECRET_KEY>

# Organizer Admin Control Plane Secret
# Generate with: openssl rand -hex 24
ORGANIZER_SECRET=<GENERATE_RANDOM_ORGANIZER_SECRET>

# PostgreSQL 16 Database
POSTGRES_SERVER=127.0.0.1
POSTGRES_PORT=5432
POSTGRES_USER=aik_prod_user
POSTGRES_PASSWORD=<GENERATE_STRONG_DB_PASSWORD>
POSTGRES_DB=aik_db
DATABASE_URL=postgresql+asyncpg://${POSTGRES_USER}:${POSTGRES_PASSWORD}@${POSTGRES_SERVER}:${POSTGRES_PORT}/${POSTGRES_DB}

# Redis 7 Cache & Rate Limiting
REDIS_HOST=127.0.0.1
REDIS_PORT=6379
REDIS_PASSWORD=<GENERATE_REDIS_PASSWORD>
REDIS_URL=redis://:${REDIS_PASSWORD}@${REDIS_HOST}:${REDIS_PORT}/0

# Server Security & Time Controls
ACCESS_TOKEN_EXPIRE_MINUTES=480
EVENT_TIMEZONE=Asia/Kolkata
MATCH_CLOCK_INITIAL_MS=300000
MATCH_MOVE_TIMEOUT_MS=2000

# Worker Transport & mTLS Configuration
WORKER_GATEWAY_HOST=0.0.0.0
WORKER_GATEWAY_PORT=8443
MTLS_CA_CERT=/etc/aiknockout/certs/ca.crt
MTLS_SERVER_CERT=/etc/aiknockout/certs/gateway.crt
MTLS_SERVER_KEY=/etc/aiknockout/certs/gateway.key
```

---

## 3. Deployment & Rollout Runbook

### Step 1: Host Preparation (Ubuntu 24.04 LTS)
```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y docker.io docker-compose-v2 postgresql-client git python3-venv openssl
```

### Step 2: Kata Containers Setup on Worker Host
```bash
# Install containerd and kata-runtime
sudo apt install -y containerd kata-containers
sudo containerd config default | sudo tee /etc/containerd/config.toml
# Ensure runtime_type = "io.containerd.kata.v2" is enabled
sudo systemctl restart containerd
# Verify Kata isolation
sudo kata-runtime check
```

### Step 3: Database & Migration Execution
Run migrations `0001` through `0011` sequentially:
```bash
cd /opt/ai-knockout/backend
source .venv/bin/activate
alembic upgrade head
```
Verify current revision:
```bash
python -c "from app.db.session import engine; import asyncio; from sqlalchemy import text; asyncio.run(engine.connect().then(lambda c: c.execute(text('SELECT version_num FROM alembic_version'))))"
# Expected: 0011_current_match_jobs
```

### Step 4: Building & Deploying the Production Images
```bash
# Build API container
docker build -t aik-api:production -f Dockerfile .

# Launch services
docker compose -f docker-compose.prod.yml up -d
```

### Step 5: Verification & Health Check Sequence
```bash
# 1. API Health
curl -s http://localhost:8000/api/v1/health | grep '"status":"ok"'

# 2. Database & Redis Readiness
curl -s http://localhost:8000/api/v1/ready | grep '"database":true'

# 3. Tournament Endpoints
curl -s http://localhost:8000/api/v1/tournaments/bracket | grep '"rounds"'
curl -s http://localhost:8000/api/v1/tournaments/leaderboard | grep '"standings"'

# 4. Admin Authentication
curl -s -H "X-Organizer-Secret: $ORGANIZER_SECRET" http://localhost:8000/api/v1/admin/overview
```

---

## 4. Backup & Rollback Procedures

### 4.1 Non-Destructive Live Database Backup
Take automated periodic snapshots every 30 minutes during the event:
```bash
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
docker exec aik-postgres pg_dump -U aik_user aik_db -F c -b -f /var/backups/aik_db_${TIMESTAMP}.dump
```

### 4.2 Verified Restoration to Disposable Target
Always verify backup dumps before performing any production schema modifications:
```bash
# 1. Create disposable test database
docker exec aik-postgres psql -U aik_user -d postgres -c "CREATE DATABASE aik_verify_tmp;"
# 2. Restore dump into test database
docker exec aik-postgres pg_restore -U aik_user -d aik_verify_tmp /var/backups/aik_db_${TIMESTAMP}.dump
# 3. Verify integrity
docker exec aik-postgres psql -U aik_user -d aik_verify_tmp -c "SELECT count(*) FROM participants;"
# 4. Drop test database
docker exec aik-postgres psql -U aik_user -d postgres -c "DROP DATABASE aik_verify_tmp;"
```

### 4.3 Rollback Plan
If an unexpected defect occurs during a live update:
1. Revert container image tag to previous release:
   `docker compose -f docker-compose.prod.yml up -d --no-deps api`
2. If schema rollback is required:
   `alembic downgrade -1`
3. Restore from the most recent pre-change snapshot if data corruption occurs.
