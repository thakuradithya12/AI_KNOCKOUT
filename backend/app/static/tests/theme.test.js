import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const themeSource = readFileSync(new URL("../assets/theme.js", import.meta.url), "utf8");

function bootTheme({ saved = null, systemDark = false, storageThrows = false } = {}) {
  const values = new Map(saved ? [["aik-theme", saved]] : []);
  const root = { dataset: {}, style: {} };
  const storage = {
    getItem(key) {
      if (storageThrows) throw new Error("storage unavailable");
      return values.get(key) ?? null;
    },
    setItem(key, value) {
      if (storageThrows) throw new Error("storage unavailable");
      values.set(key, value);
    },
  };
  const window = {
    localStorage: storage,
    matchMedia: () => ({ matches: systemDark }),
  };
  vm.runInNewContext(themeSource, { window, document: { documentElement: root } });
  return { root, theme: window.AIKTheme, values };
}

test("saved theme is applied synchronously before app startup", () => {
  const { root, theme } = bootTheme({ saved: "dark", systemDark: false });
  assert.equal(root.dataset.theme, "dark");
  assert.equal(root.style.colorScheme, "dark");
  assert.equal(theme.current(), "dark");
});

test("system preference is used only when no saved preference exists", () => {
  assert.equal(bootTheme({ systemDark: true }).theme.current(), "dark");
  assert.equal(bootTheme({ systemDark: false }).theme.current(), "light");
  assert.equal(bootTheme({ saved: "light", systemDark: true }).theme.current(), "light");
});

test("toggle persists a valid theme and invalid themes are ignored", () => {
  const { theme, values } = bootTheme();
  assert.equal(theme.set("sepia"), false);
  assert.equal(theme.current(), "light");
  assert.equal(theme.toggle(), true);
  assert.equal(theme.current(), "dark");
  assert.equal(values.get("aik-theme"), "dark");
  theme.toggle();
  assert.equal(theme.current(), "light");
  assert.equal(values.get("aik-theme"), "light");
});

test("theme remains usable when browser storage is unavailable", () => {
  const { theme, root } = bootTheme({ storageThrows: true, systemDark: true });
  assert.equal(theme.current(), "dark");
  assert.equal(theme.set("light"), true);
  assert.equal(root.dataset.theme, "light");
});

test("entry point loads theme before the stylesheet to prevent a flash", () => {
  const htmlPath = fileURLToPath(new URL("../index.html", import.meta.url));
  const html = readFileSync(htmlPath, "utf8");
  assert.ok(html.indexOf("/assets/theme.js") < html.indexOf("/assets/styles.css"));
  assert.ok(html.indexOf("/assets/styles.css") < html.indexOf("/assets/app.js"));
});

test("theme control is available only in the authenticated participant portal", () => {
  const app = readFileSync(new URL("../assets/app.js", import.meta.url), "utf8");
  assert.match(app, /function themeToggleMarkup\(\)/);
  assert.match(app, /aria-pressed="\$\{dark\}"/);
  assert.match(app, /data-theme-toggle/);
  assert.match(app, /app\.addEventListener\("click"/);
  assert.equal((app.match(/\$\{themeToggleMarkup\(\)\}/g) ?? []).length, 1);
  assert.match(app, /function shell\([\s\S]*?\$\{themeToggleMarkup\(\)\}/);
  assert.doesNotMatch(app.slice(app.indexOf("function landing("), app.indexOf("function shell(")), /themeToggleMarkup/);
});

test("theme stylesheet defines responsive layouts and reduced-motion behavior", () => {
  const styles = readFileSync(new URL("../assets/styles.css", import.meta.url), "utf8");
  assert.match(styles, /:root\[data-theme="light"\]/);
  assert.match(styles, /:root\[data-theme="dark"\]/);
  assert.match(styles, /@media \(prefers-reduced-motion: reduce\)/);
  assert.match(styles, /@media \(max-width: 480px\)/);
  assert.match(styles, /\.theme-toggle:focus-visible/);
});

test("participant access reflows before three-column minimums exceed tablet widths", () => {
  const styles = readFileSync(new URL("../assets/styles.css", import.meta.url), "utf8");
  assert.match(styles, /@media \(max-width: 1023px\)\s*\{[\s\S]*?\.access-layout\s*\{[\s\S]*?grid-template-columns: minmax\(0,1fr\) minmax\(0,1fr\);[\s\S]*?grid-template-areas: "intro board" "panel panel"/);
  assert.match(styles, /\.access-layout:not\(\.credential-layout\) \.access-panel\s*\{\s*grid-area: panel;\s*width: min\(100%, 620px\);\s*justify-self: center;/);
  assert.match(styles, /@media \(max-width: 560px\)[\s\S]*?\.access-layout\s*\{[^}]*grid-template-columns: minmax\(0,1fr\);[^}]*grid-template-areas: "intro" "panel" "board"/);
});
