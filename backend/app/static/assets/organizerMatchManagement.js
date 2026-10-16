import { resultSourceLabel } from "./resultProvenance.js";

export const ORGANIZER_RESULT_OUTCOMES = Object.freeze(["WHITE_WIN", "BLACK_WIN", "DRAW"]);
export const ORGANIZER_RESULT_REASONS = Object.freeze([
  "CHECKMATE", "STALEMATE", "DEAD_POSITION", "FIVEFOLD_REPETITION",
  "SEVENTY_FIVE_MOVE_RULE", "THREEFOLD_REPETITION", "FIFTY_MOVE_RULE",
]);

const escapeHtml = (value = "") => String(value).replace(/[&<>"']/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[char]);
export function renderOrganizerMatchManagement(matches, { integrationAvailable = false, loadError = "", organizerName = "", auditEvents = null, auditError = "", refreshSnapshot = "" } = {}) {
  const status = integrationAvailable
    ? `Secure organizer session is active${organizerName ? ` for ${escapeHtml(organizerName)}` : ""}.`
    : "Live result recording is unavailable until a server-managed organizer session and CSRF token are verified.";
  const rows = Array.isArray(matches) ? matches : [];
  const content = loadError
    ? `<p class="admin-match-load-error" role="alert">${escapeHtml(loadError)}</p>`
    : rows.length
      ? `<div class="data-table-wrap admin-table-wrap"><table class="data-table admin-match-table"><thead><tr><th>Match ID</th><th>White</th><th>Black</th><th>Current state / result</th><th>Manual result</th></tr></thead><tbody>${rows.map((match) => {
        const done = ["COMPLETED", "ABORTED"].includes(match.state);
        const finalMessage = match.state === "COMPLETED" ? "Completed matches cannot be changed through this endpoint." : "Aborted matches cannot receive a result.";
      const resultSource = match.state === "COMPLETED"
        ? `<small class="result-source">${resultSourceLabel(match.result_source)}</small>`
        : "";
      return `<tr><td><code>${escapeHtml(match.match_id || "Unavailable")}</code></td><td><strong>White</strong><br>${escapeHtml(match.white_name || "Unknown participant")}<small>${escapeHtml(match.white_participant_id || "")}</small></td><td><strong>Black</strong><br>${escapeHtml(match.black_name || "Unknown participant")}<small>${escapeHtml(match.black_participant_id || "")}</small></td><td><span class="status-badge neutral">${escapeHtml(match.state || "Unavailable")}</span><small>${escapeHtml(match.result_outcome || "No result recorded")}${match.result_reason ? ` · ${escapeHtml(match.result_reason)}` : ""}</small>${resultSource}</td><td>${done ? `<p class="participant-action-note">${finalMessage}</p>` : `<form class="manual-result-form" data-match-id="${escapeHtml(match.match_id || "")}"><label>Outcome<select name="result_outcome" required ${integrationAvailable ? "" : "disabled"}><option value="">Select outcome</option>${ORGANIZER_RESULT_OUTCOMES.map((value) => `<option value="${value}">${value.replaceAll("_", " ")}</option>`).join("")}</select></label><label>Reason<select name="result_reason" required ${integrationAvailable ? "" : "disabled"}><option value="">Select reason</option>${ORGANIZER_RESULT_REASONS.map((value) => `<option value="${value}">${value.replaceAll("_", " ")}</option>`).join("")}</select></label><label class="manual-result-confirm"><input type="checkbox" name="confirm" required ${integrationAvailable ? "" : "disabled"}> Confirm this result is final. Completed matches cannot be changed.</label><button class="button button-secondary" type="submit" ${integrationAvailable ? "" : "disabled"}>Record result</button><p class="manual-result-state" role="status" aria-live="polite">${integrationAvailable ? "Ready" : "Unavailable until an organizer session and CSRF token are verified."}</p></form>`}</td></tr>`;
      }).join("")}</tbody></table></div>`
      : "<p class=\"admin-empty-state\">No official matches have been returned by the server.</p>";
  const auditContent = auditError
    ? `<p class="admin-match-load-error" role="alert">Audit history is unavailable: ${escapeHtml(auditError)}</p>`
    : Array.isArray(auditEvents) && auditEvents.length
      ? `<div class="data-table-wrap admin-table-wrap"><table class="data-table"><thead><tr><th>Recorded</th><th>Severity</th><th>Event</th><th>Actor</th><th>Record</th><th>Sequence</th><th>Details</th></tr></thead><tbody>${auditEvents.map((event) => `<tr><td>${escapeHtml(event.recorded_at || "Unavailable")}</td><td>${escapeHtml(event.severity || "Unavailable")}</td><td>${escapeHtml(event.event_type || "Unavailable")}<small><code>${escapeHtml(event.event_id || "")}</code></small></td><td>${escapeHtml(event.actor_kind || "Unknown")}: <code>${escapeHtml(event.actor_id || "Unavailable")}</code></td><td>${escapeHtml(event.aggregate_type || "Record")}: <code>${escapeHtml(event.aggregate_id || "Unavailable")}</code></td><td>${escapeHtml(event.sequence ?? "Unavailable")}</td><td><code>${escapeHtml(JSON.stringify(event.metadata || {}))}</code></td></tr>`).join("")}</tbody></table></div>`
      : Array.isArray(auditEvents)
        ? "<p class=\"admin-empty-state\">No audit events have been returned by the server.</p>"
        : "<p class=\"loading-copy\">Audit history has not been loaded.</p>";
  return `<section class="admin-match-management"><div class="admin-empty"><strong>Organizer match management</strong><p>${escapeHtml(status)}</p></div>${content}${refreshSnapshot}<section class="organizer-audit" aria-labelledby="organizer-audit-heading"><h2 id="organizer-audit-heading">Recent audit events</h2>${auditContent}</section></section>`;
}

export function renderOrganizerRefreshSnapshot(data) {
  const errors = data?.refreshErrors || {};
  const detail = data?.match;
  const leaderboard = data?.leaderboard;
  const rounds = [...(Array.isArray(data?.swissRounds) ? data.swissRounds : []), ...(Array.isArray(data?.playoffRounds) ? data.playoffRounds : [])];
  const bracket = data?.bracket;
  const errorNames = {
    matches: "match list", match: "match detail", swissRounds: "Swiss rounds", playoffRounds: "playoff rounds",
    leaderboard: "leaderboard", bracket: "bracket", auditEvents: "audit history",
  };
  const failures = Object.entries(errors).map(([key, value]) => `<li>${escapeHtml(errorNames[key] || key)}: ${escapeHtml(value)}</li>`).join("");
  const detailView = detail
    ? `<p>Match <code>${escapeHtml(detail.match_id || data.recorded?.match_id || "Unavailable")}</code>: ${escapeHtml(detail.state || "State unavailable")} · ${escapeHtml(detail.result_outcome || "Result unavailable")}${detail.result_reason ? ` (${escapeHtml(detail.result_reason)})` : ""}${detail.state === "COMPLETED" ? ` · ${resultSourceLabel(detail.result_source)}` : ""}</p>`
    : `<p>${errors.match ? "Updated match detail is unavailable." : "No match detail was returned."}</p>`;
  const standings = Array.isArray(leaderboard?.standings)
    ? leaderboard.standings.length
      ? `<div class="data-table-wrap admin-table-wrap"><table class="data-table"><thead><tr><th>Rank</th><th>Participant</th><th>Points</th><th>W–D–L</th></tr></thead><tbody>${leaderboard.standings.map((row) => `<tr><td>${escapeHtml(row.rank)}</td><td>${escapeHtml(row.name || row.participant_id || "Unknown")}</td><td>${escapeHtml(row.points)}</td><td>${escapeHtml(row.wins)}–${escapeHtml(row.draws)}–${escapeHtml(row.losses)}</td></tr>`).join("")}</tbody></table></div>`
      : "<p>The server returned no standings rows.</p>"
    : `<p>${errors.leaderboard ? "Leaderboard refresh unavailable." : "No leaderboard response was returned."}</p>`;
  const roundsView = rounds.length
    ? `<ul>${rounds.map((round) => `<li>${escapeHtml(round.round_name || `${round.stage || "Tournament"} round ${round.round_number ?? "?"}`)}: ${escapeHtml(round.status || "Status unavailable")} · ${Array.isArray(round.pairings) ? round.pairings.length : 0} pairings</li>`).join("")}</ul>`
    : `<p>${errors.swissRounds || errors.playoffRounds ? "One or more round refreshes are unavailable." : "The server returned no rounds; this API cannot distinguish an empty tournament from a round-read failure."}</p>`;
  const bracketView = bracket
    ? `<p>${escapeHtml(bracket.status || "Status unavailable")} · ${escapeHtml(bracket.total_participants ?? 0)} participants${bracket.champion?.name ? ` · Champion: ${escapeHtml(bracket.champion.name)}` : ""}</p>${!bracket.rounds?.length && !bracket.champion ? "<p>The bracket API returns an empty bracket on database read failure, so this state needs server-side verification.</p>" : ""}`
    : `<p>${errors.bracket ? "Bracket refresh unavailable." : "No bracket response was returned."}</p>`;
  return `<section class="organizer-refresh-snapshot" aria-labelledby="organizer-refresh-heading"><h2 id="organizer-refresh-heading">Server refresh after result recording</h2><p role="status">${data?.state === "success" ? "Result recording and refresh completed." : "Result recording was confirmed; some refreshes failed."}</p>${failures ? `<div class="admin-match-load-error" role="alert"><strong>Refresh failures</strong><ul>${failures}</ul></div>` : ""}<h3>Match</h3>${detailView}<h3>Standings</h3>${standings}<h3>Rounds</h3>${roundsView}<h3>Bracket</h3>${bracketView}</section>`;
}

const errorMessages = new Map([
  [400, "The server rejected the result fields. Check the selected outcome and reason."],
  [401, "Organizer session expired. Sign in again; the server did not authorize this result request."],
  [403, "The organizer request failed CSRF verification. Refresh the session before retrying."],
  [404, "Match not found. Refresh the match list."],
  [409, "The match is completed, aborted, or otherwise conflicts with this result."],
  [500, "The server did not confirm the result. Refresh match data before retrying."],
  [0, "The event server could not be reached. The result was not confirmed; refresh server data before retrying."],
]);

export function organizerResultErrorMessage(error) {
  return errorMessages.get(error?.status) || "The result could not be confirmed. Refresh server data and check match state.";
}

export function wireOrganizerMatchManagement(container, { api, onRefresh = () => {}, onUnauthorized = () => {} } = {}) {
  if (!container || typeof api?.recordResult !== "function") return () => {};
  const recorder = createManualResultRecorder(api);
  const listeners = [];
  for (const form of container.querySelectorAll(".manual-result-form")) {
    const submit = async (event) => {
      event.preventDefault();
      if (!form.checkValidity()) {
        form.reportValidity();
        return;
      }
      const matchId = form.dataset.matchId;
      const outcome = form.elements.namedItem("result_outcome").value;
      const reason = form.elements.namedItem("result_reason").value;
      const confirmed = form.elements.namedItem("confirm").checked;
      const button = form.querySelector('button[type="submit"]');
      const state = form.querySelector(".manual-result-state");
      const controls = [...form.querySelectorAll("select, input, button")];
      if (recorder.isPending(matchId)) return;
      controls.forEach((control) => { control.disabled = true; });
      button.textContent = "Recording…";
      state.textContent = "Sending result to the server…";
      state.dataset.state = "loading";

      const result = await recorder.submit({ matchId, outcome, reason, confirmed });
      if (result.state === "success" || result.state === "recorded-refresh-failed") {
        state.textContent = result.state === "success"
          ? "Result recorded. Updated server data is shown below."
          : "Result recorded, but one or more server refreshes failed. See the refresh details below.";
        state.dataset.state = result.state === "success" ? "success" : "warning";
        button.textContent = result.state === "success" ? "Recorded" : "Refresh required";
        try {
          await onRefresh(result);
        } catch {
          state.textContent = "Result recorded; refreshed data could not be displayed. Refresh before taking further action.";
          state.dataset.state = "warning";
        }
        return;
      }
      state.textContent = result.message;
      state.dataset.state = result.state === "pending" ? "loading" : "error";
      if (result.status === 401) onUnauthorized();
      controls.forEach((control) => { control.disabled = false; });
      button.textContent = "Record result";
    };
    form.addEventListener("submit", submit);
    listeners.push(() => form.removeEventListener("submit", submit));
  }
  return () => listeners.forEach((remove) => remove());
}

/**
 * Testable organizer UI contract. Production injects an adapter bound to a
 * server-managed session and its in-memory CSRF token.
 */
export function createManualResultRecorder(api) {
  const pending = new Set();
  return {
    isPending(matchId) { return pending.has(matchId); },
    async submit({ matchId, outcome, reason, confirmed }) {
      if (!matchId || !ORGANIZER_RESULT_OUTCOMES.includes(outcome) || !ORGANIZER_RESULT_REASONS.includes(reason) || confirmed !== true) {
        return { state: "invalid", message: "Choose a valid outcome and reason, then confirm the result is final." };
      }
      if (pending.has(matchId)) return { state: "pending", message: "A result request for this match is already in progress." };
      pending.add(matchId);
      let recorded;
      try {
        recorded = await api.recordResult(matchId, { result_outcome: outcome, result_reason: reason });
        if (!recorded || recorded.status !== "COMPLETED" || String(recorded.match_id) !== String(matchId)
          || recorded.result_outcome !== outcome || recorded.result_reason !== reason) {
          pending.delete(matchId);
          return { state: "error", status: 0, message: "The server response did not confirm this match result. Refresh server data before retrying." };
        }
      } catch (error) {
        return { state: "error", status: error?.status ?? 0, message: organizerResultErrorMessage(error) };
      } finally {
        if (!recorded) pending.delete(matchId);
      }

      try {
        const refreshCalls = {
          matches: () => api.getMatches(),
          match: () => api.getMatch(matchId),
          swissRounds: () => api.getRounds("SWISS"),
          playoffRounds: () => api.getRounds("PLAYOFF"),
          leaderboard: () => api.getLeaderboard(),
          bracket: () => api.getBracket(),
          auditEvents: () => api.getAuditLog(),
        };
        const names = Object.keys(refreshCalls);
        const settled = await Promise.allSettled(names.map((name) => refreshCalls[name]()));
        const refreshed = { state: "success", recorded, refreshErrors: {} };
        settled.forEach((item, index) => {
          const name = names[index];
          if (item.status === "fulfilled") refreshed[name] = item.value;
          else refreshed.refreshErrors[name] = item.reason?.message || "Request failed";
        });
        if (Object.keys(refreshed.refreshErrors).length) {
          refreshed.state = "recorded-refresh-failed";
          refreshed.message = "Result was recorded, but one or more server refreshes failed. Refresh before taking further action.";
        }
        return refreshed;
      } finally {
        pending.delete(matchId);
      }
    },
  };
}
