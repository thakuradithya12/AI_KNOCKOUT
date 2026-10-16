export function resultSourceLabel(source) {
  if (source === "manual") return "Manual result";
  if (source === "automated") return "Automated match";
  return "Source unverified";
}
