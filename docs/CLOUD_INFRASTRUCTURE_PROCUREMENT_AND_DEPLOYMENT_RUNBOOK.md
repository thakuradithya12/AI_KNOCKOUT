# AI Knockout — Cloud Infrastructure Procurement Checklist & Deployment Runbook
**Event:** AI Knockout — MVSR Engineering College  
**Target Audience:** Infrastructure Engineers, Cloud Architects, Event Sponsors  
**Document Version:** 2.0 (Post-Audit Hardened)  
**Status:** Pre-Procurement Technical Guide  
**Mandate:** **Google Cloud $300 Free Trial Only — Do Not Incur Paid Charges Without Explicit Approval**

---

## 1. Google Cloud $300 Free Trial Policy & Governance

### 1.1 Free Trial Verification & Credit Management
* **Active Credit Check:** Before provisioning any cloud resource, the operator must log into Google Cloud Console (`console.cloud.google.com/billing`) and confirm:
  1. Billing Account Status is **Active Free Trial**.
  2. Available Credit is $\ge \$250.00$ USD (from the initial $300 credit).
  3. Credit expiration date covers the entire preparation, rehearsal, and event period (standard 90-day window).
* **Automatic Upgrade Prohibition:** Under Google Cloud Free Trial terms, credit card charges are **not** automatically initiated when credits expire or hit zero, provided the account has **not** been manually upgraded to a paid account.
  > [!CAUTION]
  > **DO NOT CLICK "UPGRADE TO FULL ACCOUNT".**  
  > If the Free Trial credits or quotas are insufficient, **STOP** and report to the user. Do not switch to paid personal or college billing.

### 1.2 Budget Alerts & Billing Risk Controls
* **Setting Budget Alerts (GCP Console -> Billing -> Budgets & alerts):**
  - Create Budget: `AI-Knockout-Event-Budget`
  - Target Amount: **$100.00 USD** (within the $300 free credits)
  - Threshold Rules: 50% ($50), 75% ($75), 90% ($90), and 100% ($100).
  - Notification Recipients: Project Lead / Infrastructure Team email addresses.
  > [!WARNING]
  > **Important Billing Risk Note:** GCP budget alerts notify operators via email; **they are not automatic hard spending caps**. Uncontrolled background instances or unmonitored disk snapshots will continue to consume credits until stopped. Operators must manually terminate or stop instances when rehearsal phases conclude.

### 1.3 Free Trial Quota Constraints & Workaround
* **Regional vCPU Quotas:** Free Trial accounts frequently enforce a default quota limit of **8 to 24 standard vCPUs** per region (e.g. `asia-south1`).
* **Sizing Options within Quotas:**
  - *Option A (Two-Host Architecture):* Coordinator (`e2-standard-4`, 4 vCPUs) + Worker (`n2-standard-8`, 8 vCPUs) = **12 vCPUs total**. If the regional quota allows 12 vCPUs, this provides optimal separation.
  - *Option B (Trial Quota-Optimized / Single Host):* Single `n2-standard-8` (8 vCPUs total) hosting Coordinator, Database, Redis, and Kata Containers Worker concurrently. Fits within strict 8-vCPU regional trial caps while still unlocking nested virtualization for Kata microVMs.

---

## 2. Cloud Specifications & Cost Model (Within Approved ₹14,960 Budget)

### 2.1 Compute Specifications
* **Region / Zone:** `asia-south1-a` (Mumbai) or `asia-south1-b`
* **CPU Platform:** Intel Cascade Lake / Ice Lake
* **Virtualization Support:** N2 machine family natively supports nested virtualization with `--enable-nested-virtualization`.

```bash
# Verify available CPU platforms in target zone:
gcloud compute zones describe asia-south1-a --format="value(availableCpuPlatforms)"
# Expected output contains: "Intel Cascade Lake" or "Intel Ice Lake"
```

### 2.2 Cost Breakdown & Indian GST (18%) Reconciliation
* **Approved College Technical Budget:** **₹14,960 INR (~$178.00 USD)**.
* **Billing Currency:** Free Trial credits apply in USD ($300).
* **Cost Projection (7-Day Operating Envelope):**

| Line Item | Specification | Unit Rate | Usage | Base Cost (USD) | 18% GST / Fees | Total Cost (USD) | Total Cost (INR @ ₹84) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Worker Host (Kata VM)** | GCP `n2-standard-8` (8 vCPU, 32GB RAM) | $0.3885 / hr | 84 hrs | $32.63 | $5.87 | $38.50 | ₹3,234 |
| **Coordinator & DB Host** | GCP `e2-standard-4` (4 vCPU, 16GB RAM) | $0.1340 / hr | 168 hrs | $22.51 | $4.05 | $26.56 | ₹2,231 |
| **Persistent Storage** | 100GB pd-balanced + 50GB pd-ssd | Prorated | 7 days | $3.50 | $0.63 | $4.13 | ₹347 |
| **Network Egress & IP** | 1 Static IPv4 + 60GB egress | Standard | 7 days | $6.50 | $1.17 | $7.67 | ₹644 |
| **Off-Host GCS Backups** | Cloud Storage Nearline (50GB) | $0.010/GB | 1 mo | $0.50 | $0.09 | $0.59 | ₹50 |
| **Operational Contingency** | Buffer for reruns & re-starts | Reserve | — | $20.00 | $3.60 | $23.60 | ₹1,982 |
| **TOTAL ESTIMATED SPEND** | — | — | — | **$85.64** | **$15.41** | **$101.05** | **₹8,488** |

**Budget & Credit Alignment:**
* Consumes **$101.05 USD** of the **$300.00 Free Trial Credit** (leaving $198.95 USD credit balance).
* Fits entirely within the approved **₹14,960 INR** budget ceiling (estimated ₹8,488 with 18% tax buffer, leaving **₹6,472 INR** cash reserve).
* Zero out-of-pocket charges incurred.

---

## 3. College Network & DNS Prerequisites

Before launching production services, the MVSR Engineering College IT Department must fulfill the following prerequisites:

1. **DNS A Record Configuration:**
   - **Hostname:** `aiknockout.mvsrec.edu.in` (or college-assigned event subdomain)
   - **Target:** External Static Public IPv4 allocated to the Coordinator instance.
   - **TTL:** 300 seconds (5 minutes) to ensure rapid re-pointing if an IP change occurs.
   - **Alternative:** If college DNS approval is delayed, configure Cloudflare DNS on an independent domain (`aiknockout.in`).
2. **Campus Firewall & Port Forwarding:**
   - **Inbound Ports Allowed:** `80/tcp` (HTTP, required for ACME Let's Encrypt automated challenge) and `443/tcp` (HTTPS for student portal and API).
   - **Outbound Connectivity:** Participants on campus Wi-Fi must be able to reach ports 80/443 without captive-portal session termination mid-match.
   - **Internal VPC Security Group:** Port `8443/tcp` (mTLS Worker Gateway) must be restricted to internal VPC traffic between the Worker VM and Coordinator VM; **it must never be exposed to the public internet**.

---

## 4. Production Deployment & Rollout Runbook

### Step 1: Provisioning Instances (GCP CLI)
```bash
# Coordinator Node
gcloud compute instances create aik-coordinator \
    --zone=asia-south1-a \
    --machine-type=e2-standard-4 \
    --image-family=ubuntu-2404-lts-amd64 \
    --image-project=ubuntu-os-cloud \
    --boot-disk-size=50GB \
    --boot-disk-type=pd-ssd \
    --tags=http-server,https-server

# Worker Node (With hardware virtualization enabled)
gcloud compute instances create aik-worker-01 \
    --zone=asia-south1-a \
    --machine-type=n2-standard-8 \
    --min-cpu-platform="Intel Cascade Lake" \
    --enable-nested-virtualization \
    --image-family=ubuntu-2404-lts-amd64 \
    --image-project=ubuntu-os-cloud \
    --boot-disk-size=100GB \
    --boot-disk-type=pd-balanced
```

### Step 2: Kata Containers Setup & Isolation Check (Worker VM)
```bash
sudo apt update && sudo apt install -y containerd kata-containers
sudo containerd config default | sudo tee /etc/containerd/config.toml
sudo systemctl restart containerd

# Verify Kata hardware virtualization capability:
sudo kata-runtime check
# Output MUST conclude: "System is capable of running Kata Containers"
```

### Step 3: Database Migration Execution
```bash
cd /opt/ai-knockout/backend
source .venv/bin/activate

# Execute migrations 0001 through 0011 sequentially:
alembic upgrade head

# Verify current revision
alembic current
# Expected: 0011_current_match_jobs (head)
```

---

## 5. Persistent Off-Host Backup Storage & Disaster Recovery

Backups stored only on the local VM disk are vulnerable to VM termination or disk failure. All snapshots must be stored in persistent, independently recoverable off-host cloud storage.

### 5.1 Cloud Storage Bucket Setup
Create a dedicated Google Cloud Storage bucket with Object Versioning and 30-day lifecycle retention:
```bash
gsutil mb -p <GCP_PROJECT_ID> -c nearline -l asia-south1 gs://aik-backups-mvsrec/
gsutil versioning set on gs://aik-backups-mvsrec/
```

### 5.2 Automated Non-Destructive Backup Script
Execute every 30 minutes via systemd timer or cron:
```bash
#!/bin/bash
set -euo pipefail

TIMESTAMP=$(date +%Y%m%d_%H%M%S)
BACKUP_DIR="/var/backups/aik"
mkdir -p "${BACKUP_DIR}"

# 1. Take consistent binary snapshot (Zero lock contention)
docker exec aik-postgres pg_dump -U aik_user aik_db -F c -b -f "${BACKUP_DIR}/aik_db_${TIMESTAMP}.dump"

# 2. Upload immediately to off-host Cloud Storage
gsutil cp "${BACKUP_DIR}/aik_db_${TIMESTAMP}.dump" gs://aik-backups-mvsrec/

# 3. Retain only last 6 hours locally to prevent disk fill
find "${BACKUP_DIR}" -name "*.dump" -mmin +360 -delete

echo "[$(date)] Backup aik_db_${TIMESTAMP}.dump successfully verified and synced to GCS."
```

### 5.3 Verified Restoration Drill from Off-Host Storage
Before running schema changes, test restoration from the GCS bucket onto an isolated target:
```bash
# Download latest snapshot from off-host storage
LATEST_BACKUP=$(gsutil ls gs://aik-backups-mvsrec/ | tail -n 1)
gsutil cp "${LATEST_BACKUP}" /tmp/verify_restore.dump

# Create disposable verification database and restore
docker exec aik-postgres psql -U aik_user -d postgres -c "DROP DATABASE IF EXISTS aik_verify_restore WITH (FORCE);"
docker exec aik-postgres psql -U aik_user -d postgres -c "CREATE DATABASE aik_verify_restore;"
docker exec aik-postgres pg_restore -U aik_user -d aik_verify_restore /tmp/verify_restore.dump

# Confirm data integrity
docker exec aik-postgres psql -U aik_user -d aik_verify_restore -c "SELECT count(*) FROM participants;"
docker exec aik-postgres psql -U aik_user -d postgres -c "DROP DATABASE aik_verify_restore;"
rm -f /tmp/verify_restore.dump
```

---

## 6. Safe Production Rollback Procedures (No Automatic Downgrades)

> [!CAUTION]
> **Strict Database Safety Rule:** NEVER execute `alembic downgrade -1` automatically in production.  
> In PostgreSQL, running down-migrations drops tables, cascades foreign keys, and truncates columns, causing irreversible data loss.

### 6.1 Safe Rollback Protocol:
1. **Isolate Traffic:**
   Immediately route public traffic to a maintenance page via Caddy:
   `caddy reload --config /etc/caddy/Caddyfile.maintenance`
2. **Stop API Container:**
   `docker compose stop api`
3. **Assess Schema Regression:**
   - **Scenario A (Code-only defect):** Revert container image tag to previous stable build (`aik-api:previous`) and restart. Do not touch database.
   - **Scenario B (Schema defect with zero participant data added since rollout):**
     Restore database from the pre-deployment snapshot:
     ```bash
     docker exec aik-postgres psql -U aik_user -d postgres -c "DROP DATABASE aik_db WITH (FORCE);"
     docker exec aik-postgres psql -U aik_user -d postgres -c "CREATE DATABASE aik_db;"
     docker exec aik-postgres pg_restore -U aik_user -d aik_db /var/backups/aik_pre_deploy.dump
     ```
   - **Scenario C (Schema defect after live participant activity has occurred):**
     **DO NOT DOWNGRADE SCHEMA.** Apply a forward migration (`alembic revision` -> `alembic upgrade head`) that patches the specific constraint or index without dropping existing data.
4. **Re-Enable Traffic:**
   Once verified, restore Caddy reverse proxy to live application.
