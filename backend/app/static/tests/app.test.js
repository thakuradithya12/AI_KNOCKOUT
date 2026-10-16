import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const app = readFileSync(new URL("../assets/app.js", import.meta.url), "utf8");

test("participant-code match execution stays fail-closed in the interface", () => {
  assert.match(app, /const PARTICIPANT_CODE_EXECUTION_ENABLED = false;/);
  const request = app.match(/async function requestDevelopmentMatch\(\) \{([\s\S]*?)\n\}/)?.[1] ?? "";
  assert.match(request, /if \(!PARTICIPANT_CODE_EXECUTION_ENABLED\)[\s\S]*?return;/);
  assert.match(app, /requestBtn\.disabled = !PARTICIPANT_CODE_EXECUTION_ENABLED/);
  assert.match(app, /button\.disabled = !PARTICIPANT_CODE_EXECUTION_ENABLED/);
  assert.match(app, /Participant-code execution is disabled pending sandbox verification/);
});

test("match details disclose that the current API has no move history", () => {
  assert.match(app, /Move history is not available from the current API\./);
  assert.doesNotMatch(app, /Match Inspection & Replay/);
});

test("tournament round failures are not converted into a successful empty state", () => {
  assert.match(app, /The server returned no Swiss rounds[\s\S]*cannot distinguish an empty tournament from a round-read failure/);
  assert.doesNotMatch(app, /api\.tournamentRounds\("SWISS"\)\.catch\(\(\) => \[\]\)/);
});

test("organizer actions depend on verified cookie session and CSRF token, never shared secret", () => {
  const renderAdmin = app.match(/function renderAdmin\(active = "overview"\) \{([\s\S]*?)\n\}/)?.[1] ?? "";
  assert.match(renderAdmin, /loadPublicOrganizerMatches\(\)/);
  assert.match(app, /const profile = await api\.organizerSession\(\)/);
  assert.match(app, /function beginOrganizerLogin\(button\)/);
  assert.match(app, /api\.organizerLogin\(\)/);
  assert.match(app, /window\.location\.assign\(authorizationUrl\.href\)/);
  assert.match(app, /window\.location\.hash === "#admin"\) renderAdmin\("matches"\)/);
  assert.match(app, /profile\?\.csrf_token/);
  assert.match(app, /Number\.isFinite\(Date\.parse\(profile\.expires_at\)\)/);
  assert.match(app, /const current = await api\.organizerSession\(\)/);
  assert.match(app, /Number\.isFinite\(Date\.parse\(current\.expires_at\)\)/);
  assert.match(app, /return api\.adminRecordMatchResult\(matchId, body, organizerCsrfToken\)/);
  assert.match(app, /organizer session expired/i);
  assert.doesNotMatch(app, /fetch\([^\n]*X-Organizer-Secret/);
  assert.doesNotMatch(app, /localStorage\.(?:setItem|getItem).*organizer/i);
  assert.doesNotMatch(app, /sessionStorage\.(?:setItem|getItem).*organizer/i);
});

test("organizer session timeout remains server-authoritative and frontend uses returned CSRF data", () => {
  const config = readFileSync(new URL("../../core/config.py", import.meta.url), "utf8");
  assert.match(config, /organizer_session_idle_minutes: int = 60/);
  assert.match(config, /organizer_session_expire_minutes: int = 480/);
  assert.match(app, /organizerCsrfToken = profile\.csrf_token/);
  assert.match(app, /organizerCsrfToken = current\.csrf_token/);
  assert.doesNotMatch(app, /organizer.*(?:60\s*\*\s*60|8\s*\*\s*60)/i);
});

test("participant status uses server fields, exposes refresh, and routes expired sessions to reauthentication", () => {
  assert.match(app, /accepted_submissions_count/);
  assert.match(app, /remaining_submissions/);
  assert.match(app, /Quota status unavailable/);
  assert.match(app, /Submission history is unavailable/);
  assert.match(app, /id="refresh-submission-history"/);
  assert.match(app, /handleSessionError\(result\.reason\)/);
  assert.match(app, /api\.submissionVersions\(sessionToken/);
  assert.match(app, /api\.validation\(sessionToken/);
});

test("starter instructions report invalid messages instead of silently swallowing errors", () => {
  assert.match(app, /except Exception as exc:\s*print\(f"Invalid engine message: \{exc\}", file=sys\.stderr\)/);
  assert.doesNotMatch(app, /except Exception:\s*pass/);
});

test("build workbench documents standard intelligence.py and choose_move contract", () => {
  assert.match(app, /def choose_move\(board_fen: str, legal_moves: list\[str\], time_left_ms: int\) -> str:/);
  assert.match(app, /STANDARD PARTICIPANT API · INTELLIGENCE\.PY/);
  assert.match(app, /id="btn-copy-intelligence"/);
  assert.match(app, /accept="\.py,\.tar,text\/x-python,application\/x-tar,application\/octet-stream"/);
});

test("interactive replay controls and match inspect buttons are wired into the match viewer", () => {
  assert.match(app, /id="replay-start"/);
  assert.match(app, /id="replay-prev"/);
  assert.match(app, /id="replay-next"/);
  assert.match(app, /id="replay-end"/);
  assert.match(app, /move-transcript-table/);
  assert.match(app, /data-inspect-match/);
  assert.match(app, /OFFICIAL TOURNAMENT MATCH/);
  assert.match(app, /PRACTICE MATCH \(HOUSE AI \/ MOCK\)/);
});


