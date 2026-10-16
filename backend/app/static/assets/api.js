const API_ROOT = "/api/v1";

export class ApiError extends Error {
  constructor(status, message, payload = null) { super(message); this.status = status; this.payload = payload; }
}

async function request(path, { method = "GET", body, rawBody, token, signal, headers = {}, csrfToken, credentials = "same-origin" } = {}) {
  let response;
  try {
    response = await fetch(`${API_ROOT}${path}`, {
      method,
      headers: { ...(body ? { "Content-Type": "application/json" } : {}), ...(token ? { Authorization: `Bearer ${token}` } : {}), ...headers },
      body: rawBody ?? (body ? JSON.stringify(body) : undefined),
      signal,
      credentials,
      cache: "no-store",
    });
  } catch (error) {
    if (error.name === "AbortError") throw error;
    throw new ApiError(0, "The event server could not be reached. Check your connection and try again.");
  }
  if (!response.ok) {
    const status = response.status;
    let payload = null;
    try { payload = await response.clone().json(); } catch { /* Responses may not carry JSON. */ }
    let message = status === 401 || status === 403 ? "Your session is no longer valid. Sign in again." : status === 409 ? "This event action is no longer available. Refresh and try again." : status === 413 ? "The archive exceeds the server upload limit. Reduce its size and try again." : status === 415 ? "Upload an uncompressed USTAR .tar archive." : status === 429 ? "Too many attempts. Wait a little and try again." : status >= 500 ? "The event server had a problem. Try again shortly." : "Please check the information and try again.";
    if (status === 403 && payload?.error === "SUBMISSION_LIMIT_EXCEEDED") message = `Submission quota reached (${payload.limit || 10} uploads). You can request an organizer exception.`;
    if (method === "DELETE" && path.startsWith("/admin/participants/") && status === 409 && String(payload?.detail || "").startsWith("CANNOT_REMOVE_PARTICIPANT_WITH_TOURNAMENT_RECORDS")) message = "This account has submission or tournament history. Suspend it instead so its records stay intact.";
    if (method === "POST" && (path === "/participants/register" || path === "/admin/participants/register") && (status === 401 || status === 403)) {
      message = (typeof payload?.detail === "string" ? payload.detail : null) || "ORGANIZER_AUTHENTICATION_REQUIRED";
    }
    if (method === "POST" && (path === "/participants/register" || path === "/admin/participants/register") && status === 409) message = payload?.detail || "That name or email is already registered.";
    if (method === "POST" && path === "/auth/login" && (status === 401 || status === 403)) message = "Participant ID or Access Key is incorrect.";
    if (method === "POST" && (path === "/participants/register" || path === "/admin/participants/register") && status === 422) {
      if (typeof payload?.detail === "string") {
        message = payload.detail;
      } else if (Array.isArray(payload?.detail) && payload.detail.length > 0) {
        message = payload.detail.map((d) => d.msg || d.detail).join("; ");
      } else {
        message = "Check the name and email format, then try again.";
      }
    }
    if (method === "POST" && path.match(/^\/submissions\/[^/]+\/versions$/) && status === 422) {
      if (typeof payload?.detail === "string") {
        message = payload.detail;
      } else if (payload?.rejection_reason) {
        message = payload.rejection_reason;
      } else if (Array.isArray(payload?.detail) && payload.detail.length > 0) {
        message = payload.detail.map((d) => d.msg || d.detail || JSON.stringify(d)).join("; ");
      } else {
        message = "The uploaded package did not pass validation.";
      }
    }
    throw new ApiError(status, message, payload);
  }
  return response.status === 204 ? null : response.json();
}

const ORGANIZER_AUTH_BLOCKED = "Organizer actions require an active server-managed session and CSRF token.";
function organizerAuthBlocked() { return Promise.reject(new ApiError(0, ORGANIZER_AUTH_BLOCKED)); }
function organizerMutation(path, body, csrfToken, extraHeaders = {}) {
  if (!csrfToken || typeof csrfToken !== "string") return organizerAuthBlocked();
  return request(path, {
    method: "POST",
    body,
    csrfToken,
    headers: { "X-CSRF-Token": csrfToken, ...extraHeaders },
  });
}

export const api = {
  health: () => request("/health"),
  register: (body, idempotencyKey) => request("/participants/register", {
    method: "POST",
    body,
    headers: idempotencyKey ? { "Idempotency-Key": idempotencyKey } : {},
  }),
  login: (body) => request("/auth/login", { method: "POST", body }),
  me: (token, signal) => request("/participants/me", { token, signal }),
  eventStatus: (token, signal) => request("/participants/me/event-status", { token, signal }),
  selectOfficialSubmission: (token, selection) => request("/participants/me/official-submission", { method: "PUT", token, body: selection }),
  submissions: (token, signal) => request("/submissions", { token, signal }),
  createSubmission: (token) => request("/submissions", { method: "POST", token }),
  uploadVersion: (token, submissionId, bytes, contentType = "application/x-tar") => request(`/submissions/${encodeURIComponent(submissionId)}/versions`, {
    method: "POST", token, rawBody: bytes, headers: { "Content-Type": contentType },
  }),
  submissionVersions: (token, submissionId, signal) => request(`/submissions/${encodeURIComponent(submissionId)}/versions`, { token, signal }),
  validation: (token, submissionId, version, signal) => request(`/submissions/${encodeURIComponent(submissionId)}/versions/${encodeURIComponent(version)}/validation`, { token, signal }),
  submissionQuota: (token, signal) => request("/submissions/quota", { token, signal }),
  requestAllowanceIncrease: (token, body) => request("/submissions/allowance-requests", { method: "POST", token, body }),
  myAllowanceRequests: (token, signal) => request("/submissions/allowance-requests", { token, signal }),
  developmentMatches: (token, signal) => request("/participants/me/development-matches", { token, signal }),
  requestDevelopmentMatch: (token, body, idempotencyKey) => request("/participants/me/development-matches", {
    method: "POST", token, body, headers: { "Idempotency-Key": idempotencyKey },
  }),
  developmentMatch: (token, matchId, signal) => request(`/participants/me/development-matches/${encodeURIComponent(matchId)}`, { token, signal }),
  logout: (token) => request("/auth/logout", { method: "POST", token }),
  tournamentBracket: async (signal) => {
    const bracket = await request("/tournaments/bracket", { signal });
    if (!bracket || !Array.isArray(bracket.rounds)) throw new ApiError(502, "The tournament bracket response did not match the server schema.");
    return bracket;
  },
  tournamentRounds: async (stage, signal) => {
    const rounds = await request(`/tournaments/rounds${stage ? `?stage_filter=${encodeURIComponent(stage)}` : ""}`, { signal });
    if (!Array.isArray(rounds)) throw new ApiError(502, "The tournament rounds response did not match the server schema.");
    return rounds;
  },
  tournamentLeaderboard: async (signal) => {
    const leaderboard = await request("/tournaments/leaderboard", { signal });
    if (!leaderboard || !Array.isArray(leaderboard.standings)) throw new ApiError(502, "The leaderboard response did not match the server schema.");
    return leaderboard;
  },
  tournamentMatches: (signal) => request("/tournaments/matches", { signal }),
  tournamentMatch: (matchId, signal) => request(`/tournaments/matches/${encodeURIComponent(matchId)}`, { signal }),
  organizerLogin: async () => {
    const response = await request("/auth/organizer/login");
    if (!response || typeof response.authorization_url !== "string") throw new ApiError(502, "The organizer login response did not include an authorization URL.");
    // The backend keeps PKCE state in its HttpOnly signed cookie; the UI only
    // needs the redirect URL and must not propagate verifier material.
    return { authorization_url: response.authorization_url };
  },
  organizerSession: () => request("/auth/organizer/session"),
  organizerLogout: (csrfToken) => organizerMutation("/auth/organizer/logout", {}, csrfToken),
  organizerRecordMatchResult: (matchId, body, csrfToken) => organizerMutation(`/admin/tournaments/matches/${encodeURIComponent(matchId)}/record-result`, body, csrfToken),
  organizerRegisterParticipant: (body, csrfToken, idempotencyKey) => organizerMutation(
    "/admin/participants/register",
    body,
    csrfToken,
    idempotencyKey ? { "Idempotency-Key": idempotencyKey } : {}
  ),
  // These methods intentionally fail closed until a server-managed organizer
  // session/CSRF contract is implemented. Never send the shared key from JS.
  organizerLoginPassword: (body) => request("/auth/organizer/login-password", { method: "POST", body }),
  // Read-only organizer views are authorized by server-managed session cookies.
  adminOverview: () => organizerAuthBlocked(),
  adminParticipants: (signal) => request("/admin/participants", { signal }),
  adminSubmissions: () => organizerAuthBlocked(),
  adminApproveSubmission: () => organizerAuthBlocked(),
  adminFreeze: () => organizerAuthBlocked(),
  // Read-only audit access is authorized by the server-managed organizer
  // session cookie. Mutations still require organizerMutation + CSRF.
  adminAuditLog: async (limit = 50, signal) => {
    const events = await request(`/admin/audit-log?limit=${encodeURIComponent(limit)}`, { signal });
    const required = ["event_id", "event_type", "actor_kind", "severity", "aggregate_type", "aggregate_id", "sequence", "metadata", "recorded_at"];
    if (!Array.isArray(events) || events.some((event) => !event || typeof event !== "object" || required.some((field) => !(field in event)))) {
      throw new ApiError(502, "The audit log response did not match the server event schema.");
    }
    return events;
  },
  adminAllowanceRequests: () => organizerAuthBlocked(),
  adminDecideAllowanceRequest: () => organizerAuthBlocked(),
  adminUpdateParticipantAllowance: (participantId, body, csrfToken) => organizerMutation(`/admin/participants/${encodeURIComponent(participantId)}/allowance`, body, csrfToken),
  adminParticipantAction: (participantId, action, body, csrfToken) => {
    if (action === "revoke-sessions") {
      return organizerMutation(`/admin/participants/${encodeURIComponent(participantId)}/revoke-sessions`, body, csrfToken);
    }
    return organizerMutation(`/admin/participants/${encodeURIComponent(participantId)}/${encodeURIComponent(action)}`, body, csrfToken);
  },
  adminRemoveParticipant: (participantId, body, csrfToken) => {
    if (!csrfToken || typeof csrfToken !== "string") return organizerAuthBlocked();
    return request(`/admin/participants/${encodeURIComponent(participantId)}`, {
      method: "DELETE",
      body,
      headers: { "X-CSRF-Token": csrfToken },
    });
  },
  adminRecordMatchResult: (matchId, body, csrfToken) => api.organizerRecordMatchResult(matchId, body, csrfToken),
};
