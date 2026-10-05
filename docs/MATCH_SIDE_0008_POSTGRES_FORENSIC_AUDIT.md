# Migration 0008 PostgreSQL Forensic Audit

**Scope:** static review of migration `0008_typed_match_sides`, its related models, services, tests, and the two requested match documents. No code, migration, tests, or contract files were changed. No database, Redis, container, `.env`, or participant artifact was accessed in this audit. The reported PostgreSQL transaction failure is treated as prior observed evidence, not reproduced here.

**Repository state reviewed:** HEAD `8a4810e4387886e903d74f0e45a5e9730523109c` plus the already-present working-tree changes for typed sides and House AI. The audit report is the only file this task adds.

## 1. Executive summary

Migration 0008 has one **confirmed PostgreSQL defect**: `aik_match_side_identity_guard()` is a polymorphic trigger over three different row shapes and uses `CASE` expressions that reference columns absent from some trigger records. The already-observed failure `record "new" has no field "match_side_id"` is consistent with PostgreSQL resolving the invalid composite-record reference despite the `CASE` condition. Because the trigger is `AFTER ... DEFERRABLE INITIALLY DEFERRED`, otherwise-valid typed-side writes can fail at transaction commit.

Static review also finds probable schema-boundary gaps: migration 0008 drops the prior unique constraint preventing a participant from occupying both sides of one match; the database allows `OFFICIAL` purpose without requiring the participant sides to be the frozen official selections from event mechanics; and worker job insertion does not visibly serialize against concurrent changes to referenced eligibility/frozen state. These require a consolidated repair decision and PostgreSQL verification. They are not repaired here.

The migration’s backfill is ordered before the new side identity triggers are created. Existing participant-vs-participant rows are copied from their legacy columns into participant bindings before those columns are dropped. Static inspection finds no new trigger firing during that copy. It cannot prove actual backfill counts, data shape, or catalog-level behavior on the target PostgreSQL version.

The current tests use SQLite fixtures and Python/application checks. They do not establish PostgreSQL trigger behavior. Prior real disposable PostgreSQL evidence established that migration 0008 applied and exposed the deferred record-field failure; it did not establish that the remaining constraints/triggers work.

## 2. Confirmed defects

### C1 — polymorphic deferred identity trigger references unavailable fields

- **Severity:** HIGH.
- **Location:** `backend/migrations/versions/0008_typed_match_sides.py`, `aik_match_side_identity_guard()` and the loop creating `{table}_typed_identity_check` triggers.
- **Evidence:** On `match_sides`, the trigger row has `id`, not `match_side_id`; the function’s `CASE` expression still contains `NEW.match_side_id` (or `OLD.match_side_id` for DELETE). On either binding table, the row has `match_side_id`, not `id`; the expression still contains `NEW.id` / `OLD.id`. PostgreSQL does not make invalid `record.field` references safe merely because the CASE branch is not selected.
- **Observed consequence:** Prior disposable PostgreSQL verification reported a valid typed-side transaction failing at commit with `record "new" has no field "match_side_id"` from this function.
- **Invariant affected:** Exactly one child binding must exist and must match `match_sides.kind`.
- **Impact:** Typed match-side insertions cannot be considered functional on PostgreSQL until corrected and verified. Depending on operation/table, INSERT/UPDATE/DELETE deferred events can raise for either unavailable-field name. The constraints are deferred, so the exception can appear only during commit.
- **Repair safety:** Can be repaired without changing architecture or contract, but should be included in one consolidated migration repair and verified on PostgreSQL before further implementation.
- **Infrastructure needed:** PostgreSQL is required to verify the trigger record semantics and commit behavior.

No other PostgreSQL runtime defect was independently reproduced during this read-only audit.

## 3. Probable defects requiring PostgreSQL verification

### P1 — participant uniqueness across opposing sides was removed

- **Severity:** MEDIUM.
- Migration 0006 enforced `uq_match_sides_participant (match_id, participant_id)`. Migration 0008 explicitly drops it when removing participant columns from `match_sides`. The replacement `uq_participant_side_identity (match_side_id, participant_id)` only scopes uniqueness to one side and cannot stop one participant from binding both colors.
- `create_match()` in `backend/app/matches/service.py` rejects self-match for the supported participant-versus-participant service path. That is application enforcement only. Direct SQL and any alternate service path can create a two-participant match with the same participant on both sides, and the DB’s side-composition trigger counts kinds but not distinct participant IDs.
- This weakens a pre-existing database invariant. Whether same-participant dual-side matches are intentionally permitted for any future purpose is not stated; the generalization document’s stated distinct-actor expectation should be reconciled.

### P2 — OFFICIAL match purpose is not bound to official selections

- **Severity:** HIGH for any path that treats `OFFICIAL` records as authoritative; otherwise MEDIUM latent risk.
- `ck_matches_purpose` allows `OFFICIAL`; `aik_match_update_guard()` allows a two-participant composition for both `PEER_TEST` and `OFFICIAL`. Participant side guards require an `ELIGIBLE` exact version, but do not require the participant/version pair to match `official_submission_selections` or event-control frozen selections created in 0007.
- `MATCH_SIDE_GENERALIZATION_V1.md` says official matches must use frozen official selections. A row written through SQL could therefore be marked `OFFICIAL` with any eligible versions, then pass the current composition gate. Existing supported match creation defaults to peer testing, but the DB must be treated as the durable integrity boundary.
- Exact intended ownership of official match creation is not fully specified in the current controller document. Verify whether `OFFICIAL` should be unavailable until official-selection binding exists or require exact selection joins.

### P3 — job eligibility/frozen-state TOCTOU is not locked by insertion guards

- **Severity:** MEDIUM.
- `aik_worker_job_insert_guard()` reads participant `submission_versions.state='ELIGIBLE'` or House AI `frozen_at IS NOT NULL` and checks matching identity, but takes no row lock. Foreign keys and append-only behavior prevent many subsequent edits; however submission-version state transitions are allowed under the existing 0003 transition guard, and House inventory append-only does not itself prevent a version from being initially inserted concurrently with a job transaction.
- Under PostgreSQL’s normal transaction isolation, an eligibility/revocation state transition concurrent with job creation may be ordered such that a job insert validates a state that is no longer eligible by commit. Whether the state transition rules allow a transition out of `ELIGIBLE` and the desired job validity semantics are central. Existing worker services re-check status during claim/authorization, which reduces—but does not eliminate—the database invariant gap.
- Requires a transaction-level concurrency test before deciding whether this is an actionable defect.

### P4 — purpose composition is checked only on QUEUED → STARTING

- **Severity:** MEDIUM.
- `aik_match_update_guard()` checks exact side counts/kinds at transition to `STARTING`. Side creation is allowed only while the match is `QUEUED`, side rows and identity rows are append-only, and side colors are unique; this combination should make the composition stable after start. However there is no deferred “match must have exactly two sides” constraint at match creation/commit. A malformed queued match can persist indefinitely, and CHECK constraints alone do not assert cross-row counts. This may be intentional queueing behavior, but it is not the same as an invariant that every committed match is complete.

### P5 — participant side exact eligibility is insertion-time only

- **Severity:** LOW/MEDIUM.
- `aik_participant_side_insert_guard()` requires the version to be `ELIGIBLE` when the binding is inserted. The side binding is then append-only; the submission-version state can be changed by lifecycle logic. A queued/starting match could retain a side whose version later transitions out of eligibility unless another service/event policy prevents it. Match creation’s service checks eligibility and exact quarantine bytes, but the database does not revalidate at start.
- Verify lifecycle expectations: whether eligibility is immutable after acceptance or merely a current state. If mutable, DB/service start-time revalidation is required.

### P6 — model/schema checks differ in digest strictness for House AI

- **Severity:** LOW.
- PostgreSQL migration checks House artifact/version/runtime/policy digests against lowercase 64-hex regexes. `backend/app/models/house_ai.py` models mostly use `length(...) = 64`; SQLite `create_all` therefore accepts strings that the production PostgreSQL migration rejects. This is a test/model fidelity gap, not a demonstrated PostgreSQL migration failure. Model metadata also omits some SQL-only checks such as House version `artifact_size >= 0` and digest format strictness for runtime/policy identity.

### P7 — House AI policy/runtime identity is not fully bound to match identity

- **Severity:** MEDIUM / specification ambiguity.
- House side insertion checks House version `policy_version`, `rules_version`, protocol, runtime profile against the match, and the version row has `policy_digest` and `runtime_identity_sha256`. `matches` has no corresponding policy digest or runtime identity digest columns. Thus equal human-readable versions can refer to different digests without the match recording which immutable digest it selected. No explicit DB cross-check can bind the two digests.
- The documents currently say the House AI version is frozen and pinned; they do not clearly state whether match identity must also pin those two digests. If yes, this is an identity-integrity gap; if version strings are authoritative immutable identifiers, clarify that.

## 4. Trigger/function audit

`NEW`/`OLD` row shapes below are PostgreSQL table row types. “Field exists” reports whether every referenced field exists on that trigger target. Triggers are row-level unless stated otherwise.

| Function | Trigger target table(s) | Actual NEW/OLD row type | NEW fields used | OLD fields used | Fields exist? | CASE safety | Timing | Invariant / potential failure |
|---|---|---|---|---|---|---|---|---|
| `aik_worker_job_insert_guard` | `worker_execution_jobs` BEFORE INSERT | worker job row; OLD unavailable | `execution_kind`, `submission_version_id`, `participant_id`, `submission_id`, `submission_version`, `artifact_sha256`, `immutable_artifact_ref`, `protocol_version`, `runtime_profile`, `house_ai_id`, `house_ai_version` | — | Yes in 0008 schema | No polymorphic CASE | Immediate | Requires participant job to reference matching eligible exact version/storage/protocol/runtime; House job to reference frozen House version and artifact ref. Missing House version makes `h.frozen_at IS NULL` true; missing artifact leaves `FOUND=false`; both fail closed. No evident record-field defect. |
| `aik_worker_job_immutable_guard` | `worker_execution_jobs` BEFORE UPDATE | worker job row | all identity/config fields in tuple | same tuple | Yes | None | Immediate | Prevents mutation of execution identity/policy. Update to protected fields rejected. Lifecycle fields outside tuple remain mutable by worker protocol. |
| `aik_match_side_insert_guard` | `match_sides` BEFORE INSERT | match-side row | `match_id`, `kind` | — | Yes | None | Immediate | Requires queued parent and allowed side kind. `kind` is also NOT NULL/CHECKed. |
| `aik_match_side_identity_guard` | `match_sides`, `participant_match_side_bindings`, `house_ai_match_side_bindings` AFTER INSERT/UPDATE/DELETE | respectively match-side row / participant-binding row / House-binding row | On non-DELETE, selects `NEW.id` for side or `NEW.match_side_id` otherwise | On DELETE, selects `OLD.id` for side or `OLD.match_side_id` otherwise | **No**: each CASE includes a field absent on one row type | **Unsafe; confirmed failure.** CASE selection does not guarantee lazy record-field resolution | Constraint trigger, `DEFERRABLE INITIALLY DEFERRED`; runs at commit | Enforces exactly one kind-matching binding child. Prior PostgreSQL commit raised missing `NEW.match_side_id`; same class of failure applies to `OLD` and other table shapes. |
| `aik_participant_side_insert_guard` | `participant_match_side_bindings` BEFORE INSERT | participant binding row | `submission_version_id`, `match_side_id`, `participant_id`, `submission_id`, `artifact_sha256` | — | Yes | None | Immediate | Requires queued participant side, eligible matching exact version, matching protocol/runtime. Does not itself lock eligibility row or recheck at match start. |
| `aik_house_side_insert_guard` | `house_ai_match_side_bindings` BEFORE INSERT | House binding row | `house_ai_id`, `house_ai_version`, `artifact_sha256`, `match_side_id` | — | Yes | None | Immediate | Requires queued House side and frozen exact compatible House version and match policy/rules version. Does not bind policy/runtime digest to match row. |
| `aik_match_execution_insert_guard` | `match_execution_bindings` BEFORE INSERT | execution-binding row | `match_side_id`, `attempt_number`, `match_id`, `execution_kind`, `execution_id`, `artifact_sha256`, `worker_id`, `lease_id`, `fencing_token`, `submission_version_id`, `house_ai_id`, `house_ai_version` | — | Yes | None; SQL boolean branches over scalar values and EXISTS queries, not composite row fields | Immediate | Requires next attempt number and active acknowledged matching job/worker/lease/fence/expiry and typed identity. Concurrent attempts may race on MAX, but unique `(match_side_id,attempt_number)` rejects duplicate. No immutable/current worker certificate fingerprint is persisted here; gateway authentication is outside this SQL guard. |
| `aik_match_move_insert_guard` | `match_moves` BEFORE INSERT | move row | `match_id`, `sequence`, `match_side_id`, `execution_binding_id` | — | Yes | None | Immediate | Locks running match row; checks next ply, side-to-move, latest binding, job/lease/fence/expiry/worker status and kind-specific identity. Application controller additionally checks protocol/request/rules. SQL does not itself prove submitted move legality; it relies on trusted controller path. |
| `aik_match_update_guard` (replaced body) | `matches` BEFORE UPDATE | match row | identity, lifecycle, state, position, clock/result fields enumerated in body | same | Yes (against `matches`) | None | Immediate | Immutability/state/side composition/readiness/position/result transitions. Composition only checks side counts at QUEUED→STARTING. OFFICIAL composition does not bind event official selections. RUNNING readiness checks two latest attempts with matching ready-event metadata, but not all active lease conditions anew; application service `_current_binding` does those checks. |
| `aik_match_immutable_record` (defined in 0006, reused) | 0008 adds append-only triggers on `house_ais`, `house_ai_artifacts`, `house_ai_versions`, `participant_match_side_bindings`, `house_ai_match_side_bindings`; also older match records | varies by target, but function reads no NEW/OLD fields | None | None | N/A | No record access | BEFORE UPDATE OR DELETE, immediate | Unconditionally raises, so row shape is safe. Prevents mutating/deleting the listed identity records. |

### Trigger target inventory

- Immediate BEFORE INSERT: `worker_execution_jobs`, `match_sides`, `participant_match_side_bindings`, `house_ai_match_side_bindings`, `match_execution_bindings`, `match_moves`.
- Immediate BEFORE UPDATE: `worker_execution_jobs`, `matches`.
- Immediate BEFORE UPDATE OR DELETE append-only: `house_ais`, `house_ai_artifacts`, `house_ai_versions`, `participant_match_side_bindings`, `house_ai_match_side_bindings`; prior 0006 append-only match tables remain.
- Deferred AFTER INSERT OR UPDATE OR DELETE: each of `match_sides`, `participant_match_side_bindings`, `house_ai_match_side_bindings`, all `DEFERRABLE INITIALLY DEFERRED` and all call the same unsafe polymorphic function.
- No 0008 trigger is attached to `match_execution_bindings` or `match_moves` for UPDATE/DELETE; inherited append-only triggers from 0006 cover these records.

## 5. Constraint audit

### Constraints created or replaced by 0008

| Constraint | Table / columns | Intended invariant and actual enforcement | Conflict with 0001–0007 / valid data / mixed identity |
|---|---|---|---|
| `ck_house_ai_artifact_size` | `house_ai_artifacts.artifact_size` | `>= 0`; enforced by PostgreSQL CHECK | New table; no conflict. Zero-byte artifact remains allowed. |
| `ck_house_ai_artifact_digest` | `house_ai_artifacts.artifact_sha256` | lowercase 64-hex; enforced by PostgreSQL regex CHECK | New table; stricter than SQLite model length check. |
| `uq_house_ai_artifact_digest_size` | artifact digest,size | supports composite version FK | Enforced; PK already makes digest unique, so size component is redundant but not harmful. |
| `uq_house_ai_artifacts.storage_ref` | storage ref | distinct storage reference | Enforced via column `unique=True`; cannot establish that storage bytes match digest. |
| `ck_house_ai_version_positive`, `ck_house_ai_version_size`, `ck_house_ai_version_digest`, `ck_house_ai_runtime_digest`, `ck_house_ai_policy_digest` | `house_ai_versions` version,size,three digests | positive version, nonnegative size, lowercase 64-hex identities | Enforced. Model metadata only partially mirrors production checks. |
| `house_ai_versions` PK / `fk_house_ai_versions.house_ai_id` / `fk_house_ai_version_artifact` / `uq_house_ai_version_digest` | `(house_ai_id,version)`; House ID; artifact digest,size; `(house_ai_id,version,digest)` | one version per House/version and exact artifact object | Enforced. No conflict with prior schema. Does not prove external storage bytes. |
| `uq_submission_version_exact_identity` | `submission_versions(id,participant_id,submission_id,artifact_sha256)` | enables typed composite FKs | Enforced. Adds no restriction on valid rows beyond existing unique version ID identity. |
| `ck_matches_purpose` | `matches.purpose` | only PEER_TEST, DEVELOPMENT, OFFICIAL | Enforced. But OFFICIAL allowed without selection binding (P2). Existing rows backfilled/defaulted PEER_TEST. |
| `fk_match_idempotency_participant` | `matches.idempotency_participant_id` | idempotency owner exists | Enforced. Null allowed outside DEVELOPMENT. |
| `uq_match_participant_idempotency_key` | participant ID,key | one idempotency key per participant | Enforced for non-NULL pairs; PostgreSQL NULL semantics allow multiple unrelated NULL pairs, intended. |
| `ck_match_idempotency_purpose` | purpose, idempotency participant/key/hash | all present for DEVELOPMENT and all absent otherwise | Enforced. Does not constrain actual request hash content beyond separate length check. |
| `ck_match_request_hash_length` | request_hash | null or 64 characters | Enforced, but does not require lowercase hex. Service generates SHA-256; direct SQL could supply nonhex 64-character text. |
| `ck_match_idempotency_key_length` | idempotency_key | null or length 1–128 | Enforced. Whitespace-only key is accepted at DB level unless app rejects it. |
| `ck_match_sides_kind` | `match_sides.kind` | participant/House discriminator | Enforced; needed to discriminate subject type. |
| `uq_match_sides_color` (retained) | `match_sides(match_id,color)` | at most one side per color | Enforced; does not require both colors to exist. |
| `uq_participant_side_identity` | binding `(match_side_id,participant_id)` | no duplicate participant binding on the same side | Enforced, but **does not replace** dropped `uq_match_sides_participant`; same participant may occupy both match sides (P1). |
| `uq_participant_side_artifact` | binding `(match_side_id,submission_version_id,digest)` | prevents duplicate exact artifact row per side | Enforced; redundant with side PK for a one-row side but harmless. |
| `participant_match_side_bindings` PK and FKs | side ID; participant ID; submission ID; composite `(version_id,participant_id,submission_id,digest)` | valid references and exact version tuple | Enforced. Composite FK correctly prevents mixed participant/submission/digest identity. Eligibility and protocol compatibility are trigger-only. |
| `house_ai_match_side_bindings` PK/FKs | side ID; composite House ID/version/digest | exact House version and side reference | Enforced. Does not include artifact size in this subject binding, but referenced House version uniquely pins its digest+size through its own FK. |
| `ck_match_execution_subject_kind` | execution kind, participant version ID, House ID/version | exactly participant subject fields OR House subject fields | Enforced by disjoint branches; invalid/NULL kind fails. Does not itself bind participant version to side; trigger does on INSERT. |
| `fk_match_execution_house_ai_subject` | House ID/version/digest | exact House version and artifact digest | Enforced. Participant `submission_version_id` retains its pre-existing simple FK; exact relationship to side/participant/digest is enforced by INSERT trigger. |
| `ck_worker_job_subject_kind` | kind + participant fields + House fields | exactly one typed job subject | Enforced. CHECK plus NOT NULL kind excludes unknown discriminator values. |
| `ck_worker_job_version_positive` (replaced) | participant `submission_version` | null for House jobs or positive for participant | Enforced; preserves participant positive-version rule while allowing House jobs. |
| `fk_worker_job_participant_subject` | version ID, participant, submission, digest | exact participant artifact identity | Enforced via newly added composite FK. `submission_version` ordinal and storage key/protocol/runtime still depend on insert trigger. |
| `fk_worker_job_subject_house_ai` | House ID/version/digest | exact House version/artifact identity | Enforced. Worker `execution_mode='MOCK_PROTOCOL_ONLY'` and existing counters/digest constraints from 0004 remain. |
| `uq_match_execution_id`, `uq_match_side_attempt` (retained) | execution ID; side+attempt | unique execution and attempt | Enforced; second one handles simultaneous MAX-attempt race by rejecting duplicate, not by allowing two rows. |
| `ck_match_execution_attempt_positive`, `ck_match_execution_fence_positive`, `ck_match_execution_sha256` (retained) | attempts/fence/digest | positive sequence/fence and digest length | Enforced; digest length is weaker than lowercase-hex regex at this table. Typed job/version FKs and triggers provide some identity checks. |
| `uq_match_move_sequence`, `uq_match_move_request`, `ck_match_move_sequence_positive`, `ck_match_move_elapsed_nonnegative`, `ck_match_move_uci_length` (retained) | move table | unique sequence/request, positive sequence, nonnegative elapsed, UCI length | Enforced. UCI length does not prove legal move; trusted rules/controller path does. |

### Important retained constraints and dropped constraints

- `uq_match_sides_participant (match_id,participant_id)` from 0006 is **dropped**. No cross-side typed replacement is created (P1).
- Legacy `match_sides` participant/submission/version/digest foreign keys and digest check are dropped only after data has been copied to the new typed binding table. New exact composite FKs replace the old identity links.
- Existing match lifecycle/state/clock/result checks, worker job status/counter/digest/mock-mode checks, match execution worker/match/side FKs, and match move FKs/uniques remain in effect unless explicitly altered above.
- CHECK constraints cannot express cross-row exact two-side composition. That rule is enforced only by `aik_match_update_guard()` at QUEUED→STARTING. The deferred identity trigger supplies exactly-one child, but currently fails due to C1.

## 6. Backfill audit

Static order in `upgrade()`:

1. Create House AI inventory tables and their local constraints.
2. Add the `submission_versions` composite unique key.
3. Add `matches.purpose` with server default `PEER_TEST`; existing matches are consequently classified as non-official peer-test records. Add optional idempotency columns and checks.
4. Add `match_sides.kind` with server default `PARTICIPANT`.
5. Create participant and House side-binding tables and exact foreign keys.
6. Extend `match_execution_bindings` with default `PARTICIPANT`, nullable House identity, allow nullable submission version for House kind, and add subject checks/FK.
7. Execute one `INSERT INTO participant_match_side_bindings ... SELECT ... FROM match_sides`, copying each existing side’s ID, participant, submission, submission-version ID, and digest.
8. Drop the former participant uniqueness/FKs/digest check from `match_sides`, then drop the four legacy participant identity columns.
9. Add `execution_kind=PARTICIPANT` and nullable House columns to worker jobs; allow participant columns null for House jobs; replace version check; replace simple version-ID FK with participant composite identity FK and add House exact FK.
10. Replace kind-sensitive insert/immutability triggers; add typed-side and child insert guards; replace execution/move guards; replace match update guard; add append-only triggers.

### Backfill findings

- **Rows created:** exactly one participant binding row for each existing `match_sides` row, as represented by the legacy participant columns. No House binding is created.
- **Rows updated:** no explicit UPDATE statement. Existing matches/sides/executions/jobs receive defaults for new kind/purpose columns. Existing jobs and execution bindings therefore become `PARTICIPANT`.
- **Trigger behavior:** the new participant child BEFORE INSERT guard and deferred typed-side constraint triggers are installed after the copy. The old `match_sides` insert guard is not invoked because the source rows are selected, not inserted. No new identity trigger runs during the backfill insert.
- **Commit behavior:** migration DDL and data migration run within Alembic’s PostgreSQL migration transaction under normal transactional DDL. Deferred triggers are not yet present during the backfill. No new deferred trigger is queued for the copy.
- **Resulting invariants:** legacy columns were non-null and protected by their old FK/check constraints; their copied fields therefore appear structurally complete. The resulting participant-side exact FK should validate because the copied tuple came from the same old row and version references. It is not statically proven that all deployed legacy rows satisfy the added composite unique key, FK, or constraints; PostgreSQL migration application would establish that.
- **Order safety:** dropping the old fields after copy is correct for a forward schema transformation. The new deferred trigger would validate copied sides if installed earlier, but it is installed later. This avoids invoking the current broken function during backfill and means successful migration does not prove the identity trigger works.
- **Participant-vs-participant validity:** both sides remain `PARTICIPANT`; `purpose` defaults to `PEER_TEST`; the historical participant identity remains in the child bindings. The old per-match participant uniqueness is not preserved (P1). Existing matches with incomplete/tampered legacy state would fail migration constraints or produce bad binding rows; no static evidence of production row counts/quality is available here.

## 7. Execution-kind audit

| Question | Finding |
|---|---|
| Is `PARTICIPANT` valid? | Yes. Defaults preserve old execution/job rows. Worker job CHECK/FK/INSERT guard and match binding trigger validate participant identity. |
| Is `HOUSE_AI` valid? | Schema-level yes: worker job and match execution subject checks/FKs include House ID/version/digest; side table and binding insert guards require a frozen compatible House version. Runtime worker service remains participant-only and expressly rejects House AI capability/download, so end-to-end House execution is not implemented. |
| Can side and execution identity disagree? | At DB INSERT time, `aik_match_execution_insert_guard()` compares side kind/identity/digest with worker job and binding fields. CHECKs also constrain subject shapes. UPDATE/DELETE append-only controls protect bindings. The deferred side-identity C1 prevents PostgreSQL validation of the child side invariant. |
| Can worker/job identity disagree? | Job insertion guard and exact FKs bind job subject to a participant/House version. Match execution insert trigger binds job to side, worker, match, lease, fence, status and expiry. Trigger-only fields are not all represented by composite FKs. |
| Are stale/invalid combinations prevented at DB level? | Many are rejected at binding/move INSERT; side/job identity uses trigger checks. Database does not itself rerun the application’s live `_current_binding()` check when `matches.state` becomes RUNNING beyond requiring ready events for two latest binding attempts. No DB-level official-selection join exists. |
| Do application checks duplicate/contradict DB? | `begin_initialization()` and DB match update guard use the same purpose/kind composition for PEER_TEST/OFFICIAL vs DEVELOPMENT. Both permit OFFICIAL as two participant sides without official-selection validation (P2). `bind_execution()` and `_current_binding()` are stricter runtime application checks; worker job service creates/claims only participant jobs. |
| Can typed values be mixed? | Participant composite FKs and House composite FKs prevent mixed version tuples. Side child-kind exclusivity depends on the broken deferred trigger. Match execution subject XOR CHECK prevents mixed subject fields. |

## 8. Match Controller compatibility audit

- `MatchSideIdentity` and `MatchExecutionBinding` in `backend/app/matches/controller.py` represent participant and House AI identity as mutually exclusive typed values. `matches_side()` compares match ID, side ID, kind, typed subject, and artifact digest. This is internally coherent at the domain-object level.
- `MatchController.__init__()` requires two sides with distinct `actor_identity` values, and `start()` requires readiness for both side executions. These are application invariants, not DB constraints. The identity tuple for participant sides includes participant ID and submission-version ID; therefore the controller alone regards two different versions belonging to the same participant as distinct actors. The currently supported `create_match()` separately rejects equal participant IDs. If another creation path is introduced, the distinction should be explicit.
- Match service binding code handles either typed kind when matching a worker job. Current `create_match()` still accepts two participant versions only; there is no supported House AI match creation path. `begin_initialization()` permits DEVELOPMENT only for one participant plus one House AI and permits OFFICIAL only for two participant sides.
- PostgreSQL `aik_match_update_guard()` duplicates the side-composition rules at QUEUED→STARTING. It does not enforce distinct participant IDs across sides after 0008 drops the old unique constraint (P1), and it does not resolve official selections (P2).
- `MATCH_CONTROLLER_V1.md` expressly says supported creation/execution is participant-vs-participant only, House AI gameplay and queue/artifact transfer are not authorized, participant-code execution is not launched, and PostgreSQL trigger verification has not been completed. The worker service’s participant-only job/capability path matches this scope; typed House AI fields should not be read as implemented gameplay.
- Durable readiness and move paths check current application binding, active worker/job status, lease/fence, and expiry. SQL protects inserts and current-attempt selection, but its RUNNING transition check is narrower than application `_current_binding()` (ready-event association for two latest attempts). Concurrent revocation/reassignment and state transition behavior needs PostgreSQL transaction tests; this audit does not infer serializability from ORM locks.

## 9. Test coverage gaps

`backend/tests/conftest.py` explicitly creates isolated SQLite databases and an ASGI transport. It overrides DB/Redis health checks and uses a local in-memory rate limiter. The `integration` marker does not mean PostgreSQL integration.

| Invariant/behavior | Existing coverage | PostgreSQL evidence status |
|---|---|---|
| House typed binding persistence, missing/dual identity rejection | `test_typed_match_identity.py`; SQLite ORM listener/constraints | Not proved by SQLite for PostgreSQL triggers. |
| Worker typed subject XOR | typed identity test, SQLite CHECK/ORM behavior | PostgreSQL SQL CHECK/FK/trigger not exhaustively verified. |
| Participant match creation, exact versions, self-match rejection | `test_matches.py` through ASGI + SQLite | Application path tested only on SQLite. |
| Match binding exact job/lease and readiness | `test_matches.py` SQLite services and unit/controller logic | PostgreSQL lock/trigger semantics not covered. |
| Match controller protocol, duplicate/gap/deadline/legal-move handling, rehydration | controller unit tests/mocks and fake rules where injected | Application behavior only, not DB trigger correctness. |
| Worker enrollment/rotation/revocation/job/artifact capability and evidence | `test_worker_gateway.py`, SQLite + ASGI/mock peer | Simulated peer/application behavior; no real PostgreSQL enforcement or real TLS proof from these tests. |
| Database append-only, composite FKs, check constraints | SQLite `create_all` constraints and ORM events | SQL dialect and trigger differences remain; SQLite does not run migration 0008 PL/pgSQL. |
| 0008 migration backfill and all constraints | no dedicated PostgreSQL migration test was found in the reviewed tests | Prior disposable PostgreSQL run applied migration but a valid typed-side commit failed C1; other constraints remain unverified. |
| Deferred trigger firing at commit; invalid record fields in CASE | no test | Confirmed by prior PostgreSQL failure; must add PG regression tests in repair milestone. |
| Concurrency: same participant both sides, duplicate attempts, eligibility changes, revocation races | mostly service/application-level locks/mocks | Not tested against PostgreSQL transactions. |

`backend/tests/integration/test_worker_gateway.py` uses a controlled inert byte fixture and mocks its worker peer. It does not execute participant code. Nothing in this audit ran tests.

## 10. Security implications

1. **Typed identity integrity currently fails closed at commit rather than accepting an invalid identity.** This blocks valid side writes and availability, but is not evidence that all invalid database writes are rejected safely; PostgreSQL trigger behavior must be repaired and retested.
2. **Official selection bypass:** a raw writer able to create match rows could mark arbitrary eligible versions `OFFICIAL` because the purpose trigger checks side kind/count, not frozen official selections. This risks official-result provenance if any consumer trusts purpose alone.
3. **Duplicate participant side:** a raw writer can potentially assign one participant to both colors because the earlier uniqueness constraint was dropped. This can undermine match meaning and downstream competition reporting.
4. **Eligibility and revocation ordering:** insert-time eligibility checks without explicit locking can have race behavior that is not established by SQLite tests. Current application worker authorization rechecks state, but database-level job creation semantics require a PostgreSQL concurrency test.
5. **Digest equality at SQL row level is not byte verification.** The House AI inventory stores a storage reference and metadata; neither SQL nor these schema rows prove external object bytes still match the digest. That requires storage-service verification at transfer/staging time. Current House transfer is not implemented.
6. **No execution authorization is implied by typed House AI schema support.** Existing worker service rejects House AI artifact capability/download; no participant artifact execution, House AI gameplay, or real sandbox is established by migration 0008.
7. **No PostgreSQL trigger verification claim is made.** SQLite tests and successful migration application cannot establish PL/pgSQL record-field, deferred trigger, row-lock, or transaction-isolation semantics.

## 11. Exact consolidated repair plan (not performed)

1. Replace `aik_match_side_identity_guard()` with table-shape-safe logic: separate trigger functions per row type or explicit `TG_TABLE_NAME` branches that access only fields available on that known row type. Avoid polymorphic `record` field expressions hidden inside CASE. Preserve deferred exact-one identity semantics and handle DELETE/UPDATE intentionally.
2. Add PostgreSQL regression tests for all three trigger targets and all operations; assert valid participant/House side transactions commit, invalid/missing/dual bindings fail at commit, and deletes/updates behave as intended.
3. Decide and restore the cross-side participant uniqueness invariant. Prefer a DB-enforceable typed identity rule that prevents a participant from being bound to both sides when match purpose requires distinct participants; preserve allowed purpose exceptions only if explicitly approved.
4. Define the `OFFICIAL` boundary. Either prevent OFFICIAL insertion/transition until official-selection binding exists, or require each participant side to match the frozen official selection and event-control state in PostgreSQL. Do not rely solely on a purpose enum and kind count.
5. Specify whether version eligibility must remain valid through match start/job claim. If so, coordinate row locking or transition restrictions with submission state changes and add concurrent PostgreSQL tests for eligibility transitions, lease reassignment, worker revocation, and match transition.
6. Decide whether House `runtime_identity_sha256` and `policy_digest` are part of pinned match identity. If yes, persist and compare them; otherwise document that immutable version IDs/strings are the authoritative binding.
7. Align SQLAlchemy model checks with migration constraints where practical so SQLite tests do not accept malformed House digest/size rows that PostgreSQL rejects. Keep PostgreSQL tests as the source of truth for PostgreSQL-only behavior.
8. Verify deployment catalog state against `0001`–`0008` in a fresh disposable PostgreSQL database, including constraint names, trigger definitions, deferrability, and rows after backfill.
9. Only after repair verification, resume implementation decisions for House AI match flow. Do not infer that schema support authorizes gameplay, worker transfer, or execution.

## 12. Exact PostgreSQL verification matrix

All cases must use a fresh disposable PostgreSQL database migrated through 0008, isolated from event services. Assert SQLSTATE/error constraint or trigger name, commit/rollback boundary, and final row state.

| Case | Operation | Expected result after repair | Layer |
|---|---|---|---|
| PG-01 | Apply 0001–0008 to empty disposable DB | Upgrade succeeds; schema/catalog matches model intent | Migration |
| PG-02 | Seed valid pre-0008 participant matches/sides/jobs/execution rows, upgrade | Correct purpose/kind defaults; one exact participant binding per old side; legacy columns removed | Backfill |
| PG-03 | Insert valid participant side + exact child and commit | Commit succeeds | Deferred trigger |
| PG-04 | Insert valid House side + exact child and commit | Commit succeeds | Deferred trigger |
| PG-05 | Commit side with no child | Fails at commit | Deferred trigger |
| PG-06 | Commit participant side with House child, and inverse | Fails at commit/insert guard | Trigger/invariant |
| PG-07 | Attempt two child identities on one side | Fails | Deferred trigger/PK |
| PG-08 | UPDATE/DELETE each side and binding target | Append-only behavior and deferred checks are intentional and deterministic | Trigger |
| PG-09 | Participant binding references wrong participant/submission/version/digest | Fails FK or guard | Composite FK/trigger |
| PG-10 | House binding references wrong House/version/digest/protocol/runtime/rules/policy | Fails FK or guard | Composite FK/trigger |
| PG-11 | Create DEVELOPMENT with exact participant+House sides; transition to STARTING | Succeeds | Purpose composition |
| PG-12 | Create PEER_TEST with two participants; transition to STARTING | Succeeds only with distinct participant identity if rule restored | Purpose/uniqueness |
| PG-13 | Put same participant on both sides; try PEER_TEST/OFFICIAL and any explicitly allowed purpose | Rejected or accepted per explicit documented policy | Uniqueness |
| PG-14 | Mark arbitrary eligible versions OFFICIAL while official freeze points elsewhere | Rejected (or OFFICIAL unavailable) | Official-selection integrity |
| PG-15 | Participant/House worker job with exact typed subject | Insert succeeds only when eligible/frozen subject identity is exact | Job guard/FK |
| PG-16 | Invalid mixed/partial/unknown execution kind job or binding | Rejected by CHECK/FK/trigger | Execution kind |
| PG-17 | Worker job points to wrong side/match/artifact/worker/lease/fence/status/expiry | Match execution binding rejected | Execution binding trigger |
| PG-18 | Concurrent same-side attempt creation | At most one attempt number commits; losing transaction gets unique conflict or serialized next attempt | Lock/unique |
| PG-19 | Eligibility/freeze transition races job insert or match start | Outcome conforms to approved lifecycle invariant; no stale accepted execution | Concurrency |
| PG-20 | Revoke worker or expire/reassign lease racing readiness/move/start | No stale execution advances match state or writes a move | Concurrency/fencing |
| PG-21 | Submit wrong-kind job for typed side; wrong latest attempt; wrong-side move | Rejected | Execution/move triggers |
| PG-22 | Tamper position without matching move/result, or complete/abort with invalid fields | Rejected | Match update guard/checks |
| PG-23 | Nonhex but 64-character digest/hash values | Rejected where digest format is contractual; document weaker request hash if retained | Constraints |
| PG-24 | Inspect `pg_trigger`/`pg_constraint` | Side triggers are deferred initially; all expected CHECK/UNIQUE/FKs installed, old uniqueness drop is deliberate/replaced | Catalog |

## 13. Files that would need modification in a future authorized repair

Likely minimum:

- `backend/migrations/versions/0008_typed_match_sides.py` only if migration source is still editable before it is applied anywhere; otherwise add a forward repair migration, never rewrite an applied migration.
- New PostgreSQL-specific integration test(s), e.g. under `backend/tests/integration/`, plus isolated disposable-PG test harness/configuration that never reads event `DATABASE_URL` implicitly.
- If policy decisions require schema/API alignment: `backend/app/models/match.py`, `backend/app/models/house_ai.py`, `backend/app/models/worker.py`, `backend/app/matches/service.py`, and the relevant contract/controller docs.
- `docs/MATCH_SIDE_GENERALIZATION_V1.md` and/or `docs/MATCH_CONTROLLER_V1.md` only after any unresolved OFFICIAL/distinct-actor/policy-digest decisions are settled.

This is a prospective list, not authorization to edit those files.

## 14. Files that must NOT be modified in this audit

No source/migration/test/contract files were modified. In particular, this audit did not modify:

- `backend/migrations/versions/0008_typed_match_sides.py`
- any `backend/app/` source or model
- any test file
- `docs/MATCH_SIDE_GENERALIZATION_V1.md`
- `docs/MATCH_CONTROLLER_V1.md`
- architecture or Core Contract documents
- `.env`, databases, Redis, containers, participant artifacts

## 15. Explicit non-goals

- No implementation or migration repair.
- No House AI gameplay, development match execution, worker queue expansion, participant execution, or sandbox work.
- No architecture/contract decision changes.
- No database connection, migration run, test run, Docker/Redis activity, participant-data access, or `.env` access during this audit.
- No claim that typed-side persistence, migration trigger security, or PostgreSQL behavior is secure or verified beyond the specific prior observed failure.

## Audit status

**AUDIT STATUS: DEFECTS FOUND**

- **Confirmed:** `aik_match_side_identity_guard()` references row fields absent on polymorphic trigger targets; prior PostgreSQL transaction failed at commit with `record "new" has no field "match_side_id"`.
- **Probable:** old `uq_match_sides_participant` invariant is removed without cross-side replacement; DB permits OFFICIAL matches not bound to frozen official selections; eligibility/freeze lifecycle may race insertion/start without row-lock semantics; eligibility is checked only at binding insert; House runtime/policy digests are not pinned in match identity; SQLite model checks are weaker than PostgreSQL migration checks.
