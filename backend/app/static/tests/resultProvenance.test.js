import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { resultSourceLabel } from "../assets/resultProvenance.js";

test("result source labels are explicit and legacy values stay unverified", () => {
  assert.equal(resultSourceLabel("manual"), "Manual result");
  assert.equal(resultSourceLabel("automated"), "Automated match");
  assert.equal(resultSourceLabel("unknown"), "Source unverified");
  assert.equal(resultSourceLabel(null), "Source unverified");
  assert.equal(resultSourceLabel("forged-value"), "Source unverified");
});

test("participant result views use the provenance label and do not call every result verified", () => {
  const app = readFileSync(new URL("../assets/app.js", import.meta.url), "utf8");
  assert.match(app, /resultSourceLabel\(match\.result_source\)/);
  assert.match(app, /resultSourceLabel\(m\.result_source\)/);
  assert.match(app, /game1_result_source/);
  assert.match(app, /sudden_death_result_sources/);
  assert.doesNotMatch(app, /Verified Server Decision/);
});
