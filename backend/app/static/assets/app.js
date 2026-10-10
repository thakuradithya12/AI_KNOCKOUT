import { api, ApiError } from "/assets/api.js?v=ui-20261007-5";
import { copyCredentialValue, credentialPasteValues, credentialsText } from "/assets/credentials.js?v=ui-20261007-5";

const app = document.querySelector("#app");
const serverLabel = document.querySelector("#server-label");
const serverState = serverLabel.parentElement;
const toast = document.querySelector("#toast");

// The existing backend issues opaque bearer tokens. Keep the token in memory
// only; a page reload intentionally requires the participant to sign in again.
let sessionToken = null;
let route = "login";
let toastTimer;
let buildClockTimer;

function notify(message) {
  toast.textContent = message;
  toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => toast.classList.remove("show"), 2300);
}

function esc(value = "") {
  return String(value).replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
}

function setServer(online) {
  serverState.classList.toggle("online", online);
  serverState.classList.toggle("offline", !online);
  serverLabel.textContent = online ? "Server connected" : "Server unavailable";
}

async function checkServer() {
  try { await api.health(); setServer(true); }
  catch { setServer(false); }
}
checkServer();
setInterval(checkServer, 30000);

function landing(active = "login", error = "") {
  document.body.classList.remove("public-interface");
  route = active;
  const registering = active === "register";
  app.innerHTML = `
    <section class="access-layout" aria-label="Participant access">
      <div class="access-context">
        <div class="section-index">PARTICIPANT SYSTEM <span>01</span></div>
        <h1>${registering ? "Register" : "Sign in"}</h1>
        <p class="context-copy">${registering ? "Create a participant identity for the competition." : "Use the credentials issued when your participant identity was registered."}</p>
        <dl class="event-facts">
          <div><dt>Event</dt><dd>2026</dd></div>
          <div><dt>Competition</dt><dd>Technical chess</dd></div>
          <div><dt>Access</dt><dd>Participants</dd></div>
        </dl>
        <nav class="access-views" aria-label="Other AI Knockout interfaces"><button class="link-button" type="button" data-view="arena">Live Arena →</button><button class="link-button" type="button" data-view="admin">Admin Control Center →</button></nav>
        ${competitionDiagram()}
      </div>
      <section class="access-panel" aria-label="${registering ? "Register participant" : "Sign in"}">
        <div class="tabs" role="tablist" aria-label="Participant account">
          <button class="tab ${!registering ? "active" : ""}" type="button" role="tab" aria-selected="${!registering}" data-page="login">Sign in</button>
          <button class="tab ${registering ? "active" : ""}" type="button" role="tab" aria-selected="${registering}" data-page="register">Register</button>
        </div>
        ${error ? `<div class="error" role="alert">${esc(error)}</div>` : ""}
        ${registering ? registrationForm() : loginForm()}
      </section>
    </section>`;
  app.querySelectorAll("[data-page]").forEach((button) => button.addEventListener("click", () => landing(button.dataset.page)));
  app.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => renderPublicView(button.dataset.view)));
  app.querySelector("form").addEventListener("submit", registering ? register : login);
  if (!registering) bindCredentialPaste();
}

function competitionDiagram() {
  return `<figure class="search-figure" aria-labelledby="search-caption">
    <svg class="search-diagram" viewBox="0 0 440 224" role="img" aria-labelledby="search-title search-description">
      <title id="search-title">Chess engine search path</title><desc id="search-description">An input chess position branches into candidate moves, then evaluations converge on a selected move and the competition server.</desc>
      <defs><pattern id="board-grid" width="18" height="18" patternUnits="userSpaceOnUse"><path d="M18 0H0V18" fill="none" stroke="#d6d7d0" stroke-width=".7"/></pattern></defs>
      <rect x="12" y="12" width="120" height="120" fill="url(#board-grid)" stroke="#bdc2bb"/>
      <path class="route route-a" d="M80 144V164M80 164L40 188M80 164V188M80 164L120 188M40 188V207M80 188V207M120 188V207"/>
      <path class="route route-b" d="M80 207V216H365V194"/>
      <g class="diagram-label"><text x="12" y="8">INPUT POSITION</text><text x="143" y="158">CANDIDATE MOVES</text><text x="143" y="202">POSITION EVALUATION</text><text x="316" y="177">SELECTED MOVE</text><text x="345" y="215">EVENT SERVER</text></g>
      <g class="search-node root"><circle cx="80" cy="144" r="5"/><text x="68" y="151">d=0</text></g>
      <g class="search-node"><circle cx="40" cy="188" r="4"/><circle cx="80" cy="188" r="4"/><circle cx="120" cy="188" r="4"/></g>
      <g class="search-leaf"><rect x="33" y="207" width="14" height="7"/><rect x="73" y="207" width="14" height="7"/><rect x="113" y="207" width="14" height="7"/></g>
      <g class="selection"><circle cx="365" cy="194" r="8"/><path d="M361 194l3 3 6-7"/></g>
      <g class="board-mark" aria-hidden="true"><path d="M21 21h36v36H21zM57 57h36v36H57zM93 93h30v30H93z"/><text x="15" y="142">a</text><text x="33" y="142">b</text><text x="51" y="142">c</text><text x="69" y="142">d</text><text x="87" y="142">e</text><text x="105" y="142">f</text><text x="123" y="142">g</text></g>
    </svg><figcaption id="search-caption">Position → candidate moves → evaluation → selected move</figcaption>
  </figure>`;
}

function loginForm() {
  return `<h2 class="form-title">Participant credentials</h2><p class="subhead">Both fields are required to continue.</p>
    <form id="login-form">
      <div class="field"><label for="participant-id">Participant ID</label><input class="input" id="participant-id" name="participant_id" autocomplete="username" placeholder="part_…" minlength="5" maxlength="50" required></div>
      <div class="field"><label for="access-key">Access Key</label><input class="input" id="access-key" name="access_key" type="password" autocomplete="current-password" minlength="16" maxlength="128" required></div>
      <button class="button button-block" type="submit">Sign in</button>
    </form><p id="credential-paste-status" class="copy-status" role="status" aria-live="polite"></p><p class="form-foot">Need a participant identity? Use the Register tab. An Access Key cannot be recovered after registration.</p>`;
}

function bindCredentialPaste() {
  const idInput = app.querySelector("#participant-id");
  const keyInput = app.querySelector("#access-key");
  for (const [input, target] of [[idInput, "participant_id"], [keyInput, "access_key"]]) {
    input.addEventListener("paste", (event) => {
      const pasted = event.clipboardData?.getData("text");
      if (!pasted) return;
      const values = credentialPasteValues(pasted, idInput.value, keyInput.value, target);
      if (!values) {
        if (/\r?\n|participant\s+id\s*:|access\s+key\s*:/i.test(pasted)) {
          event.preventDefault();
          app.querySelector("#credential-paste-status").textContent = "Credentials not recognized. Nothing was pasted.";
        }
        return;
      }
      event.preventDefault();
      idInput.value = values.participant_id || "";
      keyInput.value = values.access_key || "";
      if (values.combined) app.querySelector("#credential-paste-status").textContent = "Participant ID and Access Key filled.";
    });
  }
}

function registrationForm() {
  return `<h2 class="form-title">Participant registration</h2><p class="subhead">The server assigns your participant identity after registration.</p>
    <form id="register-form">
      <div class="field"><label for="entry-name">AI / entry name <span>Required</span></label><input class="input" id="entry-name" name="name" autocomplete="organization" placeholder="e.g. DeepKnight" minlength="2" maxlength="100" required></div>
      <div class="field"><label for="team-name">Team name <span>Optional</span></label><input class="input" id="team-name" name="team_name" autocomplete="organization-title" placeholder="Your team" maxlength="100"></div>
      <div class="field"><label for="email">Contact email <span>Optional</span></label><input class="input" id="email" name="email" type="email" autocomplete="email" placeholder="team@college.edu" maxlength="255"></div>
      <button class="button button-block" type="submit">Register participant</button>
    </form><p class="form-foot">Your Participant ID and Access Key are issued by the server and shown once after registration.</p>`;
}

function formValues(form) {
  return Object.fromEntries(new FormData(form).entries());
}

async function withSubmitState(form, action) {
  const button = form.querySelector("button[type=submit]");
  const old = button.textContent;
  button.disabled = true;
  button.textContent = "Connecting…";
  try { await action(); }
  catch (error) { landing(route, error instanceof ApiError ? error.message : "Something went wrong. Please try again."); }
  finally { if (button.isConnected) { button.disabled = false; button.textContent = old; } }
}

async function register(event) {
  event.preventDefault();
  const form = event.currentTarget;
  await withSubmitState(form, async () => {
    const values = formValues(form);
    if (!values.email) delete values.email;
    if (!values.team_name) delete values.team_name;
    const result = await api.register(values);
    credentials(result);
  });
}

function credentials(result) {
  route = "credentials";
  app.innerHTML = `<section class="access-layout credential-layout"><div class="access-context"><div class="section-index">PARTICIPANT SYSTEM <span>02</span></div><h1>Registration complete</h1><p class="context-copy"><strong>${esc(result.name)}</strong> is now registered. Save the server-issued credentials before leaving this page.</p><dl class="event-facts"><div><dt>Participant status</dt><dd>${esc(result.status)}</dd></div><div><dt>Identity source</dt><dd>Event server</dd></div></dl></div>
    <section class="access-panel credential-panel"><div class="section-index">CREDENTIAL HANDOFF <span>PRIVATE</span></div><h2 class="form-title credential-title">Participant credentials</h2><p class="subhead">The Access Key is displayed once. Keep it private.</p>
    <div class="credential"><label for="issued-id">Participant ID</label><div class="secret-row"><code id="issued-id">${esc(result.participant_id)}</code><button class="small-button" type="button" data-copy="id" aria-label="Copy Participant ID">Copy</button></div></div>
    <div class="credential"><label for="issued-key">Access Key · one-time display</label><div class="secret-row"><code id="issued-key">${esc(result.access_key)}</code><button class="small-button" type="button" data-copy="key" aria-label="Copy Access Key">Copy</button></div></div>
    <p id="copy-status" class="copy-status" role="status" aria-live="polite"></p>
    <p class="credential-warning">This browser will not save these credentials. Copy them to a secure place now; the Access Key cannot be retrieved again.</p>
    <div class="credential-actions"><button class="button" id="copy-all" type="button">Copy both</button><button class="button button-quiet" id="continue-login" type="button">Continue to sign in</button></div></section></section>`;
  app.querySelectorAll("[data-copy]").forEach((button) => button.addEventListener("click", () => copyCredential(button, button.dataset.copy === "id" ? result.participant_id : result.access_key, button.dataset.copy === "id" ? "Participant ID" : "Access Key")));
  app.querySelector("#copy-all").addEventListener("click", (event) => copyCredential(event.currentTarget, credentialsText(result.participant_id, result.access_key), "both credentials"));
  app.querySelector("#continue-login").addEventListener("click", () => landing("login"));
}

async function copyCredential(button, value, label) {
  const status = app.querySelector("#copy-status");
  try {
    await copyCredentialValue(value, (text) => navigator.clipboard.writeText(text));
    const original = button.textContent;
    button.textContent = "Copied";
    button.classList.add("copied");
    status.textContent = label === "both credentials" ? "Copied both credentials." : `Copied ${label}.`;
    setTimeout(() => { if (button.isConnected) { button.textContent = original; button.classList.remove("copied"); } }, 1600);
  } catch {
    status.textContent = `Clipboard unavailable. Select and copy the ${label === "both credentials" ? "credentials" : label} manually.`;
  }
}

async function login(event) {
  event.preventDefault();
  const form = event.currentTarget;
  await withSubmitState(form, async () => {
    const result = await api.login(formValues(form));
    sessionToken = result.access_token;
    dashboard(result.participant);
    await loadProfile();
  });
}

const portalSections = [
  ["overview", "Overview", "overview"], ["build", "Build", "build"], ["submit", "Submit", "submit"],
  ["matches", "Matches", "matches"], ["results", "Results", "results"], ["profile", "Profile", "profile"],
];
let activeParticipant = null;

function navIcon(name) {
  const paths = {
    overview: '<rect x="3.5" y="3.5" width="7" height="7" rx="1"/><rect x="13.5" y="3.5" width="7" height="7" rx="1"/><rect x="3.5" y="13.5" width="7" height="7" rx="1"/><rect x="13.5" y="13.5" width="7" height="7" rx="1"/>',
    build: '<path d="m8 7-4 5 4 5M16 7l4 5-4 5M14 5l-4 14"/>',
    submit: '<path d="M12 15V3m0 0L7.5 7.5M12 3l4.5 4.5M4 14v5a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2v-5"/>',
    matches: '<path d="M7 20h10M9 20v-3l-3-2 2-3-2-3 3-4 4 1 2 4-2 2 3 2 1 4h-5"/><circle cx="13" cy="8" r=".7"/>',
    results: '<path d="M7 4h10v4a5 5 0 0 1-10 0V4ZM7 6H4v2a4 4 0 0 0 4 4m9-6h3v2a4 4 0 0 1-4 4m-4 1v4m-4 3h8m-7-3h6"/>',
    profile: '<circle cx="12" cy="8" r="3.5"/><path d="M5 20a7 7 0 0 1 14 0"/>',
    refresh: '<path d="M20 7v5h-5M4 17v-5h5"/><path d="M5.6 9a7 7 0 0 1 11.7-2L20 12M4 12l2.7 5a7 7 0 0 0 11.7-2"/>',
  };
  return `<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.65" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths[name] || paths.overview}</svg>`;
}

function formatElapsed(totalSeconds) {
  const seconds = Math.max(0, Math.floor(totalSeconds || 0));
  const hours = Math.floor(seconds / 3600);
  const minutes = Math.floor((seconds % 3600) / 60);
  return `${String(hours).padStart(2, "0")} : ${String(minutes).padStart(2, "0")} : ${String(seconds % 60).padStart(2, "0")}`;
}

function startElapsedClock(targetId, serverElapsedSeconds) {
  const target = app.querySelector(`#${targetId}`);
  if (!target || !Number.isFinite(Number(serverElapsedSeconds))) return;
  const elapsedAtSample = Number(serverElapsedSeconds);
  const sampledAt = performance.now();
  const update = () => {
    if (!target.isConnected) {
      clearInterval(buildClockTimer);
      buildClockTimer = null;
      return;
    }
    target.textContent = formatElapsed(elapsedAtSample + (performance.now() - sampledAt) / 1000);
  };
  clearInterval(buildClockTimer);
  update();
  buildClockTimer = setInterval(update, 1000);
}

function dateTime(value) {
  if (!value) return "—";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString();
}

function badge(value = "Unavailable") {
  const key = String(value).toLowerCase();
  const tone = key.includes("eligible") || key.includes("approved") || key === "active" || key === "completed"
    ? "success" : key.includes("reject") || key.includes("fail") || key.includes("abort")
      ? "danger" : key.includes("validat") || key.includes("queued") || key.includes("starting") || key.includes("running")
        ? "warning" : "neutral";
  return `<span class="status-badge ${tone}">${esc(value.replaceAll("_", " "))}</span>`;
}

function shell(participant, active, title, content) {
  document.body.classList.remove("public-interface");
  activeParticipant = participant;
  route = active;
  clearInterval(buildClockTimer);
  buildClockTimer = null;
  app.innerHTML = `<div class="portal-app">
    <header class="topbar"><a class="brand" href="#overview" data-page="overview" aria-label="AI Knockout participant home"><span class="brand-mark">AI</span><span class="brand-word">KNOCKOUT<small>BY ENGINEERING COLLEGE</small></span></a>
      <div class="topbar-actions"><span class="service-indicator ${serverState.classList.contains("online") ? "is-online" : ""}"><i></i>${serverState.classList.contains("online") ? "System online" : "Connection status"}</span><button class="icon-button" type="button" id="refresh" aria-label="Refresh current page" title="Refresh">${navIcon("refresh")}</button><span class="participant-chip"><span class="avatar">${esc((participant.name || "P").slice(0, 1).toUpperCase())}</span><span>${esc(participant.participant_id)}</span></span><button class="text-button" type="button" data-signout>Sign out</button></div>
    </header>
    <div class="portal-frame"><aside class="sidebar"><div class="sidebar-label">PARTICIPANT</div><nav class="side-nav" aria-label="Participant navigation">${portalSections.map(([key, label, icon]) => `<button type="button" class="side-link ${active === key ? "active" : ""}" data-page="${key}" ${active === key ? 'aria-current="page"' : ""}><span class="nav-icon">${navIcon(icon)}</span><span>${label}</span></button>`).join("")}</nav><div class="sidebar-spacer"></div><div class="side-account"><span class="avatar">${esc((participant.name || "P").slice(0, 1).toUpperCase())}</span><span><strong>${esc(participant.name)}</strong><small>Participant</small></span><button class="side-signout" type="button" data-signout aria-label="Sign out">⋯</button></div></aside>
      <main class="page-main"><div class="page-wrap"><div class="page-heading"><div><div class="eyebrow">AI KNOCKOUT <span>/</span> PARTICIPANT</div><h1>${esc(title)}</h1></div>${active !== "overview" ? `<p class="page-id">${esc(participant.participant_id)}</p>` : ""}</div>${content}</div></main>
    </div></div>`;
  app.querySelectorAll("[data-signout]").forEach((button) => button.addEventListener("click", signOut));
  app.querySelectorAll("[data-page]").forEach((button) => button.addEventListener("click", (event) => {
    event.preventDefault();
    navigate(button.dataset.page, participant);
  }));
  app.querySelector("#refresh").addEventListener("click", () => navigate(active, participant));
}

function overviewMarkup(participant) {
  return `<section class="hero-panel"><div class="hero-copy"><span class="phase-pill"><i></i> PARTICIPANT WORKSPACE</span><h2>Turn your ideas<br>into a stronger player.</h2><p>Train. Test. Submit. Compete.</p><div class="hero-clock"><span>BUILD SESSION ELAPSED</span><strong id="build-elapsed" aria-live="off">— : — : —</strong><small id="build-started">Loading server event status</small></div></div><div class="hero-board" aria-hidden="true"><div class="mini-board">${miniBoard()}</div><span class="board-caption">STANDARD CHESS · SERVER-LED</span></div></section>
  <section class="competition-state" aria-labelledby="competition-state-heading"><div class="section-heading"><h2 id="competition-state-heading">Competition State</h2><span>Live participant record</span></div><div class="competition-state-grid"><article class="state-card"><span class="state-label">Participant</span><strong id="state-participant">Loading account state</strong><p>Server-issued account status</p></article><article class="state-card"><span class="state-label">AI submission</span><strong id="state-submission">Loading submission state</strong><p id="state-submission-note">Latest recorded package</p></article><article class="state-card"><span class="state-label">Validation</span><strong id="state-validation">Loading validation state</strong><p>Latest server validation outcome</p></article><article class="state-card"><span class="state-label">Next match</span><strong id="state-match">Loading match state</strong><p id="state-match-note">Development matches · not official</p></article></div></section>
  <section class="feature-grid"><article class="card feature-card"><div class="card-heading"><h2>Your Submission</h2><span class="card-mark blue">↗</span></div><div id="overview-submission" class="loading-copy">Loading your latest server record…</div><div class="card-actions"><button class="button button-secondary" type="button" data-page="submit">Submit new version</button><button class="link-button" type="button" data-page="submit">View details →</button></div></article>
  <article class="card feature-card"><div class="card-heading"><h2>Development Match</h2><span class="card-mark mint">${navIcon("matches")}</span></div><div id="overview-match" class="loading-copy">Checking your development matches…</div><div class="card-actions"><button class="button button-primary" id="request-match" type="button" disabled>Start development match <span aria-hidden="true">→</span></button><button class="link-button" type="button" data-page="matches">View matches →</button></div></article></section>
  <section class="lower-grid"><article class="card"><div class="card-heading"><h2>Submission History</h2><button class="link-button" type="button" data-page="submit">All submissions →</button></div><div id="overview-history" class="table-loading">Loading server records…</div></article>
    <article class="card"><div class="card-heading"><h2>Validation Results</h2><span class="card-mark mint">✓</span></div><div id="overview-validation" class="result-list"><p class="muted-copy">Validation evidence will appear for a selected submission version.</p></div></article>
    <article class="card"><div class="card-heading"><h2>Development Stats</h2><span class="card-mark blue">▥</span></div><div class="unavailable-block"><strong>Not available yet</strong><p>The result API does not provide aggregate development statistics.</p></div></article></section>
  <section class="official-strip card"><div><span class="eyebrow">OFFICIAL ENTRY</span><p id="official-detail">Loading event selection state…</p></div><div class="official-actions"><span id="official-state">${badge("Loading")}</span><button id="select-official" class="button button-secondary" type="button" hidden>Select eligible version</button></div></section>
  <section class="activity-strip card"><div class="card-heading"><h2>Recent Activity</h2><span class="muted-label">Event timeline</span></div><div class="unavailable-inline"><span aria-hidden="true">—</span><p><strong>Activity feed unavailable</strong><br>The backend does not expose a participant activity timeline.</p></div></section>`;
}

function miniBoard() {
  const rows = ["rnbqkbnr", "pppppppp", "........", "........", "........", "........", "PPPPPPPP", "RNBQKBNR"];
  const pieces = { p: "♟", r: "♜", n: "♞", b: "♝", q: "♛", k: "♚", P: "♙", R: "♖", N: "♘", B: "♗", Q: "♕", K: "♔" };
  return rows.map((row, rank) => [...row].map((piece, file) => `<span class="square ${(rank + file) % 2 ? "dark" : "light"}">${pieces[piece] || ""}</span>`).join("")).join("");
}

function dashboard(participant) {
  shell(participant, "overview", "AI Command Deck", overviewMarkup(participant));
  app.querySelector("#request-match").addEventListener("click", requestDevelopmentMatch);
  app.querySelector("#select-official").addEventListener("click", selectLatestOfficial);
  loadOverview(participant);
}

async function loadOverview(participant) {
  try {
    const [event, submissions] = await Promise.all([
      api.eventStatus(sessionToken), api.submissions(sessionToken),
    ]);
    let matches = null;
    try { matches = await api.developmentMatches(sessionToken); }
    catch (error) {
      if (error.status !== 404 && error.status !== 405) throw error;
    }
    if (route !== "overview") return;
    startElapsedClock("build-elapsed", event.build_elapsed_seconds);
    app.querySelector("#build-started").textContent = `Started ${dateTime(event.build_started_at)} · server time`;
    const latest = event.latest_submission;
    app.querySelector("#state-participant").innerHTML = badge(participant.status || "Status unavailable");
    app.querySelector("#state-submission").innerHTML = latest ? badge(latest.state) : "No submission yet";
    app.querySelector("#state-submission-note").textContent = latest
      ? `Version ${latest.version} · received ${dateTime(latest.submitted_at)}`
      : "Submit a package to begin validation";
    app.querySelector("#state-validation").innerHTML = latest?.validation_outcome
      ? badge(latest.validation_outcome)
      : latest ? "No result recorded" : "Not started";
    app.querySelector("#overview-submission").innerHTML = latest
      ? `<div class="submission-summary"><span class="version-tile">v${esc(latest.version)}</span><div><strong>${esc(latest.state)}</strong><small>Received ${esc(dateTime(latest.submitted_at))}</small></div>${badge(latest.state)}</div><p class="summary-copy">${latest.build_duration_seconds == null ? "Build duration unavailable" : `${esc(latest.build_duration_seconds)} seconds recorded by the server`}</p>`
      : `<div class="empty-card-copy"><strong>No submission yet</strong><p>Your first server-recorded package will appear here.</p></div>`;
    const items = Array.isArray(matches?.items) ? matches.items : [];
    const active = items.find((match) => ["QUEUED", "STARTING", "RUNNING"].includes(match.state));
    const matchSummary = active || items[0];
    app.querySelector("#state-match").textContent = active
      ? active.state.replaceAll("_", " ")
      : items.length ? "No active match" : matches ? "No match requested" : "Unavailable";
    app.querySelector("#state-match-note").textContent = active
      ? `Updated ${dateTime(active.started_at || active.created_at)}`
      : matches ? "A development match can be requested when eligible" : "Match service data is unavailable";
    app.querySelector("#overview-match").innerHTML = matchSummary
      ? `<div class="submission-summary"><span class="match-mark">${navIcon("matches")}</span><div><strong>${esc(matchSummary.state.replaceAll("_", " "))}</strong><small>${active ? "Development match in progress" : `Last updated ${esc(dateTime(matchSummary.completed_at || matchSummary.created_at))}`}</small></div>${badge(matchSummary.state)}</div><p class="summary-copy">House AI ${esc(matchSummary.house_ai?.house_ai_id || "identity unavailable")} · Development only</p>`
      : matches
        ? `<div class="empty-card-copy"><strong>No development match requested</strong><p>Request one when an eligible submission version is available.</p></div>`
        : `<div class="unavailable-block"><strong>Development match service unavailable</strong><p>This API instance does not expose development-match data.</p></div>`;
    const requestButton = app.querySelector("#request-match");
    requestButton.disabled = !matches || !latest || latest.state !== "ELIGIBLE" || Boolean(active);
    requestButton.textContent = !matches ? "Match service unavailable" : active ? "Development match active" : latest?.state === "ELIGIBLE" ? "Start development match →" : "Eligible submission required";
    renderHistory(submissions.items || [], event);
    renderValidation(latest);
    renderOfficialSelection(event);
  } catch (error) {
    if (handleSessionError(error)) return;
    for (const selector of ["#overview-submission", "#overview-match", "#overview-history", "#overview-validation"]) {
      const node = app.querySelector(selector);
      if (node) node.textContent = "Server data unavailable. Refresh to try again.";
    }
    for (const selector of ["#state-participant", "#state-submission", "#state-validation", "#state-match"]) {
      const node = app.querySelector(selector);
      if (node) node.textContent = "Live data unavailable";
    }
    for (const selector of ["#state-submission-note", "#state-match-note"]) {
      const node = app.querySelector(selector);
      if (node) node.textContent = "Refresh to retrieve the latest server state.";
    }
    const started = app.querySelector("#build-started");
    if (started) started.textContent = "Server event status unavailable.";
  }
}

async function renderHistory(submissions, event) {
  const target = app.querySelector("#overview-history");
  if (!target) return;
  const rows = [];
  for (const submission of submissions.slice(0, 6)) {
    try {
      const versions = await api.submissionVersions(sessionToken, submission.submission_id);
      for (const version of (versions.items || []).slice(0, 2)) rows.push(version);
    } catch (error) {
      if (handleSessionError(error)) return;
    }
  }
  if (!rows.length) {
    target.innerHTML = `<div class="empty-table-state">${submissions.length ? "No version records returned." : "No submissions recorded."}</div>`;
    return;
  }
  target.innerHTML = `<div class="data-table-wrap"><table class="data-table"><thead><tr><th>Version</th><th>Status</th><th>Submitted</th></tr></thead><tbody>${rows.slice(0, 6).map((row) => `<tr><td>v${esc(row.version)}</td><td>${badge(row.state)}</td><td>${esc(dateTime(row.submitted_at))}</td></tr>`).join("")}</tbody></table></div>`;
  if (event.latest_submission) target.dataset.latest = String(event.latest_submission.version);
}

async function renderValidation(latest) {
  const target = app.querySelector("#overview-validation");
  if (!target) return;
  if (!latest) {
    target.innerHTML = `<p class="muted-copy">Validation will appear after a package is submitted.</p>`;
    return;
  }
  try {
    const result = await api.validation(sessionToken, latest.submission_id, latest.version);
    if (!target.isConnected) return;
    if (!result.attempts?.length) {
      target.innerHTML = `<p class="muted-copy">No validation attempts are recorded for version ${esc(latest.version)}.</p>`;
      return;
    }
    target.innerHTML = result.attempts.slice(-5).reverse().map((attempt) => `<div class="validation-row"><span class="validation-check ${attempt.outcome === "PASSED" ? "ok" : attempt.outcome === "FAILED" ? "bad" : "pending"}">${attempt.outcome === "PASSED" ? "✓" : attempt.outcome === "FAILED" ? "!" : "·"}</span><span><strong>${esc(attempt.outcome.replaceAll("_", " "))}</strong><small>${esc(attempt.validator_version)} · ${esc(dateTime(attempt.completed_at || attempt.started_at))}</small></span></div>`).join("");
  } catch (error) {
    if (handleSessionError(error)) return;
    target.textContent = "Validation details could not be loaded.";
  }
}

function renderOfficialSelection(event) {
  const detail = app.querySelector("#official-detail");
  const state = app.querySelector("#official-state");
  const button = app.querySelector("#select-official");
  if (!detail || !state || !button) return;
  const latest = event.latest_submission;
  if (event.official_frozen_at) {
    state.innerHTML = badge("Frozen");
    detail.textContent = event.official_submission
      ? `Version ${event.official_submission.version} was frozen on ${dateTime(event.official_frozen_at)}.`
      : `Official selection closed on ${dateTime(event.official_frozen_at)}.`;
  } else if (event.official_submission) {
    state.innerHTML = badge("Selected");
    detail.textContent = `Version ${event.official_submission.version} is currently selected as your official entry.`;
  } else {
    state.innerHTML = badge("Not selected");
    detail.textContent = "Choose an eligible version before the official freeze.";
  }
  button.hidden = Boolean(event.official_frozen_at) || !latest || latest.state !== "ELIGIBLE";
}

async function selectLatestOfficial() {
  try {
    const status = await api.eventStatus(sessionToken);
    if (!status.latest_submission || status.latest_submission.state !== "ELIGIBLE") return notify("An eligible version is required.");
    await api.selectOfficialSubmission(sessionToken, { submission_id: status.latest_submission.submission_id, version: status.latest_submission.version });
    notify("Official submission selection updated");
    await loadProfile();
  } catch (error) {
    if (!handleSessionError(error)) notify(error.message || "Could not update official submission");
  }
}

function navigate(section, participant = activeParticipant) {
  if (!participant || !sessionToken) return landing("login");
  switch (section) {
    case "overview": dashboard(participant); break;
    case "build": renderBuild(participant); break;
    case "submit": renderSubmit(participant); break;
    case "matches": renderMatches(participant); break;
    case "results": renderResults(participant); break;
    case "bracket": renderCompetitionUnavailable(participant, "bracket"); break;
    case "leaderboard": renderCompetitionUnavailable(participant, "leaderboard"); break;
    case "profile": renderProfile(participant); break;
    default: dashboard(participant);
  }
}

function renderPublicView(view) {
  if (view === "admin") return renderAdminUnavailable();
  renderArena("live");
}

function returnToParticipantAccess() {
  if (sessionToken) void signOut();
  else landing("login");
}

const arenaNavigation = [["live", "Live Arena"], ["bracket", "Tournament"], ["leaderboard", "Leaderboard"]];

function renderArena(active = "live") {
  document.body.classList.add("public-interface");
  route = `arena-${active}`;
  const content = active === "live"
    ? `<section class="arena-hero"><div class="arena-copy"><span class="live-pill"><i></i> LIVE ARENA</span><h1>Two minds. One board.</h1><p>Public live-match feed is unavailable from this API instance.</p><div class="arena-state"><strong>No public match data</strong><span>Identities, clocks, moves and results will appear from the live-match service.</span></div></div><div class="arena-board-placeholder"><div class="arena-empty-board" role="img" aria-label="Empty chessboard illustration; no live position loaded">${Array.from({ length: 64 }, (_, index) => `<span class="${(Math.floor(index / 8) + index % 8) % 2 ? "dark" : "light"}"></span>`).join("")}</div><strong>Live board unavailable</strong><small>No match position is being fabricated.</small></div></section><section class="arena-lower"><article class="arena-panel"><h2>Live Matches</h2><p>No public match list is exposed by the current backend.</p></article><article class="arena-panel"><h2>Match Information</h2><p>Move list and evaluation data are not available.</p></article></section>`
    : `<section class="arena-unavailable"><span class="eyebrow">PUBLIC COMPETITION DATA</span><h1>${active === "bracket" ? "Tournament Bracket" : "Leaderboard"}</h1><p>The current backend does not publish ${active === "bracket" ? "a tournament bracket" : "leaderboard standings"} to a spectator API. No entries or rankings are displayed.</p></section>`;
  app.innerHTML = `<div class="spectator-app"><header class="spectator-header"><a class="spectator-brand" href="#arena" data-view="arena"><span>AI</span> KNOCKOUT<small>BY INVITATION ONLY</small></a><nav aria-label="Spectator navigation">${arenaNavigation.map(([key, label]) => `<button type="button" class="${active === key ? "active" : ""}" data-arena-page="${key}" ${active === key ? 'aria-current="page"' : ""}>${label}</button>`).join("")}</nav><span class="spectator-unavailable">PUBLIC FEED UNAVAILABLE</span><button class="arena-return" type="button" data-view="participant">Participant access</button></header><main class="spectator-main">${content}</main></div>`;
  app.querySelectorAll("[data-arena-page]").forEach((button) => button.addEventListener("click", () => renderArena(button.dataset.arenaPage)));
  app.querySelectorAll('[data-view="arena"]').forEach((button) => button.addEventListener("click", (event) => { event.preventDefault(); renderArena("live"); }));
  app.querySelector('[data-view="participant"]').addEventListener("click", returnToParticipantAccess);
}

function renderAdminUnavailable(active = "overview") {
  document.body.classList.add("public-interface");
  route = "admin-unavailable";
  const sections = [["overview", "Overview"], ["participants", "Participants"], ["submissions", "Submissions"], ["validation", "Validation Queue"], ["matches", "Live Matches"], ["workers", "Worker Fleet"], ["bracket", "Tournament Bracket"], ["controls", "Event Controls"], ["health", "System Health"], ["security", "Security & Audit"], ["settings", "Settings"]];
  const activeSection = sections.find(([key]) => key === active) || sections[0];
  app.innerHTML = `<div class="admin-app"><header class="admin-header"><a class="brand" href="#admin" aria-label="AI Knockout Admin Control Center"><span class="brand-mark">AI</span><span class="brand-word">KNOCKOUT<small>TOURNAMENT CONTROL CENTER</small></span></a><div class="admin-status"><span>${badge("Admin API unavailable")}</span><button type="button" class="button button-secondary" data-view="participant">Participant access</button></div></header><div class="admin-layout"><aside class="admin-nav" aria-label="Admin navigation">${sections.map(([key, label]) => `<button type="button" class="admin-nav-item ${active === key ? "active" : ""}" data-admin-page="${key}" ${active === key ? 'aria-current="page"' : ""}>${esc(label)}</button>`).join("")}</aside><main class="admin-main"><div class="page-heading"><div><span class="eyebrow">ADMINISTRATIVE INTERFACE / ${esc(activeSection[1].toUpperCase())}</span><h1>${esc(activeSection[1] === "Overview" ? "Tournament Control Center" : activeSection[1])}</h1></div><span class="status-badge danger">NON-OPERATIONAL</span></div><section class="admin-empty"><strong>Administrative API and authorization are not available.</strong><p>This service does not expose admin identity or ${esc(activeSection[1].toLowerCase())} data and controls. This view is non-operational and displays no live values.</p></section><section class="admin-empty-grid">${["Participants", "Submissions", "Validation", "Live matches", "Workers", "Security alerts"].map((label) => `<article><span>${esc(label)}</span><strong>—</strong><small>Data unavailable</small></article>`).join("")}</section></main></div></div>`;
  app.querySelectorAll("[data-admin-page]").forEach((button) => button.addEventListener("click", () => renderAdminUnavailable(button.dataset.adminPage)));
  app.querySelector('[data-view="participant"]').addEventListener("click", returnToParticipantAccess);
}

function renderBuild(participant) {
  shell(participant, "build", "Build Your Model", `<section class="build-layout"><article class="card build-main"><span class="phase-pill"><i></i> SERVER EVENT CLOCK</span><h2>Build your AI in your own environment.</h2><p class="body-copy">The portal reports the event clock and accepts a completed canonical USTAR package. It does not provide a hosted code editor or build runner.</p><div class="build-clock-panel"><span>BUILD SESSION ELAPSED</span><strong id="build-elapsed" aria-live="off">Loading</strong><small id="build-started">Loading server event status</small></div><div class="build-steps"><div><span>01</span><strong>Develop</strong><small>Use your own trusted development environment.</small></div><div><span>02</span><strong>Package</strong><small>Prepare an uncompressed USTAR archive.</small></div><div><span>03</span><strong>Submit</strong><small>The server validates the uploaded bytes.</small></div></div><button class="button button-primary" type="button" data-page="submit">Continue to Submit <span>→</span></button></article><aside class="card build-aside"><div class="card-heading"><h2>Package status</h2>${badge("Server validation")}</div><p class="body-copy">Package acceptance and eligibility come from the validation service. No build or validation result is estimated here.</p><div id="build-latest" class="unavailable-block">Loading latest submission…</div></aside></section>`);
  loadBuildStatus();
}

async function loadBuildStatus() {
  try {
    const status = await api.eventStatus(sessionToken);
    if (route !== "build") return;
    startElapsedClock("build-elapsed", status.build_elapsed_seconds);
    app.querySelector("#build-started").textContent = `Started ${dateTime(status.build_started_at)} · server time`;
    app.querySelector("#build-latest").innerHTML = status.latest_submission
      ? `<strong>Version ${esc(status.latest_submission.version)}</strong><p>${badge(status.latest_submission.state)}</p><small>Received ${esc(dateTime(status.latest_submission.submitted_at))}</small>`
      : "No submission version is recorded.";
  } catch (error) { if (!handleSessionError(error)) app.querySelector("#build-latest").textContent = "Event clock unavailable."; }
}

function renderSubmit(participant) {
  shell(participant, "submit", "Submit Your Model", `<section class="submit-layout"><article class="card upload-card"><div class="card-heading"><div><span class="eyebrow">UPLOAD → VALIDATION → ELIGIBILITY</span><h2>Submit your package</h2></div><span class="card-mark blue">↑</span></div><p class="body-copy">Upload the exact uncompressed USTAR archive produced by your build. The server records the artifact and reports validation; upload success alone does not mean the package is eligible.</p><form id="upload-form" class="upload-form"><label class="file-drop" for="archive-file"><span class="upload-symbol" aria-hidden="true">↑</span><strong>Choose a USTAR archive</strong><span>Uncompressed .tar · package limits enforced by server</span><input id="archive-file" type="file" accept=".tar,application/x-tar,application/octet-stream" required><span id="file-name" class="file-name">No file selected</span></label><p id="upload-error" class="form-message" role="alert"></p><button class="button button-primary" type="submit">Submit for validation <span>→</span></button></form><div id="upload-result" aria-live="polite"></div></article><article class="card workflow-card"><div class="card-heading"><h2>Submission workflow</h2></div><ol class="workflow"><li><span>1</span><div><strong>Uploaded</strong><small>Exact archive bytes are received.</small></div></li><li><span>2</span><div><strong>Validating</strong><small>Package checks and security validation run server-side.</small></div></li><li><span>3</span><div><strong>Eligible</strong><small>Only an eligible version can be selected or used for a development match.</small></div></li></ol><p class="callout-note">A rejected or pending result remains visible in submission history. Do not upload credentials or secrets in an archive.</p></article></section><section class="card history-card"><div class="card-heading"><h2>Submission History</h2><span class="muted-label">Server records</span></div><div id="submission-history">Loading submissions…</div></section>`);
  app.querySelector("#archive-file").addEventListener("change", (event) => {
    app.querySelector("#file-name").textContent = event.target.files[0]?.name || "No file selected";
  });
  app.querySelector("#upload-form").addEventListener("submit", uploadSubmission);
  loadSubmissionHistory();
}

async function uploadSubmission(event) {
  event.preventDefault();
  const file = app.querySelector("#archive-file").files[0];
  const button = event.currentTarget.querySelector("button[type=submit]");
  const errorNode = app.querySelector("#upload-error");
  const resultNode = app.querySelector("#upload-result");
  if (!file) return;
  button.disabled = true;
  button.textContent = "Uploading…";
  errorNode.textContent = "";
  resultNode.textContent = "";
  try {
    const created = await api.createSubmission(sessionToken);
    const uploaded = await api.uploadVersion(sessionToken, created.submission_id, await file.arrayBuffer());
    const validation = uploaded.validation || {};
    resultNode.innerHTML = `<div class="upload-outcome"><strong>Server recorded version ${esc(uploaded.version.version)}</strong>${badge(uploaded.version.state)}<p>Validation outcome: ${esc(validation.outcome || "Not reported")} · ${esc(validation.validator_version || "validator version unavailable")}</p><small>Artifact SHA-256: <code>${esc(uploaded.version.artifact_sha256)}</code></small></div>`;
    await loadSubmissionHistory();
    notify("Submission received by the server");
  } catch (error) {
    if (handleSessionError(error)) return;
    if (error.status === 422 && error.payload) {
      const v = error.payload.validation || {};
      resultNode.innerHTML = `<div class="upload-outcome rejected"><strong>Package rejected</strong>${badge(v.outcome || "Rejected")}<p>${esc(error.payload.error || "The server rejected this archive.")}</p><small>The rejected result is retained in server history.</small></div>`;
      await loadSubmissionHistory();
    } else errorNode.textContent = error.message || "Upload failed. The server did not confirm receipt.";
  } finally {
    if (button.isConnected) { button.disabled = false; button.innerHTML = 'Submit for validation <span>→</span>'; }
  }
}

async function loadSubmissionHistory() {
  const target = app.querySelector("#submission-history");
  if (!target) return;
  try {
    const submissions = await api.submissions(sessionToken);
    const versions = [];
    for (const item of (submissions.items || []).slice(0, 12)) {
      const response = await api.submissionVersions(sessionToken, item.submission_id);
      versions.push(...(response.items || []));
    }
    if (!target.isConnected) return;
    if (!versions.length) { target.innerHTML = `<div class="empty-table-state">No submissions recorded.</div>`; return; }
    target.innerHTML = `<div class="data-table-wrap"><table class="data-table"><thead><tr><th>Version</th><th>Validation</th><th>Submitted</th><th>Artifact</th><th></th></tr></thead><tbody>${versions.slice(0, 30).map((version) => `<tr><td>v${esc(version.version)}</td><td>${badge(version.state)}</td><td>${esc(dateTime(version.submitted_at))}</td><td><code>${esc(version.artifact_sha256.slice(0, 12))}…</code></td><td><button class="table-action" type="button" data-validation="${esc(version.submission_id)}" data-version="${esc(version.version)}">Details</button></td></tr>`).join("")}</tbody></table></div>`;
    target.querySelectorAll("[data-validation]").forEach((button) => button.addEventListener("click", () => renderSubmissionDetails(activeParticipant, button.dataset.validation, button.dataset.version)));
  } catch (error) { if (!handleSessionError(error)) target.textContent = "Submission history could not be loaded."; }
}

async function renderSubmissionDetails(participant, submissionId, version) {
  shell(participant, "submit", "Submission Details", `<section class="card submission-detail-card"><div class="card-heading"><div><span class="eyebrow">SERVER RECORDED ARTIFACT</span><h2>Submission version ${esc(version)}</h2></div><button class="button button-secondary" id="submission-back" type="button">← Submission history</button></div><div id="submission-detail-content" class="loading-copy">Loading exact version and validation evidence…</div></section>`);
  app.querySelector("#submission-back").addEventListener("click", () => renderSubmit(participant));
  try {
    const [versions, validation] = await Promise.all([api.submissionVersions(sessionToken, submissionId), api.validation(sessionToken, submissionId, version)]);
    const item = (versions.items || []).find((entry) => String(entry.version) === String(version));
    const target = app.querySelector("#submission-detail-content");
    if (!target || !item) return;
    const attempts = validation.attempts || [];
    const manifest = item.manifest || {};
    target.innerHTML = `<div class="submission-detail-summary"><div><span class="eyebrow">VALIDATION STATUS</span>${badge(item.state)}</div><div><span class="eyebrow">SUBMITTED</span><strong>${esc(dateTime(item.submitted_at))}</strong></div><div><span class="eyebrow">ARTIFACT SHA-256</span><code>${esc(item.artifact_sha256)}</code></div></div><section class="validation-evidence"><h3>Validation evidence</h3>${attempts.length ? `<ol class="validation-attempts">${attempts.map((attempt) => `<li><div>${badge(attempt.outcome)}<strong>${esc(attempt.validator_version)}</strong></div><time>${esc(dateTime(attempt.completed_at || attempt.started_at))}</time>${attempt.report ? `<pre>${esc(JSON.stringify(attempt.report, null, 2))}</pre>` : `<p>Detailed report not exposed by the server.</p>`}</li>`).join("")}</ol>` : `<p class="muted-copy">No validation attempts are recorded for this version.</p>`}</section><section class="manifest-summary"><h3>Manifest</h3><dl class="profile-facts"><div><dt>Engine</dt><dd>${esc(manifest.engine_name || "Not present in response")}</dd></div><div><dt>Engine version</dt><dd>${esc(manifest.engine_version || "Not present in response")}</dd></div><div><dt>Protocol</dt><dd>${esc(manifest.protocol_version || "Not present in response")}</dd></div><div><dt>Runtime</dt><dd>${esc(manifest.runtime || "Not present in response")}</dd></div></dl></section>`;
  } catch (error) { if (!handleSessionError(error)) app.querySelector("#submission-detail-content").textContent = "Submission details are unavailable."; }
}

async function requestDevelopmentMatch() {
  try {
    const status = await api.eventStatus(sessionToken);
    const latest = status.latest_submission;
    if (!latest || latest.state !== "ELIGIBLE") return notify("An eligible submission version is required.");
    const idempotencyKey = crypto.randomUUID();
    await api.requestDevelopmentMatch(sessionToken, { submission_id: latest.submission_id, version: latest.version }, idempotencyKey);
    notify("Development match request recorded");
    renderMatches(activeParticipant);
  } catch (error) {
    if (handleSessionError(error)) return;
    notify(error.status === 409 ? "A development match is already active or this request conflicts with server state." : error.message || "Could not request a development match.");
  }
}

function renderMatches(participant) {
  shell(participant, "matches", "Development Matches", `<section class="match-banner"><div><span class="phase-pill"><i></i> DEVELOPMENT · NOT OFFICIAL</span><h2>Test your AI against the event House AI.</h2><p>Exact submission versions, House AI identity, policy and match state come from the server.</p></div><button class="button button-primary" id="matches-request" type="button" disabled>Request match <span>→</span></button></section><section class="card matches-card"><div class="card-heading"><h2>Your Matches</h2><button class="link-button" id="matches-refresh" type="button">Refresh ↻</button></div><div id="matches-list">Loading match records…</div></section>`);
  app.querySelector("#matches-request").addEventListener("click", requestDevelopmentMatch);
  app.querySelector("#matches-refresh").addEventListener("click", () => renderMatches(participant));
  loadMatches(participant);
}

async function loadMatches(participant) {
  const target = app.querySelector("#matches-list");
  try {
    const [result, status] = await Promise.all([api.developmentMatches(sessionToken), api.eventStatus(sessionToken)]);
    if (!target?.isConnected) return;
    const latest = status.latest_submission;
    const active = (result.items || []).some((match) => ["QUEUED", "STARTING", "RUNNING"].includes(match.state));
    const button = app.querySelector("#matches-request");
    button.disabled = !latest || latest.state !== "ELIGIBLE" || active;
    button.title = !latest || latest.state !== "ELIGIBLE" ? "An eligible submission version is required." : active ? "An active match already exists." : "";
    const matches = result.items || [];
    if (!matches.length) { target.innerHTML = `<div class="empty-table-state"><strong>No development matches yet</strong><p>When an eligible submission is available, request a development match here.</p></div>`; return; }
    target.innerHTML = `<div class="development-match-list">${matches.map((match) => `<article class="development-match-row"><div class="match-icon">${navIcon("matches")}</div><div class="match-subject"><strong>${esc(match.participant?.submission_id ? `Submission ${match.participant.submission_version}` : "Participant AI")}</strong><small>vs ${esc(match.house_ai?.house_ai_id || "House AI")} · v${esc(match.house_ai?.version ?? "—")}</small></div><div class="match-time"><span>${esc(dateTime(match.completed_at || match.created_at))}</span><small>Development only</small></div>${badge(match.state)}<button class="button button-secondary" data-match="${esc(match.match_id)}" type="button">${match.state === "COMPLETED" ? "View result" : "View status"}</button></article>`).join("")}</div>`;
    target.querySelectorAll("[data-match]").forEach((button) => button.addEventListener("click", () => renderMatchDetail(participant, button.dataset.match)));
  } catch (error) { if (!handleSessionError(error)) target.textContent = "Match service unavailable. No match state is being inferred."; }
}

async function renderMatchDetail(participant, matchId) {
  shell(participant, "matches", "Development Match", `<section class="card match-detail-card"><div class="card-heading"><div><span class="phase-pill"><i></i> DEVELOPMENT · NOT OFFICIAL</span><h2>Match details</h2></div><button class="button button-secondary" id="match-refresh" type="button">Refresh status</button></div><div id="match-detail-content" class="loading-copy">Loading authoritative match state…</div></section>`);
  app.querySelector("#match-refresh").addEventListener("click", () => renderMatchDetail(participant, matchId));
  try {
    const match = await api.developmentMatch(sessionToken, matchId);
    if (!app.querySelector("#match-detail-content")) return;
    const state = match.state;
    app.querySelector("#match-detail-content").innerHTML = `<div class="match-detail-grid"><div class="match-overview"><div class="match-state-line">${badge(state)}<span>${esc(dateTime(match.started_at || match.created_at))}</span></div><div class="player-pair"><div class="player-card"><span class="player-color white">W</span><span><strong>${esc(match.participant?.submission_id ? `Participant · v${match.participant.submission_version}` : "Participant AI")}</strong><small>${esc(match.participant?.artifact_sha256 || "Artifact identity unavailable")}</small></span></div><div class="player-card"><span class="player-color black">B</span><span><strong>${esc(match.house_ai?.house_ai_id || "House AI")} · v${esc(match.house_ai?.version ?? "—")}</strong><small>${esc(match.house_ai?.artifact_sha256 || "Artifact identity unavailable")}</small></span></div></div><dl class="match-facts"><div><dt>Clock</dt><dd>${esc(match.initial_clock_ms / 60000)}+${esc(match.increment_ms / 1000)}</dd></div><div><dt>Move deadline</dt><dd>${esc(match.move_timeout_ms)} ms</dd></div><div><dt>Protocol</dt><dd>${esc(match.protocol_version)}</dd></div><div><dt>Rules</dt><dd>${esc(match.rules_version)}</dd></div></dl><div class="result-panel"><span class="eyebrow">SERVER RESULT</span><strong>${esc(match.result?.outcome || state.replaceAll("_", " "))}</strong><p>${esc(match.result?.reason || "No result recorded yet")}${match.result?.failure ? ` · ${esc(match.result.failure)}` : ""}</p></div></div><div class="board-panel"><div class="card-heading"><h3>Current Position</h3><small>${match.current_ply} plies</small></div>${renderChessboard(match.current_fen)}<div class="clock-row"><span>White <strong>${formatClock(match.white_remaining_ms)}</strong></span><span>Black <strong>${formatClock(match.black_remaining_ms)}</strong></span></div><p class="muted-copy">Evaluation and move transcript are not included in this API response.</p></div></div>`;
  } catch (error) { if (!handleSessionError(error)) app.querySelector("#match-detail-content").textContent = error.status === 404 ? "Match not found." : "Match state unavailable."; }
}

function formatClock(milliseconds = 0) {
  if (!Number.isFinite(milliseconds)) return "—";
  const seconds = Math.max(0, Math.floor(milliseconds / 1000));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
}

function renderChessboard(fen) {
  if (typeof fen !== "string" || !fen) return `<div class="board-unavailable">Position unavailable</div>`;
  const rows = fen.split(" ")[0].split("/");
  if (rows.length !== 8) return `<div class="board-unavailable">Server position could not be displayed.</div>`;
  const glyph = { p: "♟", r: "♜", n: "♞", b: "♝", q: "♛", k: "♚", P: "♙", R: "♖", N: "♘", B: "♗", Q: "♕", K: "♔" };
  let squares = "";
  rows.forEach((row, rank) => {
    let file = 0;
    for (const character of row) {
      const count = /[1-8]/.test(character) ? Number(character) : 1;
      for (let i = 0; i < count; i += 1) {
        const piece = glyph[character] ? character : "";
        const color = (rank + file) % 2 ? "dark" : "light";
        squares += `<span class="square ${color}" aria-label="${String.fromCharCode(97 + file)}${8 - rank}${piece ? ` ${piece}` : " empty"}">${piece ? glyph[piece] : ""}</span>`;
        file += 1;
      }
    }
    if (file !== 8) squares = "";
  });
  return squares ? `<div class="chessboard" role="img" aria-label="Server-reported chess position">${squares}</div>` : `<div class="board-unavailable">Server position could not be displayed.</div>`;
}

function renderResults(participant) {
  shell(participant, "results", "Results", `<div class="page-tabs" aria-label="Competition views"><button class="button button-secondary" type="button" data-page="bracket">Tournament Bracket</button><button class="button button-secondary" type="button" data-page="leaderboard">Leaderboard</button></div><section class="card results-card"><div class="card-heading"><div><span class="eyebrow">DEVELOPMENT ONLY</span><h2>Completed Match Results</h2></div><button class="link-button" type="button" data-page="matches">All matches →</button></div><div id="results-list">Loading server result records…</div></section><div class="callout-note">Development results do not represent official tournament standings.</div>`);
  loadResults(participant);
}

function renderCompetitionUnavailable(participant, kind) {
  const isBracket = kind === "bracket";
  const title = isBracket ? "Tournament Bracket" : "Leaderboard";
  const subject = isBracket ? "official bracket" : "official standings";
  shell(participant, kind, title, `<section class="card unavailable-competition-card"><div class="empty-state-mark" aria-hidden="true">${isBracket ? "⌘" : "≡"}</div><span class="eyebrow">OFFICIAL COMPETITION DATA</span><h2>${title}</h2><p>The current participant API does not expose ${subject}. This view will show server-published data when that capability is available.</p><p class="unavailable-label">No bracket or ranking data is being inferred.</p><button class="button button-secondary" type="button" data-page="results">← Development results</button></section>`);
}

async function loadResults(participant) {
  try {
    const response = await api.developmentMatches(sessionToken);
    const completed = (response.items || []).filter((match) => match.state === "COMPLETED" || match.state === "ABORTED");
    const target = app.querySelector("#results-list");
    if (!target) return;
    if (!completed.length) { target.innerHTML = `<div class="empty-table-state">No completed development results are recorded.</div>`; return; }
    target.innerHTML = `<div class="data-table-wrap"><table class="data-table"><thead><tr><th>Match</th><th>House AI</th><th>State</th><th>Result</th><th>Completed</th><th></th></tr></thead><tbody>${completed.map((match) => `<tr><td><code>${esc(match.match_id.slice(0, 8))}</code></td><td>${esc(match.house_ai?.house_ai_id || "—")} · v${esc(match.house_ai?.version ?? "—")}</td><td>${badge(match.state)}</td><td>${esc(match.result?.outcome || "No result")}${match.result?.reason ? ` · ${esc(match.result.reason)}` : ""}</td><td>${esc(dateTime(match.completed_at))}</td><td><button class="table-action" data-match="${esc(match.match_id)}" type="button">Details</button></td></tr>`).join("")}</tbody></table></div>`;
    target.querySelectorAll("[data-match]").forEach((button) => button.addEventListener("click", () => renderMatchDetail(participant, button.dataset.match)));
  } catch (error) { if (!handleSessionError(error)) app.querySelector("#results-list").textContent = "Result service unavailable."; }
}

function renderProfile(participant) {
  shell(participant, "profile", "Participant Profile", `<section class="card profile-card"><div class="card-heading"><div><span class="eyebrow">SERVER-ISSUED IDENTITY</span><h2>Your account</h2></div>${badge(participant.status)}</div><dl class="profile-facts"><div><dt>AI / entry name</dt><dd>${esc(participant.name)}</dd></div><div><dt>Participant ID</dt><dd><code>${esc(participant.participant_id)}</code></dd></div><div><dt>Team</dt><dd>${participant.team_name ? esc(participant.team_name) : "Not provided"}</dd></div><div><dt>Contact email</dt><dd>${participant.email ? esc(participant.email) : "Not provided"}</dd></div><div><dt>Registered</dt><dd>${esc(dateTime(participant.created_at))}</dd></div><div><dt>Account status</dt><dd>${badge(participant.status)}</dd></div></dl><p class="callout-note">Access keys are never displayed again after registration. Sign out clears this browser session.</p></section>`);
}

function handleSessionError(error) {
  if (error.status === 401 || error.status === 403) {
    sessionToken = null;
    landing("login", "Your session expired or was revoked. Sign in again.");
    return true;
  }
  return false;
}

async function loadProfile() {
  if (!sessionToken) return landing("login");
  try {
    const profile = await api.me(sessionToken);
    activeParticipant = profile;
    navigate(route === "login" || route === "dashboard" ? "overview" : route, profile);
  } catch (error) {
    if (!handleSessionError(error)) notify(error.message || "Could not refresh the profile");
  }
}

async function signOut() {
  const token = sessionToken;
  sessionToken = null;
  activeParticipant = null;
  clearInterval(buildClockTimer);
  landing("login");
  let revoked = !token;
  try { if (token) { await api.logout(token); revoked = true; } }
  catch { /* The local credential is cleared even when server revocation cannot be confirmed. */ }
  notify(revoked
    ? "Signed out. Server session revoked."
    : "Signed out on this device; server session revocation could not be confirmed.");
}

landing("login");
