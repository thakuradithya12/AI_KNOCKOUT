# Match Side Generalization V1: Participant and House AI

**Status:** Design proposal for implementation review. No House AI match, queue,
execution, or portal capability is implemented by this document. Existing
participant-versus-participant behavior remains the only supported persisted
match form until a later implementation and verification milestone.

## 1. Current architecture

`matches` is the authoritative match aggregate. It owns lifecycle, protocol and
runtime profile, policy and rules versions, seed, board/clock snapshot, and
server-derived terminal outcome or failure. `match_sides` owns immutable WHITE
and BLACK identity. `match_execution_bindings` binds attempts to worker leases
and fencing tokens. `match_moves` and `competition_events` hold authoritative
transcript and lifecycle evidence. `MatchController` owns protocol sequencing,
clocks, legality through `ChessRulesAdapter`, terminal outcomes, and failure
classification.

Today, each `MatchSide` is a participant side. `create_match()` accepts two
different eligible participant versions and verifies both quarantine objects.
The worker job and artifact capability are likewise participant-submission
specific. Readiness and transition to RUNNING require two current worker
execution bindings and their readiness evidence. The participant portal has no
development-match API and accurately reports this capability unavailable.

There is no generic `Execution` table, no House AI artifact inventory, no House
AI worker identity, and no House AI runtime or participant sandbox. The worker
gateway is a control-plane protocol, not evidence that a participant sandbox
has run.

## 2. Exact blocker analysis

The following source-enforced assumptions block PARTICIPANT-vs-HOUSE_AI today:

1. `MatchSide.participant_id`, `submission_id`, and `submission_version_id` are
   non-null participant foreign keys. Its digest is required.
2. `create_match()` requires two eligible `SubmissionVersion` rows, distinct
   versions and participants, and exact participant quarantine bytes.
3. Migration `0006_match_controller.py` creates those non-null side columns and
   `aik_match_side_insert_guard()` requires each side to join an eligible,
   participant-owned exact submission version with the match protocol/runtime.
4. `WorkerExecutionJob` and `WorkerArtifactCapability` bind participant,
   submission/version, and participant artifact digest. There is no execution
   kind or House AI artifact target.
5. `MatchExecutionBinding` requires participant `submission_version_id`, worker,
   lease, fencing token, and artifact digest. Its trigger validates the matching
   worker job.
6. `mark_match_running()` requires two sides, latest worker bindings for both,
   current unexpired leases, and `EXECUTION_READY` evidence for both bindings.
   Migration 0006 repeats these constraints in PostgreSQL triggers.
7. `MatchSideIdentity` and `MatchExecutionBinding` require participant execution
   identity. `MatchController` rejects two sides with the same participant ID.
8. The participant portal’s development-match copy is unavailable-state text;
   it has no match endpoint or persisted data from which to render results.

These restrictions are correct for the current participant-only contract. They
must not be bypassed by inserting a fake participant, fake submission, mock
worker job, unverified ready event, or fabricated result.

## 3. Proposed MatchSide model

Keep one authoritative `Match` aggregate and one `match_sides` row per color.
Do not add nullable participant and House AI columns directly to `match_sides`.
Use a common side row plus mutually exclusive typed identity rows:

```text
match_sides
  id, match_id, color, kind, created_at

participant_match_side_bindings
  match_side_id (PK/FK), participant_id, submission_id,
  submission_version_id, artifact_sha256

house_ai_match_side_bindings
  match_side_id (PK/FK), house_ai_id, house_ai_version,
  artifact_sha256
```

`kind` is exactly `PARTICIPANT` or `HOUSE_AI`. Deferred constraint triggers
(or an equivalent transaction-safe database constraint design) require exactly
one matching child row for each side and prohibit the other child row. Identity
is inserted as part of the same transaction as the side. This avoids nullable
subject columns and makes the subject's foreign keys explicit. PostgreSQL is the
production authority; ORM guards mirror these rules for portable unit tests.

Common protocol version and runtime profile remain pinned on `Match` and must
match each subject version. Rules version and policy identity remain match-level
immutable configuration; the House AI version also pins its compatible values.
The artifact digest remains on each typed identity binding and is checked
against its immutable artifact version at side creation and execution staging.

Side composition is purpose constrained:

- `DEVELOPMENT`: exactly one `PARTICIPANT` and one `HOUSE_AI`.
- `OFFICIAL`: exactly two distinct `PARTICIPANT` sides, each bound to an
  eligible frozen official version when official tournament creation is later
  implemented.
- Existing participant-versus-participant records retain their current meaning;
  adding purpose must not imply that old records are official tournament
  records. Backfill legacy rows to an explicit compatibility purpose such as
  `PEER_TEST` and prohibit that purpose from being exposed as official.

Purpose belongs on `Match` (or a required one-to-one identity row if deployment
compatibility makes the column unsafe). It must be immutable with match
configuration. Development and official result pipelines remain distinct.

## 4. Proposed execution model

Do not pretend a House AI is a participant. Generalize the existing worker job
as a discriminated execution subject while preserving one lease/fence/readiness
authority:

- Common job identity: `execution_id`, match/side, `execution_kind`, worker,
  lease, fencing token, expiry, status, sequence, protocol, runtime identity,
  and evidence.
- `PARTICIPANT` subject: exact participant, submission, submission version,
  digest, and participant artifact capability.
- `HOUSE_AI` subject: exact House AI ID/version, digest, and House AI artifact
  capability.
- Database checks and composite foreign keys must enforce that exactly one
  subject is present and agrees with `match_sides.kind`. Every job is assigned
  to an active worker under the existing mTLS identity, lease, fence, sequence,
  expiry, and revocation rules.

Before schema implementation, compare a single discriminated job with typed
subject child tables. Prefer typed child tables if they can enforce exact-one
subject and digest binding without a proliferation of nullable columns. Preserve
one common execution/job ID and one lease/fencing implementation; do not build
independent worker control planes.

Generalize `MatchExecutionBinding` to bind a side to the common execution ID,
worker lease/fence/attempt and digest, while the typed execution subject proves
participant-version or House-AI-version binding. A participant execution must
never satisfy a House AI side, or vice versa. Readiness is accepted only from
the current acknowledged job with the same typed subject, live lease and fence,
matching worker certificate identity, verified staged artifact digest, and
durable evidence. Recheck all of these at every move and result commit.

Participant artifact capabilities stay scoped to a participant version. House
AI capabilities use an organizer-only House AI artifact scope; they must not be
minted by participant credentials or resolve through participant quarantine.
Capability redemption is one-use/short-lived, digest/version/execution/worker/
lease/fence-bound, and audited under the existing worker protocol.

## 5. House AI identity and artifact

Use separate organizer-owned tables, not `SubmissionVersion` or participant
quarantine:

```text
house_ai_versions
  house_ai_id, version, artifact_sha256, artifact_size,
  protocol_version, runtime_profile, runtime_identity_sha256,
  rules_version, policy_version, created_at, frozen_at

house_ai_artifacts
  house_ai_id, version, immutable storage reference, exact bytes or
  content-addressed object metadata, digest, size
```

The composite `(house_ai_id, version)` is the immutable version identity.
`runtime_identity_sha256` pins the organizer runtime/build identity separately
from the profile name; the policy version/digest identifies behavior. Foreign
keys bind each artifact record, match-side binding and execution target to that
version. Digests use lowercase SHA-256 hex and are verified over the exact bytes
when registered, staged and consumed. Update means create a new version; never
mutate a referenced version. Freeze is a one-way audited action before any match
uses the version.

The organizer artifact store must be separate from the untrusted participant
quarantine store. No participant download endpoint is added. Do not choose a
House AI algorithm, strength, side/color, runtime image or scoring policy in
this design; those values require their own approved policy and reproducible
build process.

## 6. Database and migration plan

Create a new forward-only migration after `0007_event_mechanics`; do not edit
0006/0007 or rewrite deployed history.

1. Create immutable House AI version/artifact tables and indexes/foreign keys.
2. Create the typed identity child tables for participant and House AI sides.
3. Add side kind and match purpose with safe defaults for legacy records; backfill
   existing sides as participant and existing match purpose as a non-official
   compatibility value. Verify row counts and binding joins before constraints.
4. Move participant identity fields out of the common side table only after
   verified backfill. Add non-null/common fields and composite ownership/digest
   foreign keys to the participant child table.
5. Replace the insert/update/readiness triggers with kind-aware triggers. Enforce
   exactly two colors, exactly one subject child per side, valid purpose/side
   composition, eligible exact participant versions, frozen exact House AI
   versions, immutable side and typed bindings, and valid digest/runtime/protocol
   agreement.
6. Preserve terminal match immutability and existing result-state constraints.
   Add no trigger path that lets an execution report set terminal chess results.
7. Keep existing peer-match behavior covered and migrate only the test/deployment
   disposable databases until rollback/upgrade results are accepted. Downgrade
   may remove new records only if no generalized side has been created; otherwise
   fail closed and require forward recovery rather than deleting evidence.

The migration must be exercised against disposable PostgreSQL. SQLite tests
cannot establish trigger correctness. Never use the event database for this
verification.

## 7. Match Controller changes

Keep one controller, board, clock and transcript. Replace participant-only
`MatchSideIdentity` with a discriminated immutable identity (`kind` plus typed
participant or House AI subject). Exactly one identity must be present. Replace
the assumption that the two sides have different participant IDs with the
invariant that their typed actor identities differ and agree with the persisted
side rows.

Keep binding checks generic at controller entry but require the binding subject
to equal the side subject. Sequence, request ID, monotonic deadline, FEN/history,
side-to-move, move legality, terminal outcome and failure handling remain
unchanged. Persist/replay must verify each move’s exact side and execution
binding, including kind and typed artifact digest. No controller code may infer
identity from a client message.

`mark_match_running()` must require readiness from both differently typed
execution jobs. Failure or cleanup failure aborts without a chess result.
Development matches store only controller-derived `MatchOutcome` and
`MatchResultReason`; official scoring remains separate and unimplemented.

## 8. AIK protocol implications

Use the existing AIK JSONL/1 hello, ready, new-game, position, ack and move
proposal messages for both side kinds. The protocol does not need to know
whether the proposer is a participant or House AI. The gateway/supervisor
attaches authenticated execution identity out of band; a message cannot claim
its actor, opponent, result, digest or privileges. The controller validates all
messages identically. Only introduce a protocol revision if an actual required
capability cannot be expressed by the existing contract, with its own frozen
contract decision and compatibility tests.

## 9. Worker, sandbox, and platform boundary

Both kinds run through authorized worker execution and AIK transport. Participant
execution uses the exact eligible participant artifact inside the separately
accepted sandbox. House AI execution uses its organizer-owned artifact and a
separately scoped capability, on an approved worker runtime. A trusted organizer
artifact does not justify bypassing lease/fencing, artifact digest checks, output
bounds, cleanup evidence, or the controller.

Windows-testable with fakes: typed identity validation, DB/service invariants
where the test DB supports them, idempotency, message encoding/decoding,
controller legal/illegal proposals, authorization filtering, stale binding
rejection, migration SQL generation, and UI rendering from actual API fixtures.
These tests do not prove process isolation.

Linux required: real worker process, control-plane mTLS, exact-byte staging,
process lifecycle, deadlines, signal/process-tree cleanup, resource and I/O
bounds, no-network/no-host-filesystem enforcement, and worker fencing during
real execution. KVM/Kata-specific claims require a suitable Linux host with
hardware virtualization and the chosen Kata runtime/guest kernel; verify guest
isolation, device/mount/network restrictions, resource enforcement, crash and
cleanup behavior, and escape-resistance tests. Do not claim a participant process
has executed securely until these pass. Current Windows restrictions and absent
verified Linux/KVM/Kata environment block these runtime claims.

## 10. Development-match lifecycle

1. Authenticated participant or trusted orchestrator requests an attempt for
   that participant’s exact submission version.
2. Lock and verify ownership, `ELIGIBLE` state, exact participant artifact bytes
   and digest; reject deprecated, revoked, rejected, quarantined or other
   ineligible versions.
3. Select the single event-approved frozen House AI version. Participants cannot
   choose or mutate it.
4. Create immutable `Match` purpose `DEVELOPMENT`, server seed and configuration,
   exact participant and House AI side bindings, and a durable outbox event in
   one transaction. This creates `QUEUED`, never `RUNNING` or `COMPLETED`.
5. Create typed execution jobs through the authorized worker control plane.
   Require exact artifact capability, worker assignment, lease/fence and
   acknowledged job for each side.
6. Stage/verify both artifacts and collect readiness evidence. Only then may
   the existing controller enter `RUNNING`.
7. Controller processes AIK messages and persists accepted transcript and
   terminal result/evidence. Results are development-only.
8. Participant-owned API exposes the persisted state. New submission versions
   receive separate match attempts and never rewrite old matches.

## 11. Idempotency

Every authenticated creation request carries a client idempotency key (an
orchestrator may generate a random UUID). The unique request identity is
`(authenticated_participant_id, idempotency_key)`; the server stores a canonical
request hash committing exact submission/version/digest, purpose and
server-selected House AI version/configuration. Same key and same request returns
the existing match. Same key with any different field is a conflict. The key
does not choose the opponent, seed, or any policy. A deliberate new attempt uses
a new key; whether multiple attempts for one exact participant version are
permitted remains an organizer policy decision. Do not treat an HTTP retry as a
new attempt or use artifact digest alone as the attempt identity. The current
repository has no persisted event identity/configuration row, so this key is
scoped to the authenticated participant rather than an invented `event_id`.

## 12. Future participant API and portal contract

Minimum authenticated contracts (names are proposals, not implemented):

- `POST /api/v1/participants/me/development-matches` with exact owned
  `submission_id`/`version` and an idempotency key. The server chooses the House
  AI and returns the durable match identity/state.
- `GET /api/v1/participants/me/development-matches` returns only matches whose
  participant side belongs to the authenticated participant.
- Each response carries `match_id`, `purpose`, status, submission version,
  House AI ID/version, protocol/runtime/rules identity, server timestamps, and
  terminal outcome/reason only after a persisted terminal match state. Abort
  returns failure classification without a chess outcome.

Return queued/starting/running/completed/aborted only from `matches.state`.
No percentage, predicted duration, opponent statistics or result before
terminal evidence. Cross-participant IDs return not-found without revealing
whether another participant’s match exists. Official tournament APIs must query
only official purpose and frozen versions; development outcomes never enter
standings automatically.

## 13. Security invariants

- A side kind and exactly one typed identity are database-enforced; no synthetic
  participant identity is allowed for House AI.
- Match-side and artifact version/digest/runtime/protocol/rules/policy bindings
  are immutable and verified over exact bytes at staging.
- Participant auth can request only its own exact eligible submission. It cannot
  choose/alter House AI, worker, execution kind, seed policy or result.
- Execution kind and typed target agree with side kind; no participant capability
  can retrieve House AI bytes and no House AI capability can retrieve participant
  quarantine bytes.
- Worker certificate identity, active status, lease, fencing token, attempt,
  sequence, expiry, artifact digest and readiness are revalidated. Stale workers
  cannot submit moves or readiness.
- Only Match Controller persists legal moves, clocks, board state and terminal
  chess outcomes. Workers/agents cannot submit a result.
- Development match purpose cannot be converted to official; official matches
  require frozen eligible versions. Development scores/results do not feed
  official scores or seeding.
- Participant code receives no control-plane credentials, Docker socket, database
  or Redis credentials, host mounts or network access; execution is denied until
  the approved sandbox is available. No direct subprocess path is allowed.
- Cleanup failure cannot be reported as successful completion. Fail closed on
  malformed identity, missing evidence, digest mismatch, stale fence, or
  transaction/audit failure.

## 14. Backward compatibility

Preserve the existing `create_match()` contract for participant-versus-participant
test/legacy matches or adapt it through an explicit wrapper that creates two
`PARTICIPANT` typed sides. Keep existing controller transition, AIK, chess,
clock, transcript and worker fencing tests. Backfill every current side to
`PARTICIPANT`; do not infer House AI from a reserved participant ID. Existing
match records must not become official tournament matches merely by migration.

## 15. Implementation sequence

1. Approve this subject/identity and execution-subtype design plus purpose and
   retry policy. Record House AI runtime/policy identity and artifact storage
   owner requirements.
2. Implement the forward schema migration, typed immutable models, backfill and
   database triggers. Verify upgrade and guards against disposable PostgreSQL.
3. Generalize domain identities and Match Controller binding checks while
   preserving all existing participant-versus-participant behavior.
4. Generalize worker job/capability target under the same lease/fence control
   plane; add exact House AI artifact storage and registration/freeze service.
5. Add a development-match creation service and idempotency/event transaction;
   queue only and prove ineligible submissions are rejected.
6. Add authenticated participant match API and portal rendering from persisted
   match state only.
7. Exercise protocol with controlled inert test agents and server chess rules;
   label these as fake-adapter evidence only.
8. Verify actual worker/TLS/sandbox path on real Linux, then KVM/Kata-specific
   isolation. Only after acceptance may participant artifacts be run.
9. Separately implement organizer House AI runtime and event-approved development
   match operational policy. Official tournament/scoring/seeding remains a later
   milestone.

## 16. Required infrastructure

No Windows-only test can verify hostile-code isolation. Real Linux is required
for worker TLS and actual process/sandbox integration. KVM/Kata validation needs
a Linux machine with enabled hardware virtualization, a supported Kata runtime,
guest kernel/image, resource/network/storage controls, and disposable PostgreSQL
and worker infrastructure. Do not change Docker/WSL security settings to
manufacture that prerequisite. The environment must remain separate from event
PostgreSQL/Redis and use temporary credentials/artifacts.

## 17. Explicit non-goals

This design implements no House AI code or artifact, no match queue, no execution,
no migration, no API/portal feature, no match result, no scoring/seeding,
no official tournament, no leaderboard or spectator mode, and no sandbox/Kata
implementation. It does not certify the current worker/TLS stack as operational.

## 18. Acceptance criteria for the next implementation milestone

- Migration upgrades cleanly from 0007 on disposable PostgreSQL and preserves
  existing match rows and constraints.
- Database rejects missing, duplicate, or cross-kind side identity and all
  participant/House AI artifact/version/digest mismatches.
- Legacy participant-versus-participant match creation, execution binding,
  readiness, transcript replay and tests remain valid.
- Match purpose is immutable and side composition is enforced per purpose.
- House AI version/artifact identity is immutable and organizer-controlled.
- Generic execution job cannot be assigned a subject inconsistent with side kind;
  lease/fence/readiness protections remain effective.
- Same idempotency key returns the exact existing queued attempt and conflicting
  reuse fails; no duplicate match/outbox events are created.
- No match leaves QUEUED before both typed executions are verified ready.
- Controller protocol and result tests use the existing AIK and chess rules ports;
  fake evidence is labeled and never claimed as runtime isolation.
- Authenticated API cannot access another participant’s match; portal displays
  only persisted state and terminal results.
- Development results cannot enter official results, scores, or standings.
- Direct participant execution is absent; real execution remains blocked until
  Linux and KVM/Kata acceptance criteria are verified.
- Backend, frontend/API, compile, syntax, targeted lint, Alembic and disposable
  PostgreSQL checks pass before any implementation milestone is accepted.
