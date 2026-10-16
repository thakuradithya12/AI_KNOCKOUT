import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const app = readFileSync(new URL("../assets/app.js", import.meta.url), "utf8");

test("BUG-PRAC-001: matches page renders Play Test Match entry point alongside refresh", () => {
  // Practice matches banner defines Play Test Match button and Refresh button
  assert.match(app, /id="matches-play-test-match"/);
  assert.match(app, /id="matches-refresh"/);
  assert.match(app, /Play Test Match/);

  // Fallback direct request remains fail-closed in accordance with sandbox safety policy
  assert.match(app, /const PARTICIPANT_CODE_EXECUTION_ENABLED = false;/);
  assert.match(app, /id="matches-request" type="button" disabled style="display:none;"/);
  assert.match(app, /title="Participant-code execution is disabled pending sandbox verification\."/);
});

test("BUG-PRAC-001: empty practice matches state reports that execution is unavailable", () => {
  assert.match(app, /<strong>No practice matches recorded<\/strong>/);
  assert.match(app, /New practice match requests are unavailable until the security release gates are verified\./);
  assert.match(app, /SANDBOX_ENABLED=False/);
  assert.match(app, /WORKER_EXECUTION_MODE=mock/);
  assert.match(app, /testMatchBtn\.disabled = !PARTICIPANT_CODE_EXECUTION_ENABLED \|\| active/);
  assert.doesNotMatch(app, /Click <strong>Play Test Match<\/strong> above to queue/);
});

test("practice arena does not promise sandbox execution while the release gate is closed", () => {
  assert.doesNotMatch(app, /runs your submitted intelligence\.py in an isolated sandbox/);
  assert.doesNotMatch(app, /Launch an isolated test match/);
  assert.match(app, /if \(!PARTICIPANT_CODE_EXECUTION_ENABLED\)[\s\S]*?No match request was sent\./);
  assert.match(app, /Signing in will not start a match/);
});

test("BUG-PRAC-001: queued matches accurately disclose awaiting worker dispatch without implying running", () => {
  // Match rows must label queued state distinctly
  assert.match(app, /Queued on server · Awaiting sandbox worker/);

  // Overview status note must differentiate QUEUED from RUNNING
  assert.match(app, /Practice match queued vs [\s\S]*? · Awaiting worker dispatch \(Sandbox unverified\)/);
  assert.match(app, /Match in progress vs [\s\S]*? · \$\{activeMatch\.current_ply \|\| 0\} plies/);

  // Play Test Match button on overview reflects queued status accurately
  assert.match(app, /activeMatch\.state === "QUEUED" \? "Match Queued…" : "Match In Progress…"/);

  // Match detail view must disclose queued status and sandbox execution limitation
  assert.match(app, /QUEUED \(Pending Worker Execution\)/);
  assert.match(app, /Execution Sandbox Pending Verification/);
  assert.match(app, /Submitted participant code will not run in the shared API process\./);
});

test("BUG-PRAC-001: modal supports submission version, side selection, and House AI maximum difficulty", () => {
  // Modal provides White/Black selection, submission version, and Maximum Difficulty option
  assert.match(app, /id="test-match-submission"/);
  assert.match(app, /id="test-match-opponent"/);
  assert.match(app, /value="house-ai-maximum-difficulty"/);
  assert.match(app, /name="test-match-side"/);

  // Modal dispatches requestDevelopmentMatch with sessionToken
  assert.match(app, /api\.requestDevelopmentMatch\(\s*sessionToken,\s*\{\s*submission_id,\s*version,\s*side,\s*house_ai_id\s*\},/);
});
