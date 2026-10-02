const API_ROOT = "/api/v1";

export class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

async function request(path, { method = "GET", body, token, signal } = {}) {
  let response;
  try {
    response = await fetch(`${API_ROOT}${path}`, {
      method,
      headers: { ...(body ? { "Content-Type": "application/json" } : {}), ...(token ? { Authorization: `Bearer ${token}` } : {}) },
      body: body ? JSON.stringify(body) : undefined,
      signal,
      cache: "no-store",
    });
  } catch (error) {
    if (error.name === "AbortError") throw error;
    throw new ApiError(0, "The event server could not be reached. Check your connection and try again.");
  }
  if (!response.ok) {
    const status = response.status;
    let message = status === 401 || status === 403 ? "Your session is no longer valid. Sign in again." : status === 409 ? "That name or email is already registered." : status === 429 ? "Too many attempts. Wait a little and try again." : status >= 500 ? "The event server had a problem. Try again shortly." : "Please check the information and try again.";
    if (method === "POST" && path === "/auth/login" && (status === 401 || status === 403)) message = "Participant ID or Access Key is incorrect.";
    if (method === "POST" && path === "/participants/register" && status === 422) message = "Check the name and email format, then try again.";
    throw new ApiError(status, message);
  }
  return response.status === 204 ? null : response.json();
}

export const api = {
  health: () => request("/health"),
  register: (body) => request("/participants/register", { method: "POST", body }),
  login: (body) => request("/auth/login", { method: "POST", body }),
  me: (token, signal) => request("/participants/me", { token, signal }),
  logout: (token) => request("/auth/logout", { method: "POST", token }),
};
