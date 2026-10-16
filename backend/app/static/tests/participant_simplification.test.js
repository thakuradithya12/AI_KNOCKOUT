import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const app = readFileSync(new URL("../assets/app.js", import.meta.url), "utf8");

test("AC1: participant navigation contains exactly Welcome, Build, Submit, Leaderboard, and Live Match", () => {
  // portalSections must contain exactly the 5 required navigation entries
  const sectionsMatch = app.match(/const portalSections = \[([\s\S]*?)\];/);
  assert.ok(sectionsMatch, "portalSections must be defined");
  const sectionsContent = sectionsMatch[1];

  assert.match(sectionsContent, /\["welcome",\s*"Welcome"\]/);
  assert.match(sectionsContent, /\["build",\s*"Build"\]/);
  assert.match(sectionsContent, /\["submit",\s*"Submit"\]/);
  assert.match(sectionsContent, /\["leaderboard",\s*"Leaderboard"\]/);
  assert.match(sectionsContent, /\["live",\s*"Live Match"\]/);

  // Exactly 5 items in portalSections
  const entries = sectionsContent.split("],").filter((s) => s.trim().length > 0);
  assert.equal(entries.length, 5, "Must have exactly 5 participant navigation entries");
});

test("AC2: practice-match features are removed from participant navigation and workspace", () => {
  // Practice Arena and separate Matches/Results/Profile are removed from participant nav
  const sectionsMatch = app.match(/const portalSections = \[([\s\S]*?)\];/)?.[1] ?? "";
  assert.doesNotMatch(sectionsMatch, /"arena"/i);
  assert.doesNotMatch(sectionsMatch, /"matches"/i);
  assert.doesNotMatch(sectionsMatch, /"profile"/i);

  // Participant welcome markup contains no practice launch buttons or stray buttons
  const welcomeMarkup = app.match(/function welcomeMarkup\(participant\) \{([\s\S]*?)\n\}/)?.[1] ?? "";
  assert.doesNotMatch(welcomeMarkup, /Play Your First Practice Match/);
  assert.doesNotMatch(welcomeMarkup, /PRACTICE BOARD PREVIEW/);
  assert.doesNotMatch(welcomeMarkup, /Play Test Match/);
  assert.doesNotMatch(welcomeMarkup, /Start development match/);
  assert.doesNotMatch(welcomeMarkup, /Select eligible version/);
  assert.doesNotMatch(welcomeMarkup, /id="btn-play-test-match"/);
  assert.doesNotMatch(welcomeMarkup, /id="request-match"/);
  assert.doesNotMatch(welcomeMarkup, /id="select-official"/);
});

test("AC3: build workbench contains instructions and submission contract only", () => {
  // Documents authoritative intelligence.py choose_move contract
  assert.match(app, /def choose_move\(board_fen: str, legal_moves: list\[str\], time_left_ms: int\) -> str:/);
  assert.match(app, /STANDARD PARTICIPANT API · INTELLIGENCE\.PY/);
  assert.match(app, /id="btn-copy-intelligence"/);

  // Documents aik-jsonl/1 protocol
  assert.match(app, /CONTAINER PROTOCOL · AIK-JSONL\/1/);
  assert.match(app, /id="btn-copy-starter"/);
});

test("AC4: submit view displays quota banner, upload flow, and version history", () => {
  assert.match(app, /id="quota-banner"/);
  assert.match(app, /id="archive-file"/);
  assert.match(app, /id="upload-form"/);
  assert.match(app, /id="upload-result"/);
  assert.match(app, /id="submission-history"/);
  assert.match(app, /id="refresh-submission-history"/);
  // Decorative 3-step workflow panel removed
  assert.doesNotMatch(app, /class="card workflow-card"/);
});

test("AC5: leaderboard renders official results with pending state for unplayed tournaments", () => {
  assert.match(app, /function renderLeaderboard\(participant\)/);
  assert.match(app, /Official Tournament Standings Pending · Round 1 Scheduled/);
  assert.match(app, /1\.0 pt Win · 0\.5 pt Draw · 0\.0 pt Loss/);
  // Practice match tab removed from leaderboard
  assert.doesNotMatch(app, /data-res-tab="practice"/);
  assert.doesNotMatch(app, /id="res-tab-practice"/);
});

test("AC6: live match view displays real official match data and handles absence of active match", () => {
  assert.match(app, /function renderLiveMatch\(participant/);
  assert.match(app, /OFFICIAL TOURNAMENT MATCH/);
  assert.match(app, /No active match currently in progress/);
  assert.match(app, /id="arena-board-container"/);
  assert.match(app, /id="arena-top-competitor"/);
  assert.match(app, /id="arena-bottom-competitor"/);
  assert.match(app, /id="arena-transcript-container"/);
});

test("AC7: no dead navigation links in participant workspace", () => {
  assert.match(app, /data-page="welcome"/);
  assert.match(app, /data-page="build"/);
  assert.match(app, /data-page="submit"/);
  assert.match(app, /data-page="leaderboard"/);
  assert.match(app, /data-page="live"/);
});
