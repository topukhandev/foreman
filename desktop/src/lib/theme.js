// Light, dark or the system's choice: an app preference kept in this browser profile (not Foreman's config), applied
// as data-theme on <html> so app.css picks the matching tokens.

const KEY = "foreman.theme";
export const THEMES = [["system", "System"], ["light", "Light"], ["dark", "Dark"]];

export function savedTheme() {
  try {
    const t = localStorage.getItem(KEY);
    return THEMES.some(([v]) => v === t) ? t : "system";
  } catch {
    return "system";
  }
}

export function applyTheme(theme) {
  const root = document.documentElement;
  if (theme === "light" || theme === "dark") root.setAttribute("data-theme", theme);
  else root.removeAttribute("data-theme");
  try {
    localStorage.setItem(KEY, theme);
  } catch {}
}

/** The theme in effect now (light or dark). */
export function effectiveTheme(theme) {
  if (theme === "light" || theme === "dark") return theme;
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}
