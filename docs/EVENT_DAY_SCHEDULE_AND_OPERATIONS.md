# AI Knockout — Event-Day Schedule, Operations & Policy Decisions
**Event:** AI Knockout — MVSR Engineering College  
**Target Audience:** Faculty Coordinators, Student Organizers, Technical Staff  
**Document Version:** 1.0  
**Status:** Event Operations Blueprint

---

## 1. Event-Day Schedule (09:00 - 16:30 IST)

| Time Window | Event Stage | Key Actions & Protocols |
| :--- | :--- | :--- |
| **09:00 – 09:30** | **System Pre-Flight & Health Checks** | Systems Engineer verifies PostgreSQL, Redis, API, and Worker fleet. Admin verifies clean database state. |
| **09:30 – 10:00** | **Participant Check-In & Briefing** | Participants arrive at the lab/venue. Welcome address, rules overview, packaging spec review. |
| **10:00 – 13:00** | **Build & Iteration Window (Live Hackathon)** | Registration open. Participants develop AI models, test locally, submit packages, and run development matches against House AI. Reviewers approve eligible submissions. |
| **12:50 – 13:00** | **Freeze Countdown** | Announcements at T-10m, T-5m, and T-2m. Participants lock their official submission version. |
| **13:00** | **Official Submission Freeze (SHARP)** | Tournament Director triggers `POST /api/v1/admin/freeze`. Official selections locked permanently. |
| **13:00 – 13:30** | **Lunch Break & Bracket Seeding** | Bracket generation via `GET /api/v1/tournaments/bracket`. Review seeds and byes. |
| **13:30 – 14:30** | **Knockout Stage: Round of 16 & Quarterfinals** | Official matches run across worker nodes. Spectator arena broadcasts live boards. |
| **14:30 – 15:30** | **Knockout Stage: Semifinals & 3rd Place Match** | Live commentary on the main projector. Standings update in real-time. |
| **15:30 – 16:00** | **Grand Championship Final** | Best-of-three or single showdown between the two top-ranked engines. |
| **16:00 – 16:30** | **Awards & Closing Ceremony** | Final Leaderboard presentation. Trophy presentation to AI Knockout Champion and Runner-Up. |

---

## 2. Staff Roles and Responsibilities

### 2.1 Tournament Director (Faculty Lead / Head Judge)
- Ultimate authority on rules, disputes, and official standings.
- Authorizes submission eligibility approvals.
- Triggers the official submission freeze.
- Announces bracket pairings and match starts.

### 2.2 Lead Systems Engineer (DevOps / Infrastructure Lead)
- Manages the cloud host, database backups, and worker fleet.
- Monitors server CPU, memory, and container health.
- Executes disaster recovery / failover if necessary.
- Manages admin credentials and mTLS certificates.

### 2.3 Participant Support Desk (Technical Volunteers, 2-3 Staff)
- Assists students with Wi-Fi connectivity and browser access.
- Troubleshoots packaging and tar archive errors (USTAR format).
- Handles lost credential verification.

### 2.4 Spectator & Arena Coordinator
- Operates the public projection screen displaying the Live Spectator Arena (`/arena`).
- Keeps participants and attendees updated on match progress and leaderboard rankings.

---

## 3. Server Outage & Disaster Recovery Procedures

### 3.1 Worker Node Failure during a Match
1. If a worker node crashes or loses network connectivity mid-match:
   - Match controller flags worker heartbeat timeout (`WORKER_FAILURE`).
   - Match transitions to `ABORTED`.
2. Systems Engineer verifies host worker health.
3. Tournament Director re-queues the match via `POST /api/v1/admin/tournaments/schedule-match`.

### 3.2 Coordinator or Database Restart
1. All PostgreSQL transactions are WAL-logged and crash-safe.
2. In-flight matches whose transcripts were interrupted fail closed and are marked `ABORTED`.
3. Registered participants, approved versions, and completed matches are fully preserved.
4. Run `GET /api/v1/admin/overview` to verify state immediately upon restart.

---

## 4. Approval-Gated Organizer Decisions

The following policy decisions must be explicitly decided and approved by the Faculty Organizing Committee prior to event launch:

| # | Policy Area | Proposed Default | Options / Discussion | Status |
| :--- | :--- | :--- | :--- | :--- |
| **1** | **Event Date & Timezone** | 09:30–16:00 IST (UTC+05:30) | Specific calendar date to be fixed with MVSR administration. | **Pending Date** |
| **2** | **Tie-Breaker Rule for Finals** | Playoff match with reversed colors (Blitz 3m+0s). | Option A: Armageddon match (White 5m, Black 4m, Draw odds to Black). Option B: Sonneborn-Berger higher rank wins. | **Approved Default** |
| **3** | **No-Selection Freeze Policy** | Highest version number with `APPROVED` state automatically pinned. | Option A: Auto-select highest approved version. Option B: Disqualify participant if they forgot to select. *(Default Recommended: Option A)* | **Approved Default** |
| **4** | **Late Registration Policy** | Cutoff at 11:30 AM (1.5h before freeze). | After cutoff, new registrations are blocked; existing participants continue developing. | **Approved Default** |
| **5** | **Grand Finals Format** | Best-of-3 games (alternating colors, game 3 randomized color). | Option A: Best of 3. Option B: Single game with coin toss for color. *(Default Recommended: Best of 3)* | **Approved Default** |
