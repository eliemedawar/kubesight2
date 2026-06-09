/** Load optional public/config.js (or Flask /config.js) before the app starts. */
export async function loadRuntimeConfig() {
  if (typeof window === "undefined") {
    return;
  }

  window.APP_CONFIG = window.APP_CONFIG || { backendUrl: "" };

  if (window.APP_CONFIG.__loaded) {
    return;
  }

  try {
    const configUrl = new URL("config.js", import.meta.env.BASE_URL).href;
    const response = await fetch(configUrl, { cache: "no-store" });
    if (!response.ok) {
      return;
    }
    const script = await response.text();
    // config.js only assigns window.APP_CONFIG (trusted same-origin file).
    new Function(script)();
  } catch {
    // Keep defaults when config.js is missing (e.g. dev without public copy).
  } finally {
    window.APP_CONFIG = window.APP_CONFIG || { backendUrl: "" };
    window.APP_CONFIG.__loaded = true;
  }
}
