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
    if (response.status === 403 || message === "Forbidden") {
      throw new Error("You do not have access to this resource.");
    }
    throw new Error(message);
  }

  if (typeof payload.success === "boolean") {
    if (!payload.success) {
      throw new Error(payload.error || "Request failed");
    }
    return payload.data;
  }

  return payload;
}
