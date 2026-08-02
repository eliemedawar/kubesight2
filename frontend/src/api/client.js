import { clearStoredSession, getStoredToken } from "../authStorage";

function resolveDefaultBackendUrl() {
  if (import.meta.env.DEV) {
    return "";
  }
  if (typeof window !== "undefined" && window.location?.protocol !== "file:") {
    return window.location.origin;
  }
  return "http://127.0.0.1:5000";
}

export const getBaseUrl = () => {
  const configured = import.meta.env.VITE_API_BASE_URL || window.APP_CONFIG?.backendUrl;
  if (configured !== undefined && configured !== null && String(configured).trim() !== "") {
    return String(configured).replace(/\/$/, "");
  }
  return resolveDefaultBackendUrl();
};

const toQueryString = (query = {}) => {
  const params = new URLSearchParams();
  Object.entries(query).forEach(([key, value]) => {
    if (value === undefined || value === null || value === "") {
      return;
    }
    params.append(key, String(value));
  });
  const encoded = params.toString();
  return encoded ? `?${encoded}` : "";
};

let onUnauthorized = null;

export const setUnauthorizedHandler = (handler) => {
  onUnauthorized = handler;
};

/**
 * Session handling, contract 4.
 *
 * The target model is a cookie session with a rotating refresh token and CSRF
 * on every mutation. A3 lands the server half in three steps and this is the
 * middle one, so everything here is built to be inert until the endpoints
 * exist rather than to wait for them:
 *
 *   `credentials: "include"` is sent now. Harmless while there are no cookies.
 *   The CSRF token is fetched lazily and its absence tolerated, so a 404 from
 *   an endpoint A3 has not written yet costs one request and changes nothing.
 *   A 401 attempts one refresh; if that endpoint 404s the refresh simply fails
 *   and we log out, which is exactly today's behaviour.
 */
const AUTH_PATHS = {
  csrf: "/api/auth/csrf",
  refresh: "/api/auth/refresh",
};

let csrfToken = "";
let csrfFetch = null;
let refreshFetch = null;

/** Reset between tests, and on logout so a stale token is never re-sent. */
export function resetSessionState() {
  csrfToken = "";
  csrfFetch = null;
  refreshFetch = null;
}

async function getCsrfToken() {
  if (csrfToken) {
    return csrfToken;
  }
  // One in-flight fetch shared by concurrent mutations, so a burst of saves
  // does not race for the same token.
  if (!csrfFetch) {
    csrfFetch = fetch(`${getBaseUrl()}${AUTH_PATHS.csrf}`, { credentials: "include" })
      .then((response) => (response.ok ? response.json() : null))
      .then((payload) => {
        csrfToken = payload?.data?.token || payload?.data?.csrfToken || "";
        return csrfToken;
      })
      .catch(() => "")
      .finally(() => {
        csrfFetch = null;
      });
  }
  return csrfFetch;
}

/**
 * One refresh attempt, shared by every request that got a 401 at the same time.
 *
 * Resolves true only when the server actually rotated the session. Anything
 * else — a 404 because the endpoint does not exist yet, a network failure, a
 * refused refresh — is false, and the caller logs out.
 */
async function refreshSession() {
  if (!refreshFetch) {
    refreshFetch = fetch(`${getBaseUrl()}${AUTH_PATHS.refresh}`, {
      method: "POST",
      credentials: "include",
      headers: csrfToken ? { "X-CSRF-Token": csrfToken } : undefined,
    })
      .then((response) => {
        if (!response.ok) {
          return false;
        }
        // The rotation issues a new CSRF token with the new session.
        csrfToken = "";
        return true;
      })
      .catch(() => false)
      .finally(() => {
        refreshFetch = null;
      });
  }
  return refreshFetch;
}

// Concurrent identical GETs (e.g. two components loading the same list at
// mount) share one network request instead of hitting the backend twice.
const inflightGets = new Map();

export async function request(path, { method = "GET", body, query, auth = true, authToken } = {}) {
  if (method === "GET") {
    const key = `${path}${toQueryString(query)}|${auth ? authToken || getStoredToken() || "" : ""}`;
    const pending = inflightGets.get(key);
    if (pending) {
      return pending;
    }
    const promise = performRequest(path, { method, body, query, auth, authToken }).finally(() => {
      if (inflightGets.get(key) === promise) {
        inflightGets.delete(key);
      }
    });
    inflightGets.set(key, promise);
    return promise;
  }
  return performRequest(path, { method, body, query, auth, authToken });
}

async function performRequest(
  path,
  { method = "GET", body, query, auth = true, authToken } = {},
  { allowRefresh = true } = {}
) {
  const url = `${getBaseUrl()}${path}${toQueryString(query)}`;
  const headers = { "Content-Type": "application/json" };
  if (auth) {
    // An explicit `authToken` (interim onboarding / MFA token) overrides the
    // stored session token; those flows run before a session exists.
    const token = authToken || getStoredToken();
    if (token) {
      headers.Authorization = `Bearer ${token}`;
    }
  }

  // CSRF applies to cookie-authenticated mutations. Skipped when a token is in
  // play, and skipped entirely if the endpoint has not been written yet.
  if (auth && method !== "GET") {
    const csrf = await getCsrfToken();
    if (csrf) {
      headers["X-CSRF-Token"] = csrf;
    }
  }

  const response = await fetch(url, {
    method,
    headers,
    credentials: "include",
    body: body ? JSON.stringify(body) : undefined,
  });

  const payload = await response.json().catch(() => ({}));

  if (response.status === 401 && auth) {
    // Refresh once, then retry once. `allowRefresh` is false on the retry, so
    // a second 401 falls through to logout instead of looping.
    if (allowRefresh) {
      const refreshed = await refreshSession();
      if (refreshed) {
        return performRequest(path, { method, body, query, auth, authToken }, { allowRefresh: false });
      }
    }
    clearStoredSession();
    resetSessionState();
    if (onUnauthorized) {
      onUnauthorized();
    }
    throw new Error(payload.error || "Session expired. Please sign in again.");
  }

  if (!response.ok) {
    const message = payload.error || payload.message || `Request failed (${response.status})`;
    const error =
      response.status === 403 || message === "Forbidden"
        ? new Error("You do not have access to this resource.")
        : new Error(message);
    // Expose the HTTP status so callers can react to specific codes (e.g. a 404
    // for a cluster that was deleted out from under a stale tab), and any
    // structured detail payload (e.g. account-lock kind + retry seconds).
    error.status = response.status;
    error.data = payload.data ?? null;
    throw error;
  }

  if (typeof payload.success === "boolean") {
    if (!payload.success) {
      throw new Error(payload.error || "Request failed");
    }
    return payload.data;
  }

  return payload;
}

function parseSseEvent(raw) {
  let event = "message";
  const dataLines = [];
  for (const line of raw.split("\n")) {
    if (!line || line.startsWith(":")) {
      continue; // comment / heartbeat
    }
    if (line.startsWith("event:")) {
      event = line.slice(6).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).replace(/^ /, ""));
    }
  }
  if (!dataLines.length) {
    return null;
  }
  let data = dataLines.join("\n");
  try {
    data = JSON.parse(data);
  } catch {
    /* leave as raw string */
  }
  return { event, data };
}

/**
 * Open a Server-Sent Events stream using fetch (so the Bearer token can be
 * sent as a header — EventSource cannot set headers). Calls `onEvent({ event,
 * data })` for each frame. Pass an AbortSignal to stop the stream.
 */
export async function streamSse(path, { query, signal, onEvent } = {}) {
  const url = `${getBaseUrl()}${path}${toQueryString(query)}`;
  const headers = { Accept: "text/event-stream" };
  const token = getStoredToken();
  if (token) {
    headers.Authorization = `Bearer ${token}`;
  }

  const response = await fetch(url, { headers, signal });
  if (response.status === 401) {
    clearStoredSession();
    if (onUnauthorized) {
      onUnauthorized();
    }
    throw new Error("Session expired. Please sign in again.");
  }
  if (!response.ok || !response.body) {
    let message = `Request failed (${response.status})`;
    try {
      const payload = await response.json();
      message = payload.error || payload.message || message;
    } catch {
      /* non-JSON error body */
    }
    const error =
      response.status === 403 || message === "Forbidden"
        ? new Error("You do not have access to this resource.")
        : new Error(message);
    error.status = response.status;
    throw error;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) {
        break;
      }
      buffer += decoder.decode(value, { stream: true });
      let boundary;
      while ((boundary = buffer.indexOf("\n\n")) !== -1) {
        const rawEvent = buffer.slice(0, boundary);
        buffer = buffer.slice(boundary + 2);
        const parsed = parseSseEvent(rawEvent);
        if (parsed && onEvent) {
          onEvent(parsed);
        }
      }
    }
  } finally {
    try {
      await reader.cancel();
    } catch {
      /* already closed */
    }
  }
}
