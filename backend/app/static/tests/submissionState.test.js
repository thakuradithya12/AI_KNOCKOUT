import test from "node:test";
import assert from "node:assert/strict";
import { resolveQuotaSnapshot, resolveSubmissionSnapshot } from "../assets/submissionState.js";

test("submission snapshot distinguishes authoritative empty from failed requests", () => {
  const empty = resolveSubmissionSnapshot({
    submissionsResult: { status: "fulfilled", value: { items: [] } },
    eventResult: { status: "fulfilled", value: { latest_submission: null } },
  });
  assert.equal(empty.state, "empty");

  const unavailable = resolveSubmissionSnapshot({
    submissionsResult: { status: "rejected", reason: new Error("offline") },
    eventResult: { status: "rejected", reason: new Error("offline") },
  });
  assert.equal(unavailable.state, "unavailable");
  assert.equal(unavailable.latest, null);
});

test("submission snapshot prefers server event latest status and quota never invents values", () => {
  const snapshot = resolveSubmissionSnapshot({
    submissionsResult: { status: "fulfilled", value: { items: [{ version: 1, state: "REJECTED" }] } },
    eventResult: { status: "fulfilled", value: { latest_submission: { version: 2, state: "ELIGIBLE" } } },
  });
  assert.deepEqual(snapshot.latest, { version: 2, state: "ELIGIBLE" });

  assert.equal(resolveQuotaSnapshot({ status: "rejected" }).state, "unavailable");
  assert.equal(resolveQuotaSnapshot({ status: "fulfilled", value: { submission_limit: 10 } }).state, "unavailable");
  assert.equal(resolveQuotaSnapshot({ status: "fulfilled", value: { accepted_submissions_count: 2, submission_limit: 10, remaining_submissions: 8 } }).state, "available");
});
