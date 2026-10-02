const participantIdPattern = /^part_[a-f0-9]{16}$/;
const accessKeyPattern = /^ak_[A-Za-z0-9_-]{43}$/;

export function classifyCredential(value) {
  const candidate = value.trim();
  if (participantIdPattern.test(candidate)) return { participant_id: candidate, access_key: null };
  if (accessKeyPattern.test(candidate)) return { participant_id: null, access_key: candidate };
  return null;
}

export function parseCredentialBlock(value) {
  const lines = value.trim().split(/\r?\n/).map((line) => line.trim()).filter(Boolean);
  if (lines.length !== 2) return null;
  const parsed = { participant_id: null, access_key: null };
  for (const line of lines) {
    const labeled = line.match(/^(participant\s+id|access\s+key)\s*:\s*(\S+)$/i);
    if (labeled) {
      const field = /^participant/i.test(labeled[1]) ? "participant_id" : "access_key";
      const token = classifyCredential(labeled[2]);
      if (!token || !token[field] || parsed[field]) return null;
      parsed[field] = token[field];
      continue;
    }
    const token = classifyCredential(line);
    if (!token) return null;
    const field = token.participant_id ? "participant_id" : "access_key";
    if (parsed[field]) return null;
    parsed[field] = token[field];
  }
  return parsed.participant_id && parsed.access_key ? parsed : null;
}

export function credentialPasteValues(value, currentId, currentKey, target) {
  const combined = parseCredentialBlock(value);
  if (combined) {
    if ((currentId && currentId !== combined.participant_id) || (currentKey && currentKey !== combined.access_key)) return null;
    return { participant_id: currentId || combined.participant_id, access_key: currentKey || combined.access_key, combined: true };
  }
  const single = classifyCredential(value);
  if (!single) return null;
  return {
    participant_id: single.participant_id || currentId,
    access_key: single.access_key || currentKey,
    combined: false,
  };
}

export function credentialsText(participantId, accessKey) {
  return `Participant ID: ${participantId}\nAccess Key: ${accessKey}`;
}

export async function copyCredentialValue(value, writeText) {
  await writeText(value);
}
