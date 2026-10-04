# AI Knockout — Security Policy & Baseline (Phase 1)

## 1. Scope & Purpose

This document outlines the security architecture, baseline controls, threat assumptions, and ethical hacker review for **Phase 1: Production Server Foundation** of the AI Knockout project.

> [!IMPORTANT]
> **Historical Phase-1 scope snapshot:** that phase established the infrastructure and API foundation. Submission acceptance/static validation and a mock-only worker control-plane prototype were added in later milestones. Current implementation and verification status is in Section 6.

---

## 2. Threat Assumptions (Phase 1)

1. **Deployment Environment**: Local developer workstation initially, transitioning to an isolated college LAN environment.
2. **Attacker Profile**: Local network users attempting unauthorized access to internal data stores (PostgreSQL, Redis), probing for unauthenticated management endpoints, or seeking confidential tokens.
3. **Attack Surfaces**:
   - Host-mapped service ports (API, PostgreSQL, Redis)
   - HTTP API request handling and error disclosures
   - Environment variables and secret storage
   - Docker container privilege boundaries

---

## 3. Implemented Security Controls

### 3.1 Network Isolation & Port Binding
- **Controlled Internal Network**: Services communicate over an internal Docker bridge network (`aik-internal`).
- **Development Port Mapping**:
  - PostgreSQL is mapped to host port `5433` (preventing conflict with host services on 5432).
  - For LAN deployment, PostgreSQL and Redis host port mappings MUST be commented out in `compose.yml`, leaving only the API port (`8000`) exposed.

### 3.2 Secret Management & Fail-Fast Validation
- **No Secrets in Code**: No API keys, passwords, or salts exist in source files or Dockerfiles.
- **`.gitignore` Enforcement**: `.env` and all backup files are strictly excluded from version control.
- **Startup Validation**:
  - `SECRET_KEY` is checked at startup; default placeholders (e.g., `CHANGE_ME...`) or keys under 32 characters cause an immediate startup crash (`pydantic.ValidationError`).
  - `DATABASE_URL` is checked for invalid default tokens.

### 3.3 Container Hardening
- **Non-Root Execution**: The API container creates and runs under an unprivileged user `appuser` (UID 1000, GID 1000).
- **No Docker Socket Mounting**: `/var/run/docker.sock` is NOT mounted inside any application container.
- **Minimal Base Images**: Containers use Alpine Linux and Debian Slim base images.
- **No Privileged Flags**: Containers do not run in `--privileged` mode.

### 3.4 API Protections & Headers
- **Security Headers Middleware**: Every HTTP response automatically includes:
  - `X-Content-Type-Options: nosniff`
  - `X-Frame-Options: DENY`
  - `X-XSS-Protection: 1; mode=block`
  - `Referrer-Policy: strict-origin-when-cross-origin`
  - `Cache-Control: no-store`
- **CORS Control**: Explicitly configured via `CORS_ALLOWED_ORIGINS`. Wildcards (`*`) are disallowed in production mode.
- **Error Sanitization**: Unhandled exceptions return generic `Internal Server Error` responses in production mode, withholding stack traces and database schema specifics.

### 3.5 Structured Logging & Redaction
- Log output is formatted as structured JSON.
- Sensitive fields (`password`, `secret`, `token`, `api_key`, `authorization`, etc.) are automatically redacted to `[REDACTED]`.

---

## 4. Historical Phase-1 Limitations Snapshot

The following table records the security status when the Phase-1 baseline document was written. It is historical; current worker-control-plane implementation and verification status are summarized in Section 6 below.

| Item | Phase-1 snapshot | Planned resolution at that time |
|---|---|---|
| Participant TLS / HTTPS | HTTP plaintext locally | Reverse proxy with TLS termination in production |
| Participant authentication | Participant identity/session authentication implemented | Production TLS deployment and operations review |
| Worker mTLS transport | Context/identity abstraction only; no live listener or verified handshake | Dedicated TLS-capable worker gateway deployment and handshake testing |
| Rate Limiting | Basic connection limits | Redis-backed token bucket rate limiting in Phase 2 |
| Worker protocol | Mock-only control-plane prototype; not deployed | Dedicated worker listener/agent after security review |
| Participant Sandbox | Not implemented; no participant code is executed | Dedicated Linux/KVM worker with approved Kata runtime and security gate |
| Web Application Firewall | None | Reverse proxy / Cloudflare / Nginx in production |

---

## 5. Security Verification Checklist

- [x] Docker socket (`/var/run/docker.sock`) is not mounted.
- [x] Containers run as non-root user (`appuser`).
- [x] Secrets are loaded exclusively from environment variables.
- [x] Weak or placeholder secrets fail startup immediately.
- [x] Sensitive fields are redacted in logs.
- [x] Security headers are attached to all HTTP responses.
- [x] Database is not exposed on standard public port (5432).

## 6. Current implementation and verification status

**IMPLEMENTED:** participant identity/authentication; submission and quarantine; mock-only worker control protocol; separate worker gateway; enrollment; certificate activation/rotation/revocation logic; execution-scoped artifact capabilities; leases/fencing; TLS configuration. Participant bearer credentials are rejected by the worker gateway. Worker private keys are not stored in PostgreSQL. Gateway startup requires `WORKER_DATABASE_URL` separately from the participant API database setting. Migration `0005` defines a `NOLOGIN` `aik_worker_gateway` privilege role with worker-table access and read-only access to the exact submission/quarantine tables needed for artifact transfer. Deployment must create a distinct login principal and grant membership; no credentials are created by the repository.

**NOT VERIFIED:** real TLS handshake and certificate transport; deployed worker listener; deployment-applied PostgreSQL role grants; dedicated Linux/KVM worker; Kata; sandbox isolation. The Hypercorn listener entry point has an explicit TLS 1.2 minimum and mandatory client authentication in source, but is not deployed or handshake-verified. Tests use synthetic ASGI TLS identity and controlled fixture artifacts; they are **MOCKED — DOES NOT PROVE TLS OR SANDBOX SECURITY**. **PARTICIPANT EXECUTION: DISABLED.** No participant package was executed. Network, host filesystem, privilege, resource-limit, container escape, cross-execution and Kata isolation properties are **NOT VERIFIED — DEDICATED WORKER REQUIRED**. See `SECURE_EXECUTION_ENVIRONMENT_V1.md` for details.

**MATCH RULES AND RECOVERY:** the controller defaults to the directly pinned
`chess==1.11.2` rules adapter; participant output remains a move proposal, and
the server validates and applies it. Transcript recovery checks legal replay,
position digests, side identity, historical execution binding identity, stored
position/ply/turn, and server-derived terminal results. Corrupt evidence is
classified as `TRANSCRIPT_CORRUPTED` and the transactional recovery helper
aborts an active match without rewriting its transcript. Active monotonic
deadlines are never restored. PostgreSQL migration and trigger execution is
**NOT VERIFIED** on this Windows sandbox: the installed PostgreSQL `initdb` and
`pg_ctl` utilities could not create their restricted process tokens. No Docker,
WSL, event database, event Redis, or `.env` workaround was used.
