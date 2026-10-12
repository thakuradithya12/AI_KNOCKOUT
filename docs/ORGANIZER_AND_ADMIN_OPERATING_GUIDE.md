# AI Knockout — Organizer and Administrator Operating Guide
**Event:** AI Knockout — MVSR Engineering College  
**Target Audience:** Tournament Directors, System Administrators, Lead Organizers  
**Document Version:** 1.0  
**Status:** Authoritative Operational Guide

---

## 1. Control Plane Overview

The AI Knockout Administrative Control Plane provides authorized event officials with secure, audited, and deterministic governance over:
- Participant registrations, active sessions, and account status.
- Submitted model packages, static validation logs, and eligibility approvals.
- Global submission freeze enforcement.
- Official knockout match creation, sequencing, and bracket progression.
- Live system audit trails capturing every administrative intervention.

---

## 2. Authentication & Security Policy

### 2.1 Organizer Secret Header
All admin endpoints require the administrative header:
```http
X-Organizer-Secret: <ORGANIZER_ADMIN_SECRET>
```
- The secret is loaded from server environment configuration (`SECRET_KEY` / `ORGANIZER_SECRET`).
- Failed authentication returns `401 Unauthorized`.
- Non-admin requests (missing or invalid secret) cannot read participant metadata, access keys, or internal event queues.
- Admin secrets are never exposed to browser clients, spectator views, or participant tokens.

---

## 3. Administrative Control Endpoints

### 3.1 Overview & System Telemetry
`GET /api/v1/admin/overview`
Returns live system counts across all domains:
```json
{
  "total_participants": 32,
  "active_sessions": 28,
  "total_submissions": 45,
  "approved_versions": 31,
  "official_frozen": false,
  "freeze_reason": null,
  "total_matches": 12,
  "completed_matches": 8,
  "running_matches": 2
}
```

### 3.2 Participant Oversight
`GET /api/v1/admin/participants`
Lists all registered participants, registration timestamps, and account status (`ACTIVE`, `SUSPENDED`, `DISQUALIFIED`).
- Passwords and Access Keys are **never** returned in this response.

### 3.3 Submission Queue & Eligibility Review
`GET /api/v1/admin/submissions`
Lists all submitted versions with their static validation outcomes, SHA-256 artifact digests, and current eligibility state (`QUARANTINED`, `VALIDATED`, `APPROVED`, `REJECTED`).

### 3.4 Approving a Submission Version
`POST /api/v1/admin/submissions/approve`
Grants official competition eligibility to a validated package.
**Request Body:**
```json
{
  "submission_version_id": "7421e66d-143a-4913-9f28-ad634b90f8b2",
  "reviewer_id": "prof-sharma",
  "notes": "Code reviewed; adheres to entrypoint specification."
}
```
**Response (200 OK):**
```json
{
  "submission_version_id": "7421e66d-143a-4913-9f28-ad634b90f8b2",
  "state": "APPROVED",
  "approved_at": "2026-10-12T11:00:00Z",
  "reviewer_id": "prof-sharma"
}
```

### 3.5 Freezing Submissions for Tournament Commencement
`POST /api/v1/admin/freeze`
Atomically closes the build window. Pinning the official submission for all eligible participants.
**Request Body:**
```json
{
  "reason_code": "TOURNAMENT_STARTED",
  "actor_id": "tournament-director-1"
}
```
**Response (200 OK):**
```json
{
  "status": "FROZEN",
  "frozen_at": "2026-10-12T13:30:00Z",
  "frozen_by": "tournament-director-1",
  "reason": "TOURNAMENT_STARTED"
}
```
*Note: Once frozen, participants can no longer modify their official submission selection.*

### 3.6 Scheduling Official Knockout Matches
`POST /api/v1/admin/tournaments/schedule-match`
Enqueues an official match between two frozen participant versions.
**Request Body:**
```json
{
  "white_submission_version_id": "92323326-24c6-47ef-a3a5-fdfb2e21a40b",
  "black_submission_version_id": "d1d9fb3f-33c8-4cca-8889-a9f8fc88dc6f",
  "round_name": "Quarterfinal 1",
  "initial_clock_ms": 300000,
  "move_timeout_ms": 2000
}
```
**Response (201 Created):**
```json
{
  "match_id": "84df0021-ba32-4770-bc42-f815049b4911",
  "purpose": "OFFICIAL",
  "state": "QUEUED",
  "white_participant": {"participant_id": "part_alpha", "name": "Team_Alpha"},
  "black_participant": {"participant_id": "part_beta", "name": "Team_Beta"},
  "created_at": "2026-10-12T13:35:00Z"
}
```

### 3.7 Inspecting Audit Log
`GET /api/v1/admin/audit-log`
Returns an append-only timeline of all administrative interventions, approvals, freeze actions, and manual overrides.

---

## 4. Standard Operating Procedure (SOP)

### Pre-Event Setup (T minus 60 minutes)
1. Verify system health at `/api/v1/health` and `/api/v1/ready`.
2. Inspect worker fleet connectivity.
3. Check `/api/v1/admin/overview` to verify clean state.

### Build Window (10:00 - 13:00)
1. Monitor registration via `/api/v1/admin/participants`.
2. Review newly validated submissions via `/api/v1/admin/submissions`.
3. Approve eligible versions using `/api/v1/admin/submissions/approve`.
4. Encourage participants to lock their official submission via their participant portal.

### Tournament Freeze & Seeding (13:00)
1. Announce 10-minute and 2-minute freeze warnings to the room.
2. At exactly 13:00, execute `POST /api/v1/admin/freeze`.
3. Verify freeze confirmation in `/api/v1/admin/overview`.
4. Inspect the generated bracket via `GET /api/v1/tournaments/bracket`.

### Match Execution (13:30 - 16:00)
1. Schedule matches round-by-round (Round of 16 -> Quarterfinals -> Semifinals -> Finals).
2. Monitor live match status via `GET /api/v1/tournaments/matches`.
3. In case of draws, schedule a playoff match with reversed colors.
4. Verify the official leaderboard standings at `GET /api/v1/tournaments/leaderboard`.
5. Confirm the final Champion from the top-ranked standing.
