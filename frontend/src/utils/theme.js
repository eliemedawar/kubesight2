// Theme application. The select in Settings stores "system" | "light" | "dark";
// "system" is resolved against the OS preference. We always set a concrete
// `data-theme` of "light" or "dark" on <html> so the CSS only needs one override.

export const THEME_STORAGE_KEY = "kubesight-theme";

export function resolveTheme(theme) {
  if (theme === "light" || theme === "dark") return theme;
  if (typeof window !== "undefined" && window.matchMedia) {
    // Light-first: only an explicit OS dark preference yields dark.
    return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
  }
  return "light";
}

export function applyTheme(theme) {
  const resolved = resolveTheme(theme);
  const root = document.documentElement;
  root.setAttribute("data-theme", resolved);
  root.style.colorScheme = resolved;
  return resolved;
}

export function storeThemePreference(theme) {
  try {
    localStorage.setItem(THEME_STORAGE_KEY, theme);
  } catch {
    /* storage unavailable — ignore */
  }
}

export function readThemePreference() {
  try {
    return localStorage.getItem(THEME_STORAGE_KEY) || "system";
  } catch {
    return "system";
  }
}
