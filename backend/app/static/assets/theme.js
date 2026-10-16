(() => {
  const storageKey = "aik-theme";
  const root = document.documentElement;
  const valid = (theme) => theme === "light" || theme === "dark";

  function storedTheme() {
    try {
      const value = window.localStorage.getItem(storageKey);
      return valid(value) ? value : null;
    } catch {
      return null;
    }
  }

  function preferredTheme() {
    try {
      return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    } catch {
      return "light";
    }
  }

  function apply(theme, persist = true) {
    if (!valid(theme)) return false;
    root.dataset.theme = theme;
    root.style.colorScheme = theme;
    const themeColor = document.querySelector?.('meta[name="theme-color"]');
    if (themeColor) themeColor.setAttribute("content", theme === "dark" ? "#101317" : "#f4f3ed");
    if (persist) {
      try { window.localStorage.setItem(storageKey, theme); } catch { /* Preference storage is optional. */ }
    }
    return true;
  }

  apply(storedTheme() || preferredTheme(), false);
  window.AIKTheme = Object.freeze({
    current: () => root.dataset.theme || "light",
    set: (theme) => apply(theme),
    toggle: () => apply(root.dataset.theme === "dark" ? "light" : "dark"),
  });
})();
