import test from "node:test";
import assert from "node:assert/strict";
import { classifyCredential, copyCredentialValue, credentialPasteValues, credentialsText, parseCredentialBlock } from "../assets/credentials.js";

const participantId = "part_0123456789abcdef";
const accessKey = `ak_${"A".repeat(43)}`;

test("copy Participant ID writes only its value", async () => {
  let copied;
  await copyCredentialValue(participantId, async (value) => { copied = value; });
  assert.equal(copied, participantId);
});

test("copy Access Key writes only its value", async () => {
  let copied;
  await copyCredentialValue(accessKey, async (value) => { copied = value; });
  assert.equal(copied, accessKey);
});

test("copy both uses the labeled registration receipt format", () => {
  assert.equal(credentialsText(participantId, accessKey), `Participant ID: ${participantId}\nAccess Key: ${accessKey}`);
});

test("paste only Participant ID fills only that field", () => {
  assert.deepEqual(credentialPasteValues(` ${participantId}\n`, "", "", "participant_id"), { participant_id: participantId, access_key: "", combined: false });
});

test("paste only Access Key fills only that field", () => {
  assert.deepEqual(credentialPasteValues(accessKey, "", "", "participant_id"), { participant_id: "", access_key: accessKey, combined: false });
});

test("combined credentials pasted into Participant ID fill both fields", () => {
  assert.deepEqual(credentialPasteValues(credentialsText(participantId, accessKey), "", "", "participant_id"), { participant_id: participantId, access_key: accessKey, combined: true });
});

test("combined credentials pasted into Access Key fill both fields", () => {
  assert.deepEqual(credentialPasteValues(credentialsText(participantId, accessKey), "", "", "access_key"), { participant_id: participantId, access_key: accessKey, combined: true });
});

test("labeled credentials parse with flexible label case and whitespace", () => {
  assert.deepEqual(parseCredentialBlock(` participant id : ${participantId} \r\n ACCESS KEY:  ${accessKey} `), { participant_id: participantId, access_key: accessKey });
});

test("unlabeled credentials parse on separate lines in either order", () => {
  assert.deepEqual(parseCredentialBlock(`${accessKey}\n${participantId}`), { participant_id: participantId, access_key: accessKey });
});

test("invalid or ambiguous clipboard content is rejected", () => {
  assert.equal(classifyCredential("part_fake"), null);
  assert.equal(parseCredentialBlock(`${participantId}\n${participantId}`), null);
  assert.equal(parseCredentialBlock(`Participant ID: ${participantId}\nextra\nAccess Key: ${accessKey}`), null);
  assert.equal(parseCredentialBlock(`Participant ID: ${participantId}\nAccess Key: wrong`), null);
});

test("combined paste does not overwrite conflicting existing credentials", () => {
  assert.equal(credentialPasteValues(credentialsText(participantId, accessKey), "part_ffffffffffffffff", "", "participant_id"), null);
});

test("clipboard write failures remain rejectable for graceful UI handling", async () => {
  await assert.rejects(copyCredentialValue(accessKey, async () => { throw new Error("clipboard denied"); }), /clipboard denied/);
});
