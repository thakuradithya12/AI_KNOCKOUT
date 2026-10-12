const API_ROOT = "/api/v1";

export class ApiError extends Error {
  constructor(status, message, payload = null) { super(message); this.status = status; this.payload = payload; }
}

async function request(path, { method = "GET", body, rawBody, token, signal, headers = {} } = {}) {
  let response;
  try {
    response = await fetch(`${API_ROOT}${path}`, {
      method,
      headers: { ...(body ? { "Content-Type": "application/json" } : {}), ...(token ? { Authorization: `Bearer ${token}` } : {}), ...headers },
      body: rawBody ?? (body ? JSON.stringify(body) : undefined),
      signal,
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
    if (method === "POST" && path === "/participants/register" && status === 409) message = "That name or email is already registered.";
    if (method === "POST" && path === "/auth/login" && (status === 401 || status === 403)) message = "Participant ID or Access Key is incorrect.";
    if (method === "POST" && path === "/participants/register" && status === 422) message = "Check the name and email format, then try again.";
    if (method === "POST" && path.match(/^\/submissions\/[^/]+\/versions$/) && status === 422) message = "The uploaded package did not pass validation.";
    throw new ApiError(status, message, payload);
  }
  return response.status === 204 ? null : response.json();
}

export const api = {
  health: () => request("/health"),
  register: (body) => request("/participants/register", { method: "POST", body }),
  login: (body) => request("/auth/login", { method: "POST", body }),
  me: (token, signal) => request("/participants/me", { token, signal }),
  eventStatus: (token, signal) => request("/participants/me/event-status", { token, signal }),
  selectOfficialSubmission: (token, selection) => request("/participants/me/official-submission", { method: "PUT", token, body: selection }),
  submissions: (token, signal) => request("/submissions", { token, signal }),
  createSubmission: (token) => request("/submissions", { method: "POST", token }),
  uploadVersion: (token, submissionId, bytes) => request(`/submissions/${encodeURIComponent(submissionId)}/versions`, {
    method: "POST", token, rawBody: bytes, headers: { "Content-Type": "application/x-tar" },
  }),
  submissionVersions: (token, submissionId, signal) => request(`/submissions/${encodeURIComponent(submissionId)}/versions`, { token, signal }),
  validation: (token, submissionId, version, signal) => request(`/submissions/${encodeURIComponent(submissionId)}/versions/${encodeURIComponent(version)}/validation`, { token, signal }),
  developmentMatches: (token, signal) => request("/participants/me/development-matches", { token, signal }),
  requestDevelopmentMatch: (token, body, idempotencyKey) => request("/participants/me/development-matches", {
    method: "POST", token, body, headers: { "Idempotency-Key": idempotencyKey },
  }),
  developmentMatch: (token, matchId, signal) => request(`/participants/me/development-matches/${encodeURIComponent(matchId)}`, { token, signal }),
  logout: (token) => request("/auth/logout", { method: "POST", token }),
  tournamentBracket: (signal) => request("/tournaments/bracket", { signal }),
  tournamentLeaderboard: (signal) => request("/tournaments/leaderboard", { signal }),
  tournamentMatches: (signal) => request("/tournaments/matches", { signal }),
  tournamentMatch: (matchId, signal) => request(`/tournaments/matches/${encodeURIComponent(matchId)}`, { signal }),
};
