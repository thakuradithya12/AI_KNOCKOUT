# Participant submissions and intake validation

**Historical scope of this submission-intake milestone:** it implements participant-owned submissions, immutable version metadata, exact-byte quarantine storage and static validation. That milestone did **not** implement participant-code execution, a sandbox, chess logic, matches, workers, tournament behavior or runtime eligibility. Worker-control-plane implementation is documented separately in `SECURE_EXECUTION_ENVIRONMENT_V1.md`; participant execution remains disabled.

## Identity and records

The existing authenticated participant session is the only identity source. Each logical submission has a server-generated UUID and immutable `participant_id` owner. Each uploaded digest creates an append-only positive version number. A repeated `(participant, submission, SHA-256)` upload returns the existing version and original validation attempt. New bytes create a new version; they do not replace earlier versions. Identical archive bytes from different owners reference one digest-addressed quarantine object while each owner gets distinct metadata.

The migration adds:

- `submissions`: logical participant-owned entries.
- `quarantine_objects`: exact raw archive bytes keyed by lowercase SHA-256.
- `submission_versions`: immutable artifact metadata plus current lifecycle projection.
- `validation_attempts`: append-only validator/policy version, times, outcome, bounded reason report.
- `artifact_state_transitions`: append-only actor, source/target state, reason and correlation ID.
- `competition_events`: transactionally written submission lifecycle event outbox records; this is separate from Phase 2 `auth_events`. No publisher, WebSocket, Redis queue or live event service is included.

PostgreSQL triggers reject updates/deletes to immutable artifact bytes, submission identity, validation attempts, transitions and competition events. They protect version identity columns and require a recorded, permitted transition for state changes. Portable SQLAlchemy flush guards enforce the same rules in isolated SQLite tests.

## Package and manifest

Uploads are raw, uncompressed POSIX USTAR archives. V1 permits only root `manifest.json`, optional root `README.md`, and UTF-8 `.py` files below `engine/`. `engine/main.py` is required. Directory entries are implicit. No extraction occurs: the parser reads bounded member data in memory and stores the original archive bytes unchanged.

Archive rules follow [Core Contract V1](AI_KNOCKOUT_CORE_CONTRACT_V1.md), including its clarified rightmost USTAR `prefix`/`name` split. ASCII-only member paths are a deliberate V1 policy to avoid Unicode normalization and canonical-equivalence ambiguity. UTF-8 byte limits are checked before applying the ASCII allowlist; non-ASCII paths are rejected without normalization or equivalence handling. Only canonical regular files are accepted. Links, special files, extensions, traversal, duplicate/case-colliding/conflicting paths, malformed headers, bad checksums, unsorted members, nonzero padding, trailing bytes and noncanonical field encodings are rejected.

The server enforces configurable caps no larger than the provisional V1 ceilings:

| Limit | Default ceiling |
|---|---:|
| Raw archive | 16 MiB |
| Members | 128 |
| One member | 4 MiB |
| Total member data | 16 MiB |
| Manifest | 16 KiB |

`manifest.json` must be strict UTF-8 JSON with unique keys and exactly these fields: `contract_version`, `protocol_version`, `engine_name`, `engine_version`, `runtime`, and `entrypoint`. V1 accepts only contract `1`, protocol `aik-jsonl/1`, and runtime `python-3.12`; the entrypoint must resolve to a permitted `.py` member below `engine/`. Unknown fields, including any participant-supplied security/resource/configuration field, fail closed. Limits and runtime policy remain server controlled.

## HTTP API

All endpoints require the existing active participant Bearer session except where an unauthorized request is deliberately returned as 401. Use the authenticated participant's existing portal login to obtain a session token.

| Method and path | Behavior |
|---|---|
| `POST /api/v1/submissions` | Create an empty logical submission; response includes server-generated `submission_id`. |
| `GET /api/v1/submissions` | List only the authenticated owner's submissions. |
| `GET /api/v1/submissions/{submission_id}` | Get owner-scoped submission and latest version metadata. Other owners receive the same 404 as an unknown ID. |
| `POST /api/v1/submissions/{submission_id}/versions` | Stream and bound a raw `application/x-tar` or `application/octet-stream` body; return version and validation result. Filename and client size/hash fields are not used. |
| `GET /api/v1/submissions/{submission_id}/versions` | List owner-scoped version metadata. |
| `GET /api/v1/submissions/{submission_id}/versions/{version}` | Get owner-scoped version metadata. |
| `GET /api/v1/submissions/{submission_id}/versions/{version}/validation` | Get persisted append-only validation results. |

Successful static validation returns `201` and state `APPROVED`; a duplicate successful upload returns `200`. A complete but invalid archive is stored in quarantine with a `REJECTED` version and append-only validation attempt, then returns `422` with a stable `error` code and version reference. An upload exceeding the configured raw byte cap is rejected with `413` before persistence. Unsupported content types are `415`. Artifact bytes, filesystem paths, storage backend details, credentials and runtime internals are never returned. No endpoint exposes an activation action; `APPROVED` is not `ELIGIBLE`.

## Lifecycle

The intake pipeline records these states from the frozen contract:

```text
complete raw bytes -> QUARANTINED -> INSPECTING
  -> REJECTED (permanent package defect)
  -> VALIDATING -> REJECTED (manifest/compatibility defect)
  -> VALIDATING -> APPROVED (static checks pass)
```

Static parser faults are participant/content rejections. Database/storage infrastructure failures roll the transaction back; they do not create a false approved result. Retries of identical accepted content are idempotent and do not reset a rejection or create a new attempt. `APPROVED` remains non-runnable because eligibility/activation belongs to a later competition workflow.

## Stable validation and API codes

Archive/content rejection codes: `PACKAGE_TOO_LARGE`, `ARCHIVE_MALFORMED`, `ARCHIVE_TRAILING_DATA`, `ARCHIVE_FORMAT_UNSUPPORTED`, `ARCHIVE_LINK_FORBIDDEN`, `ARCHIVE_SPECIAL_FILE_FORBIDDEN`, `ARCHIVE_EXTENSION_FORBIDDEN`, `ARCHIVE_TYPE_FORBIDDEN`, `ARCHIVE_NON_CANONICAL`, `ARCHIVE_CHECKSUM_INVALID`, `ARCHIVE_PATH_ENCODING_INVALID`, `ARCHIVE_PATH_INVALID`, `ARCHIVE_PATH_SPLIT_INVALID`, `ARCHIVE_PATH_SPLIT_NONCANONICAL`, `ARCHIVE_MEMBER_ORDER_INVALID`, `ARCHIVE_DUPLICATE_MEMBER`, `ARCHIVE_PATH_CONFLICT`, `ARCHIVE_TOO_MANY_MEMBERS`, `ARCHIVE_MEMBER_TOO_LARGE`, `ARCHIVE_CONTENT_TOO_LARGE`, and `ARCHIVE_TRUNCATED`.

Manifest/content rejection codes: `MANIFEST_MISSING`, `ENGINE_ENTRYPOINT_MISSING`, `MANIFEST_TOO_LARGE`, `MANIFEST_BOM_FORBIDDEN`, `MANIFEST_INVALID_JSON`, `MANIFEST_OBJECT_REQUIRED`, `MANIFEST_DUPLICATE_KEY`, `MANIFEST_UNKNOWN_FIELD`, `MANIFEST_REQUIRED_FIELD_MISSING`, `MANIFEST_FIELD_TYPE_INVALID`, `UNSUPPORTED_CONTRACT_VERSION`, `UNSUPPORTED_PROTOCOL_VERSION`, `UNSUPPORTED_RUNTIME`, `MANIFEST_ENGINE_NAME_INVALID`, `MANIFEST_ENGINE_VERSION_INVALID`, `MANIFEST_ENTRYPOINT_INVALID`, `MANIFEST_ENCODING_INVALID`, `SOURCE_BOM_FORBIDDEN`, and `SOURCE_ENCODING_INVALID`.

Successful static validation uses report code `STATIC_VALIDATION_PASSED`. API errors include `SUBMISSION_NOT_FOUND`, `SUBMISSION_VERSION_NOT_FOUND`, `UNSUPPORTED_MEDIA_TYPE`, `CONTENT_LENGTH_INVALID`, `PACKAGE_TOO_LARGE`, and `ARTIFACT_STORAGE_INTEGRITY_FAILURE`. Reports contain stable codes/stage only; no raw source or untrusted exception output is returned.

## Storage and guarantees

The zero-budget local adapter stores raw bytes in a separate PostgreSQL `quarantine_objects` table keyed by `sha256:<digest>`. It commits object bytes, version metadata, validation attempt, transition log and competition outbox records in one database transaction. The `QuarantineStorage` interface separates the logical digest key from this PostgreSQL adapter so an object-storage implementation can replace it without changing submission/version ownership. The raw bytea is never materialized as a file in the application source tree. Existing PostgreSQL volume configuration provides persistence; database-at-rest encryption, backup/retention and secure deletion remain infrastructure/policy decisions.

The server computes SHA-256 over the exact complete request-body bytes. Client-provided hashes/sizes are ignored. Existing digest objects are checked against both the digest and exact bytes before reuse. The accepted bytes are never repacked or rewritten. A DB constraint checks byte length, application/ORM checks validate digest, and immutable-record triggers/guards prohibit replacement. No `tarfile.extract`, subprocess, import, compile, or participant-code execution occurs.

## Security limits and known limitations

- This is hostile-input intake and static validation, **not a sandbox**. Passing manifest/archive validation proves neither that Python code is safe nor that an engine follows the AI protocol.
- The API/PostgreSQL/Redis host remains control-plane only. Submission source is never executed there.
- Content is bounded in request memory and PostgreSQL by provisional technical limits; there is no participant storage quota, rate policy or retention job in this milestone.
- The default PostgreSQL quarantine adapter shares existing database backup/availability characteristics. This release does not add encryption-at-rest, blob garbage collection, migration cleanup, object-store replication or a separate execution store.
- Validation is synchronous and limited to deterministic USTAR, UTF-8 and manifest checks. It does not import or compile participant source, resolve Python dependencies, execute a smoke test, validate AIK runtime messages, or approve an artifact for matches.
- Static schema and API tests run against isolated in-memory SQLite and mock health dependencies. PostgreSQL migration SQL is reviewed/generated offline; do not point these tests or migration validation at the active event database.

## Running

Apply the schema migration explicitly in the intended development environment:

```powershell
cd C:\TECH_EVENT\AI_KNOCKOUT\backend
alembic upgrade head
```

Tests force in-memory SQLite, in-process ASGI, and the process-local rate-limit fallback; they do not honor `API_BASE_URL` or connect to the active PostgreSQL/Redis stack:

```powershell
cd C:\TECH_EVENT\AI_KNOCKOUT\backend
pytest -q
```

To upload with an existing Bearer token:

```powershell
curl.exe -X POST "http://127.0.0.1:8000/api/v1/submissions" -H "Authorization: Bearer <session-token>"
curl.exe -X POST "http://127.0.0.1:8000/api/v1/submissions/<submission-uuid>/versions" -H "Authorization: Bearer <session-token>" -H "Content-Type: application/x-tar" --data-binary "@engine.tar"
```
