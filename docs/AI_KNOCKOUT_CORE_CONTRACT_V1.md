# AI Knockout Core Competition Contract — V1 Freeze

**Status:** Proposed V1 engineering contract. Values marked OPEN POLICY DECISION require organizer approval before implementation or event use.  
**Purpose:** Freeze compatible contracts for participant packages, the engine protocol, and execution isolation before building competition features.

This is a specification, not a description of existing functionality. MUST/MUST NOT/SHOULD/MAY refer to future implementation obligations.

## 1. Purpose and scope

This contract governs the first complete-match milestone: two independent participant AI packages play one complete, reproducible, server-authoritative standard chess game. It defines package intake, immutable identity, validation states, process communication, chess state, execution isolation, failure semantics, match binding, future worker communication, logical data models and event records.

This freeze does NOT implement an upload API, artifact storage, validation service, sandbox runtime, chess runtime, match controller, tournament engine, worker service, leaderboard, spectator system, admin control center or public deployment. Existing authentication and identity behavior is outside scope and MUST remain unchanged.

## 2. Current repository boundary

The source inspection uses the project as authoritative. A preceding forensic audit is available in conversation history; no forensic-audit document was found in the repository.

| Current source capability | Evidence |
|---|---|
| FastAPI app, health/readiness/version and static portal | backend/app/main.py; backend/app/api/v1/health.py |
| PostgreSQL connectivity and SQLAlchemy sessions | backend/app/db/session.py |
| Redis connectivity and rate-limit use | backend/app/services/redis.py; backend/app/services/rate_limiter.py |
| Participant registration/profile/auth/sessions | backend/app/api/v1/participants.py; backend/app/api/v1/auth.py; backend/app/services/auth.py; migration 0002_phase2_identity_auth |
| Current Compose services | api, postgres, redis in compose.yml |
| Current tests | backend unit/integration tests and static frontend Node tests |

No source route, model, migration, service, worker or test implementing artifact submission, validation, sandbox execution, chess rules, AI protocol, match control/persistence, tournament, workers, leaderboard, live spectator or competition administration was found. Existing auth_events are identity/security audit records, not competition events. The portal’s non-identity sections are unavailable states, not backend features. The preceding audit could not independently verify Docker container health; this document is not runtime evidence.

## 3. Architectural principles and trust boundaries

### 3.1 Server authority

The server is authoritative. It owns the board, side to move, legal-move determination, clocks, match lifecycle, opponent and pinned artifact identities, execution policy, game result, tournament state, score and official events. The participant controls only its algorithm and proposed move.

The AI MUST NOT set or mutate board state, declare a move legal, change a clock, choose its opponent, choose the artifact bound to a match, set the result, or change tournament state or score. An AI response is an untrusted proposal. Only the control plane can accept a move, apply it to its board, persist it and advance the game.

### 3.2 Components and trust

~~~text
Participant browser
  └── authenticated control-plane API (existing identity boundary)
        ├── submission/validation services (future; quarantine is non-executable)
        ├── PostgreSQL (authoritative records, board transitions, event outbox)
        ├── Redis (cache/dispatch hint only; never official state)
        └── authenticated worker control channel (future)
              └── worker agent (trusted, least privilege; not match authority)
                    └── disposable isolated runtime
                          ├── White AI process (stdin/stdout protocol only)
                          └── Black AI process (stdin/stdout protocol only)
~~~

The controller validates every worker-reported proposal against its own board and clock before the worker supplies the resulting state to the next engine turn. Workers are execution nodes, not rules, clock, result or tournament authorities. PostgreSQL is the durable source of official state; Redis delivery is not proof that an event committed. Future spectator delivery must replay committed events.

Participant code is hostile, including code that passes package checks. Workers are trusted infrastructure in the V1 threat model, but not authoritative for game rules. A compromised worker can fabricate plausible legal engine output; control-plane validation cannot prove that a particular binary produced a move. Mitigating malicious workers requires further trusted execution/attestation and remains a residual risk, not a property supplied by Docker.

### 3.3 Responsibilities

Participants provide a self-contained package, valid manifest and process that follows the protocol and returns a move proposal within server-issued limits. They must not include server configuration, network access, Docker configuration, secrets, control-plane credentials or host data.

The control plane authenticates ownership, freezes package identity, validates policy, selects runtime/resource policy, assigns a worker, pins artifacts to a match, supplies state, measures authoritative time, validates/applies moves, persists transitions/results/events and decides termination. Workers verify hashes and enforce supplied execution policy. The sandbox receives no database, Redis, participant-session or control-plane credentials.

## 4. Contract A — package format

### 4.1 V1 choice and layout

V1 uses a canonical, uncompressed POSIX USTAR archive (.tar), not ZIP, compressed tar, container image or participant-built image. USTAR avoids decompression bombs and permits canonical headers. ZIP is common but has more path/metadata/symlink and decompression edge cases; compressed tar retains decompression-bomb risk; participant images conflict with server-owned runtime policy. USTAR is still hostile input and must be parsed with bounded hardened code. The server rejects rather than repairs a non-canonical archive.

The archive root is the package root; there is no enclosing AIK_submission directory:

~~~text
manifest.json                 required
engine/
  main.py                     required default entrypoint
  *.py                        optional Python modules; nested modules allowed
README.md                     optional plain UTF-8 documentation
~~~

No directory entries are accepted; directories are inferred from paths. Only regular files are allowed. Paths use / and ASCII letters/digits/underscore/dot/hyphen; no leading slash, drive prefix, backslash, empty, . or .. component; path length is at most 255 bytes, basename at most 100 bytes and USTAR prefix at most 155 bytes; paths are unique case-insensitively. **V1 deliberately permits ASCII member paths only** to avoid Unicode normalization and canonical-equivalence ambiguity in archive identity. Raw path bytes are UTF-8-decoded and UTF-8 byte limits are checked, then the ASCII allowlist is applied; any non-ASCII path is rejected. No Unicode normalization or Unicode-equivalence handling is performed. Entrypoint MUST be a .py file below engine/. Package MUST contain root manifest.json and at least one Python source file.

### 4.2 Canonical archive rules

1. USTAR headers only; no PAX/GNU extensions, sparse files, links, devices, FIFOs or sockets.
2. Regular-file paths sorted lexicographically by ASCII bytes. Each path appears once.
3. UID/GID, device numbers and mtime are zero; owner/group names empty; mode 0644; no executable bits; regular-file type; unused header fields and block padding zero. USTAR magic/version bytes use the POSIX USTAR values. Numeric fields use one fixed octal encoding with leading zeroes and NUL termination; checksum is computed with its field filled by spaces, then encoded as six octal digits, NUL and space.
4. Exactly two zero end blocks follow the last member. No trailing bytes.
5. Manifest and README are UTF-8 without BOM. Python source is UTF-8 without BOM.
6. Reject duplicate/case-colliding paths, malformed headers, numeric overflow, unexpected file types, invalid encoding, excessive sizes/counts and path escape. Validate the full table of contents and limits before extracting any member.

**V1 canonical USTAR path split clarification:** Encode the canonical POSIX path as UTF-8 before measuring bytes. The path character allowlist is deliberately ASCII-only under V1; no Unicode normalization or Unicode-equivalence handling is part of V1, and any non-ASCII path is rejected after byte accounting. If the complete path is at most 100 bytes, store all bytes in `name` and leave `prefix` empty. A path longer than 100 bytes MUST split at the **rightmost** `/` for which the bytes before it are at most 155 and the bytes after it are at most 100; store those sides in `prefix` and `name` respectively, with the separator in neither field. If no such separator exists, reject the member. A path that fits in `name` MUST NOT use `prefix`; a longer path MUST use exactly the rightmost valid split. Different splits for the same logical path are different, non-canonical archive encodings and are rejected. UTF-8 byte lengths, not character counts, govern all field and total-path bounds. PAX, GNU long-name and other alternate long-path encodings are rejected. Never normalize/repack an uploaded archive and accept the rewritten bytes.

### 4.3 File and dependency policy

V1 permits only manifest.json, optional README.md and .py files under engine/. Native libraries, bytecode, executables, arbitrary data/model/book files, package managers, requirements files, wheels, vendored dependencies and dynamic dependency installation are prohibited. Python standard library only; runtime is server-supplied and pinned. This reduces supply-chain ambiguity but constrains engines.

**OPEN POLICY DECISION:** Whether V1 needs approved pure-Python third-party dependencies or opening-book/data files. Any dependency requires a server-owned, version-pinned allowlist/runtime profile; participants cannot choose versions or fetch packages during a match. Source-encoded opening logic is allowed; external book files are not.

### 4.4 Initial bounds

Limits are server-configured and enforced during intake, before extraction or execution.

| Limit | Provisional technical default | Reason and policy status |
|---|---:|---|
| Raw archive bytes | 16 MiB | Bounds upload, hashing and quarantine storage; PROVISIONAL, organizers approve final cap |
| Members including manifest/README | 128 | Bounds parser work/filesystem objects; PROVISIONAL |
| One member | 4 MiB | Bounds source/document parsing; PROVISIONAL |
| Total regular-file bytes | 16 MiB | Uncompressed archive cannot inflate beyond accepted member bytes; PROVISIONAL |
| Manifest | 16 KiB | Bounds JSON parser input; PROVISIONAL |

The published event maximum and technical ceiling must agree. An incomplete/rejected upload is not an accepted version. None of these values is final event policy.

## 5. Manifest contract

manifest.json is a UTF-8 JSON object at archive root. V1 requires exactly the listed fields; unknown fields are rejected. It describes identity/compatibility only and cannot override server/event policy.

~~~json
{
  "contract_version": "1",
  "protocol_version": "aik-jsonl/1",
  "engine_name": "Example Engine",
  "engine_version": "1.0.0",
  "runtime": "python-3.12",
  "entrypoint": "engine/main.py"
}
~~~

Strict JSON Schema (Draft 2020-12):

~~~json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "$id": "https://aik.invalid/contracts/manifest-v1.schema.json",
  "title": "AI Knockout submission manifest V1",
  "type": "object",
  "additionalProperties": false,
  "required": ["contract_version", "protocol_version", "engine_name", "engine_version", "runtime", "entrypoint"],
  "properties": {
    "contract_version": {"const": "1"},
    "protocol_version": {"const": "aik-jsonl/1"},
    "engine_name": {
      "type": "string", "minLength": 1, "maxLength": 64,
      "pattern": "^[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}$"
    },
    "engine_version": {
      "type": "string", "minLength": 1, "maxLength": 64,
      "pattern": "^(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)\\.(0|[1-9][0-9]*)(-[A-Za-z0-9.-]+)?$"
    },
    "runtime": {"const": "python-3.12"},
    "entrypoint": {
      "type": "string", "minLength": 10, "maxLength": 255,
      "pattern": "^engine/[A-Za-z0-9_./-]+\\.py$"
    }
  }
}
~~~

Implementations additionally reject duplicate JSON keys, non-finite numbers, invalid UTF-8, BOM and invalid semantic paths. Check that entrypoint exists as a regular member below engine/ and contains no dot/dot-dot segments. Engine name/version are display metadata, not authenticated identity. Runtime/protocol/contract compatibility is exact-match. No CPU, memory, timeout, network, filesystem, privilege, Docker or environment fields are legal.

## 6. Contract A — artifact identity and lifecycle

### 6.1 Identity, hashing and storage

Each accepted version is identified by:

~~~text
participant_id + submission_id + submission_version + artifact_sha256
~~~

submission_id is a server-generated opaque UUID; submission_version is a server-assigned positive integer starting at 1 and increasing within the submission. Ownership comes from the authenticated session, never an upload-body participant ID.

artifact_sha256 is lowercase hexadecimal SHA-256 over the exact complete canonical USTAR bytes received, including headers, content, padding and end blocks. Hash while streaming into a newly created quarantine object, enforce raw byte limit, verify upload completion, then validate archive. Never hash extracted/repacked contents instead of received bytes. Canonical rules make byte-identical packages reproducible; different bytes are different artifact identities.

Storage SHOULD be content addressed, e.g. sha256/ab/cd/<full-digest>, with no participant ID in object path. Metadata carries ownership. Object creation is conditional/write-once; any reused object is re-hashed. Store raw archive bytes, manifest snapshot, size, contract/runtime/protocol, creation time and validation policy version. Workers receive an exact-digest read-only object/short-lived scoped handle before sandbox launch; no participant/session credential is included.

Storage lifecycle: stream to a newly allocated non-executable temporary object; after upload completion and digest computation, atomically commit it to quarantine and create metadata. Validators get read-only access. Approval changes metadata, not bytes or object key. Workers stage only a verified exact-digest object into a fresh read-only mount. Destroy temporary upload/staging copies after cleanup; retain the canonical blob while submission, match, appeal or audit retention references it. Rejected objects remain quarantined and inaccessible to execution until explicit retention expiry. Retention duration and secure deletion are OPEN POLICY/INFRASTRUCTURE DECISIONS; deletion cannot leave dangling match references.

**OPEN POLICY/INFRASTRUCTURE DECISION:** Choose actual storage backend and encryption, retention, replication and backup. The logical content-addressed/write-once interface is mandatory regardless of backend.

### 6.2 Duplicate and replacement behavior

- Same participant + same submission_id + same digest + same contract is idempotent: refer to existing version; do not create a version or reset rejection. Revalidation is a separately recorded validation attempt.
- Identical bytes from another participant may reuse the physical blob but MUST create ownership-scoped metadata and MUST NOT disclose another participant’s existence.
- New digest creates a new immutable version; it cannot overwrite an archive or change an old match.
- Current eligible version remains eligible while a replacement is quarantined/validated. Participant explicitly activates a newly approved version; activation deprecates the old version for new matches only. Existing match pins persist.
- Participants cannot delete/rewrite accepted version metadata. Retention/deletion is an operator policy action and cannot erase match/audit references.

### 6.3 State machine

UPLOADED is a request phase, not a runnable persisted state. A complete raw object is first durably stored in quarantine; it is never executable because it uploaded.

~~~mermaid
stateDiagram-v2
  [*] --> QUARANTINED: complete bytes durably stored
  QUARANTINED --> INSPECTING: validator claims version
  INSPECTING --> VALIDATING: canonical archive accepted
  INSPECTING --> REJECTED: permanent archive/policy violation
  INSPECTING --> RETRYABLE_FAILURE: validator infrastructure fault
  VALIDATING --> APPROVED: manifest and static checks pass
  VALIDATING --> REJECTED: permanent contract/compatibility failure
  VALIDATING --> RETRYABLE_FAILURE: validator infrastructure fault
  RETRYABLE_FAILURE --> INSPECTING: bounded retry of same digest
  APPROVED --> ELIGIBLE: authorized activation
  ELIGIBLE --> APPROVED: withdrawn before match pin
  ELIGIBLE --> DEPRECATED: replacement activated or entry closed
  APPROVED --> DEPRECATED: owner/competition retires version
  QUARANTINED --> REVOKED: security/operator action
  INSPECTING --> REVOKED: security/operator action
  VALIDATING --> REVOKED: security/operator action
  APPROVED --> REVOKED: security/operator action
  ELIGIBLE --> REVOKED: security/operator action
  RETRYABLE_FAILURE --> REVOKED: security/operator action
  DEPRECATED --> REVOKED: security/operator action
~~~

| State | Meaning and match eligibility | Entry/failure/retry |
|---|---|---|
| QUARANTINED | Complete immutable bytes stored; not schedulable | Enter after durable commit. Storage failure means no accepted version; retry idempotently. |
| INSPECTING | Bounded archive/path/type/size inspection; not schedulable | Validator claims with lease. Permanent content defect -> REJECTED; infrastructure fault -> RETRYABLE_FAILURE. |
| VALIDATING | Manifest, compatibility and static checks; no participant-code execution; not schedulable | Records exact digest and validator/policy version. Permanent failure -> REJECTED; transient failure -> RETRYABLE_FAILURE. |
| APPROVED | Automated checks passed; not yet selected; not schedulable | Authorized workflow may activate. |
| ELIGIBLE | Approved and selected; can be bound to new match after dispatch rechecks | One active version per entry. Replacement is inactive until approved and activated. |
| REJECTED | Permanent contract/content failure; never schedulable | Terminal for this version; correction requires a new version. |
| RETRYABLE_FAILURE | Validator infrastructure failed; not a participant rejection; not schedulable | Retry same digest with bounded attempts/idempotency. Exhaustion needs operator review, never silent approval. |
| DEPRECATED | Historical only; not eligible for new matches | Terminal for new scheduling; old match references remain resolvable. |
| REVOKED | Security/operator block; not schedulable | Terminal. Blocks future runs and stops active runs under security-abort policy. |

State changes are append-recorded with actor, old/new state, reason, policy/version, timestamp and correlation ID. Validation MUST NOT execute participant code. Any later smoke execution is a separate execution record inside the sandbox.

## 7. Contract B — AI engine protocol

### 7.1 Choice

V1 uses AIK JSON Lines 1 (aik-jsonl/1) over stdin/stdout pipes, not UCI wire format. No UCI text commands/responses are accepted, including uci, isready, ucinewgame, position, go, info, bestmove, stop, ponderhit, setoption or quit. Their needed lifecycle concepts map to hello/ready, new_game, position, go/bestmove and shutdown JSON messages. UCI coordinate move notation is reused within strict bounded JSON messages. Asynchronous info streams and engine options are intentionally excluded. There is no network socket between controller and engine.

The process has no terminal. stdin/stdout/stderr are separate pipes. Encoding is strict UTF-8; one JSON object per LF-terminated line (CRLF may be normalized on input). One request is outstanding at a time. Each request has a server-generated request_id echoed by response. Reject duplicate JSON keys, non-finite values, malformed encoding, extra response lines, unexpected types/fields and wrong request IDs. Flush each response. Stdout is protocol only; no debug logging.

### 7.2 Lifecycle and messages

~~~text
server -> hello(protocol, match, side, runtime)       engine -> ready
server -> new_game(color, variant, start, seed, clocks) engine -> ack
server -> position(fen, uci_history, side, clocks)     engine -> ack
server -> go(request_id, deadline, clocks)              engine -> bestmove
  [optional claim_draw -> claim_rejected -> bestmove, same turn/deadline]
server -> position -> ack -> go -> bestmove ...
server -> game_over(outcome, reason, final_fen)         engine -> ack
server -> shutdown                                      process exits
~~~

Successful engine response types are ready, ack and bestmove. Each includes protocol_version aik-jsonl/1 and request ID. On a turn, the engine may instead send the optional claim_draw request described below before returning bestmove. bestmove has one move property: four-character UCI coordinate move (e2e4) or five with promotion suffix q/r/b/n (e7e8q). No SAN, pass/null move, 0000, engine-selected result or clock adjustment.

Exact V1 message envelopes (all message types use exactly these properties; IDs are opaque server-generated strings; clock values are non-negative integer milliseconds; seed is 64 lowercase hex characters). Angle-bracket text is a schematic value placeholder; numerical examples are illustrative only, not event time-control decisions.

~~~json
{"protocol_version":"aik-jsonl/1","type":"hello","request_id":"r1","match_id":"<uuid>","side":"BLACK","runtime":"python-3.12"}
{"protocol_version":"aik-jsonl/1","type":"ready","request_id":"r1"}
{"protocol_version":"aik-jsonl/1","type":"new_game","request_id":"r2","side":"BLACK","variant":"standard","start_fen":"<server FEN>","seed":"<64 hex characters>","clock":{"initial_ms":300000,"increment_ms":0}}
{"protocol_version":"aik-jsonl/1","type":"ack","request_id":"r2"}
{"protocol_version":"aik-jsonl/1","type":"position","request_id":"r3","fen":"<server FEN>","uci_moves":["e2e4"],"side_to_move":"BLACK","clocks_ms":{"white":295000,"black":300000}}
{"protocol_version":"aik-jsonl/1","type":"ack","request_id":"r3"}
{"protocol_version":"aik-jsonl/1","type":"go","request_id":"r4","move_budget_ms":1000,"clocks_ms":{"white":295000,"black":300000}}
{"protocol_version":"aik-jsonl/1","type":"bestmove","request_id":"r4","move":"e7e5"}
{"protocol_version":"aik-jsonl/1","type":"claim_draw","request_id":"r4","reason":"threefold_repetition","intended_move":null}
{"protocol_version":"aik-jsonl/1","type":"claim_rejected","request_id":"r4","reason":"claim_not_available"}
{"protocol_version":"aik-jsonl/1","type":"game_over","request_id":"r5","outcome":"DRAW","termination_reason":"FIVEFOLD_REPETITION","final_fen":"<server FEN>"}
{"protocol_version":"aik-jsonl/1","type":"ack","request_id":"r5"}
{"protocol_version":"aik-jsonl/1","type":"shutdown","request_id":"r6"}
~~~

For claim_draw, intended_move is null or a valid UCI move proposed as the move that would enable a claim. The server verifies claim grounds and that move. claim_rejected is a server message; it does not reset the clock. Unknown server messages/fields are fatal to an engine; unknown engine response types/fields are rejected.

- hello carries compatibility and non-secret match context. Engine answers ready after initialization. Manifest is trusted display identity; self-identification is not authority.
- new_game carries side, standard variant, initial FEN, persisted match seed and server time-control configuration. Engine answers ack.
- position carries full current FEN, full UCI move history, authoritative side-to-move and both remaining clock values. Engine answers ack.
- go starts one move request with request ID and server deadline/budget. Engine returns bestmove or a draw claim.
- claim_draw MAY request threefold_repetition or fifty_move_rule. Server verifies independently. Valid claim ends draw. Invalid claim gets claim_rejected; same turn/clock continues, then engine must return bestmove before same deadline. Repeated claims on that turn are protocol violations.
- game_over carries server-determined outcome/reason/final FEN; engine answers ack.
- shutdown requires prompt exit; no response needed.

V1 has no arbitrary engine options, runtime paths, dependency installation, participant-provided timeout, debug command or opponent identity. Debug diagnostics MAY go to stderr only; stderr is bounded, private and never sent to opponent/public events. Engine identity comes from manifest. Extra stdout, unsolicited lines, malformed JSON, unknown response/field, wrong ID or extra bestmove is INVALID_PROTOCOL.

### 7.3 Timeouts and I/O

Deadlines use monotonic trusted infrastructure clocks, never engine timestamps. Startup/readiness/per-move/total-game/shutdown deadlines and stdout/stderr/line limits are configurable and recorded in execution policy. Partial lines do not stop the timer. A response counts only when a complete bounded LF line is read. After deadline, ignore late bytes and kill the process tree/container. Drain stdout and stderr concurrently to prevent pipe deadlocks. Output-limit, malformed framing or hang terminates the process. Unknown server command/version is fatal; engine must not guess.

## 8. Chess state and move validation

V1 is standard chess, not Chess960. Server uses a version-pinned rules engine and owns initial position, board, history, FEN, side, counters, clocks and outcome. AI receives current FEN and all UCI moves from initial position and returns one proposed UCI move.

Rules engine supports legal moves, castling, en passant, promotion, check/checkmate, stalemate, insufficient/dead material, repetition and move counters. Server adjudicates checkmate/stalemate/dead-position, automatic fivefold repetition and 75-move draw. Threefold and fifty-move claims use claim_draw and server verifies full history. A claim never creates a draw by itself.

Initial position is standard chess. Organizer-selected opening positions are an OPEN POLICY DECISION; if allowed, match config pins exact FEN/history. Participant-supplied starting position is forbidden.

Authoritative turn sequence:

~~~text
controller starts clock and issues go
 -> receives bounded response before deadline
 -> validates request ID/type/UCI syntax
 -> confirms side is server's side to move
 -> checks legality with server rules engine
 -> records elapsed time and applies move to server board
 -> atomically commits move, resulting FEN/clocks/event
 -> sends resulting position to next engine
~~~

Control plane alone applies increment and clocks. On a local worker, clock starts immediately before go is handed to the engine pipe and stops when the complete response line is received. For remote workers, the control plane starts its monotonic deadline immediately before dispatching go authorization and stops when the authenticated proposal arrives at the control plane; transport latency therefore counts. Worker monotonic time enforces an earlier local kill but is not authoritative and cannot extend the control-plane deadline. Late moves never apply; illegal moves never change board. Each accepted ply has unique (match_id, ply) and idempotency key.

## 9. Contract C — secure execution and network

### 9.1 Threat model

Assume participant code attempts data theft, sandbox escape, resource exhaustion, process spawning, service discovery, network access, protocol forgery, parser exploitation or persistence. Package inspection does not make code safe. Trusted computing base: control plane, archive parser/validator, worker agent, host kernel/hypervisor, sandbox runtime, chess rules engine and artifact store. A compromised worker can fabricate outputs; control plane still validates every move but cannot prove which process produced it.

### 9.2 Required isolation

V1 runs on dedicated Linux worker machines separate from PostgreSQL/Redis control-plane host. Each engine uses a fresh disposable sandbox, non-root identity and pinned server-owned Linux runtime image. Use a sandbox runtime such as gVisor/Kata or equivalent hardened boundary in addition to namespaces/cgroups. Docker alone is not sufficient for hostile code. Pin runtime/kernel identity and record it. Before public untrusted-code use, require adversarial escape/resource testing and independent security review.

Requirements:

- No privileged mode; no host PID/network/IPC; no host filesystem mounts; no Docker socket; drop all capabilities; no-new-privileges; restrictive seccomp plus AppArmor/SELinux.
- Read-only base and artifact; only quota-bounded private tmpfs writable; destroy after each match.
- cgroup CPU, memory, PID/process-tree limits; bounded disk/inodes/descriptors/file size; no core dumps. Kill whole tree/container on failure and confirm termination.
- Minimal environment allowlist: fixed PATH, private temp HOME, UTF-8 locale, UTC, fixed runtime settings and match seed. No inherited host variables, secrets, DB/Redis URLs, API/session/cloud credentials or Docker config.
- Runtime/dependencies server-built, pinned by digest, offline/read-only. No pip/package manager/toolchain/internet install. Server selects interpreter; manifest cannot select shell commands or host paths.
- Verify archive digest and eligibility before staging. Sandbox receives no participant identity/contact data beyond side and needed game state.

Exact VM/container orchestration and host patch/rotation process are OPEN INFRASTRUCTURE DECISIONS. Dedicated-worker separation and minimum controls above are engineering requirements.

### 9.3 Network policy

~~~text
NETWORK: DENY BY DEFAULT
~~~

Sandbox has no public internet, external/cloud AI/chess API, participant listener, host network, loopback listener, metadata service, PostgreSQL, Redis or control-plane socket. Localhost is not needed. Controller/worker/engine communication uses inherited pipes, not networking. Worker retrieves job/artifact before sandbox launch and communicates with control plane outside sandbox using worker-scoped authenticated transport. No worker credential is inherited by the engine. Enforce egress denial at namespace/firewall/runtime layers and test independently.

## 10. Resource and determinism policy

Every limit is server-selected, configurable, recorded and immutable for an execution. Technical caps protect service availability; event policy chooses actual allocation and clock rules.

| Resource | Provisional technical default | Event decision |
|---|---:|---|
| CPU | 1 vCPU-equivalent per engine | PROVISIONAL; organizers approve fair hardware-equivalent policy |
| RAM | 256 MiB per engine | PROVISIONAL; include runtime overhead |
| Processes/PIDs | 16 per engine tree | PROVISIONAL; cgroup enforced |
| Writable disk/tmpfs | 32 MiB and bounded inodes | PROVISIONAL |
| stdout | 64 KiB per line; 1 MiB cumulative | PROVISIONAL; normal protocol output is tiny |
| stderr | 256 KiB cumulative | PROVISIONAL; diagnostic only |
| Startup | 5 seconds | PROVISIONAL technical bound |
| Ready response | 2 seconds after hello | PROVISIONAL technical bound |
| Move time | No final value | OPEN POLICY DECISION |
| Total game time | No final value | OPEN POLICY DECISION |
| Graceful shutdown | 1 second then kill | PROVISIONAL cleanup bound |

CPU quota is not wall-clock. Excess output, memory/PID/disk caps or wall deadline terminate that attempt. All provisional caps require event load testing and approval; published event limits and technical ceilings must agree.

### 10.1 Reproducibility

Record exact artifact digests, manifest, contract/protocol, runtime image digest, worker/runtime/kernel, architecture, rules-engine version, match config, initial FEN/history, per-side seed, resource/clock policy, accepted move transcript, authoritative timestamps and termination reason.

V1 runtime profile is Python 3.12 on pinned Linux x86-64 image. Runtime patch/dependencies fixed by image digest; locale C.UTF-8, timezone UTC, allowlisted environment and fixed PYTHONHASHSEED. Control plane creates and persists per-side seed; compliant engines seed their PRNG from it. Randomness is not preventable: code can use OS entropy/time. Reproducibility therefore means exact inputs/policy/artifact and replayable authoritative transcript, not identical engine choices on rerun. Transcript is official record.

OPEN POLICY DECISIONS: whether randomness is permitted (recommend yes, seeded), seed visibility, other CPU architectures, opening books, time controls/increments, max match duration and alternate openings. Seed is never an authentication secret.

## 11. Failure and termination

Match states: QUEUED, STARTING, RUNNING, COMPLETED, ABORTED. Completed match has server-derived WHITE_WIN/BLACK_WIN/DRAW and reason. Infrastructure abort has no chess result until explicit retry creates a new execution attempt. Each attempt and event is idempotent.

| Failure | Detection and state | Persistence, cleanup, event |
|---|---|---|
| Start/init failure | Supervisor launch error or bounded initialization failure; side cannot act | Record PROCESS_START_FAILED or EXECUTION_ERROR, stop counterpart, destroy sandbox; classify participant vs infrastructure from evidence |
| Not ready | Monotonic deadline -> READY_TIMEOUT | Terminate tree; forfeit mapping is OPEN POLICY DECISION |
| Malformed/extra/unknown output, bad request ID | INVALID_PROTOCOL; no board change | Persist bounded diagnostic/correlation, kill, emit protocol failure |
| Illegal/wrong-side move | Server rejects; board unchanged | Record ILLEGAL_MOVE; terminate; scoring/forfeit rule OPEN POLICY |
| Move/total timeout | Server deadline; late output ignored | Persist clock/deadline, kill, emit TIMEOUT; published policy maps forfeit |
| Crash/hang | Exit/pipe closure/no response | Record exit/signal and PROCESS_CRASH/HANG; kill descendants |
| Resource/output limit | Runtime/cgroup/pipe quota | Terminate; record observation and RESOURCE_LIMIT; scoring mapping OPEN POLICY |
| Network/filesystem policy attempt | Denial; runtime audit signal if available | No access should succeed. If confirmed violation, terminate and record security event; telemetry completeness is residual risk |
| Worker loss/storage unavailable before start | Lease/fetch failure/hash check | Infrastructure abort/retry, no clock/result. Hash mismatch quarantines object and raises security event |
| Worker loss mid-match | Heartbeat/lease failure | Stop if no trustworthy continuation; ABORTED, no participant penalty absent policy |
| Suspended participant/revoked artifact | Dispatch recheck/revocation signal | Block new run; stop active run and record abort/security termination; tournament treatment OPEN POLICY |
| Output after timeout/kill | Attempt already closed | Ignore; never apply/reopen; persist late-output diagnostic |

Infrastructure failures must not silently become opponent wins. Participant faults can be represented as forfeits, but retry allowance, scoring, appeals and exact forfeit/void treatment are OPEN POLICY DECISIONS. Do not put tournament points in the sandbox/controller contract.

Canonical execution termination reasons are PROCESS_START_FAILED, INIT_TIMEOUT, READY_TIMEOUT, INVALID_PROTOCOL, ILLEGAL_MOVE, TIMEOUT, PROCESS_CRASH, PROCESS_HANG, RESOURCE_LIMIT, POLICY_VIOLATION, EXECUTION_ERROR, WORKER_LOST, ARTIFACT_UNAVAILABLE, ARTIFACT_HASH_MISMATCH and MATCH_ABORTED. Chess terminal reasons include CHECKMATE, STALEMATE, DEAD_POSITION, FIVEFOLD_REPETITION, SEVENTY_FIVE_MOVE_RULE and accepted THREEFOLD_REPETITION/FIFTY_MOVE_RULE claims. The control plane derives the terminal result; policy decides scoring/appeal consequences where marked open.

Canonical execution termination reasons are PROCESS_START_FAILED, INIT_TIMEOUT, READY_TIMEOUT, INVALID_PROTOCOL, ILLEGAL_MOVE, TIMEOUT, PROCESS_CRASH, PROCESS_HANG, RESOURCE_LIMIT, POLICY_VIOLATION, EXECUTION_ERROR, WORKER_LOST, ARTIFACT_UNAVAILABLE, ARTIFACT_HASH_MISMATCH and MATCH_ABORTED. Chess terminal reasons include CHECKMATE, STALEMATE, DEAD_POSITION, FIVEFOLD_REPETITION, SEVENTY_FIVE_MOVE_RULE and accepted THREEFOLD_REPETITION/FIFTY_MOVE_RULE claims. The control plane derives the terminal result; policy decides scoring/appeal consequences where marked open.

## 12. Match artifact binding

Before dispatch, control plane checks both sides reference ELIGIBLE versions owned by named participants, participant is allowed, object exists and recomputed digest matches. Immutable match-side tuple:

~~~text
match_id
side (WHITE | BLACK)
participant_id
submission_id
submission_version
artifact_sha256
~~~

Worker job and execution record include the same tuple. Match config pins initial FEN, rule/protocol/runtime/policy versions, clocks and seeds. New uploads never change current/historic pins.

- Participant suspended after approval: block new dispatch; stop active execution under §11; tournament result policy remains open.
- Deprecated version: no new match, existing pins retained.
- Storage unavailable before start: infra abort/retry; do not start clock.
- Missing/hash-mismatched artifact: fail closed, quarantine, security event, no execution.
- New version: old eligible version remains until replacement passes and activates.
- Mid-match storage loss: continue only from verified read-only worker copy; otherwise infra abort. Never fetch mutable “latest”.

## 13. Proposed future logical data model (not current schema)

Current tables are system_info, participants, participant_sessions and auth_events from migrations 0001_initial_schema and 0002_phase2_identity_auth. Do not add/migrate any entity in this task.

| Entity | Purpose and important fields | Relationships/immutability |
|---|---|---|
| Submission | Logical entry: server submission_id, participant owner, competition scope, created time | Owner/ID immutable; many versions |
| SubmissionVersion/Artifact | Submission/version, owner, digest, size, manifest, contract/runtime/protocol, state, object key | Digest/content/owner/version immutable; transitions separately recorded |
| ValidationAttempt | Attempt, artifact, validator/policy version, time, outcome, bounded report ref | Append-only, retry same digest |
| Tournament/Round | Rules/time policy, lifecycle, rounds/order | Match belongs to round; config version pinned |
| Match | State/result/reason, FEN, rules/protocol/runtime/policy, clocks, times, event sequence | Control-plane authoritative; terminal result stored here, not a second mutable result source |
| MatchSide | Match, side, participant, exact submission/version/hash | One side each; immutable once STARTING |
| MatchMove | Match/ply, side, UCI, before/after FEN, elapsed time, server time | Append-only; unique match/ply; atomically commit board/clocks/event |
| Worker | Worker ID/cert reference, status/heartbeat, capabilities/software | Authenticated node, not game authority; secrets stored separately |
| Execution | Attempt, match/side/worker, lease/fence, image/policy, state/resource/exit | Append-only attempts; idempotency; exact match side reference |
| CompetitionEvent | Event envelope in §14, aggregate sequence/payload | Append-only transaction/outbox |
| SecurityEvent | Restricted security evidence/reference, actor/target/severity/source/retention | Separate access/retention policy; public event only sanitized reference |

Leaderboard SHOULD derive from terminal match/tournament results, not independently mutable source values. Tournament scoring/tie-break config is versioned. Existing AuthEvent remains distinct from competition/security events.

## 14. Competition event model

Every event carries event_id, authoritative recorded_at, event_type, actor {kind,id}, severity, aggregate {type,id,sequence}, schema version, correlation/causation IDs and safe structured metadata. Optional refs: participant_id, submission_id/version, artifact_sha256, match_id, worker_id, execution_id. Worker occurred_at is untrusted; control-plane receipt/commit time orders official state. Never log keys, bearer tokens, environment secrets, full untrusted stdout or private contact data in public events.

Categories: submission (received/quarantined/state/activated/deprecated/revoked); validation (started/passed/rejected/retryable); execution (assigned/started/ready/resource/exit/cleanup); match (created/started/proposal/rejection/committed move/claim/result/abort); security (hash/policy/worker incident); worker (registered/heartbeat/lease/lost/recovered); tournament (round/pairing/scoring).

Persist official state and event in one transaction via an outbox. Redis/pubsub/WebSocket are at-least-once notifications only; consumers deduplicate by event ID and resume with cursor/aggregate sequence. Worker reports remain claims until control plane validates and commits them.

## 15. Future worker compatibility

Control-plane job carries unique job/attempt ID and fencing token, match ID, exact artifact refs, read-only artifact handles, initial state, rules/protocol/runtime, time/resource policy, seeds and expiry. No participant access key or DB/Redis credential. Worker authenticates as a worker, claims lease, verifies hashes, runs sandbox, relays proposals/observations with sequence and reports exit/cleanup evidence.

Control plane validates lease/fence, every move/clock and result. Stale reports cannot commit after lease expiry/reassignment. Duplicate (match, attempt, sequence) is idempotent. Retry creates a new Execution attempt. Worker cannot declare match result or tournament score.

## 16. Threat model

Mitigations below are required future controls, not claims about current code.

| Threat | Attack surface | Required mitigation | Residual risk |
|---|---|---|---|
| Path traversal, absolute/colliding names | USTAR paths | ASCII grammar, validate before extraction, safe descriptor-relative creation | Parser bugs; fuzzing required |
| Malformed archive, links/devices | Parser/extractor | USTAR regular files only; reject extensions/types; enforce bounds | Parser/library vulnerabilities |
| Decompression bomb/huge input | Package intake | No compression; streaming byte/member/count caps | Many valid uploads need quota/rate policy |
| Native payload/dependency supply chain | Package/runtime | Python source only, stdlib, pinned offline image | Python remains arbitrary hostile code |
| Fork bomb/resource exhaustion | Process tree | cgroups, CPU/RAM/PID limits, deadline, kill tree | Kernel/runtime flaws |
| Network/service discovery | Sandbox networking | No network/loopback/host net; egress-deny test | Isolation escape |
| Host filesystem/metadata/env secrets | Mounts/environment | Separate worker host; minimal env; no host mounts/metadata route/secrets | Host/hypervisor/runtime compromise |
| Docker socket/privilege escape | Sandbox boundary | No socket/caps/privilege; seccomp/MAC; hardened runtime; disposable worker VM | Escape cannot be ruled out absolutely |
| Artifact TOCTOU/replacement | Validation-to-run | Exact-byte digest, write-once object, read-only stage, rehash before launch, match pin | Compromised storage/worker |
| Protocol forgery/state corruption | stdout/worker report | Strict IDs/schema; server-only board/clocks/result; independent legality | Compromised trusted worker can fabricate legal proposals |
| Credential/private-data leakage | Process environment/log | No credentials in sandbox; minimal game input; bounded private stderr | Runtime/host compromise |

## 17. Explicit non-goals

This contract task does not implement: participant upload API; artifact storage; validation service; sandbox runtime or Docker/gVisor changes; chess runtime or move validation; match controller/clock/persistence; tournament engine; workers; leaderboard; spectator system; admin center; public deployment; frontend changes; database/schema changes or migrations.

## 18. Open policy decisions

1. Final package/member/file-count limits and participant upload quotas.
2. Stdlib-only versus approved dependency/data allowlist; opening-book policy.
3. CPU/RAM/PID/disk/output/concurrency/startup/ready limits (defaults are provisional).
4. Move/total clocks, increment/delay, transport-latency and startup charging.
5. Randomness and seed policy, seed visibility, x86-64-only or other architectures.
6. Draw claims, starting positions/openings.
7. Forfeit/void/scoring, appeal, retries, byes, concurrent entries, suspension/revocation during game.
8. Artifact store, encryption, retention/backup, security-event access/retention.
9. Isolation runtime and evidence required before public code execution.
10. Worker auth/TLS/control-plane transport and remote-worker trust/attestation.

These belong to versioned server/event policy, never participant-controlled manifest fields.

## 19. Versioning and compatibility

contract_version versions package/manifest semantics; protocol_version versions process wire semantics; runtime names a server-maintained profile whose image digest is stored per execution. V1 accepts exactly contract 1, aik-jsonl/1 and python-3.12. Unsupported values fail closed with stable rejection. V1 schemas are strict. Breaking package/wire changes require a new major version; additive fields require a separately negotiated protocol revision. Servers may support multiple protocol majors, but each match pins one version for both engines. Never silently reinterpret an old manifest. Persist schema/policy/runtime digests per validation and match.

## 20. Consistency review

| Check | Contract resolution |
|---|---|
| Manifest cannot override security | Closed manifest contains identity/compatibility only; all sandbox policy is server-side |
| Artifact immutable | SHA-256 binds exact canonical bytes; versions append-only; content-addressed write-once storage |
| Match binds exact artifact | Immutable MatchSide includes participant/submission/version/digest; worker re-verifies |
| Server authoritative | Controller validates every move/clock/state/result/event |
| AI cannot alter board/clock | Response is move proposal only; clock is supplied read-only |
| Network denial vs protocol | Pipes carry engine protocol; worker networking is outside sandbox |
| Worker not authority | Worker relays; control plane verifies legality/time and commits outcomes |
| Failures representable | Execution reason plus COMPLETED vs ABORTED separates participant/infrastructure outcomes |
| Tournament/spectator compatibility | Persisted result and sequenced committed events are future inputs; scoring is versioned policy |
| Existing identity compatibility | Owner references current participant_id; no auth/schema changes |

Current constraints, not contradictions: only API/PostgreSQL/Redis are in Compose; no competition runtime exists; current Redis fallback is process-local; auth_events is not the future event ledger. No current code requires network access from AI because no engine runtime exists. Future implementation must not run untrusted engines on the current API/DB/Redis host/network.

## 21. Recommended implementation dependency sequence

After contract and policy approval:

~~~text
approved core contract + policy values
 -> submission/version records + bounded quarantine storage
 -> canonical archive parser + manifest/static validation
 -> isolated Linux worker/runtime controls + adversarial tests
 -> pinned Python runtime + AIK protocol runner + chess rules engine
 -> authoritative match/clock controller + exact artifact binding + move persistence
 -> durable results + transactional event outbox
 -> authenticated worker leases/retries before distributing execution
 -> tournament scheduling/scoring
 -> derived leaderboard
 -> replayable real-time spectator feed
 -> admin operations/monitoring + event deployment hardening
~~~

A one-worker end-to-end match should be reproducible before distributing execution. A worker prototype may be used earlier for isolation testing, but is never match authority. This is a dependency proposal, not implementation authorization.

## 22. First complete-match milestone

Acceptance means two independent participant packages are validated and pinned by exact digest; launched in isolated runtimes; communicate only through AIK JSON Lines 1; return moves that the server independently validates and applies; use server-owned clocks; produce a server-determined terminal result; persist the complete move/event transcript; and clean up both executions. A rerun uses the same pinned artifacts/runtime/config/seeds and can replay the official move transcript. Engine re-execution need not make identical choices unless it follows the seed contract. A mock engine, UI state or health endpoint does not satisfy any match milestone.
