# AI Knockout Event Master Plan V1

**Status:** Authoritative event-level requirements; implementation details remain subject to the core contract and explicit organizer policy approval.

## 1. Event objective and operating envelope

AI Knockout is a live AI-building chess competition. Participants register, receive server-issued credentials, build and submit chess agents, receive static validation and eligibility decisions, test eligible versions against an organizer-provided House AI, improve and resubmit, select an official version before a server-enforced freeze, and compete in a final knockout tournament.

- Expected capacity: approximately 30–40 participants or teams.
- Event window: 09:30–16:00 on the organizer-configured event date and event-local timezone. The date and timezone must be approved and configured before the event is opened; the browser clock is never authoritative.
- The event server is authoritative for identity, timestamps, build duration, version status, validation, eligibility, freeze state, matches, chess state, results, and scores.

## 2. Participant lifecycle

### Registration and credentials

1. A participant submits the registration fields accepted by the participant API.
2. The server creates one participant identity and one initial build-session record in the same database transaction.
3. The server issues a Participant ID and one-time Access Key. Only the Access Key hash is persisted. The browser displays the key once and does not define identity or event time.
4. Repeated requests, browser refreshes, and retries must not create a second build-session start for an existing participant. Duplicate registration remains a conflict and does not restart the clock.

### Build session and duration

- The server records `build_started_at` when the participant identity is successfully created. This timestamp is immutable.
- For each newly accepted, non-duplicate submission version, the server records its authoritative receipt time and calculates build duration as `submission_received_at - build_started_at` using UTC-aware server timestamps.
- Validation completion time is not a substitute for submission receipt time. Network/client timestamps are ignored for official duration.
- Duplicate uploads return the existing version and do not create a new version, receipt time, or duration.
- Whether late registration or a build session after the configured event close is permitted is an organizer policy decision that must be resolved before enabling registration in production.

### Submission, validation, and eligibility

- A participant may create a submission and upload successive immutable versions under the existing submission contract.
- Each exact artifact digest, manifest snapshot, protocol/runtime identity, version number, submission receipt time, and build duration remain bound to that version.
- Existing quarantine, static validation, append-only validation attempts, and explicit organizer eligibility approval remain authoritative. Upload or validation success alone does not confer eligibility.
- A later version never mutates or replaces the historical bytes or validation evidence of an earlier version.

## 3. Development matches and House AI

- A development match is a participant’s exact eligible submission version against an organizer-owned, frozen House AI version. It provides feedback and development scoring only.
- A participant need not wait for another participant. Once eligible, a version should be independently queueable for House AI testing, subject to available capacity and organizer-approved scheduling policy.
- The House AI is an organizer-owned competition asset. Its immutable identity tuple is: `house_ai_id`, version, artifact digest, protocol version, runtime profile, rules version, and policy/runtime identity. The artifact is not implemented, executed, or selected by this milestone.
- A House AI version may be frozen for an event before any match binds to it. Existing bindings retain that exact version identity; updating the configured House AI creates a new version and never rewrites old bindings.
- A development match record must explicitly identify its kind and both exact sides. Its result is not an official tournament result and cannot contribute to official standings unless a separately approved policy explicitly defines an independent conversion (none is defined in V1).

## 4. Iteration and official submission freeze

- Participants may continue to submit and validate versions during the build period, subject to configured event rules.
- Development submission state and official tournament submission selection are separate concepts. “Latest version” is not implicitly the official entry.
- Before the official freeze, an authenticated participant may designate an eligible version as their intended official submission. The server records an append-only selection history or equivalent auditable transition.
- At the configured freeze instant, the server atomically closes official selection and pins one eligible exact version per participant. No client timestamp or request arrival claim can move the freeze.
- After freeze, official selections and their version/digest bindings are immutable. New uploads may be accepted or rejected according to approved policy, but cannot silently replace the pinned tournament version.
- The freeze time, eligibility cutoff, handling of participants without a selection, tie/withdrawal handling, and whether organizers may make audited overrides are policy decisions requiring approval.

## 5. Official knockout tournament and scoring separation

- Official matches use only frozen eligible participant versions and existing server-authoritative match, execution-binding, chess, and transcript records.
- Official tournament state/results are separate from development match state/results.
- Development feedback/scoring is maintained independently from official tournament scoring. V1 defines no scoring formula, point weights, win/draw/loss values, tie breaks, retries, or tournament seeding rules. These are explicit organizer policy decisions requiring approval before implementation.
- The event server alone computes and persists scores. Participant agents, clients, workers, House AI processes, and spectators cannot declare official outcomes.

## 6. Participant portal contract

The portal may display only server-backed data:

- server build-start timestamp and a remaining/elapsed display derived from server timestamps plus client monotonic elapsed time; it must not submit browser-clock values as evidence;
- current submission version, authoritative receipt time, build duration, validation and eligibility state;
- development-match queue/status/result only when a persisted server record exists;
- pinned official submission and freeze state only when exposed by an authenticated server contract.

Unimplemented capabilities must be labeled unavailable. No mock scores, matches, validation progress, opponents, activity, or tournament data may be shown. Existing authentication, credential handling, navigation, sign-out, accessibility, responsive behavior, and reduced-motion support must remain intact.

## 7. Admin and spectator requirements

### Admin/operator

The event needs an authenticated, role-scoped, audited control plane for event configuration, open/close state, eligibility decisions, House AI version registration/freezing, official submission freeze, queue monitoring, match intervention, participant support, and recovery. High-impact actions require actor identity, reason, timestamp, correlation, and immutable audit evidence. No such authority may be inferred from a participant or worker API. Admin UI/API authorization and emergency-access policy must be designed and approved before use.

### Spectator

Spectators may receive only explicitly public participant-safe event state, official match state, and approved standings. They must not receive Access Keys, session tokens, private contact fields, unapproved submission contents, worker certificates, internal security events, or development information not approved for publication. Spectator APIs and mode are future scope.

## 8. Failure, recovery, and operational workflow

- Registration, build-session creation, submission version creation, quarantine persistence, receipt-time capture, and duration calculation must be transactionally consistent.
- Retries are idempotent. A participant, version, event freeze, or match binding must never be duplicated or silently rewritten because a client timed out.
- Database unavailability fails closed: no invented timestamp, eligibility, selection, result, or score. The operator procedure records the incident and uses audited recovery after service restoration.
- Match/controller restart follows the existing transcript verification and abort/recovery policy; active participant execution remains disabled until the dedicated sandbox milestone is accepted.
- Operators rehearse registration, lost-credential support, upload/validation, eligibility review, development-match queue saturation, freeze, tournament launch, database backup/restore, network loss, and incident communications using disposable data.

### Event-day workflow

1. Verify event configuration, event-local clock synchronization, database/Redis health, backup, and operator access.
2. Open registration/build sessions and monitor capacity (30–40 participants expected).
3. Accept submissions into quarantine; expose only server validation and explicit eligibility states.
4. Queue eligible versions for House AI development matches only after the House AI identity and matching capability are approved and implemented.
5. Accept iterative versions while the approved build window remains open.
6. Apply the configured official freeze atomically; reconcile every participant against the approved no-selection policy.
7. Create official matches from frozen exact versions according to separately approved seeding policy.
8. Publish only approved official state, complete the final, confirm the winner from server records, and preserve event evidence.

## 9. Capacity and rehearsal requirements

Before production use, rehearse at least the expected 30–40 participant cohort and peak concurrent upload/validation and development-match demand. Measure queue latency, transaction contention, validation throughput, storage headroom, recovery time, and operator workload. Capacity targets and acceptable latency are policy/operations decisions and must be recorded from rehearsal results rather than invented here. Rehearsal artifacts must be inert test fixtures; no participant code is run before the sandbox gate is accepted.

## 10. Current implementation boundary

Existing implementation includes participant registration/authentication, hashed credentials, immutable quarantined submission versions, static validation and explicit eligibility approval, append-only lifecycle evidence, and server-authoritative participant-versus-participant match records with immutable execution bindings and chess transcripts.

Not yet implemented by this event plan alone: build sessions/timers/duration, event configuration and freeze, official submission designation, House AI inventory or execution, development-match queue/type/binding, development scoring, official tournament scheduling/seeding/scoring, admin control plane, and spectator mode. This plan does not authorize participant-code execution, sandbox/Kata work, House AI execution, or tournament operation.

## 11. Approval-gated policy decisions

The organizer must approve these before their corresponding operational feature is enabled:

1. Event date, timezone, registration opening/closing, build cutoff, and treatment of late registration/submission.
2. Whether build time is continuous wall time or has approved pauses/accommodations; V1 records elapsed UTC wall duration unless policy changes before implementation.
3. House AI artifact, identity tuple values, version lifecycle, update policy, match limits, queue priority, retry behavior, and opponent-side/randomness policy.
4. Development scoring formula and whether any feedback metric is visible publicly.
5. Official submission selection deadline, no-selection/withdrawal/appeal behavior, audited organizer override, and late upload policy.
6. Official tournament seeding, bracket/byes, colors, timing, forfeits, retries, tie-breaks, scoring, and winner determination.
7. Admin roles, authentication, break-glass access, audit retention, spectator publication policy, and operational SLO/capacity thresholds.

## 12. Final event acceptance criteria

The event is ready only when all of the following are demonstrated in an isolated rehearsal: 30–40 participant registration with exactly one server-timestamped build session each; immutable server-derived submission receipt/duration for successive versions; existing validation and explicit eligibility remain authoritative; eligible versions can be independently queued against one immutable approved House AI identity without running outside the accepted sandbox; development results are distinct from official records/scores; freeze atomically pins eligible versions and rejects later official-selection mutation; official matches use only frozen exact participant versions; the server alone owns timing, state, chess, results, and scores; admin actions are authenticated/audited; public data is approved and secret-free; failure/recovery and backup procedures pass; accessibility and security regressions pass; and no unresolved scoring, seeding, time, House AI, or operational policy is silently assumed.
