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
    return new Response(JSON.stringify({ ok: true }), { status: 200 });
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

test("admin overview, participants, and audit log require organizer secret header", async () => {
  const seen = [];
  const restore = mockFetch(async (url, options) => {
    seen.push([url, options]);
    return new Response(JSON.stringify({ ok: true }), { status: 200 });
  });
  try {
    await api.adminOverview("test-organizer-secret");
    await api.adminParticipants("test-organizer-secret");
    await api.adminAuditLog("test-organizer-secret");
    assert.equal(seen[0][0], "/api/v1/admin/overview");
    assert.equal(seen[0][1].headers["X-Organizer-Secret"], "test-organizer-secret");
    assert.equal(seen[1][0], "/api/v1/admin/participants");
    assert.equal(seen[1][1].headers["X-Organizer-Secret"], "test-organizer-secret");
    assert.equal(seen[2][0], "/api/v1/admin/audit-log");
    assert.equal(seen[2][1].headers["X-Organizer-Secret"], "test-organizer-secret");
  } finally { restore(); }
});

test("admin submissions approval and event freeze send correct mutations and headers", async () => {
  const seen = [];
  const restore = mockFetch(async (url, options) => {
    seen.push([url, options]);
    return new Response(JSON.stringify({ status: "OK" }), { status: 200 });
  });
  try {
    await api.adminApproveSubmission("test-organizer-secret", {
      submission_version_id: "00000000-0000-0000-0000-000000000001",
      reviewer_id: "lead-organizer",
      notes: "Reviewed and approved",
    });
    await api.adminFreeze("test-organizer-secret", {
      reason_code: "OFFICIAL_BUILD_WINDOW_CLOSED",
      actor_id: "lead-organizer",
    });
    assert.equal(seen[0][0], "/api/v1/admin/submissions/approve");
    assert.equal(seen[0][1].method, "POST");
    assert.equal(seen[0][1].headers["X-Organizer-Secret"], "test-organizer-secret");
    assert.deepEqual(JSON.parse(seen[0][1].body), {
      submission_version_id: "00000000-0000-0000-0000-000000000001",
      reviewer_id: "lead-organizer",
      notes: "Reviewed and approved",
    });
    assert.equal(seen[1][0], "/api/v1/admin/freeze");
    assert.equal(seen[1][1].method, "POST");
    assert.equal(seen[1][1].headers["X-Organizer-Secret"], "test-organizer-secret");
    assert.deepEqual(JSON.parse(seen[1][1].body), {
      reason_code: "OFFICIAL_BUILD_WINDOW_CLOSED",
      actor_id: "lead-organizer",
    });
  } finally { restore(); }
});

test("admin calls reject with dedicated organizer authentication message on 401/403", async () => {
  const restore = mockFetch(async () => new Response(JSON.stringify({ detail: "ORGANIZER_AUTHENTICATION_REQUIRED" }), { status: 401 }));
  try {
    await assert.rejects(api.adminOverview("invalid-secret"), (error) => {
      assert.ok(error instanceof ApiError);
      assert.equal(error.status, 401);
      assert.equal(error.message, "Organizer authentication failed. Invalid organizer secret.");
      return true;
    });
  } finally { restore(); }
});

