import { api, ApiError } from "/assets/api.js";
import { copyCredentialValue, credentialPasteValues, credentialsText } from "/assets/credentials.js";

const app = document.querySelector("#app");
const serverLabel = document.querySelector("#server-label");
const serverState = serverLabel.parentElement;
const toast = document.querySelector("#toast");

// The existing backend issues opaque bearer tokens. Keep the token in memory
// only; a page reload intentionally requires the participant to sign in again.
let sessionToken = null;
let route = "login";
let toastTimer;

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

function dashboard(participant) {
  route = "dashboard";
  app.innerHTML = `<section class="dashboard-head"><div><div class="section-index">AI KNOCKOUT <span>PARTICIPANT SYSTEM</span></div><h1 id="welcome">${esc(participant.name)}</h1><div class="identity-line"><span>Participant ID</span><code>${esc(participant.participant_id)}</code><span class="status-tag status-active">${esc(participant.status)}</span></div></div><div class="account-controls"><button class="button button-quiet" id="refresh" type="button">Refresh profile</button><button class="button button-quiet" data-signout type="button">Sign out</button></div></section>
    ${sectionNav("overview")}
    <section class="competition-state" aria-labelledby="competition-state-title">
      <div class="section-heading"><h2 id="competition-state-title">Current competition state</h2><span>Profile and service availability</span></div>
      <div class="competition-state-grid">
        <article class="state-card state-card-primary"><span class="state-label">Participant</span><strong><span class="status-tag status-active">${esc(participant.status)}</span></strong><p>Status from your participant profile.</p></article>
        <article class="state-card"><span class="state-label">AI submission</span><strong class="unavailable">Unavailable</strong><p>Submission status is not exposed by this server.</p></article>
        <article class="state-card"><span class="state-label">Validation</span><strong class="unavailable">Unavailable</strong><p>No validation state is available.</p></article>
        <article class="state-card"><span class="state-label">Next match</span><strong class="unavailable">Unavailable</strong><p>The server does not expose match scheduling.</p></article>
      </div>
    </section>
    <section class="overview-grid">
      <div class="overview-main">
        <section class="data-section services-section"><div class="section-heading"><h2>Competition services</h2><span>Availability</span></div>
          <div class="service-row"><div><h3>Submissions & validation</h3><p>The server does not provide submission or validation endpoints yet.</p></div><span class="status-tag status-unavailable">Unavailable</span></div>
          <div class="service-row"><div><h3>Matches & standings</h3><p>Scheduling, live game state, match history, and standings are not available.</p></div><span class="status-tag status-unavailable">Unavailable</span></div>
        </section>
      </div>
      <aside class="identity-panel" aria-labelledby="identity-title"><div class="section-heading"><h2 id="identity-title">My AI</h2><span>Server identity</span></div>
        <div class="identity-name">${esc(participant.name)}</div>
        <dl class="identity-facts"><div><dt>Participant ID</dt><dd>${esc(participant.participant_id)}</dd></div><div><dt>Team</dt><dd>${participant.team_name ? esc(participant.team_name) : "Not provided"}</dd></div><div><dt>Account status</dt><dd><span class="status-tag status-active">${esc(participant.status)}</span></dd></div></dl>
      </aside>
    </section>
    <section class="activity-panel" aria-labelledby="activity-title"><div class="section-heading"><h2 id="activity-title">Recent activity</h2><span>Event timeline</span></div><div class="activity-empty"><span class="activity-mark" aria-hidden="true">—</span><div><strong>Activity data unavailable</strong><p>The server does not provide a participant activity or event timeline endpoint yet.</p></div></div></section>`;
  app.querySelector("#refresh").addEventListener("click", loadProfile);
  app.querySelector("[data-signout]").addEventListener("click", signOut);
  bindSectionNav(participant);
}

const portalSections = [["overview", "Overview"], ["identity", "My AI"], ["submission", "Submissions"], ["matches", "Matches"], ["viewer", "Live match"], ["leaderboard", "Leaderboard"], ["history", "History"]];
function sectionNav(active) {
  return `<nav class="portal-nav" aria-label="Participant sections">${portalSections.map(([key, label]) => `<button type="button" class="portal-link ${key === active ? "selected" : ""}" data-section="${key}">${label}</button>`).join("")}</nav>`;
}
function bindSectionNav(participant) {
  app.querySelectorAll("[data-section]").forEach(button => button.addEventListener("click", () => button.dataset.section === "overview" ? dashboard(participant) : unavailableSection(participant, button.dataset.section)));
}
function unavailableSection(participant, section) {
  const sections = {
    identity: ["AI identity", "Identity details", "This account was issued by the participant service. The current profile contract provides your entry name, Participant ID, optional team name, contact email, status, and registration date."],
    submission: ["AI submission", "Submission service unavailable", "The current server has no submission endpoint or upload specification. No file has been uploaded and no validation state can be reported."],
    matches: ["My matches", "Schedule not published", "The current server has no match scheduling endpoint. Your match list will appear here when the tournament service publishes server-backed fixtures."],
    viewer: ["Live match", "No live match data", "A server-authoritative game feed is not available. The server currently exposes no FEN, move, clock, or match-state contract, so a chessboard cannot be rendered truthfully yet."],
    leaderboard: ["Leaderboard", "Standings unavailable", "The current server has no public leaderboard or scoring contract. No ranks or scores are being estimated."],
    history: ["Match history", "No match history service", "The current server has no match result or history endpoint. Completed games will appear here when those records are available from the server."]
  };
  const [title, heading, copy] = sections[section];
  route = section;
  app.innerHTML = `<section class="dashboard-head"><div><div class="section-index">AI KNOCKOUT <span>${esc(title.toUpperCase())}</span></div><h1>${esc(title)}</h1><div class="identity-line"><span>${esc(participant.name)}</span><code>${esc(participant.participant_id)}</code><span class="status-tag status-active">${esc(participant.status)}</span></div></div><div class="account-controls"><button class="button button-quiet" id="refresh" type="button">Refresh profile</button><button class="button button-quiet" data-signout type="button">Sign out</button></div></section>${sectionNav(section)}<section class="empty-state"><div class="empty-code">SERVICE STATUS / UNAVAILABLE</div><h2>${esc(heading)}</h2><p>${esc(copy)}</p></section>`;
  app.querySelector("#refresh").addEventListener("click", loadProfile);
  app.querySelector("[data-signout]").addEventListener("click", signOut);
  bindSectionNav(participant);
}

async function loadProfile() {
  if (!sessionToken) return landing("login");
  const refresh = document.querySelector("#refresh");
  if (refresh) { refresh.disabled = true; refresh.textContent = "Refreshing…"; }
  try {
    const profile = await api.me(sessionToken);
    dashboard(profile);
  } catch (error) {
    if (error.status === 401 || error.status === 403) {
      sessionToken = null;
      landing("login", "Your session expired or was revoked. Sign in again.");
    } else {
      notify(error.message || "Could not refresh the profile");
    }
  }
}

async function signOut() {
  const token = sessionToken;
  sessionToken = null;
  landing("login");
  try { if (token) await api.logout(token); }
  catch { /* Local sign-out still clears the in-memory credential. */ }
  notify("Signed out");
}

landing("login");
