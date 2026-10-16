import test from "node:test";
import assert from "node:assert/strict";
import { api, ApiError } from "../assets/api.js";

function mockFetch(handler) {
  const original = globalThis.fetch;
  globalThis.fetch = handler;
  return () => { globalThis.fetch = original; };
}

test("registration calls the existing participant contract", async () => {
  const restore = mockFetch(async (url, options) => {
    assert.equal(url, "/api/v1/participants/register");
    assert.equal(options.method, "POST");
    assert.deepEqual(JSON.parse(options.body), { name: "Entry", email: "a@b.com" });
    return new Response(JSON.stringify({ participant_id: "part_server", access_key: "one-time" }), { status: 201 });
  });
  try { assert.equal((await api.register({ name: "Entry", email: "a@b.com" })).participant_id, "part_server"); }
  finally { restore(); }
});

test("login and profile use the existing endpoint and bearer session", async () => {
  const seen = [];
  const restore = mockFetch(async (url, options) => {
    seen.push([url, options]);
    return new Response(JSON.stringify({ participant_id: "part_server", name: "Entry" }), { status: 200 });
  });
  try {
    await api.login({ participant_id: "part_server", access_key: "secret-access-key" });
    await api.me("opaque-session");
    assert.equal(seen[0][0], "/api/v1/auth/login");
    assert.equal(seen[1][0], "/api/v1/participants/me");
    assert.equal(seen[1][1].headers.Authorization, "Bearer opaque-session");
  } finally { restore(); }
});

test("login failures use a generic message and hide server details", async () => {
  const restore = mockFetch(async () => new Response(JSON.stringify({ detail: "Internal database trace" }), { status: 401 }));
  try {
    await assert.rejects(api.login({ participant_id: "part_missing", access_key: "secret-access-key" }), (error) => {
      assert.ok(error instanceof ApiError);
      assert.equal(error.message, "Participant ID or Access Key is incorrect.");
      assert.equal(error.message.includes("database"), false);
      return true;
    });
  } finally { restore(); }
});

test("network errors become a safe connection message", async () => {
  const restore = mockFetch(async () => { throw new TypeError("socket details"); });
  try { await assert.rejects(api.health(), (error) => error.status === 0 && !error.message.includes("socket")); }
  finally { restore(); }
});

test("logout revokes the bearer token at the existing endpoint", async () => {
  const restore = mockFetch(async (url, options) => {
    assert.equal(url, "/api/v1/auth/logout");
    assert.equal(options.method, "POST");
    assert.equal(options.headers.Authorization, "Bearer opaque-session");
    return new Response(JSON.stringify({ status: "ok" }), { status: 200 });
  });
  try { assert.equal((await api.logout("opaque-session")).status, "ok"); }
  finally { restore(); }
});

test("event status and official selection use authenticated event contracts", async () => {
  const seen = [];
  const restore = mockFetch(async (url, options) => {
    seen.push([url, options]);
    return new Response(JSON.stringify({ official_frozen: false }), { status: 200 });
  });
  try {
    await api.eventStatus("opaque-session");
    await api.selectOfficialSubmission("opaque-session", {
      submission_id: "00000000-0000-0000-0000-000000000001",
      version: 2,
    });
    assert.equal(seen[0][0], "/api/v1/participants/me/event-status");
    assert.equal(seen[0][1].headers.Authorization, "Bearer opaque-session");
    assert.equal(seen[1][0], "/api/v1/participants/me/official-submission");
    assert.equal(seen[1][1].method, "PUT");
    assert.equal(seen[1][1].headers.Authorization, "Bearer opaque-session");
    assert.deepEqual(JSON.parse(seen[1][1].body), {
      submission_id: "00000000-0000-0000-0000-000000000001",
      version: 2,
    });
  } finally { restore(); }
});

test("submission upload sends exact archive bytes with participant authorization", async () => {
  const bytes = new Uint8Array([0, 1, 37, 255]);
  const restore = mockFetch(async (url, options) => {
    assert.equal(url, "/api/v1/submissions/sub_123/versions");
    assert.equal(options.method, "POST");
    assert.equal(options.headers.Authorization, "Bearer opaque-session");
    assert.equal(options.headers["Content-Type"], "application/x-tar");
    assert.deepEqual([...new Uint8Array(options.body)], [...bytes]);
    return new Response(JSON.stringify({ version: { state: "APPROVED" } }), { status: 201 });
  });
  try {
    const result = await api.uploadVersion("opaque-session", "sub_123", bytes);
    assert.equal(result.version.state, "APPROVED");
  } finally { restore(); }
});

test("oversized artifact errors tell the participant to reduce the package size", async () => {
  const restore = mockFetch(async () => new Response(JSON.stringify({ error: "PACKAGE_TOO_LARGE" }), { status: 413 }));
  try {
    await assert.rejects(api.uploadVersion("opaque-session", "sub_123", new Uint8Array([1])), (error) => {
      assert.equal(error.status, 413);
      assert.match(error.message, /reduce its size/i);
      return true;
    });
  } finally { restore(); }
});

test("submission upload supports text/x-python content type for intelligence.py", async () => {
  const bytes = new TextEncoder().encode("def choose_move(board_fen, legal_moves, time_left_ms):\n    return legal_moves[0]\n");
  const restore = mockFetch(async (url, options) => {
    assert.equal(url, "/api/v1/submissions/sub_123/versions");
    assert.equal(options.method, "POST");
    assert.equal(options.headers["Content-Type"], "text/x-python");
    return new Response(JSON.stringify({ version: { state: "APPROVED" } }), { status: 201 });
  });
  try {
    const result = await api.uploadVersion("opaque-session", "sub_123", bytes, "text/x-python");
    assert.equal(result.version.state, "APPROVED");
  } finally { restore(); }
});

test("submission upload preserves detailed 422 validation failure message from server", async () => {
  const restore = mockFetch(async () => new Response(JSON.stringify({
    detail: "The uploaded Python file is missing a choose_move(...) entrypoint function.",
    rejection_reason: "INTELLIGENCE_ENTRYPOINT_MISSING",
  }), { status: 422 }));
  try {
    await assert.rejects(api.uploadVersion("opaque-session", "sub_123", new Uint8Array([1]), "text/x-python"), (error) => {
      assert.equal(error.status, 422);
      assert.match(error.message, /missing a choose_move\(\.\.\.\) entrypoint/i);
      assert.equal(error.payload.rejection_reason, "INTELLIGENCE_ENTRYPOINT_MISSING");
      return true;
    });
  } finally { restore(); }
});

test("development match creation binds exact request and idempotency key", async () => {
  const restore = mockFetch(async (url, options) => {
    assert.equal(url, "/api/v1/participants/me/development-matches");
    assert.equal(options.method, "POST");
    assert.equal(options.headers.Authorization, "Bearer opaque-session");
    assert.equal(options.headers["Idempotency-Key"], "dev-match-1");
    assert.deepEqual(JSON.parse(options.body), {
      submission_id: "00000000-0000-0000-0000-000000000001",
      version: 2,
    });
    return new Response(JSON.stringify({ match_id: "match-1", state: "QUEUED" }), { status: 201 });
  });
  try {
    const result = await api.requestDevelopmentMatch("opaque-session", {
      submission_id: "00000000-0000-0000-0000-000000000001",
      version: 2,
    }, "dev-match-1");
    assert.equal(result.state, "QUEUED");
  } finally { restore(); }
});

test("tournament bracket and leaderboard use public tournament contracts", async () => {
  const seen = [];
  const restore = mockFetch(async (url, options) => {
    seen.push([url, options]);
    const payload = url.endsWith("/bracket") ? { rounds: [] } : { standings: [] };
    return new Response(JSON.stringify(payload), { status: 200 });
  });
  try {
    await api.tournamentBracket();
    await api.tournamentLeaderboard();
    assert.equal(seen[0][0], "/api/v1/tournaments/bracket");
    assert.equal(seen[0][1].method, "GET");
    assert.equal(seen[1][0], "/api/v1/tournaments/leaderboard");
    assert.equal(seen[1][1].method, "GET");
  } finally { restore(); }
});

test("tournament matches handles bare array and object responses", async () => {
  const bareMatches = [{ match_id: "m-1", state: "RUNNING", white_name: "Alice", black_name: "Bob" }];
  const objectMatches = { items: [{ match_id: "m-2", state: "COMPLETED", white_name: "Carol", black_name: "Dave" }] };
  const emptyMatches = [];

  let callCount = 0;
  const restore = mockFetch(async (url) => {
    assert.equal(url, "/api/v1/tournaments/matches");
    callCount++;
    if (callCount === 1) return new Response(JSON.stringify(bareMatches), { status: 200 });
    if (callCount === 2) return new Response(JSON.stringify(objectMatches), { status: 200 });
    return new Response(JSON.stringify(emptyMatches), { status: 200 });
  });

  try {
    const res1 = await api.tournamentMatches();
    assert.deepEqual(res1, bareMatches);
    const res2 = await api.tournamentMatches();
    assert.deepEqual(res2, objectMatches);
    const res3 = await api.tournamentMatches();
    assert.deepEqual(res3, emptyMatches);
  } finally { restore(); }
});

test("tournament rounds uses the public Swiss and playoff rounds contract", async () => {
  const seen = [];
  const restore = mockFetch(async (url, options) => {
    seen.push([url, options]);
    return new Response(JSON.stringify([]), { status: 200 });
  });
  try {
    await api.tournamentRounds("SWISS");
    await api.tournamentRounds("PLAYOFF");
    assert.equal(seen[0][0], "/api/v1/tournaments/rounds?stage_filter=SWISS");
    assert.equal(seen[1][0], "/api/v1/tournaments/rounds?stage_filter=PLAYOFF");
    assert.equal(seen[0][1].method, "GET");
  } finally { restore(); }
});

test("public tournament endpoints reject mismatched bracket, rounds, and leaderboard schemas", async () => {
  for (const [call, payload] of [
    [() => api.tournamentBracket(), { status: "PENDING" }],
    [() => api.tournamentRounds("SWISS"), { items: [] }],
    [() => api.tournamentLeaderboard(), { standings: {} }],
  ]) {
    const restore = mockFetch(async () => new Response(JSON.stringify(payload), { status: 200 }));
    try {
      await assert.rejects(call(), (error) => error.status === 502 && /did not match the server schema/.test(error.message));
    } finally { restore(); }
  }
});

test("organizer session check uses same-origin cookies and returns the in-memory CSRF contract", async () => {
  let seen;
  const restore = mockFetch(async (url, options) => {
    seen = [url, options];
    return new Response(JSON.stringify({ organizer_email: "organizer@example.test", csrf_token: "csrf-test-value", expires_at: "2026-10-14T20:00:00Z", last_active_at: "2026-10-14T12:00:00Z" }), { status: 200 });
  });
  try {
    const profile = await api.organizerSession();
    assert.equal(profile.csrf_token, "csrf-test-value");
    assert.equal(profile.expires_at, "2026-10-14T20:00:00Z");
    assert.equal(profile.last_active_at, "2026-10-14T12:00:00Z");
    assert.equal(seen[0], "/api/v1/auth/organizer/session");
    assert.equal(seen[1].credentials, "same-origin");
    assert.equal(seen[1].headers.Authorization, undefined);
  } finally { restore(); }
});

test("organizer login bootstrap uses the same-origin endpoint without exposing callback credentials", async () => {
  let seen;
  const restore = mockFetch(async (url, options) => {
    seen = [url, options];
    return new Response(JSON.stringify({ authorization_url: "https://idp.example.test/authorize?state=test", state: "opaque-state", code_verifier: "opaque-verifier" }), { status: 200 });
  });
  try {
    const response = await api.organizerLogin();
    assert.equal(response.authorization_url, "https://idp.example.test/authorize?state=test");
    assert.deepEqual(Object.keys(response), ["authorization_url"]);
    assert.equal(seen[0], "/api/v1/auth/organizer/login");
    assert.equal(seen[1].method, "GET");
    assert.equal(seen[1].credentials, "same-origin");
    assert.equal(seen[1].headers.Authorization, undefined);
  } finally { restore(); }
});

test("organizer result mutation sends same-origin cookie and CSRF header, and refuses missing CSRF", async () => {
  const seen = [];
  const restore = mockFetch(async (url, options) => {
    seen.push([url, options]);
    return new Response(JSON.stringify({ status: "COMPLETED", match_id: "match-42", result_outcome: "DRAW", result_reason: "STALEMATE" }), { status: 200 });
  });
  try {
    await assert.rejects(api.adminRecordMatchResult("match-42", { result_outcome: "DRAW", result_reason: "STALEMATE" }), (error) => error.status === 0);
    assert.equal(seen.length, 0);
    await api.adminRecordMatchResult("match/42", { result_outcome: "DRAW", result_reason: "STALEMATE" }, "csrf-test-value");
    assert.equal(seen[0][0], "/api/v1/admin/tournaments/matches/match%2F42/record-result");
    assert.equal(seen[0][1].credentials, "same-origin");
    assert.equal(seen[0][1].headers["X-CSRF-Token"], "csrf-test-value");
    assert.equal(seen[0][1].headers.Authorization, undefined);
    assert.deepEqual(JSON.parse(seen[0][1].body), { result_outcome: "DRAW", result_reason: "STALEMATE" });
  } finally { restore(); }
});

test("organizer logout requires a CSRF token and uses same-origin cookie credentials", async () => {
  const seen = [];
  const restore = mockFetch(async (url, options) => {
    seen.push([url, options]);
    return new Response(JSON.stringify({ status: "ok" }), { status: 200 });
  });
  try {
    await assert.rejects(api.organizerLogout(), (error) => error.status === 0);
    assert.equal(seen.length, 0);
    await api.organizerLogout("csrf-logout");
    assert.equal(seen[0][0], "/api/v1/auth/organizer/logout");
    assert.equal(seen[0][1].method, "POST");
    assert.equal(seen[0][1].credentials, "same-origin");
    assert.equal(seen[0][1].headers["X-CSRF-Token"], "csrf-logout");
  } finally { restore(); }
});

test("organizer audit read uses the implemented session-protected endpoint and response array", async () => {
  let seen;
  const audit = [{ event_id: "event-1", event_type: "match.result_recorded", actor_kind: "organizer", actor_id: "a@example.test", severity: "info", aggregate_type: "match", aggregate_id: "m-1", sequence: 1, metadata: {}, recorded_at: "2026-10-14T12:00:00Z" }];
  const restore = mockFetch(async (url, options) => {
    seen = [url, options];
    return new Response(JSON.stringify(audit), { status: 200 });
  });
  try {
    assert.deepEqual(await api.adminAuditLog(25), audit);
    assert.equal(seen[0], "/api/v1/admin/audit-log?limit=25");
    assert.equal(seen[1].method, "GET");
    assert.equal(seen[1].credentials, "same-origin");
    assert.equal(seen[1].headers.Authorization, undefined);
  } finally { restore(); }
});

test("organizer audit rejects an object or event missing required backend fields", async () => {
  for (const payload of [{ items: [] }, [{ event_id: "e-1", event_type: "x" }]]) {
    const restore = mockFetch(async () => new Response(JSON.stringify(payload), { status: 200 }));
    try {
      await assert.rejects(api.adminAuditLog(), (error) => {
        assert.equal(error.status, 502);
        assert.match(error.message, /did not match the server event schema/);
        return true;
      });
    } finally { restore(); }
  }
});

test("participant quota and allowance requests call existing contracts", async () => {
  const seen = [];
  const restore = mockFetch(async (url, options) => {
    seen.push([url, options]);
    if (url === "/api/v1/submissions/quota") {
      return new Response(JSON.stringify({ participant_id: "part_test", submission_limit: 10, accepted_submissions_count: 2, remaining_submissions: 8 }), { status: 200 });
    }
    if (url === "/api/v1/submissions/allowance-requests" && options.method === "POST") {
      return new Response(JSON.stringify({ id: "req-1", status: "PENDING" }), { status: 201 });
    }
    return new Response(JSON.stringify({ items: [] }), { status: 200 });
  });
  try {
    const quota = await api.submissionQuota("user-token");
    assert.equal(quota.submission_limit, 10);
    assert.equal(seen[0][0], "/api/v1/submissions/quota");
    assert.equal(seen[0][1].headers.Authorization, "Bearer user-token");

    const req = await api.requestAllowanceIncrease("user-token", { requested_limit: 15, reason: "Testing" });
    assert.equal(req.status, "PENDING");
    assert.equal(seen[1][0], "/api/v1/submissions/allowance-requests");
    assert.equal(seen[1][1].method, "POST");

    const list = await api.myAllowanceRequests("user-token");
    assert.deepEqual(list.items, []);
  } finally { restore(); }
});

test("submission quota exceeded error provides clear guidance", async () => {
  const restore = mockFetch(async () => new Response(JSON.stringify({ error: "SUBMISSION_LIMIT_EXCEEDED", limit: 10, current: 10 }), { status: 403 }));
  try {
    await assert.rejects(api.uploadVersion("user-token", "sub-1", new Uint8Array([1, 2, 3])), (error) => {
      assert.ok(error instanceof ApiError);
      assert.equal(error.status, 403);
      assert.ok(error.message.includes("quota reached (10 uploads)"));
      return true;
    });
  } finally { restore(); }
});

