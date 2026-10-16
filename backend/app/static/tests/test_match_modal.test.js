import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const app = readFileSync(new URL("../assets/app.js", import.meta.url), "utf8");

test("test-match modal and engine selection are defined", () => {
  // 1. Modal function defines required selections: submission, maximum difficulty house AI, side
  assert.match(app, /async function openTestMatchModal\(participant\)/);
  assert.match(app, /id="test-match-submission"/);
  assert.match(app, /value="house-ai-maximum-difficulty"/);
  assert.match(app, /House AI — Maximum Difficulty/);
  assert.match(app, /name="test-match-side"/);
  assert.match(app, /value="WHITE"/);
  assert.match(app, /value="BLACK"/);

  // 4. Modal explicitly notes that test matches do not affect official Swiss standings
  assert.match(app, /Test matches do NOT affect official Swiss tournament standings/);

  // 5. Allows APPROVED or ELIGIBLE states
  assert.match(app, /\["APPROVED", "ELIGIBLE"\]\.includes\(s\.latest_version\.state\)/);

  // 6. Submits with side and house_ai_id
  assert.match(app, /api\.requestDevelopmentMatch\(\s*sessionToken,\s*\{\s*submission_id,\s*version,\s*side,\s*house_ai_id\s*\},/);
});

test("match viewer distinguishes test match against House AI Maximum Difficulty", () => {
  assert.match(app, /TEST MATCH · HOUSE AI \(MAXIMUM DIFFICULTY\)/);
  assert.match(app, /House AI \(Maximum Difficulty\)/);
  assert.match(app, /Iterative Minimax \(Depth 4\)/);
});
