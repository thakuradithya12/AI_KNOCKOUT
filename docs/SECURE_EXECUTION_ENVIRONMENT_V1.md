# AI Knockout — Secure Participant AI Execution Environment V1

**Status:** Design specification plus a mock-only worker control-plane protocol prototype. REAL SANDBOX EXECUTION: NOT IMPLEMENTED / NOT VERIFIED.  
**Scope:** Future execution of hostile participant Python packages after static validation and authorized match binding.  
**Policy status:** Values marked **PROPOSED / ORGANIZER APPROVAL REQUIRED** are engineering starting points, not approved event rules.

This document is subordinate to [AI_KNOCKOUT_CORE_CONTRACT_V1.md](AI_KNOCKOUT_CORE_CONTRACT_V1.md). It does not amend the AIK JSON Lines protocol, package format, chess rules, authority model, or event decisions. It expands the execution design needed before any participant artifact is run. The current submission milestone ends at static validation and `APPROVED`; that state is not runnable.

## 1. Execution trust boundary

### 1.1 Components and trust

| Component | Trust classification | Authority and boundary |
|---|---|---|
| **Control plane** (API, match controller, worker gateway, scheduler, policy/config service) | Trusted computing base (TCB); privileged, patched, access-controlled service | Authenticates users/workers; decides assignments and policy; owns clocks, board, legal-move checks, match results and official tournament state. The match controller validates worker reports and commits state. |
| **Artifact/quarantine storage** | Trusted for integrity/availability, not assumed infallible | Holds immutable exact archive bytes and metadata. API/database credentials stay on control-plane hosts. Workers receive narrowly scoped artifact bytes for an assigned job only. Any digest mismatch fails closed and raises a security event. |
| **Execution worker agent / host OS / hypervisor** | Trusted infrastructure code and host boundary, but fallible and not match-authoritative | Authenticates as a worker, accepts bounded jobs, verifies leases and artifact digests, creates/destroys sandboxes, enforces local resource limits, and relays protocol/evidence. It cannot determine legal moves or official results. A worker compromise can fabricate plausible outputs; control-plane move validation cannot prove which binary produced them. |
| **Sandbox launcher/runtime** | TCB security boundary; must be pinned, patched, configured, monitored, and independently tested | Creates per-engine isolation, mounts, namespaces, cgroups and network denial. It is not trusted to decide chess outcomes. Runtime escape remains a residual risk. |
| **Participant AI process and all artifact contents** | **Untrusted hostile code**, even after approval | May attempt arbitrary Python behavior within the guest, exploit the runtime/kernel, consume resources, forge protocol data, or attack other components. It receives no credentials and can only propose protocol messages over pipes. |
| **Chess/match controller** | Trusted control-plane component; not part of participant sandbox | Sends AIK messages, measures authoritative deadlines, parses responses, validates legal moves/claims on its own state, commits board/clocks/results/events, and rejects stale or malformed reports. It is never loaded into the participant process. |

### 1.2 Data and authority boundaries

```text
Participant browser --HTTPS--> Control plane/API --private DB network--> PostgreSQL
                                      |                    Redis (cache/dispatch hint only)
                                      |
                             authenticated worker gateway
                                      | mTLS, scoped job/lease
                                      v
                         Dedicated Linux execution worker
                         [trusted agent + privileged launcher]
                              |                 |
                    private pipes         private pipes
                              v                 v
                     AI sandbox A        AI sandbox B
                     [untrusted]          [untrusted]
                              \                 /
                               protocol proposals
                                      |
                            control-plane controller
```

Participant source never runs in the API, PostgreSQL, Redis, artifact-storage, or control-plane host process. The sandbox cannot call the worker agent, launcher, Docker/containerd, controller, API or databases. The worker receives no database or Redis password. Redis may notify/dispatch; it is not authoritative job or match state. Jobs, leases, accepted moves, and results are validated against durable control-plane records.

## 2. Execution location and deployment choice

### 2.1 Required event architecture

1. Keep the existing Windows 11 + Docker Desktop + WSL2 Compose stack as a **development/control-plane environment only**. It must not launch participant source.
2. Run participant code on a **dedicated Linux worker machine**, physically or logically separate from the API/PostgreSQL/Redis host and its LAN. The worker has no database/Redis credentials and no shared writable storage with the control plane.
3. Launch **one participant AI per disposable Kata Containers microVM sandbox** on that worker, using a server-owned OCI runtime image. The two AIs for a match use separate microVMs, private PID/network/filesystem namespaces, separate cgroups, and separate pipes. Neither sees the other process, files, environment or IPC.
4. Kata's VM boundary is the event-day required stronger isolation layer; ordinary Docker containers or Compose services alone do not meet this design. The worker host needs a supported Linux/KVM configuration and hardware virtualization. If that is unavailable, participant execution is disabled until organizers approve a tested alternative and the security gate is passed. A gVisor OCI runtime can be evaluated as a separate candidate, but it is not silently substituted for the selected event boundary.
5. Run the trusted match controller on the control plane. The worker relays engine protocol bytes and bounded observations. If later latency requirements motivate a worker-side controller, it must remain trusted, separately sandboxed from AIs, and the control plane must still validate every proposal and own clocks/results; that is a new reviewed design decision.

Kata, kernel, guest image, OCI runtime, launcher, host OS and policy versions are pinned and recorded per execution. Use a minimal read-only guest/runtime image containing only the pinned Python 3.12 runtime and approved standard library. No participant-built image, package installation, native extension, compiler, shell selection, or dynamic dependency download is allowed by V1.

### 2.2 Current Compose and network boundary findings

At review time, `compose.yml` defines only `api`, `postgres`, and `redis` on the shared `aik-internal` bridge. API can reach PostgreSQL and Redis on that network. PostgreSQL is host-published at `127.0.0.1:${POSTGRES_PORT:-5433}` and Redis at `127.0.0.1:${REDIS_PORT:-6379}`; the API is published on `${API_PORT:-8000}` without an explicit loopback bind. Compose named volumes persist PostgreSQL and Redis. The API image runs as non-root `appuser`; the Compose file currently has no worker, sandbox runtime, worker network, resource caps or execution service.

These current mappings are development choices, not an event-day deployment recipe. Before LAN operation, PostgreSQL and Redis host mappings must be removed; API ingress must be intentionally bound/firewalled; the control-plane internal network must have no participant sandbox members. A dedicated worker host must be on a separate worker-control network/VLAN/firewall zone with only the authenticated worker gateway reachable. Do not attach worker sandbox networks to `aik-internal`.

The host's API, database and Redis services remain control-plane services. Do not add a route, queue consumer, helper, startup hook, Compose profile, or endpoint that executes submitted source in those containers.

## 3. Container and sandbox model

Each execution is created from a control-plane-generated immutable policy object. Participant manifest values cannot change sandbox parameters.

| Control | Required design |
|---|---|
| Filesystem/root image | Pinned minimal guest root filesystem; read-only root mount. No host path bind mounts, host home, source checkout, Docker data root, secrets directory, or control-plane volume. No writable container layer. |
| Participant artifact | Worker verifies original archive SHA-256, parses the canonical USTAR again under the pinned validator/policy version, then safely stages only validated regular files under a new execution-scoped directory. Create files descriptor-relative with exclusive creation and no-follow semantics; reject symlinks and path traversal. Mount staged source read-only, `nodev`, `nosuid`, and `noexec` at `/opt/aik/artifact`. Never execute files directly from mutable quarantine storage. |
| Writable temporary storage | One private per-sandbox tmpfs at `/tmp` (proposed 32 MiB, bounded inodes); `nosuid,nodev,noexec` where supported. It contains no secrets and is destroyed with the guest. No writable artifact or home mount. |
| Working directory | Fixed `/opt/aik/run` inside the guest, controlled by the launcher. If a writable cwd is needed, use a distinct quota-bounded temporary subdirectory; never use the artifact mount as cwd. |
| User and groups | Dedicated unprivileged numeric UID/GID (proposed 65532:65532 inside guest), no supplementary groups, no host UID mapping with privileges. User namespace maps to an unprivileged host identity where supported. |
| Capabilities / privilege | Drop all Linux capabilities; `no-new-privileges`; no setuid binaries; no privileged mode; no host PID, IPC, UTS, user, cgroup, or network namespace. Do not pass host devices or security labels writable by the AI. |
| Seccomp and mandatory access control | Enforce a minimal versioned seccomp allowlist/deny profile in guest/container and host launcher; enforce AppArmor or SELinux in enforcing mode on the worker host. Sandbox may not choose/relax either profile. Record profile digests and test on the exact kernel/runtime build. |
| Docker/containerd | Docker/containerd socket is never mounted, forwarded or reachable from the guest. The worker launcher is a privileged host-side TCB component exposed only over a permission-restricted local Unix socket to the worker agent. It accepts typed job parameters only, never shell fragments or participant-selected OCI flags. |
| `/proc`, `/sys`, `/dev` | Guest-private PID namespace with a minimal read-only `/proc`; hide unrelated process/environment details. No host `/proc`, `/sys`, cgroup files, kernel logs or host namespaces. Do not mount `/sys` unless a required read-only virtual subset is explicitly tested. Minimal synthetic `/dev` only (`null`, `zero`, `random`, `urandom`, plus required tty/pipe devices); no host devices, GPU, block devices or device passthrough. |
| Namespaces / PID tree | Fresh mount, PID, user, UTS, IPC and network namespace per AI; fresh cgroup subtree per execution; private IPC and hostname. PID 1/supervisor reaps children. Kernel/VM identity is pinned. |
| Process tree | Launch only the server-selected Python interpreter with an argument vector (no shell). Apply cgroup PID, memory and CPU controls before starting code. Track descendants in the per-execution cgroup; termination kills the whole cgroup/guest, not just the initial process. |

Artifact staging must not use generic archive extraction with permissive path semantics. The re-parser verifies canonical archive bytes and policy, while the staging code must independently enforce the same allowlist and safe file creation. Only the trusted launcher can write staging files. Once verified and staged, the runtime sees the artifact read-only; no replacement can race between verification and use.

## 4. Resource limits

All values below are per AI sandbox unless specified. Current Core Contract provisional values are preserved; additional wall-time and concurrency suggestions are new and require organizer approval. Never accept limits from the participant manifest.

| Resource | Proposed server-side starting limit | Enforcement mechanism | Contract/event status |
|---|---:|---|---|
| CPU | 1 vCPU-equivalent | cgroup v2 CPU quota/weight plus trusted monotonic deadline; host capacity admission control | Core Contract provisional; event fairness approval and calibration required |
| RAM | 256 MiB hard limit; OOM kill contained to sandbox | cgroup v2 `memory.max`; no swap for execution cgroup if supported; measure guest/runtime overhead separately | Core Contract provisional; event approval/load test required |
| PID/process tree | 16 tasks | cgroup v2 `pids.max`; kill the complete cgroup on breach | Core Contract provisional |
| Writable disk | 32 MiB tmpfs and 128 inodes | tmpfs size/inode cap; read-only root/artifact; per-file-size and descriptor limits | Core Contract provisional size; inode count is proposed |
| stdout | 64 KiB maximum protocol line; 1 MiB cumulative per AI execution | Parent pipe reader enforces byte limits before parsing; bounded buffers; kill on overflow | Core Contract provisional |
| stderr | 256 KiB cumulative per AI execution | Concurrent drain, bounded private ring/file, then discard excess and terminate on policy threshold | Core Contract provisional; never public |
| File descriptors | 64 open descriptors | `RLIMIT_NOFILE` and guest/host supervisor enforcement | Additional proposed cap; organizer/engineering review |
| Core dumps | 0 bytes | `RLIMIT_CORE=0`, guest policy and no writable host path | Required hardening control |
| Startup | 5 seconds from process launch to completing initialization boundary | Trusted monotonic clock; launcher and controller deadlines | Core Contract provisional |
| Ready | 2 seconds after `hello` is delivered | Control-plane monotonic deadline; full valid `ready` line required | Core Contract provisional |
| Move response | 2 seconds proposed per `go` | Control-plane monotonic deadline; worker may kill earlier but cannot extend it | Core Contract leaves open; organizer must approve or replace |
| Total AI process lifetime | 15 minutes proposed hard safety ceiling per AI per match attempt, including startup and turns | Control-plane absolute monotonic deadline plus worker watchdog; infrastructure/participant outcome classification per evidence | New technical proposal; organizer must approve and reconcile with match clock policy |
| Graceful stop | 1 second, then force kill | Trusted supervisor; kill cgroup/VM and verify it is gone | Core Contract provisional |
| Concurrent executions | 1 match per worker host by default (2 separately isolated AI sandboxes); no oversubscription | Worker admission controller; reserve both sandboxes' CPU/RAM plus guest overhead before lease acceptance | Proposed safe development/event starting point; organizer approves global concurrency and capacity |

Move and total process deadlines are separate from chess clocks. The control plane owns event clock rules and starts/stops official time according to Core Contract; the proposed hard limits are final safety ceilings for this design, not a substitute chess time control. CPU quota is not wall time. Resource breaches terminate only the affected attempt unless host/worker health is uncertain. Do not silently turn an infrastructure limit/worker failure into an opponent win. Tune after representative engine load tests and approve published limits before event use.

## 5. Network policy

**Default policy: no participant AI network access.** The AIK protocol uses inherited stdin/stdout/stderr pipes; no socket is needed.

- Give each sandbox a fresh network namespace not attached to the worker-control or control-plane bridge. No host network mode, Docker bridge, macvlan, shared network namespace, published port, or port-forward.
- Configure no external interfaces or routes. Loopback may exist as a sandbox-local interface but has no listener/service; the AI receives no sidecar/controller listener. The `localhost` test must demonstrate that no worker, host, API, or other AI service is reachable. If implementation can reliably disable loopback without breaking Python, do so; otherwise document loopback as self-only and verify host isolation.
- No DNS resolver, search domains, proxy variables, service-discovery data, or DNS egress. A DNS request must fail without leaving the worker boundary.
- Deny outbound and inbound traffic at the namespace/runtime and worker-host firewall layers. There are no allowed AI egress destinations in V1. No host or control-plane port is reachable, including PostgreSQL, Redis, API/worker gateway, SSH, metadata/cloud endpoints, container runtime sockets, or another sandbox.
- No network path from AI namespaces to the worker agent. Worker control-plane connectivity is outside the guest and performed by the agent using a worker identity. Firewall rules must be enforced by host networking, not only by a container setting.
- No mounted Unix-domain socket or shared IPC endpoint to host processes. Protocol pipes are created by the trusted supervisor and are the only AI/control interaction channel.
- Block cloud instance metadata addresses/routes at the worker host/network edge as defense in depth. Event workers should run without cloud role credentials; if cloud is selected later, policy must explicitly block metadata and test it from guest and host.

An approved future network feature requires a new versioned security policy, narrowly allowlisted proxy/gateway, explicit organizer approval, protocol review, and adversarial egress tests. Do not add general network access as a debugging convenience.

## 6. Secrets and visible environment

**Default: no credentials of any kind in the participant sandbox.** The launcher constructs a new environment from an explicit allowlist and does not inherit worker or host environment.

Permitted environment is limited to server-owned deterministic runtime settings, for example:

```text
PATH=/usr/local/bin:/usr/bin
HOME=/tmp/home
TMPDIR=/tmp
LANG=C.UTF-8
LC_ALL=C.UTF-8
TZ=UTC
PYTHONHASHSEED=<server-generated non-secret seed>
PYTHONDONTWRITEBYTECODE=1
```

The process receives only match protocol context over the existing AIK messages (protocol version, match ID, side, runtime name, server-selected seed, board/history/clocks). It receives no participant name/contact information unless a future organizer-approved protocol requires a non-identifying field. A match ID or deterministic seed is not an authentication secret.

The sandbox MUST NOT receive participant access keys/session tokens, database URLs/users/passwords, Redis URL/password, API signing/session secrets, worker mTLS keys/certificates, control-plane or artifact-store credentials, cloud credentials/metadata tokens, host environment variables, Docker/containerd configuration, registry credentials, SSH keys, CI secrets, event admin credentials, or another participant's information. None may be mounted, placed in `/proc`, passed in argv, exposed as a file, or included in stderr/stdout diagnostics. Environment inspection tests verify the allowlist exactly, not just the absence of known variable names.

Worker identity material is stored outside the sandbox, protected by host permissions/TPM or equivalent where available, and accessible only to the worker agent. The worker can request only the job/artifact assigned to its authenticated identity. Rotation/revocation is operationally required; participant execution never receives the worker credential.

## 7. Artifact flow and TOCTOU protection

```text
immutable quarantine object
  -> control plane selects exact approved version and match pin
  -> worker receives scoped assignment and expected SHA-256
  -> worker fetches bytes into private exclusive staging
  -> worker recomputes SHA-256 over exact archive bytes
  -> worker re-parses canonical USTAR and policy/version
  -> trusted safe extraction into fresh execution directory
  -> participant files mounted read-only in one-AI sandbox
  -> launch / protocol execution
  -> stop, verify cleanup, discard temporary copies
```

Rules:

1. Match-side binding is immutable: participant ID, submission ID/version, digest, match ID, side and policy are sourced from control-plane durable state. Never ask for a “latest” artifact at worker time.
2. Worker checks an active, unexpired lease/fencing token before fetching. Artifact request is scoped to that worker, execution, exact digest and expiry. No reusable participant credential or broad storage credential is issued.
3. Fetch into a new per-execution directory owned by the launcher, mode 0700, not writable by any participant process. Use exclusive/no-follow creation and do not follow archive symlinks; V1 archive policy rejects links. Never stage beneath a shared writable directory.
4. Stream bytes through SHA-256 and enforce raw size ceiling. Compare the lowercase digest and byte count to the immutable control-plane tuple. Mismatch means no parse/run; quarantine the worker copy, emit a restricted security event, and invalidate the worker/job pending investigation.
5. Re-run the canonical archive parser using the exact pinned validator and package policy version. Compare manifest compatibility and member table to approved metadata. A worker must not trust a prior “approved” flag alone. Any mismatch is fail-closed; never repair/repack.
6. Extract only regular files using trusted descriptor-relative operations, no symlink following, no absolute paths, and the V1 ASCII path policy. Create a fresh directory tree, exclusive files, fixed modes/ownership, and enforce member/count/size caps again. Do not run participant code during validation/staging.
7. Make the staged artifact immutable to the participant: root-owned or otherwise inaccessible for writes, read-only mount, no writable overlay. Keep the source outside the writable tmpfs. After mount, verify mount flags and staged member metadata before launch.
8. The staging directory and any archive copy are private to one execution. Worker agent must not modify them while mounted. The launcher holds a private reference/mount until the process tree is stopped. Do not reopen by mutable pathname after verification where descriptor/mount pinning is available.
9. Start only after lease, eligibility, revocation, digest, policy and resource checks pass. Bind one AI process to one artifact and side for this execution. Never use participant-controlled path, UID, image, runtime flags, environment, resource limits or commands.
10. On completion/failure, stop the process/VM, release mount/handles, remove temporary archive and staging copies, and prove the execution cgroup/VM is gone before reusing capacity. Quarantine uncertain hosts rather than reusing contaminated state.

The exact extraction implementation must be separately threat-tested. It is not implemented by the current intake API; current quarantine is PostgreSQL `BYTEA`. Any future object-store adapter must preserve write-once identity, access scoping, worker-side rehash, backup/retention and audit requirements.

## 8. Execution identity, lease and fencing

| Identifier | Authoritative source | Worker handling |
|---|---|---|
| `worker_id` | Control-plane worker registry, bound to mTLS certificate identity | The worker asserts an ID; gateway derives/checks it from the authenticated certificate. Payload value alone is never trusted. |
| `execution_id` | Control-plane-generated unique attempt ID | Worker echoes it; control plane checks active assignment and idempotency. |
| `match_id` | Control-plane match record | Worker echoes it; exact equality to active lease required. |
| `participant_id` | Control-plane participant and immutable match-side binding | Worker gets only the opaque ID if operationally required; it is not an authorization credential. AI gets no participant ID unless protocol explicitly requires it (V1 does not). |
| `submission_id`, version | Control-plane approved version and match-side binding | Worker echoes; must match assignment. Never request latest. |
| Artifact SHA-256 | Control plane computes over accepted raw bytes and stores immutably | Worker independently computes and reports its observation. Mismatch prevents execution. |
| Lease/fencing token | Control plane issues monotonic fencing generation plus short expiry, scoped to one execution attempt | Worker reports token/generation; control plane rejects stale, expired, duplicated, or wrong-worker evidence. Token is not passed to AI. |
| Policy/runtime digest | Server-owned deployment configuration | Worker may report observed runtime/profile digest; control plane compares against assignment allowlist. |

Worker-reported timestamps, resource values, exit code, signal, termination reason, mount observations and cleanup assertions are **claims/evidence**, not official authority. Transport authenticates the worker but does not prove an uncompromised worker. Control plane validates lease/fence on every report and commits only idempotent, correctly sequenced evidence. A stale report cannot alter match state after lease expiry or re-assignment. A new attempt gets a new execution ID/fence; artifacts and side bindings remain exact.

## 9. Process and protocol model

Use Core Contract V1 `aik-jsonl/1` unchanged: separate stdin/stdout/stderr pipes, strict UTF-8 JSON Lines, server-generated request IDs, one outstanding request, and bounded line framing. The trusted supervisor bridges pipes to the control plane; there is no TCP or Unix socket in the participant sandbox. Drain stdout and stderr concurrently. Parse stdout as protocol only; extra lines, unsolicited output, malformed JSON, duplicate keys, non-finite values, wrong request IDs, unknown fields/types, oversized lines or late responses are protocol faults.

For each match, use two independent VMs/sandboxes with one AI process each. Do not mount a shared directory or socket. The controller sends only the appropriate `hello`, `new_game`, `position`, `go`, claim response, `game_over`, and `shutdown` messages. It independently validates every proposed move/draw claim against its board/history and authoritative deadline. The AI cannot:

- declare the match result, set outcome, or advance its own clock;
- mutate authoritative board, FEN, side to move, history, or tournament score;
- choose the opponent, artifact, runtime, resource policy, or job;
- address, signal, inspect or read another AI;
- access the worker, controller, API, database, Redis or tournament state.

The worker relays proposals and local observations. It cannot mark a move legal or a match complete. Only the control plane applies accepted moves and commits official state/result/events. Public participant APIs never expose raw AI stdout/stderr.

## 10. Failure handling and classification

The control plane distinguishes **participant/content faults**, **worker/host infrastructure faults**, and **security incidents**. Classification is evidence-based and recorded with policy version; it does not itself decide tournament scoring. Do not silently award a win for infrastructure failure.

| Condition | Required result | Classification and action |
|---|---|---|
| Startup error / missing interpreter/runtime | No protocol request accepted; terminate and clean sandbox | Infrastructure fault unless evidence ties failure to approved participant entrypoint behavior; record `PROCESS_START_FAILED` / `EXECUTION_ERROR`. |
| Ready deadline | Ignore late ready, kill full process tree and VM | Participant fault only if worker/host health and launch evidence are healthy; record `READY_TIMEOUT`. |
| Crash or non-zero exit | Close pipes, capture bounded exit/signal evidence, stop peer sandbox as match policy requires | Runtime crash after successful launch is provisionally participant fault; launcher/host/runtime crash is infrastructure. Record `PROCESS_CRASH` / `PROCESS_START_FAILED`. Organizer approves forfeit/retry treatment. |
| Move/total timeout | Do not apply late output; kill sandbox/cgroup | Participant fault if trusted deadline path is healthy; infrastructure if control/worker timing cannot be trusted. Record timeout reason and authoritative timestamps. |
| Memory, CPU wall, PID, disk or output exhaustion | Stop sandbox and enforce cleanup; record observed limit and counter | Usually participant resource fault when measured by trusted enforcement; host-wide pressure/measurement failure is infrastructure. No result mapping without organizer policy. |
| Protocol violation / illegal proposal | No board change; stop or reject according to Core Contract | Participant fault if parser/controller healthy. Preserve only bounded private evidence; never echo arbitrary output. |
| Sandbox policy violation / attempted egress | Block access; terminate; raise restricted security event; suspend worker if enforcement uncertain | Security incident; do not infer successful host compromise solely from denied attempt. Organizer decides participant sanction. |
| Worker heartbeat/lease loss before start | Do not start clock; fence worker and requeue on healthy worker | Infrastructure fault. New attempt/execution ID and fencing token. |
| Worker loss mid-execution | Reject late/stale reports; cancel lease; terminate/inspect if reachable | Infrastructure fault; abort execution unless a reviewed recovery protocol proves safe continuation. Never accept speculative worker result. |
| Stale execution / expired fence | Ignore all outputs and state mutations; stop if still running | Control-plane safety rejection. Record stale report/security telemetry. |
| Artifact digest/manifest/policy mismatch | Never start participant code; quarantine local copy and worker; alert operators | Storage/worker security fault unless verified malicious submission evidence; no automatic participant penalty. |
| Cleanup cannot be proven | Do not reuse worker capacity; isolate/reboot/reimage worker | Infrastructure/security fault; mark worker unhealthy until attested clean. |
| Control plane/database unavailable | Do not accept official proposal/result without durable validation/commit | Infrastructure abort; no unofficial fallback authority in Redis or worker. |

For every case: kill the whole cgroup and VM, close pipes, stop protocol relay, fence the execution, persist a bounded event if control plane is healthy, and verify cleanup. If evidence persistence fails, retain a local signed/bounded spool on the worker for later authenticated upload; it remains non-authoritative until validated and committed. Spool must not contain secrets or unlimited participant output.

## 11. Cleanup and cross-run isolation

Before the worker accepts another lease it MUST:

1. Request graceful shutdown for at most one second; then force-stop the guest/VM and kill its host-side cgroup.
2. Verify no guest process, host helper, descendant, or runtime task for the execution remains. A `docker stop`/API success response alone is insufficient; query supervisor/runtime and cgroup state.
3. Close and drain/close stdin, stdout and stderr pipes; cancel readers; discard unconsumed protocol bytes; remove per-run IPC and network namespace state.
4. Unmount read-only artifact and root filesystem; remove the private tmpfs, working directory, staged files and local archive copy. If unlink fails, quarantine the worker and securely destroy its scratch disk/VM before reuse.
5. Remove per-run cgroups, veth/firewall state, temporary credentials/artifact fetch handles and lease state. Revoke or expire scoped URLs/tokens.
6. Retain only approved bounded audit records and resource summaries. Raw source remains in quarantine according to retention policy; local staging is not retained as a convenience.
7. Run a clean-room check before reuse: fresh VM ID/kernel boundary, fresh namespaces/cgroups, empty staging, no previous file/IPC/network handles, current runtime digest, clean worker health.

Do not reuse a VM, writable overlay, home directory, `/tmp`, process namespace or IPC namespace between participant execution attempts. A cleanup failure is a worker isolation failure and blocks new work on that worker.

## 12. Observability and data exposure

Control plane may record: lifecycle timestamps, worker/execution/match IDs, exact artifact digest and version, runtime/kernel/policy/image digests, lease/fencing generation, CPU/RAM/PID/disk usage summaries, exit code/signal, normalized termination reason, bounded stderr reference, protocol violation category and security events. All records are versioned, append-only where contract requires, correlated, access-controlled and retained under organizer policy.

- AI stdout is protocol traffic. Do not log full valid protocol streams by default. Persist only the authoritative validated move/claim transcript and minimal metadata required for replay.
- stderr is private diagnostic data, capped at 256 KiB per AI execution. Store access-restricted with retention/deletion policy; never return it in participant APIs, public events, opponent views or leaderboard/spectator feeds.
- On malformed/oversized stdout, capture only a short sanitized prefix or digest and failure offset; do not store arbitrary megabytes or terminal escape/control sequences.
- Security events are restricted operator records. Public output uses a stable sanitized code only.
- Redact tokens, credential-like material and host paths from logs; never log environment dumps, command secrets, full artifacts, raw database rows or worker key material.
- Worker local evidence is signed/authenticated and size-bounded but remains a claim until control-plane validation.

## 13. Threat model

Residual risk is non-zero for a kernel, hypervisor, Kata, host-agent, parser or hardware flaw. Tests are evidence of expected controls, not proof of absence of vulnerabilities. “Verification test” below is a required adversarial test in the planned security suite.

| Threat | Attack surface | Required mitigation | Residual risk | Verification test |
|---|---|---|---|---|
| Fork bomb | Python process creation | Per-VM cgroup PID cap; `pids.max`; process-tree termination | Kernel/runtime accounting defect | Fork until denied; assert hard cap and complete kill |
| Memory exhaustion | Python allocation / mmap | cgroup hard memory limit; no swap; OOM confined to guest | Guest/runtime overhead or host pressure | Allocate beyond cap; worker survives and measured limit is recorded |
| CPU exhaustion | Busy loop / many threads | CPU quota, per-move and total monotonic deadline, admission control | Quota fairness varies by hardware/kernel | Busy-loop, spawn threads; verify quota/deadline and peer/control plane remain responsive |
| Disk/inode exhaustion | `/tmp`, cwd, file creation | Read-only root/source; bounded tmpfs/inodes/file size | Kernel tmpfs accounting bug | Fill bytes/inodes; assert ENOSPC/limit and cleanup |
| Filesystem traversal | Archive parser/stager, path operations | Canonical ASCII path policy; re-parse; dirfd-relative no-follow extraction | Parser/extraction implementation flaw | `../`, absolute, drive/backslash and prefix tricks cannot write outside stage |
| Symlink attacks | Archive members / writable temp paths | Reject archive links; exclusive no-follow file creation; no shared writable dirs | Runtime bug or trusted stager compromise | Link/hardlink entries reject; symlink race fixture cannot escape stage |
| Container/VM escape | Syscalls, devices, runtime interfaces | Dedicated Linux/KVM worker; Kata VM per AI; patch/pin; seccomp/MAC; no host devices | Hypervisor/kernel/runtime 0-day | Approved escape corpus/red-team suite; independent security review; no claim of proof |
| Privilege escalation | Capabilities, setuid, user mapping | Non-root UID/GID; all caps dropped; no-new-privileges; user namespace; no setuid | Guest kernel/hypervisor bug | Inspect IDs/caps; attempt setuid/capability syscalls; all fail to gain host privilege |
| Docker socket/daemon access | Socket mounts, host ports, env | No socket mount or proxy; no network route; launcher typed local API inaccessible to guest | Worker agent/host compromise | Search mounts/env; connect to common socket paths/ports; access denied |
| Host filesystem access | Bind mounts, `/proc`, guest device paths | No host mounts; private VM root; minimal synthetic proc/dev; no shared folders | Hypervisor/VM escape; accidental mount | Attempt read known canary paths and host-mounted locations; all absent/inaccessible |
| `/proc` or process inspection | procfs, PID/IPC sharing | Per-VM PID namespace; minimal read-only proc; no host PID/IPC | Guest-visible same-AI processes may be visible | Enumerate proc; only own sandbox tree; no worker/other AI process or environment |
| Network scanning / DNS | Interfaces, routes, resolver, loopback | No network namespace attachment/routes/DNS; host firewall deny all; no listeners | Misconfigured host firewall/runtime | Scan loopback, worker subnet, LAN, public IPs; DNS and all connections fail; monitor no egress |
| PostgreSQL access | Control network, host port, credentials | Separate networks/host; no secrets; firewall deny; no routes | Accidental port exposure or worker compromise | Attempt Compose/dev/event PG IP/ports and credentials are absent; no successful connection |
| Redis access | Control network, host port, credentials | Same as PostgreSQL; Redis never authority and no AI route | Accidental exposure | Attempt Redis TCP and service discovery; fail at network boundary |
| API/control-plane access | Published API/gateway ports, DNS | No route to API/gateway; worker network firewall allowlists only agent-to-gateway mTLS | Firewall drift | Probe API, health, admin and gateway from guest; all unreachable |
| Metadata endpoint access | Cloud link-local endpoints | No cloud role on worker; host edge blocks metadata IPs/routes; no metadata env | Cloud/platform misconfiguration | Probe standard metadata ranges/hostnames; denied and logged outside guest |
| Credential theft | Env, files, argv, proc, logs | Exact allowlist env; no creds mounted/argv; worker keys outside guest | Runtime escape / secret accidentally in protocol | Environment/file/argv/proc scan; assert no canary credential values |
| Artifact replacement / TOCTOU | Quarantine fetch, stage, mutable mounts | Immutable digest key; rehash exact bytes; private exclusive stage; read-only mount; pin descriptor/mount | Compromised storage/worker or implementation race | Replace artifact during fetch/stage/run; hash mismatch/no mutation/no execution |
| Artifact tampering | Storage or worker disk | Hash before use, policy re-parse, stage manifest comparison, read-only mount | SHA-256 theoretical collision or trusted TCB compromise | Modify one byte at each stage; execution is denied and incident recorded |
| Malicious native binary | Upload and runtime | V1 only `.py`, stdlib, strict archive types; Python interpreter only; no native files/deps | Python can still exploit runtime; native behavior via stdlib syscalls | Upload ELF/extension/bytecode/dependency packages; reject intake or never stage |
| Dependency/supply-chain attack | Base image, Python packages, runtime tooling | Server-built minimal image pinned by digest; stdlib only; signed/provenance-checked updates; no runtime package manager/network | Base image/build pipeline compromise | SBOM/image digest allowlist; runtime has no pip compiler/network; provenance review |
| Protocol abuse | Pipes/parser/output | Strict bounded JSONL, request IDs, schema, one outstanding request, deadline; server validates moves | Parser bug, confusing unicode/JSON edge cases | Fuzz malformed/duplicate/oversized/late/extra-line messages; no state mutation |
| Cross-participant contamination | Shared VM/temp/IPC/cache/logs | One fresh VM per AI; private mounts/pipes/cgroups; cleanup verification; no shared writable volume | Host compromise or cleanup defect | Write unique canaries/processes/IPC; next AI cannot discover them after cleanup |
| Stale/forged worker report | Worker API/queue/retries | mTLS worker identity; short lease; monotonic fence; sequence/idempotency; controller validates every report | Compromised authorized worker can lie within its lease | Replay old lease, wrong worker ID, altered sequence/result; control plane rejects |
| Worker host loss/compromise | Agent, kernel, credentials | Least-scoped worker identity; no DB/Redis; revoke cert; quarantine/reimage; independent control-plane authority | Worker can fabricate legal-looking proposals; attestation absent | Kill/revoke worker midgame; stale reports rejected; test restore/reimage procedure |
| Excessive stdout/stderr/log injection | Pipes, logs, public views | Per-line/cumulative limits; concurrent drain; sanitize private bounded diagnostics; no public raw output | Evidence loss due truncation | Emit huge output, ANSI/control chars, secrets; terminate/bound storage and verify public APIs leak none |

## 14. Security test plan (before execution implementation)

Build the adversarial suite and threat fixtures before enabling any real participant code. Tests must run only on a dedicated disposable worker/test VM with synthetic credentials/data; never attach to the event database, Redis or worker fleet. Each test records runtime/kernel/image/policy digests and asserts both the denial and the cleanup result.

### 14.1 Isolation tests

1. Attempt outbound TCP/UDP, public internet, DNS resolution, inbound listener, loopback connection, worker-control gateway, host services, Docker bridge, API, PostgreSQL, Redis and another AI address. Assert no route/connection/DNS success and host-side packet capture shows no guest egress.
2. Probe common cloud metadata IPs/hostnames, Docker socket paths, containerd sockets, SSH, host ports and service-discovery names. Assert absent or denied.
3. Inspect environment, argv, `/proc`, `/sys`, `/dev`, mounts, UID/GID, groups, capabilities, namespaces, cgroups, hostname and filesystem. Assert only documented values/resources are visible; no host process, secret, mount, device, worker identity or other AI appears.
4. Attempt privilege escalation: setuid, setgid, capability changes, `no_new_privs` reversal, namespace creation, mount, ptrace, BPF, raw sockets and prohibited syscalls. Assert deny; verify worker host and sibling sandbox unchanged.
5. Attempt direct/indirect Docker and launcher access over file path and network. Assert no socket/mount/route and no operation reaches daemon.
6. Inspect and attempt to read Windows host, WSL distribution, worker host, control-plane source, database data, Redis data, Docker storage, credentials and unrelated directories. Use canary files; assert inaccessible and unchanged.

### 14.2 Resource and protocol tests

7. Fork repeatedly and exceed PID cap; ensure no further process is created, process tree is killed, PID charge returns to zero and next clean VM has no residue.
8. Allocate RAM/mmap beyond limit; force OOM; verify only the AI VM is terminated and worker/control plane remains healthy.
9. Run CPU-bound loop and thread storm; verify quota, per-move/total deadlines, peer fairness and kill latency.
10. Fill writable tmpfs bytes/inodes and attempt oversized file/FD/core dump; verify caps and cleanup.
11. Flood stdout with oversized line, cumulative output, invalid UTF-8, partial line, no newline, extra protocol line, ANSI escape/control data and valid-looking move after deadline. Flood stderr separately. Verify concurrent drains, hard bounds, termination and no public raw-output disclosure.
12. Fuzz AIK JSONL: duplicate JSON keys, non-finite numbers, invalid types/fields, wrong protocol/request IDs, unexpected messages, bad UCI, illegal move, repeated claims, huge nested JSON and extra bytes. Assert no board/clock/result mutation and correct normalized reason.

### 14.3 Artifact, TOCTOU and cleanup tests

13. Attempt path escape, absolute/drive/backslash paths, dot components, case collisions, USTAR alternate split, PAX/GNU extension, symlink, hardlink, device, malformed/truncated archive and size/member overages. Assert fail-closed before launch.
14. Replace a quarantine object after approval, alter bytes during fetch, change it between hash and staging, tamper staged file before mount, and attempt write from AI. Assert digest/policy failure or read-only denial; no participant process launches on mismatch.
15. Race symlink creation/replacement against staging; attempt traversal through preexisting path. Assert exclusive descriptor-relative creation cannot leave the execution directory.
16. Attempt to modify mounted artifact and base image; assert read-only. Attempt to create executable in `noexec` temp and execute it; assert mount policy; separately verify the approved Python interpreter can read the mounted source.
17. Start multiple hostile jobs sequentially and concurrently. Plant files, symlinks, processes, env markers, pipes and IPC canaries; kill at every phase; prove the next execution cannot see or retain any canary.
18. Kill worker/agent/runtime/host mid-download, launch, ready, move and cleanup. Assert lease fencing, stale report rejection, cleanup/quarantine behavior and no false match result.

### 14.4 Worker/control-plane and operational tests

19. Replay expired lease; submit with wrong mTLS worker identity; forge worker ID, match, submission, version, digest, policy, sequence, timestamp, move and result; duplicate and reorder reports. Assert rejection/idempotency and no official-state mutation.
20. Revoke worker certificate and artifact during execution; assert no new job is accepted and active execution terminates per policy.
21. Inject PostgreSQL/Redis/control-plane outage. Assert Redis alone cannot authorize/commit execution, clock/result are not trusted without durable controller, and execution aborts as infrastructure fault.
22. Verify worker agent can fetch only its assigned artifact and only while lease is valid; cannot query participant credentials, unrelated artifacts or admin APIs.
23. Verify bounded private stderr/security logs and role-based access; participant, opponent and public event endpoints cannot retrieve arbitrary stdout/stderr or worker secret data.
24. Run runtime escape corpus, image/SBOM/signature checks, kernel/seccomp/AppArmor/Kata profile checks, and independent security review on the exact event configuration. Any runtime/kernel/config change invalidates the prior gate until regression suite passes.

## 15. Worker architecture and authority

```text
Control plane
  -> creates immutable execution job + lease/fence + exact artifact binding
  -> authenticates worker via mTLS and assigns least-privilege task
Worker agent
  -> validates assignment and lease
  -> fetches exact artifact; verifies digest and policy
  -> creates two independent one-AI sandboxes for a match
  -> relays private protocol streams and bounded execution evidence
  -> kills, cleans and reports completion/failure
Control-plane match controller
  -> validates worker identity, lease, fence, order and protocol
  -> validates each move/claim against authoritative board/history/clock
  -> applies move and commits official state/results/events transactionally
```

Worker agent must use an outbound authenticated control connection where feasible; firewall permits only worker-gateway/control endpoints and required infrastructure management. Do not expose PostgreSQL, Redis, API admin or Docker daemon ports to workers. Worker credentials are unique per machine, revocable, rotated, stored outside sandboxes, and scoped to receive/ack only assigned jobs and upload bounded evidence. No participant credentials are reused as worker auth. A signed job payload may bind IDs/policy/digests, but signature does not make a stale job valid; lease/fence and durable state are checked at execution and report time.

The trusted launcher and worker agent may have host privileges needed to create Kata VMs. Reduce this TCB: small typed API, fixed command/image allowlists, no shell interpolation, no arbitrary mounts, no participant-controlled labels/OCI options, authenticated local IPC, strict update process, audit log and host firewall. Keep launcher administration off the participant network. If worker authentication, lease fencing, artifact scope, or cleanup proof is absent, do not dispatch.

## 16. Zero-budget development architecture

### Development security (Windows 11 + Docker Desktop + WSL2)

The supported worker listener and production worker deployment target is dedicated Linux. The Python private-key permission check validates POSIX mode bits only; it does not inspect or enforce Windows ACLs. Running the worker listener directly on Windows is unsupported and must not be treated as satisfying private-key protection. Windows/Docker Desktop remains a development/control-plane environment only.

- Keep current Compose API/PostgreSQL/Redis stack for control-plane work only. Tests remain isolated temporary SQLite/in-process tests as documented in `docs/TESTING.md`; never aim sandbox experiments at the event database or Redis.
- Do not execute participant code in the API container, ordinary Compose service, Windows host, or shared WSL distribution. Do not mount Docker socket or host source/secrets into a participant test container.
- Use only synthetic packages and synthetic credentials. First develop parser/launcher policy tests with a harmless trusted test binary; run hostile fixtures only inside an explicitly disposable Linux VM/worker sandbox with snapshots and no event data/network.
- A local Docker `--network none`, non-root user, read-only filesystem, dropped capabilities, seccomp, cgroups and tmpfs can test wiring and negative controls. They are **development checks**, not equivalent to the event Kata microVM boundary. Docker Desktop's WSL2 backend shares the WSL kernel model; integration between Windows/WSL files and Docker exists by design. Do not claim Windows 11 + Docker Desktop + WSL2 offers the same isolation as a dedicated hardened Linux/KVM worker.
- Docker Desktop documents Hyper-V or Docker VMM as VM-backed alternatives and Enhanced Container Isolation as a Business feature with backend-specific limitations. These may be locally evaluated only when available; they do not replace event-day Linux-worker review or security gates. Record the actual backend/version and do not assume availability.
- Keep egress off during malicious tests, or isolate the test VM behind a deny-all firewall with synthetic/private networks only. Collect test artifacts outside the VM after shutdown.

### Event-day security

- Dedicated supported Linux worker(s), no desktop workload, patched/locked host, minimal services, restricted administration, secure boot where available, host firewall, monitoring and reimage procedure.
- One Kata microVM per AI with server-pinned read-only runtime/artifact, independent cgroups/namespaces and deny-all participant networking. Do not colocate API, PostgreSQL or Redis.
- Authenticated worker identity, per-job lease/fence and narrowly scoped artifact transfer. Worker has no database/Redis/participant credentials and no authority over official results.
- Exact kernel/Kata/guest/runtime/image/profile digests, CPU/RAM/PID/disk/output caps, cleanup verification, operator runbook, adversarial suite and independent review pass before enabling execution.
- If the event only has the Windows Docker Desktop host and no separately hardened, supported Linux worker, the security gate remains **closed**. Do not waive this by calling development mode production-ready.

## 17. Event-day remote worker model

Another PC becomes a worker only after operator enrollment, hardening and acceptance tests. It receives a unique worker certificate/identity and connects to the worker gateway over mutually authenticated TLS. The gateway binds certificate identity to registered `worker_id`; jobs are encrypted in transit and scoped to worker, execution, match, side/artifact digest, policy, lease expiry and fencing generation.

The worker receives only:

- execution/match IDs and opaque participant/side references required to correlate the assignment;
- exact submission ID/version and expected artifact digest/size;
- short-lived single-assignment artifact retrieval authorization or streamed artifact bytes;
- server-owned runtime/policy/image digests, protocol version, resource policy and expiry;
- minimum protocol context needed to run the engine and report bounded observations.

It receives no PostgreSQL/Redis access or credentials, participant access/session keys, API secrets, broad object-store credentials, admin credentials, full participant profile/contact data, other matches' artifacts, or authority to create/score matches. It cannot select an artifact or modify job resource policy. Job request from worker is not authoritative; gateway confirms server assignment.

Worker control traffic stays outside the sandbox. Worker should initiate outbound TLS to an allowlisted gateway; inbound worker ports are closed unless a separately approved network design requires them. Control plane validates certificate, active lease and fence on every event. All reports have sequence/idempotency IDs. Stale worker reports are rejected even if cryptographically authentic. Workers are trusted execution infrastructure for the base threat model but remain fallible; malicious-worker resistance/remote attestation is not provided by mTLS and remains an open risk.

## 18. Security gate before first participant execution

Participant code remains disabled unless every gate below has an owner, evidence artifact, and approval:

1. Approved Core Contract and organizer values for package policy, move/total clocks, resource limits, concurrency, timeout/resource-fault treatment, retry/forfeit/void/appeal and logging retention.
2. Dedicated event Linux worker isolated from control-plane/API/PostgreSQL/Redis hosts and networks; no event data on development machines.
3. Exact Kata/KVM/kernel/guest/runtime build pinned, supported, patched and independently reviewed; no ordinary Docker-only execution.
4. TCB review of worker agent, launcher, archive re-parser/stager, lease/fencing, artifact transport, protocol bridge and control-plane validation/commit paths.
5. No participant network route, host/control-plane/database/Redis/Docker/metadata reachability, credentials, sockets, host mounts or devices, verified from inside the actual guest and by host-side packet/permission observations.
6. Hard CPU, memory, PID, disk/inode, FD, output, startup, ready, move and total execution limits enforced and load-tested on event hardware; organizer signs final policy.
7. End-to-end exact-byte digest verification, safe extraction, immutable read-only staging and TOCTOU/race tests pass.
8. Fork bomb, memory/CPU/disk exhaustion, protocol fuzz, filesystem traversal, symlink, cross-run, output flood, cleanup, worker-loss/fencing and network-denial suite passes on the exact event image/runtime/kernel/policy digests.
9. Worker identity, credential protection/revocation, least-scoped artifacts, stale-report rejection, audit monitoring and restore/reimage runbooks exercised.
10. Independent security review/red-team sign-off and named event operator approval. All critical/high findings fixed or explicitly accepted in writing with compensating controls.
11. Operator kill switch blocks new leases and terminates/fences active executions; runbook tested. Evidence storage and incident response are available.
12. A harmless reference engine completes protocol lifecycle in the sandbox and a deliberately malicious fixture is contained. This is an isolation test, not tournament implementation approval.

Any failed, missing, stale or unverified condition keeps the execution feature flag off. Re-run relevant gates whenever kernel, hypervisor, Kata, OCI image, launcher, seccomp/MAC, network policy or resource configuration changes.

## 19. Open policy and infrastructure decisions

Organizer approval is still required for:

1. Final CPU/RAM/PID/disk/inode/FD/output/startup/ready/move/total lifetime values and fair hardware normalization.
2. Global match concurrency, worker count, queue policy, worker eligibility/capacity and event workload peak.
3. Official move clock, increment/delay, whether transport/startup is charged, and treatment when proposed 2-second / 15-minute technical ceilings are reached.
4. Whether participant faults map to forfeit, retry, void, disqualification or another outcome; infrastructure retry budgets and appeal/suspension process.
5. Whether Kata is deployed via a particular VMM (QEMU/Cloud Hypervisor), required KVM/hardware, host distro/kernel lifecycle and worker provisioning/patch cadence.
6. Whether to evaluate gVisor or another runtime as an alternate to Kata; equivalence evidence and reapproval requirements.
7. Artifact transport/storage backend, encryption at rest/in transit, retention/deletion, backup/restore, signed image and SBOM policy.
8. Worker TLS CA, identity enrollment, key storage/rotation/revocation, remote worker network/VPN/firewall and any attestation requirement.
9. Security-event and stderr access roles, retention periods, incident disclosure and participant appeal access.
10. Worker trust assumptions: whether organizers accept trusted-but-fallible worker hardware, require independent review/attestation, or require trusted hardware for result evidence.
11. Whether participants may see any additional non-identifying match context or randomness seed; V1 default is protocol-minimum context and no secrets.
12. Whether event operation has sufficient Linux/KVM machines and staffing. If not, postpone execution; do not weaken the boundary implicitly.

## 20. Implementation sequence

Each stage has independent acceptance tests. Later stages depend on the previous stage passing. No stage is authorization for tournament features.

1. **Policy freeze and threat-model review** — organizer approves exact limits, failure/scoring treatment, worker trust, retention and event infrastructure; update versioned policy only.
2. **Linux worker lab baseline** — provision disposable dedicated Linux/KVM host; pin kernel/Kata/guest image; verify host isolation, patching, logging, firewall and reimage runbook. No participant artifacts.
3. **Sandbox launcher contract** — typed local launch/stop/status API; create harmless fixed-image guest, namespaces, mounts, cgroups, capabilities, seccomp/MAC, network deny and cleanup. No app API integration.
4. **Adversarial isolation suite** — implement tests in §14 against exact host/runtime; resolve findings before any participant code. Test synthetic hostile programs only inside the lab.
5. **Artifact-to-read-only staging** — worker fetches synthetic approved packages, rehashes, reparses and safely stages; TOCTOU, symlink, path and immutable mount tests; no match/chess logic.
6. **Worker identity and lease/fencing** — mTLS enrollment, scoped job/artifact access, expiry, idempotency, stale-worker rejection, key rotation/revocation; test with fake jobs and no DB credentials on worker.
7. **AIK protocol bridge** — trusted pipe broker implements existing protocol limits and sends proposals to a mock authoritative controller; strict fuzzing and time/output caps; no chess tournament.
8. **Resource/failure/cleanup lifecycle** — enforce cgroup and wall limits, classify infrastructure vs participant faults, verify process/VM teardown, worker health quarantine and kill switch.
9. **Independent security review and security gate** — run full tests, inspect exact event configuration and sign gate evidence before any real participant artifact. Keep feature disabled until approved.
10. **Separate chess rules/controller milestone** — implement server-authoritative chess move validation, clocks, result persistence and events only after sandbox prerequisites; keep workers non-authoritative.
11. **Single controlled end-to-end match prototype** — two test-owned harmless engines, exact digest pinning, replay, failure injection and cleanup; no tournament scheduling or public execution.
12. **Operational readiness** — event load tests, backup/restore, monitoring, incident/worker-loss drills, operator kill switch and explicit organizer go/no-go.

## 21. Current implementation status and non-goals

### Worker trust and transfer prototype status

This implementation adds a worker identity/enrollment data model and a separate `create_worker_gateway_app()` ASGI application under `/worker/v1`. The router is deliberately **not mounted** in the participant API and has no public enrollment-token issuance route. An operator-only service function issues a one-time, five-minute hashed enrollment token. Enrollment binds a verified certificate fingerprint and the `urn:aik:worker:<worker_id>` URI SAN to that worker ID, then stages the certificate. Explicit operator activation is required before the worker can authenticate. The database stores only public certificate identity/serial/fingerprint and lifecycle state; worker and CA private keys are never stored there.

`WorkerGatewayTLSConfig.from_env()` is fail-closed: it requires `WORKER_TLS_ENABLED=true`, CA/server certificate and key paths, a server DNS name, and a specific loopback/private bind IP. It validates certificate dates, direct CA signature, SAN, serverAuth EKU, key match, and builds TLS 1.2+ with `CERT_REQUIRED`. The gateway lifespan requires a dedicated `WORKER_DATABASE_URL` and refuses to start without it or valid TLS material; it does not silently reuse the participant API database setting. Migration `0005_worker_gateway_privileges` defines the non-login `aik_worker_gateway` role: reads workers, enrollment tokens, jobs and capabilities; narrowly updates only pending enrollment fields, heartbeat/capabilities, token consumption, job lease/sequence fields and capability-use state; inserts append-only evidence/events; and reads only `submission_versions` and `quarantine_objects` for immutable artifact retrieval. It has no access to participant, session, match, submission, Redis, or unrelated tables and cannot activate/revoke workers, issue enrollment/rotation tokens, insert worker registrations, or create execution jobs. A separate operator/control-plane principal must perform those actions. Applying the migration requires a database migration principal allowed to create roles; deployment must create a separate login principal and grant it membership in this role. The migration does not create credentials or connect to any database. These are source-defined grants and have not been applied or verified against a deployed PostgreSQL role. Worker agents must never receive the URL or DB credentials. Worker client configuration checks its worker URI SAN, clientAuth EKU, key, CA and server hostname. Peer identity is accepted only from the ASGI TLS extension and is independently checked against the configured trusted CA and certificate lifetime; forwarding headers are never used. Current ASGI TLS extension versions do not require a `tls_version` field, so protocol strength must be enforced by the listener SSL context. See the [ASGI TLS extension specification](https://asgi.readthedocs.io/en/latest/specs/tls.html).

`python -m app.workers.serve` is the dedicated listener entry point. It uses pinned Hypercorn 0.17.3, binds only the configured private/loopback address, supplies the server certificate/key and CA, explicitly sets TLS 1.2 minimum and `CERT_REQUIRED` on Hypercorn's created SSL context, and disables access logging. Its error logger emits only bounded listener categories; it does not print exception text or certificate/request contents. This does **not** establish that Hypercorn emits an event for every pre-ASGI TLS handshake failure. A bounded listener category can be logged only when the selected Hypercorn path reports an error; ASGI security-event handlers do not observe failures rejected before ASGI. Verify failure visibility, logger construction and TLS negotiation on the real pinned Hypercorn version before relying on TLS failure audit completeness. The application independently validates the supplied leaf against the configured CA and URI SAN. The app lifespan also validates/constructs TLS policy before startup. The listener and connection path have **not been run through a real TLS handshake**. Do not expose the gateway or enable worker traffic until a deployment has proved Hypercorn's mTLS behavior, TLS extension contents, listener failure visibility and rejection behavior with real certificates. Current participant Uvicorn configuration is not used for the worker gateway. The ASGI test shim only simulates the extension.

Certificate provisioning and operator procedure (to be performed on the dedicated worker environment, not in this repository):

1. Create a dedicated worker CA offline; keep its private key offline and back it up under the organization’s key-management policy. Never put CA private keys in source control, images, Compose, or the API host.
2. Generate each worker private key on that worker with restrictive file permissions. Generate a CSR and issue a short-lived leaf certificate containing exactly one URI SAN `urn:aik:worker:<registered-worker-id>` and `clientAuth` EKU. Never upload the worker private key.
3. Issue the gateway leaf with the configured DNS name in its DNS SAN and `serverAuth` EKU. Keep the gateway private key readable only by the dedicated service account.
4. Mount the CA certificate bundle, gateway certificate and private key read-only. Configure `WORKER_TLS_ENABLED=true`, `WORKER_TLS_CA_CERT`, `WORKER_TLS_SERVER_CERT`, `WORKER_TLS_SERVER_KEY`, `WORKER_TLS_SERVER_NAME`, `WORKER_TLS_BIND_HOST` (specific loopback/private IP), and optional `WORKER_TLS_PORT` (default 8443). The worker process uses its own CA bundle, client certificate/key, registered worker ID, and expected server name.
5. An operator issues an ENROLL token, the worker presents the matching certificate and token, and the control plane stages it. The operator reviews the worker identity/capabilities out of band and calls the internal `activate_worker` operation. There is intentionally no public operator/admin HTTP route.
6. For rotation, issue a ROTATE token to an already active worker. PostgreSQL enforces at most one unused/unrevoked rotation token per worker; the service locks the worker row, revokes expired tokens, and refuses a second outstanding issuance. The old certificate remains active while the new URI-SAN-bound certificate is staged. After checking possession and out-of-band worker state, activate the pending certificate; the database atomically changes the fingerprint, and the old certificate is rejected on its next request. Revoke the worker on suspected key compromise; revocation fences leases, revokes capabilities, and invalidates unused enrollment/rotation tokens.
7. Monitor certificate expiry and rotate before expiry. Deployment must define issuance, revocation/CA compromise response, audit retention, and synchronized clocks before event use. Current implementation supports direct leaf signing by a configured CA certificate; intermediate-chain PKI is not implemented.

### Deployment boundary and network policy

```text
Control plane / operator (private control network)
           │ internal authorized operations and database state
           ▼
Worker TLS gateway (private address, TCP 8443; mTLS required)
           ▲
           │ worker initiates outbound TCP 8443; response uses same TLS connection
           └────────── Worker agent (no inbound listener)
```

The gateway is a separate ASGI application and process boundary; it is not mounted in the participant API. The only planned worker-facing port is TCP 8443 on a private/VPN interface. Worker machines initiate outbound mTLS to the allowlisted gateway address; do not open an inbound port on a worker. The gateway may open a private database connection using `WORKER_DATABASE_URL`, but does not expose PostgreSQL or Redis to workers and has no Redis dependency. Do not publish gateway, PostgreSQL, Redis, participant API administration, Docker, or launcher ports to the public internet. Worker network policy denies access to all of those services except the gateway connection; the worker process receives no participant credentials, API signing secrets, Redis settings, or DB credentials. This network segmentation is a required deployment control and has not been deployed or verified in this environment.

Jobs can only be created by a trusted internal service call from an `ELIGIBLE` immutable submission version. Job identity and policy binding are immutable. Every job is hard-coded and database-constrained to `MOCK_PROTOCOL_ONLY`. Worker claims use expiring leases, monotonically increasing fencing tokens, and ordered sequence numbers; revocation fences active work and invalidates capabilities. Artifact authorization is a short-lived, single-use capability bound to worker, execution, lease/fence, digest, submission version, operation and expiry. The gateway returns exact quarantine bytes only after re-verifying their digest and size and marks the response `no-store`. Worker request bodies are capped at 16 KiB in the ASGI app and each protocol version is capped at 64 UTF-8 bytes with total protocol metadata capped at 512 bytes; enforcement by the real Hypercorn listener has not been verified. The current artifact response is buffered, not resumable or staged: authorization and capability consumption commit before the response object is returned to the ASGI server. Revocation prevents subsequent application-layer authorization and fences leases, but cannot recall bytes already buffered or transmitted. This source-level transaction boundary does not make claims about cancellation/timing on a network. If policy later requires aborting an in-progress stream, implement and verify a streaming transport with cancellation/fencing checks before each chunk. If a transfer is interrupted after capability consumption, the worker must obtain a fresh capability using a valid lease and next sequence. No extracted files or package contents are staged by this control plane. Evidence is bounded, append-only, and cannot authoritatively set match outcomes. A worker cleanup report always enters `CLEANUP_REPORTED`; only an internal trusted control-plane operation can finalize it. PostgreSQL rejects `COMPLETED` transitions by a login that is a member of `aik_worker_gateway`. The mock worker only verifies explicitly allowlisted fixture bytes and has no artifact launch/execute operation.

The PostgreSQL migration adds the worker/enrollment/job/capability/evidence/security-event tables and database guards. It has **not** been applied to a database; only offline SQL generation is supported in this environment. There is no migration application to event data in this prototype.

### Explicit security status

- **WORKER TRUST:** mTLS certificate URI-SAN identity plus server-side worker authorization is the protocol design; certificate parsing/context construction, direct CA verification, staged activation/rotation and simulated ASGI protocol behavior have isolated tests. The Hypercorn listener entry point exists but real handshake/chain validation and deployment have not been verified. Operator lifecycle functions are internal service calls; no trusted operator interface is implemented.
- **ARTIFACT ACCESS:** execution-scoped, short-lived, one-use capability is implemented and protocol-tested using controlled fixture bytes. The worker has no database, Redis, quarantine mount, or broad storage credentials.
- **MATCH AUTHORITY:** control plane only. The gateway accepts bounded, non-authoritative evidence; it has no result-setting route.
- **AUDIT:** lifecycle, transfer, validation and protocol rejection events are append-only and exclude request bodies, authorization headers, and raw token values. The listener disables access logs and uses bounded error categories without exception, certificate or request contents. Pre-ASGI handshake failures are not guaranteed to reach that logger; no claim of complete TLS failure auditing is made until tested with the pinned Hypercorn build and real TLS infrastructure. Record only a bounded category such as `WORKER_LISTENER_ERROR` if the listener reports a failure; never record raw handshake exceptions or certificate material.
- **DEVELOPMENT:** mock worker only. Tests are **MOCKED — DOES NOT PROVE SANDBOX SECURITY**.
- **EVENT-DAY:** dedicated Linux/KVM host and approved Kata runtime remain required before real participant execution.
- **REAL SANDBOX EXECUTION:** **NOT IMPLEMENTED / NOT VERIFIED**. No participant artifact was executed. Container escape resistance, network isolation, host filesystem/privilege isolation, resource-limit enforcement, Kata isolation, and cross-execution isolation are **NOT VERIFIED — DEDICATED WORKER REQUIRED**.

The current environment has no accessible Docker daemon and no Kata runtime. Worker gateway, job creation, token issuance, listener binding, certificate issuance tooling, and migration application are not wired into Compose or the participant API. This prototype does not implement a live worker agent, CA signing/provisioning tooling, sandbox launcher, process lifecycle, chess, AIK match loop, tournament, leaderboard, spectator feed or admin UI. Resource policy includes provisional defaults from the contract, while move timeout, total process lifetime and concurrency require organizer-approved values. No worker suitability scheduler/admission controller is implemented.

## 22. Reference material reviewed

Current upstream documentation reviewed on 2026-10-02 for platform distinctions and isolation model:

- [Docker Desktop WSL 2 backend on Windows](https://docs.docker.com/desktop/features/wsl/) — WSL2 integration/security model and shared-kernel considerations.
- [Docker Desktop Enhanced Container Isolation limitations](https://docs.docker.com/desktop/enterprise/hardened-desktop/enhanced-container-isolation/limitations/) — backend-specific ECI limitations.
- [Docker Desktop permission requirements on Windows](https://docs.docker.com/desktop/setup/install/windows-permission-requirements/) — Linux VM boundary and host access conditions.
- [Docker seccomp profiles](https://docs.docker.com/engine/security/seccomp/) — default syscall filtering behavior.
- [gVisor security model](https://gvisor.dev/docs/architecture_guide/security/) and [gVisor networking](https://gvisor.dev/docs/user_guide/networking/) — gVisor isolation model and networking options; gVisor is an evaluated alternative, not the selected event runtime in this design.

These references do not certify a deployment. Confirm supported versions, platform compatibility and current advisories when implementing and again before the event.
