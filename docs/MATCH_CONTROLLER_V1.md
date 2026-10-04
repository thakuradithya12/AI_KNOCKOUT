# Match Controller V1

## Authority and scope

The match controller is trusted control-plane logic. A participant process can
only return a bounded AIK JSONL proposal. It cannot set the board position,
clock, match state, execution identity, opponent, outcome, or scoring. The
controller accepts a move only after checking the active side, exact execution
binding, request identity, server sequence, deadline, protocol schema, and the
configured rules adapter.

This milestone creates the domain boundary, durable records, and a production
rules adapter. It does not launch participant processes. The deterministic fake
transition table remains available only to tests that explicitly inject it.
Actual Linux/KVM/Kata enforcement remains unimplemented.

## Pinned match identity

`Match` captures the protocol, runtime, policy, rules version, initial position,
clock parameters, a server-selected 256-bit match seed, and current authoritative
match state. Its contract states are
`QUEUED`, `STARTING`, `RUNNING`, `COMPLETED`, and `ABORTED`. Legal durable
transitions are:

```text
QUEUED -> STARTING -> RUNNING -> COMPLETED
   |          |          |
   +----------+----------+-> ABORTED
```

`MatchSide` has one WHITE and one BLACK record. Each immutable side pins
participant ID, submission ID, submission-version ID, exact SHA-256 digest,
protocol, and runtime profile. Match creation accepts version IDs only, requires
both versions to be eligible, verifies exact quarantined bytes against the
stored digest, and rejects self-match. It never accepts participant-supplied
artifact bytes and never enqueues execution.

## Execution binding and readiness

`MatchExecutionBinding` append-only rows bind a match side to an execution,
worker, lease, fencing token, attempt, exact version, and digest. Binding requires
an acknowledged worker execution job with matching match/side participant,
submission, version, digest, protocol/runtime, worker, lease, fence, and live
expiry. The current binding must remain the latest attempt and its worker/job
must remain active when readiness is recorded. A match enters `RUNNING` only
after each exact current binding has a durable `EXECUTION_READY` event.

This is a control-plane reference only. It does not start or inspect a process.
The worker and sandbox layers remain responsible for actual execution lifecycle
and enforcement.

## AIK, moves, and clocks

`MatchController` follows the Core Contract handshake: `hello` / `ready`, then
`new_game` with the persisted seed and clock policy, then `position` with the
authoritative FEN, accepted UCI history, side and clocks, then `go`. Every
response must match its pending request before the next stage. Move time starts
when `go` is issued, and a trusted caller can expire a silent turn. The
controller accepts only a response for its outstanding request. The existing bounded
`AIKTransport` rejects malformed or oversized JSONL, invalid move syntax,
duplicate payloads, and participant result claims. The controller also rejects
stale bindings, wrong side, duplicate/gapped sequence, late response, illegal
rule transition, and proposals after terminal state.

Accepted moves produce append-only `MatchMove` rows with server sequence, side,
move, request ID, exact execution binding, before/after position hashes and
positions, elapsed time, and server timestamp. The match position, side to move,
clocks, terminal state, move transcript, and corresponding `CompetitionEvent`
outbox records are persisted transactionally with an optimistic controller
snapshot check. Database constraints and PostgreSQL triggers provide additional
append-only and state-transition guards; they have not been exercised against a
live PostgreSQL instance in this milestone.

Clock state belongs to the controller. Runtime deadlines use a monotonic clock;
wall-clock timestamps are evidence only. A participant cannot extend its own
deadline. The persisted remaining clocks do not reconstruct an in-flight
monotonic deadline.

## Results, failures, and events

Only contract-defined chess terminal reasons can complete a match:
`CHECKMATE`, `STALEMATE`, `DEAD_POSITION`, `FIVEFOLD_REPETITION`,
`SEVENTY_FIVE_MOVE_RULE`, `THREEFOLD_REPETITION`, and `FIFTY_MOVE_RULE`. A
trusted rules adapter supplies terminal outcomes. No scoring policy is added.
Failure observations (including illegal move, timeout, process, protocol,
resource, worker, sandbox, cleanup, fencing, and controller-restart failures)
abort without inventing a chess result. Mapping participant-fault failures to a
forfeit or score remains an open contract policy.

Match lifecycle and move events use existing append-only `CompetitionEvent`
outbox rows with per-match aggregate sequence. There is no parallel match event
table and no spectator/WebSocket delivery.

## Recovery and current limitations

The production controller defaults to `ChessRulesAdapter`, which uses the
directly pinned `chess==1.11.2` (`python-chess`) package and requires the match
configuration to pin `standard-chess/python-chess-1.11.2`. The controller sees
only the typed `RulesAdapter` port. The adapter owns FEN encoding, UCI legality,
position application, check/mate/stalemate, dead position, fivefold repetition,
75-move automatic draws, and claimable threefold/50-move draws. It replays the
full UCI transcript from the configured initial FEN for each operation, so
repetition history is not lost when a position is serialized. This identifies
a pinned library and its implemented rules surface; it is not a claim of
exhaustive chess-rules certification.

`rehydrate_controller` reconstructs a queued or terminal controller by replaying
each accepted move through the rules port. It checks contiguous sequence,
alternating side, exact before/after FEN and digest, move legality, exact side
identity, historical execution binding identity/digest, persisted side-to-move,
and a completed result. Any mismatch fails closed; it does not repair the
transcript. A completed result based on a claimable draw is re-verified against
the replayed position. Active `STARTING`/`RUNNING` records cannot be resumed:
recovery must record `CONTROLLER_RESTART` and abort, because the monotonic
deadline and handshake state cannot be reconstructed safely. Persisted
remaining clocks are audit values, not a restarted deadline.

An active controller restart is fail-closed: the match becomes `ABORTED` with
`CONTROLLER_RESTART` and no chess result because a monotonic deadline cannot be
reconstructed safely. This is not distributed recovery or automatic resume.
Exactly-once behavior across process loss still depends on database
transactions, row locking, unique constraints, and idempotent caller retries;
no cross-database concurrency claim is made here.

## Not implemented

- Real PostgreSQL migration/trigger verification against a disposable cluster.
- Participant process launch, sandbox creation, resource/network/filesystem
  enforcement, or cleanup verification.
- API endpoints that schedule matches or control executions.
- Tournament scheduling, scoring, leaderboard, spectator, or frontend systems.
- Linux/KVM/Kata verification.

Participant-code execution remains disabled.
