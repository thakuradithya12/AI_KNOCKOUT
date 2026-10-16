import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const app = readFileSync(new URL("../assets/app.js", import.meta.url), "utf8");
const css = readFileSync(new URL("../assets/styles.css", import.meta.url), "utf8");

test("grandmaster arena shell defines three distinct modes with accessible navigation", () => {
  // Practice, Tournament, and Spectator arena modes
  assert.match(app, /data-arena-mode="practice"/);
  assert.match(app, /data-arena-mode="tournament"/);
  assert.match(app, /data-arena-mode="spectator"/);
  assert.match(app, /currentArenaMode = "practice"/);
  assert.match(app, /currentArenaMode = "tournament"/);
  assert.match(app, /currentArenaMode = "spectator"/);

  // Mode buttons in the topbar
  assert.match(app, /data-arena-mode="practice">Practice<\/button>/);
  assert.match(app, /data-arena-mode="tournament">Tournament<\/button>/);
  assert.match(app, /data-arena-mode="spectator">Spectator<\/button>/);
});

test("symmetrical competitor panels display authoritative identities, turns, clocks, and captured pieces", () => {
  // Symmetrical layout containers
  assert.match(app, /id="arena-top-competitor"/);
  assert.match(app, /id="arena-bottom-competitor"/);
  assert.match(app, /class="side-avatar \$\{color\}"/);

  // Turn indicator and digital clock
  assert.match(app, /turn-indicator \$\{isTurn \? "active" : ""\}/);
  assert.match(app, /● TO MOVE/);
  assert.match(app, /digital-clock \$\{isTurn \? "clock-active" : ""\}/);
  assert.match(app, /formatClock\(clockMs\)/);

  // Material balance and captured pieces calculation
  assert.match(app, /function calculateMaterialBalance\(fen\)/);
  assert.match(app, /captured-pieces-bar/);
  assert.match(app, /captured-score-diff/);
});

test("chessboard supports flicker-free in-place updates, orientation flipping, and king check detection", () => {
  // Flicker-free in-place updating function
  assert.match(app, /function updateCinematicBoard\(container, fen, options = \{\}\)/);
  assert.match(app, /function detectKingInCheck\(fen\)/);

  // King check highlight class
  assert.match(app, /in-check/);
  assert.match(css, /\.square\.in-check/);

  // Board orientation flip controls
  assert.match(app, /id="arena-btn-flip"/);
  assert.match(app, /Flip Board/);
  assert.match(app, /arenaBoardFlipped = !arenaBoardFlipped/);
  assert.match(app, /data-flipped="\$\{Boolean\(flipped\)\}"/);
});

test("interactive replay controls, auto-play, and keyboard navigation are implemented", () => {
  // Interactive controls
  assert.match(app, /id="replay-start"/);
  assert.match(app, /id="replay-prev"/);
  assert.match(app, /id="replay-next"/);
  assert.match(app, /id="replay-end"/);
  assert.match(app, /id="replay-autoplay"/);
  assert.match(app, /id="replay-step-info"/);

  // Replay logic
  assert.match(app, /function arenaJumpReplay\(targetIdx\)/);
  assert.match(app, /function arenaStepReplay\(delta\)/);
  assert.match(app, /function toggleArenaAutoPlay\(\)/);

  // Keyboard navigation shortcuts
  assert.match(app, /e\.key === "ArrowLeft"/);
  assert.match(app, /e\.key === "ArrowRight"/);
  assert.match(app, /e\.key === "Home"/);
  assert.match(app, /e\.key === "End"/);
  assert.match(app, /move-transcript-table/);
  assert.match(app, /active-ply/);
});

test("verified terminal result presentation avoids client-side invention", () => {
  assert.match(app, /id="arena-result-banner"/);
  assert.match(app, /id="btn-replay-from-start"/);
  assert.match(app, /Termination Reason:/);
  assert.match(app, /resultSourceLabel\(match\.result_source\)/);
  assert.match(app, /OFFICIAL TOURNAMENT MATCH/);
  assert.match(app, /PRACTICE MATCH \(HOUSE AI \/ MOCK\)/);
  assert.match(app, /TEST MATCH · HOUSE AI \(MAXIMUM DIFFICULTY\)/);
});

test("technical diagnostics section isolates execution metadata from standard viewing", () => {
  assert.match(app, /id="arena-diagnostics-details"/);
  assert.match(app, /Technical Execution Diagnostics & Policy Verification/);
  assert.match(app, /Rules Engine:/);
  assert.match(app, /Runtime Profile:/);
  assert.match(app, /Clock Setting:/);
  assert.match(app, /Worker Isolation:/);
});

test("real-time synchronization handles polling backoff, error recovery, and terminal state", () => {
  assert.match(app, /arenaRefreshTimer = setTimeout/);
  assert.match(app, /RECONNECTING…/);
  assert.match(app, /MATCH TERMINAL/);
  assert.match(app, /arenaLiveRequestPending = true/);
  assert.match(app, /arenaLiveRequestPending = false/);
  assert.match(app, /Arena temporarily unavailable/);
  assert.match(app, /We couldn’t load arena data\./);
  assert.match(app, /id="arena-retry-load"/);
  assert.match(app, /Sign-in required/);
  assert.match(app, /No practice matches/);
  assert.match(app, /No official matches/);
  assert.match(css, /\.arena-load-error\s*\{/);
});

test("stylesheet provides grandmaster styling, responsive breakpoints, and reduced-motion guard", () => {
  assert.match(css, /\.grandmaster-arena-shell/);
  assert.match(css, /\.arena-topbar/);
  assert.match(css, /\.arena-stage-grid/);
  assert.match(css, /\.arena-competitor-card/);
  assert.match(css, /\.digital-clock/);
  assert.match(css, /\.turn-indicator\.active/);
  assert.match(css, /@keyframes pulseTurn/);
  assert.match(css, /@keyframes checkGlow/);
  assert.match(css, /@media\s*\(max-width:\s*1023px\)/);
  assert.match(css, /@media\s*\(max-width:\s*560px\)/);
  assert.match(css, /@media\s*\(prefers-reduced-motion:\s*reduce\)[\s\S]*?animation:\s*none\s*!important/);
});
