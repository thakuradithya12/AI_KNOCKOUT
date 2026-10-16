import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { api, ApiError } from "../assets/api.js";
import { credentialsText } from "../assets/credentials.js";

const appCode = readFileSync(new URL("../assets/app.js", import.meta.url), "utf8");

function mockFetch(handler) {
  const original = globalThis.fetch;
  globalThis.fetch = handler;
  return () => { globalThis.fetch = original; };
}

test("public portal renders exactly three navigation destinations and requires organizer-issued credentials without public registration", () => {
  // Exactly three destinations
  assert.match(appCode, /data-view="participant"/);
  assert.match(appCode, /data-view="admin"/);
  assert.match(appCode, /data-view="arena"/);
  assert.doesNotMatch(appCode, /data-view="tournament"/);
  assert.doesNotMatch(appCode, /data-view="leaderboard"/);

  // Participant authentication is sign-in only with organizer access notice
  assert.match(appCode, /id="panel-login"/);
  assert.match(appCode, /id="login-form"/);
  assert.match(appCode, /ORGANIZER-CONTROLLED ACCESS/);
  assert.match(appCode, /id="register-form"/);
});

test("chessboard piece animation sequence and reduced-motion guard are implemented", () => {
  assert.match(appCode, /const OPENING_ANIMATION_SEQUENCE =/);
  assert.match(appCode, /function startLandingAnimation\(\)/);
  assert.match(appCode, /function stopLandingAnimation\(\)/);
  assert.match(appCode, /function isReducedMotionPreferred\(\)/);
  assert.match(appCode, /prefers-reduced-motion: reduce/);
  assert.match(appCode, /pieceSpan\.style\.transition =/);
  assert.match(appCode, /pieceSpan\.style\.transform =/);
});

test("register form collects name, optional team name, and optional email with limits", () => {
  assert.match(appCode, /id="register-form"/);
  assert.match(appCode, /id="reg-name" name="name"[\s\S]*?minlength="2" maxlength="100"[\s\S]*?required/);
  assert.match(appCode, /id="reg-team" name="team_name"[\s\S]*?maxlength="100"/);
  assert.match(appCode, /id="reg-email" name="email" type="email"[\s\S]*?maxlength="255"/);
  assert.match(appCode, /<button class="button button-block" type="submit">Register<\/button>/);
});

test("client-side registration validation rejects invalid names and malformed emails", () => {
  assert.match(appCode, /if \(name\.length < 2\)/);
  assert.match(appCode, /Participant name must be at least 2 characters\./);
  assert.match(appCode, /if \(name\.length > 100\)/);
  assert.match(appCode, /if \(\/\[\\x00-\\x1f<>\]\/\.test\(name\)\)/);
  assert.match(appCode, /Participant name contains prohibited characters\./);
  assert.match(appCode, /if \(team_name && \/\[\\x00-\\x1f<>\]\/\.test\(team_name\)\)/);
  assert.match(appCode, /Team name contains prohibited characters\./);
  assert.match(appCode, /Please enter a valid email address\./);
});

test("registration binds idempotency key and prevents duplicate submit during transit", () => {
  assert.match(appCode, /const idempotencyKey =/);
  assert.match(appCode, /api\.register\(payload, idempotencyKey\)/);
  assert.match(appCode, /button\.setAttribute\("aria-busy", "true"\)/);
  assert.match(appCode, /button\.dataset\.busy = "true"/);
  assert.match(appCode, /button\.disabled = true/);
});

test("credential receipt view renders credentials, copy controls, and pre-fills sign-in", () => {
  assert.match(appCode, /class="access-layout credential-layout"/);
  assert.match(appCode, /class="access-panel credential-panel"/);
  assert.match(appCode, /code id="cred-participant-id"/);
  assert.match(appCode, /code id="cred-access-key"/);
  assert.match(appCode, /id="copy-participant-id"/);
  assert.match(appCode, /id="copy-access-key"/);
  assert.match(appCode, /id="copy-all-credentials"/);
  assert.match(appCode, /id="proceed-to-login"/);
  assert.match(appCode, /class="credential-warning"/);
  assert.match(appCode, /landing\("login", "", \{ participant_id: result\.participant_id, access_key: result\.access_key \}\)/);
});

test("api.register passes idempotency key header when provided", async () => {
  let capturedHeaders = null;
  const restore = mockFetch(async (url, options) => {
    capturedHeaders = options.headers;
    return new Response(JSON.stringify({
      participant_id: "part_test123",
      name: "Test",
      status: "ACTIVE",
      access_key: "ak_secretkey12345678901234567890",
      created_at: new Date().toISOString(),
    }), { status: 201 });
  });

  try {
    const res = await api.register({ name: "Test" }, "idem-uuid-999");
    assert.equal(res.participant_id, "part_test123");
    assert.equal(capturedHeaders["Idempotency-Key"], "idem-uuid-999");
  } finally {
    restore();
  }
});

test("api.register formats server 409 and 422 errors gracefully", async () => {
  const restore409 = mockFetch(async () => new Response(JSON.stringify({ detail: "A participant with this name is already registered." }), { status: 409 }));
  try {
    await assert.rejects(
      api.register({ name: "Dupe" }),
      (err) => err instanceof ApiError && err.status === 409 && err.message === "A participant with this name is already registered."
    );
  } finally {
    restore409();
  }

  const restore422 = mockFetch(async () => new Response(JSON.stringify({ detail: [{ msg: "Name contains prohibited characters." }] }), { status: 422 }));
  try {
    await assert.rejects(
      api.register({ name: "<script>" }),
      (err) => err instanceof ApiError && err.status === 422 && err.message === "Name contains prohibited characters."
    );
  } finally {
    restore422();
  }
});

test("api.register formats server 401 ORGANIZER_AUTHENTICATION_REQUIRED error without masking", async () => {
  const restore401 = mockFetch(async () => new Response(JSON.stringify({ detail: "ORGANIZER_AUTHENTICATION_REQUIRED" }), { status: 401 }));
  try {
    await assert.rejects(
      api.register({ name: "Unauthenticated" }),
      (err) => err instanceof ApiError && err.status === 401 && err.message === "ORGANIZER_AUTHENTICATION_REQUIRED"
    );
  } finally {
    restore401();
  }
});

test("api.organizerRegisterParticipant passes CSRF and idempotency headers to admin endpoint", async () => {
  let capturedUrl = null;
  let capturedHeaders = null;
  const restore = mockFetch(async (url, options) => {
    capturedUrl = url;
    capturedHeaders = options.headers;
    return new Response(JSON.stringify({
      participant_id: "part_org123",
      name: "OrgTest",
      status: "ACTIVE",
      access_key: "ak_orgsecretkey12345678901234567890",
      created_at: new Date().toISOString(),
    }), { status: 201 });
  });

  try {
    const res = await api.organizerRegisterParticipant({ name: "OrgTest" }, "csrf-token-abc", "idem-uuid-org");
    assert.equal(res.participant_id, "part_org123");
    assert.match(capturedUrl, /\/admin\/participants\/register$/);
    assert.equal(capturedHeaders["X-CSRF-Token"], "csrf-token-abc");
    assert.equal(capturedHeaders["Idempotency-Key"], "idem-uuid-org");
  } finally {
    restore();
  }
});

test("organizer dashboard defines authorized Create Participant form with fields and limits", () => {
  assert.match(appCode, /id="admin-create-participant-form"/);
  assert.match(appCode, /id="admin-reg-name" name="name"[\s\S]*?minlength="2" maxlength="100"[\s\S]*?required/);
  assert.match(appCode, /id="admin-reg-team" name="team_name"[\s\S]*?maxlength="100"/);
  assert.match(appCode, /id="admin-reg-email" name="email" type="email"[\s\S]*?maxlength="255"/);
  assert.match(appCode, /id="admin-create-submit">Create Participant<\/button>/);
});

test("organizer credential receipt displays issued credentials once with separate copy controls", () => {
  assert.match(appCode, /id="admin-credential-receipt"/);
  assert.match(appCode, /id="admin-cred-id"/);
  assert.match(appCode, /id="admin-cred-key" class="cred-secret"/);
  assert.match(appCode, /id="admin-copy-id"/);
  assert.match(appCode, /id="admin-copy-key"/);
  assert.match(appCode, /id="admin-copy-both"/);
  assert.match(appCode, /id="admin-signin-participant"/);
  assert.match(appCode, /id="admin-create-another"/);
  assert.match(appCode, /Issued credentials are displayed <strong>once<\/strong>/);
});

test("organizer console provides navigation between Matches and Participants", () => {
  assert.match(appCode, /data-admin-page="matches"/);
  assert.match(appCode, /data-admin-page="participants"/);
  assert.match(appCode, /loadAdminParticipants\(\)/);
  assert.match(appCode, /loadPublicOrganizerMatches\(\)/);
});

test("api client exposes organizerLoginPassword for credential-based organizer auth", async () => {
  let capturedBody = null;
  const restore = mockFetch(async (url, options) => {
    capturedBody = JSON.parse(options.body);
    return new Response(JSON.stringify({
      status: "ok",
      session_token: "org_token_xyz",
      csrf_token: "org_csrf_xyz",
    }), { status: 200 });
  });

  try {
    const res = await api.organizerLoginPassword({ email: "arbiter@test.org", password: "Password12345!" });
    assert.equal(res.status, "ok");
    assert.equal(capturedBody.email, "arbiter@test.org");
    assert.equal(capturedBody.password, "Password12345!");
  } finally {
    restore();
  }
});

