import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";
import { bindArchiveDrop } from "../assets/uploadDrop.js";

const testsDir = path.dirname(fileURLToPath(import.meta.url));
const assetsDir = path.resolve(testsDir, "../assets");

test("redesign stylesheet retains responsive, typography, and motion support", async () => {
  const [html, css] = await Promise.all([
    readFile(path.resolve(testsDir, "../index.html"), "utf8"),
    readFile(path.join(assetsDir, "styles.css"), "utf8"),
  ]);
  assert.match(html, /styles\.css/);
  assert.match(css, /:root\[data-theme="dark"\][\s\S]*--accent:\s*#[0-9a-f]{6}/i);
  assert.match(css, /\.access-layout/);
  assert.match(css, /\.overview-stage-grid/);
  assert.match(css, /\.workbench-grid/);
  assert.match(css, /\.submit-layout/);
  assert.match(css, /\.arena-hero/);
  assert.match(css, /@media\s*\(max-width:\s*1023px\)/);
  assert.match(css, /@media\s*\(max-width:\s*560px\)/);
  assert.match(css, /@media\s*\(max-width:\s*560px\)[\s\S]*?grid-template-areas:\s*"intro"\s*"panel"\s*"board"/);
  assert.match(css, /@media\s*\(max-width:\s*560px\)[\s\S]*?\.admin-layout\s*\{[^}]*grid-template-columns:\s*minmax\(0,1fr\)/);
  assert.match(css, /\.admin-main \.page-heading\s*\{\s*flex-wrap:\s*wrap/);
  assert.match(css, /@media\s*\(prefers-reduced-motion:\s*reduce\)/);
});

function fakeEvent(type, props = {}) {
  return { type, defaultPrevented: false, preventDefault() { this.defaultPrevented = true; }, ...props };
}

test("archive drop highlights the target, transfers files, and dispatches the existing change flow", () => {
  const handlers = new Map();
  const classes = new Set();
  const zone = {
    classList: { add: (v) => classes.add(v), remove: (v) => classes.delete(v) },
    dataset: {},
    addEventListener: (name, handler) => handlers.set(name, handler),
    removeEventListener: (name) => handlers.delete(name),
    contains: () => false,
  };
  const selected = [];
  const input = { files: [], dispatchEvent: (event) => selected.push(event.type) };
  class FakeDataTransfer {
    constructor() { this.files = []; this.items = { add: (file) => this.files.push(file) }; }
  }
  class FakeEvent { constructor(type, init) { this.type = type; this.bubbles = init.bubbles; } }
  const dispose = bindArchiveDrop(zone, input, { DataTransfer: FakeDataTransfer, Event: FakeEvent });
  const entering = fakeEvent("dragenter");
  handlers.get("dragenter")(entering);
  assert.equal(entering.defaultPrevented, true);
  assert.equal(classes.has("drag-active"), true);
  const file = { name: "agent.tar" };
  const dropped = fakeEvent("drop", { dataTransfer: { files: [file] } });
  handlers.get("drop")(dropped);
  assert.equal(dropped.defaultPrevented, true);
  assert.deepEqual(input.files, [file]);
  assert.deepEqual(selected, ["change"]);
  assert.equal(classes.has("drag-active"), false);
  dispose();
  assert.equal(handlers.size, 0);
});

test("archive drop leaves the native file picker intact when file transfer is unsupported", () => {
  const handlers = new Map();
  const classes = new Set();
  const status = { textContent: "No file selected" };
  const zone = {
    classList: { add: (v) => classes.add(v), remove: (v) => classes.delete(v) },
    dataset: {}, addEventListener: (name, handler) => handlers.set(name, handler),
    removeEventListener: () => {}, contains: () => false,
    querySelector: () => status,
  };
  const input = { files: [], dispatchEvent() { throw new Error("unexpected change"); } };
  const file = { name: "agent.tar" };
  class RestrictedDataTransfer { constructor() { throw new Error("restricted"); } }
  bindArchiveDrop(zone, input, { DataTransfer: RestrictedDataTransfer, Event: class {} });
  handlers.get("drop")(fakeEvent("drop", { dataTransfer: { files: [file] } }));
  assert.deepEqual(input.files, []);
  assert.equal(classes.has("drag-active"), false);
  assert.equal(classes.has("drop-error"), true);
  assert.match(status.textContent, /choose the archive/i);
});

test("all static asset javascript files pass node syntax validation without errors", async () => {
  const { readdir } = await import("node:fs/promises");
  const { execFileSync } = await import("node:child_process");
  const files = await readdir(assetsDir);
  const jsFiles = files.filter((f) => f.endsWith(".js"));
  assert.ok(jsFiles.length >= 6, "Expected at least 6 static JS asset modules");
  for (const file of jsFiles) {
    const fullPath = path.join(assetsDir, file);
    assert.doesNotThrow(() => {
      execFileSync(process.execPath, ["--check", fullPath], { stdio: "pipe" });
    }, `Syntax check failed for ${file}`);
  }
});
