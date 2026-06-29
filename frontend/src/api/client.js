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

export async function request(path, { method = "GET", body, query, auth = true } = {}) {
  const url = `${getBaseUrl()}${path}${toQueryString(query)}`;
  const headers = { "Content-Type": "application/json" };
  if (auth) {
    const token = getStoredToken();
    if (token) {
      headers.Authorization = `Bearer ${token}`;
    }
  }

  const response = await fetch(url, {
    method,
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });

  const payload = await response.json().catch(() => ({}));
  if (response.status === 401 && auth) {
    clearStoredSession();
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
    // for a cluster that was deleted out from under a stale tab).
    error.status = response.status;
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
