export function resolveSubmissionSnapshot({ submissionsResult, eventResult }) {
  const submissionsAvailable = submissionsResult?.status === "fulfilled" && Array.isArray(submissionsResult.value?.items);
  const eventAvailable = eventResult?.status === "fulfilled" && eventResult.value != null;
  const submissions = submissionsAvailable ? submissionsResult.value.items : [];
  const latest = (eventAvailable ? eventResult.value.latest_submission : null) || submissions[0] || null;

  if (latest) return { state: "available", latest, submissions, submissionsAvailable, eventAvailable };
  if (submissionsAvailable || eventAvailable) return { state: "empty", latest: null, submissions, submissionsAvailable, eventAvailable };
  return { state: "unavailable", latest: null, submissions: [], submissionsAvailable, eventAvailable };
}

export function resolveQuotaSnapshot(result) {
  if (result?.status !== "fulfilled") return { state: "unavailable", quota: null };
  const quota = result.value;
  const valid = Number.isFinite(quota?.accepted_submissions_count)
    && Number.isFinite(quota?.submission_limit)
    && Number.isFinite(quota?.remaining_submissions);
  return valid ? { state: "available", quota } : { state: "unavailable", quota: null };
}
