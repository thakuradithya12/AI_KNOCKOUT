# AI Knockout — Security Policy & Baseline (Phase 1)

## 1. Scope & Purpose

This document outlines the security architecture, baseline controls, threat assumptions, and ethical hacker review for **Phase 1: Production Server Foundation** of the AI Knockout project.

> [!IMPORTANT]
> Phase 1 provides the infrastructure and API server foundation only.
> Participant code execution and sandboxing remain deferred. Submission acceptance and static validation exist; worker control-plane support is mock-only and not deployed.

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

## 4. Known Limitations & Future Work

| Item | Current Status (Phase 1) | Scheduled Resolution |
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

## 6. Worker trust prototype status (mock-only)

The worker protocol prototype adds a separate, unmounted `/worker/v1` gateway application, per-worker certificate identity records, one-time enrollment tokens, fail-closed TLS configuration validation, explicit certificate activation/rotation/revocation, worker-scoped job leases/fencing, short-lived one-use artifact capabilities, bounded non-authoritative evidence, and append-only security events. Participant bearer credentials are rejected by the worker gateway. Worker private keys are not stored in PostgreSQL. Gateway startup requires `WORKER_DATABASE_URL` separately from the participant API database setting. See `SECURE_EXECUTION_ENVIRONMENT_V1.md` for the full design and operational limits.

This is not a deployed worker service. A separate Hypercorn mTLS listener entry point exists, but no real mTLS handshake has been verified; the migration has not been applied, and least-privilege database grants have not been implemented. Tests use synthetic ASGI TLS identity and controlled fixture artifacts. They are **MOCKED — DOES NOT PROVE SANDBOX SECURITY**. No participant package was executed. Real sandbox execution is **NOT IMPLEMENTED / NOT VERIFIED** and remains blocked until a dedicated Linux/KVM worker with the approved Kata runtime and the full security gate is available. Network, host filesystem, privilege, resource-limit, container escape, cross-execution and Kata isolation properties are **NOT VERIFIED — DEDICATED WORKER REQUIRED**.
