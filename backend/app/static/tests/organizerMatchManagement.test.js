import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import {
  createManualResultRecorder,
  ORGANIZER_RESULT_REASONS,
  organizerResultErrorMessage,
  renderOrganizerMatchManagement,
  renderOrganizerRefreshSnapshot,
  wireOrganizerMatchManagement,
} from "../assets/organizerMatchManagement.js";

const selection = { matchId: "match-42", outcome: "WHITE_WIN", reason: "CHECKMATE", confirmed: true };

test("browser preview labels fixture data and keeps organizer actions disabled", () => {
  const preview = readFileSync(new URL("./organizer-preview.html", import.meta.url), "utf8");
  assert.match(preview, /MOCK DATA ONLY/);
  assert.match(preview, /no server or participant data is connected/i);
  assert.match(preview, /integrationAvailable: false/);
  assert.match(preview, /MOCK-MATCH-001/);
});

test("organizer match view shows match identity, colors, state, and disabled blocked controls", () => {
  const markup = renderOrganizerMatchManagement([{ match_id: "match-42", white_name: "Ada", black_name: "Lin", state: "RUNNING" }]);
  assert.match(markup, /match-42/);
  assert.match(markup, /White/);
  assert.match(markup, /Ada/);
  assert.match(markup, /Black/);
  assert.match(markup, /Lin/);
  assert.match(markup, /RUNNING/);
  assert.match(markup, /<button[^>]*disabled>Record result/);
  assert.match(markup, /until a server-managed organizer session and CSRF token are verified/);
});

test("organizer audit view renders server events and escapes untrusted fields", () => {
  const markup = renderOrganizerMatchManagement([], {
    integrationAvailable: true,
    auditEvents: [{
      event_id: "event-1",
      event_type: "match.result_recorded",
      actor_kind: "organizer",
      actor_id: "<script>alert(1)</script>",
      severity: "info",
      aggregate_type: "match",
      aggregate_id: "match-42",
      sequence: 2,
      metadata: { outcome: "DRAW" },
      recorded_at: "2026-10-14T12:00:00Z",
    }],
  });
  assert.match(markup, /Recent audit events/);
  assert.match(markup, /match\.result_recorded/);
  assert.match(markup, /<th>Severity<\/th>/);
  assert.match(markup, /event-1/);
  assert.match(markup, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
  assert.doesNotMatch(markup, /<script>alert/);
});

test("completed and aborted matches never render editable result controls", () => {
  const markup = renderOrganizerMatchManagement([
    { match_id: "manual-1", state: "COMPLETED", result_outcome: "DRAW", result_source: "manual" },
    { match_id: "automated-1", state: "COMPLETED", result_outcome: "WHITE_WIN", result_source: "automated" },
    { match_id: "legacy-1", state: "COMPLETED", result_outcome: "BLACK_WIN", result_source: "unknown" },
    { match_id: "abort-1", state: "ABORTED" },
  ]);
  assert.match(markup, /Completed matches cannot be changed/);
  assert.match(markup, /Aborted matches cannot receive a result/);
  assert.match(markup, /Manual result/);
  assert.match(markup, /Automated match/);
  assert.match(markup, /Source unverified/);
  assert.doesNotMatch(markup, /<form class="manual-result-form"/);
});

test("match refresh detail labels source and treats absent legacy source as unverified", () => {
  const manual = renderOrganizerRefreshSnapshot({
    state: "success",
    match: { match_id: "manual-1", state: "COMPLETED", result_outcome: "DRAW", result_source: "manual" },
  });
  const legacy = renderOrganizerRefreshSnapshot({
    state: "success",
    match: { match_id: "legacy-1", state: "COMPLETED", result_outcome: "DRAW" },
  });
  assert.match(manual, /Manual result/);
  assert.match(legacy, /Source unverified/);
});

test("match form has required outcome, reason, and explicit final-result confirmation fields", () => {
  const markup = renderOrganizerMatchManagement([{ match_id: "match-42", state: "SCHEDULED" }], { integrationAvailable: true });
  assert.match(markup, /<select name="result_outcome" required/);
  assert.match(markup, /<select name="result_reason" required/);
  assert.match(markup, /<input type="checkbox" name="confirm" required/);
  assert.match(markup, /Completed matches cannot be changed/);
  assert.match(markup, /<button[^>]*>Record result<\/button>/);
});

test("result reason choices exactly match backend allowed reasons", () => {
  const backend = readFileSync(new URL("../../api/v1/admin.py", import.meta.url), "utf8");
  const block = backend.match(/VALID_MATCH_REASONS\s*=\s*\{([^}]+)\}/)?.[1];
  assert.ok(block, "backend VALID_MATCH_REASONS constant must exist");
  const allowed = [...block.matchAll(/"([A-Z_]+)"/g)].map((match) => match[1]).sort();
  assert.deepEqual([...ORGANIZER_RESULT_REASONS].sort(), allowed);
  assert.equal(allowed.length, 7);
});

test("mock recorder rejects invalid fields and missing confirmation before calling API", async () => {
  let calls = 0;
  const recorder = createManualResultRecorder({ recordResult: async () => { calls++; } });
  const invalid = await recorder.submit({ ...selection, confirmed: false });
  assert.equal(invalid.state, "invalid");
  assert.equal(calls, 0);
  assert.equal(recorder.isPending(selection.matchId), false);
});

test("server success must confirm the requested result and reason", async () => {
  for (const payload of [
    { status: "COMPLETED", match_id: selection.matchId, result_outcome: "DRAW", result_reason: "STALEMATE" },
    { status: "COMPLETED", match_id: selection.matchId, result_outcome: "WHITE_WIN", result_reason: "FIFTY_MOVE_RULE" },
    { status: "COMPLETED", match_id: "other-match", result_outcome: "WHITE_WIN", result_reason: "CHECKMATE" },
  ]) {
    const result = await createManualResultRecorder({ recordResult: async () => payload }).submit(selection);
    assert.equal(result.state, "error");
    assert.match(result.message, /did not confirm/);
  }
});

test("mock recorder success refreshes match list, match, rounds, leaderboard, and bracket", async () => {
  const calls = [];
  const mockApi = {
    recordResult: async (...args) => { calls.push(["record", ...args]); return { status: "COMPLETED", match_id: "match-42", result_outcome: "WHITE_WIN", result_reason: "CHECKMATE" }; },
    getMatches: async () => { calls.push(["matches"]); return { items: [{ match_id: "match-42", state: "COMPLETED" }] }; },
    getMatch: async (id) => { calls.push(["match", id]); return { match_id: id, state: "COMPLETED" }; },
    getRounds: async (stage) => { calls.push(["rounds", stage]); return [{ stage, status: "COMPLETED" }]; },
    getLeaderboard: async () => { calls.push(["leaderboard"]); return { standings: [{ participant_id: "p-a", points: 2 }] }; },
    getBracket: async () => { calls.push(["bracket"]); return { rounds: [] }; },
    getAuditLog: async () => { calls.push(["audit"]); return [{ event_type: "match.result_recorded" }]; },
  };
  const recorder = createManualResultRecorder(mockApi);
  const result = await recorder.submit(selection);
  assert.equal(result.state, "success");
  assert.equal(result.match.state, "COMPLETED");
  assert.equal(result.matches.items[0].state, "COMPLETED");
  assert.deepEqual(result.leaderboard.standings, [{ participant_id: "p-a", points: 2 }]);
  assert.deepEqual(result.bracket, { rounds: [] });
  assert.deepEqual(calls, [
    ["record", "match-42", { result_outcome: "WHITE_WIN", result_reason: "CHECKMATE" }],
    ["matches"], ["match", "match-42"], ["rounds", "SWISS"], ["rounds", "PLAYOFF"], ["leaderboard"], ["bracket"], ["audit"],
  ]);
  assert.deepEqual(result.auditEvents, [{ event_type: "match.result_recorded" }]);
});

test("mock recorder prevents duplicate clicks while a request is pending", async () => {
  let resolveRecord;
  let recordCalls = 0;
  const recorder = createManualResultRecorder({
    recordResult: () => { recordCalls++; return new Promise((resolve) => { resolveRecord = resolve; }); },
    getMatches: async () => [], getMatch: async () => ({}), getRounds: async () => [], getLeaderboard: async () => ({}), getBracket: async () => ({}), getAuditLog: async () => [],
  });
  const first = recorder.submit(selection);
  await Promise.resolve();
  assert.equal(recorder.isPending(selection.matchId), true);
  const second = await recorder.submit(selection);
  assert.equal(second.state, "pending");
  assert.equal(recordCalls, 1);
  resolveRecord({ status: "COMPLETED", match_id: "match-42", result_outcome: "WHITE_WIN", result_reason: "CHECKMATE" });
  assert.equal((await first).state, "success");
  assert.equal(recorder.isPending(selection.matchId), false);
});

test("mock recorder maps validation, unauthorized, conflict, and server errors", async () => {
  for (const [status, expected] of [[400, /rejected the result fields/], [401, /session expired/], [403, /CSRF verification/], [409, /conflicts/], [500, /did not confirm/]]) {
    const recorder = createManualResultRecorder({ recordResult: async () => { throw { status }; } });
    const result = await recorder.submit(selection);
    assert.equal(result.state, "error");
    assert.equal(result.status, status);
    assert.match(result.message, expected);
  }
  assert.match(organizerResultErrorMessage({ status: 401 }), /Sign in again/);
  assert.match(organizerResultErrorMessage({ status: 403 }), /CSRF verification/);
  assert.match(organizerResultErrorMessage({ status: 404 }), /Match not found/);
});

test("mock recorder distinguishes a recorded result from failed post-success refresh", async () => {
  const recorder = createManualResultRecorder({
    recordResult: async () => ({ status: "COMPLETED", match_id: "match-42", result_outcome: "WHITE_WIN", result_reason: "CHECKMATE" }),
    getMatches: async () => [], getMatch: async () => { throw { status: 500 }; }, getRounds: async () => [], getLeaderboard: async () => ({}), getBracket: async () => ({}), getAuditLog: async () => [],
  });
  const result = await recorder.submit(selection);
  assert.equal(result.state, "recorded-refresh-failed");
  assert.equal(result.recorded.status, "COMPLETED");
  assert.match(result.message, /Result was recorded/);
  assert.equal(result.refreshErrors.match, "Request failed");
});

test("partial post-result refresh preserves server data and identifies failures", async () => {
  const recorder = createManualResultRecorder({
    recordResult: async () => ({ status: "COMPLETED", match_id: selection.matchId, result_outcome: selection.outcome, result_reason: selection.reason }),
    getMatches: async () => [{ match_id: selection.matchId, state: "COMPLETED" }],
    getMatch: async () => ({ match_id: selection.matchId, state: "COMPLETED", result_outcome: "WHITE_WIN" }),
    getRounds: async (stage) => stage === "SWISS" ? [{ round_name: "Round 1", status: "COMPLETED", pairings: [] }] : Promise.reject({ message: "playoff unavailable" }),
    getLeaderboard: async () => ({ standings: [{ rank: 1, name: "Ada", points: 2, wins: 1, draws: 0, losses: 0 }] }),
    getBracket: async () => ({ status: "IN_PROGRESS", total_participants: 2 }),
    getAuditLog: async () => [{ event_type: "MATCH_RESULT_RECORDED" }],
  });
  const result = await recorder.submit(selection);
  assert.equal(result.state, "recorded-refresh-failed");
  assert.equal(result.match.state, "COMPLETED");
  assert.equal(result.leaderboard.standings[0].name, "Ada");
  assert.deepEqual(result.refreshErrors, { playoffRounds: "playoff unavailable" });
  const markup = renderOrganizerRefreshSnapshot(result);
  assert.match(markup, /Result recording was confirmed/);
  assert.match(markup, /Ada/);
  assert.match(markup, /playoff rounds: playoff unavailable/);
});

test("audit fetch failure is reported separately after the result is recorded", async () => {
  const recorder = createManualResultRecorder({
    recordResult: async () => ({ status: "COMPLETED", match_id: selection.matchId, result_outcome: selection.outcome, result_reason: selection.reason }),
    getMatches: async () => [], getMatch: async () => ({}), getRounds: async () => [],
    getLeaderboard: async () => ({ standings: [] }), getBracket: async () => ({ rounds: [] }),
    getAuditLog: async () => { throw { status: 502, message: "The audit log response did not match the server event schema." }; },
  });
  const result = await recorder.submit(selection);
  assert.equal(result.state, "recorded-refresh-failed");
  assert.equal(result.recorded.status, "COMPLETED");
  assert.match(result.refreshErrors.auditEvents, /did not match the server event schema/);
  assert.match(renderOrganizerRefreshSnapshot(result), /audit history: The audit log response/);
});

test("empty tournament responses are labeled as ambiguous rather than confirmed empty state", () => {
  const markup = renderOrganizerRefreshSnapshot({ state: "success", recorded: { match_id: "m-1" }, match: {}, matches: [], swissRounds: [], playoffRounds: [], leaderboard: { standings: [] }, bracket: { status: "PENDING", rounds: [], total_participants: 0 } });
  assert.match(markup, /cannot distinguish an empty tournament from a round-read failure/);
  assert.match(markup, /returns an empty bracket on database read failure/);
  assert.match(markup, /No standings rows|server returned no standings rows/);
});

function fakeResultForm() {
  const button = { disabled: false, textContent: "Record result" };
  const state = { textContent: "Ready", dataset: {} };
  const fields = {
    result_outcome: { value: "WHITE_WIN", disabled: false },
    result_reason: { value: "CHECKMATE", disabled: false },
    confirm: { checked: true, disabled: false },
  };
  let submit;
  const controls = [...Object.values(fields), button];
  const form = {
    dataset: { matchId: selection.matchId },
    elements: { namedItem: (name) => fields[name] },
    checkValidity: () => true,
    reportValidity: () => true,
    querySelector: (selector) => selector.includes("button") ? button : state,
    querySelectorAll: () => controls,
    addEventListener: (name, handler) => { if (name === "submit") submit = handler; },
    removeEventListener: () => {},
    trigger: () => submit({ preventDefault() {} }),
  };
  return { form, button, state, controls };
}

test("wired mocked match form shows loading, then success after server refresh", async () => {
  const { form, button, state, controls } = fakeResultForm();
  const calls = [];
  const adapter = {
    recordResult: async (id, body) => { calls.push(["record", id, body]); return { status: "COMPLETED", match_id: id, result_outcome: body.result_outcome, result_reason: body.result_reason }; },
    getMatches: async () => ({ items: [{ match_id: selection.matchId, state: "COMPLETED" }] }),
    getMatch: async () => ({ state: "COMPLETED" }),
    getRounds: async (stage) => ({ stage }),
    getLeaderboard: async () => ({ items: [] }),
    getBracket: async () => ({ rounds: [] }),
    getAuditLog: async () => [{ event_type: "match.result_recorded", actor_id: "organizer@example.test" }],
  };
  const cleanup = wireOrganizerMatchManagement({ querySelectorAll: () => [form] }, {
    api: adapter,
    onRefresh: async (data) => calls.push(["render", data.match.state, data.leaderboard.items.length]),
  });
  const pending = form.trigger();
  assert.equal(state.dataset.state, "loading");
  assert.ok(controls.every((control) => control.disabled));
  await pending;
  assert.equal(state.dataset.state, "success");
  assert.match(state.textContent, /updated server data is shown below/i);
  assert.equal(button.textContent, "Recorded");
  assert.deepEqual(calls[0], ["record", selection.matchId, { result_outcome: "WHITE_WIN", result_reason: "CHECKMATE" }]);
  assert.deepEqual(calls.at(-1), ["render", "COMPLETED", 0]);
  cleanup();
});

test("wired mocked match form shows authorization error and restores controls", async () => {
  const { form, button, state, controls } = fakeResultForm();
  let expired = 0;
  wireOrganizerMatchManagement({ querySelectorAll: () => [form] }, {
    api: {
      recordResult: async () => { throw { status: 401 }; },
    },
    onUnauthorized: () => { expired++; },
  });
  await form.trigger();
  assert.equal(state.dataset.state, "error");
  assert.match(state.textContent, /session expired/);
  assert.equal(expired, 1);
  assert.equal(button.textContent, "Record result");
  assert.ok(controls.every((control) => !control.disabled));
});

test("mock result response without server confirmation is not treated as success", async () => {
  const recorder = createManualResultRecorder({ recordResult: async () => ({ ok: true }) });
  const result = await recorder.submit(selection);
  assert.equal(result.state, "error");
  assert.match(result.message, /did not confirm/);
  assert.equal(recorder.isPending(selection.matchId), false);
});

test("network failure is reported without claiming the result was recorded", async () => {
  const recorder = createManualResultRecorder({ recordResult: async () => { throw { status: 0 }; } });
  const result = await recorder.submit(selection);
  assert.equal(result.state, "error");
  assert.equal(result.status, 0);
  assert.match(result.message, /could not be reached/);
});

test("failure in any post-record refresh is distinct from a failed recording", async () => {
  const recorder = createManualResultRecorder({
    recordResult: async () => ({ status: "COMPLETED", match_id: selection.matchId, result_outcome: selection.outcome, result_reason: selection.reason }),
    getMatches: async () => [], getMatch: async () => ({}),
    getRounds: async (stage) => { if (stage === "PLAYOFF") throw { status: 500 }; return []; },
    getLeaderboard: async () => ({}), getBracket: async () => ({}), getAuditLog: async () => [],
  });
  const result = await recorder.submit(selection);
  assert.equal(result.state, "recorded-refresh-failed");
  assert.equal(result.recorded.status, "COMPLETED");
  assert.match(result.message, /one or more server refreshes failed/);
});
