# Backend test safety and execution

The normal `pytest` suite is isolated by `backend/tests/conftest.py`. Every test gets a temporary SQLite database file, an in-process ASGI client, and a process-local Redis rate-limit fallback. Health dependency responses are stubbed where needed. Tests ignore `API_BASE_URL`, overwrite `DATABASE_URL`/`DATABASE_URL_SYNC` inside the test process, and never contact the configured event PostgreSQL or Redis services. Temporary SQLite files are created below pytest's per-test temporary directory and removed by pytest cleanup.

No test runs the application's lifespan, starts a service, runs a container, applies a migration to a real database, or issues global Redis commands. Do not add `FLUSHALL`, session mass updates, or external API clients to these fixtures.

## Run the isolated suite

```powershell
cd C:\TECH_EVENT\AI_KNOCKOUT\backend
python -m pip install -e ".[dev]"
pytest -q
```

The `integration` marker means HTTP-level application integration using the isolated app/database fixtures; it does not mean Docker/PostgreSQL/Redis connectivity. Connectivity, migration acceptance and backup/restore checks need a separately provisioned test-only environment with explicit credentials and no connection to event services.

## Validate migration SQL without a database connection

Run Alembic in offline mode with a deliberately inert example URL to generate SQL only:

```powershell
$env:DATABASE_URL_SYNC = "postgresql+psycopg2://offline:offline@127.0.0.1:1/aik_offline"
alembic upgrade head --sql
```

This does not connect or apply DDL. To exercise PostgreSQL triggers, provision a disposable PostgreSQL instance/volume and test database explicitly; never substitute the event database URL.

## Worker protocol prototype (mock-only)

The worker gateway tests use the same temporary SQLite test database and an in-process FastAPI client. A test transport shim injects a synthetic ASGI TLS extension containing generated CA-signed test certificates; deterministic checks exercise direct CA signature, URI SAN, expiry, EKU, certificate/key matching, `CERT_REQUIRED`, and fail-closed environment configuration. These tests do **not** perform a TLS handshake or verify a deployed ASGI server's client-certificate support. The mock worker accepts only explicitly allowlisted fixture bytes and never launches an archive. Test labels are **MOCKED — DOES NOT PROVE SANDBOX SECURITY**.

Run the worker protocol tests:

```powershell
cd C:\TECH_EVENT\AI_KNOCKOUT\backend
pytest tests/integration/test_worker_gateway.py -q
```

These tests cover worker registration and token lifecycle, explicit activation and certificate rotation, identity separation, lease expiry/reassignment/fencing including stale/future fences, sequence replay, capability scope/expiry/one-use semantics, digest mismatch, bounded evidence, revocation, and cleanup reporting. They do not verify real mTLS transport, CA provisioning/rotation operations, Linux/KVM isolation, Kata, networking, filesystem or privilege boundaries, resource limits, cross-execution isolation, or execution cleanup. Those remain **NOT VERIFIED — DEDICATED WORKER REQUIRED**. The worker migration can be rendered without connecting to any database using the offline command above; it has not been applied to PostgreSQL.

For eventual transport acceptance, launch the selected TLS-capable server with the configured server SSL context, require a client certificate from the worker CA, and verify that it supplies the peer chain using the ASGI TLS extension. From an isolated test worker, prove: no-cert and wrong-CA handshakes fail; a valid SAN-bound worker completes enrollment/activation and a scoped artifact transfer; hostname/SAN mismatch fails; certificate rotation rejects the old cert after activation; revocation rejects the active cert. Capture only event codes/status and certificate fingerprints where necessary; never log PEM contents, keys, bearer headers, enrollment tokens, participant credentials, or database URLs. Mark the result **NOT VERIFIED — REAL TLS ENVIRONMENT REQUIRED** until executed against that listener.
