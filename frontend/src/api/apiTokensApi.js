import { request } from "./client";

// ---------------------------------------------------------------------------
// API tokens — how a machine authenticates as a person.
//
// A token is shown exactly once, at creation. Only its prefix is stored, so
// there is no endpoint that can return it again and no way for this file to
// offer one.
// ---------------------------------------------------------------------------

export const listApiTokens = (query = {}) =>
  request("/api/auth/tokens", { query });

/**
 * Create a token. The response carries the secret ONCE — the caller must show
 * it immediately, because nothing can retrieve it afterwards.
 */
export const createApiToken = (payload) =>
  request("/api/auth/tokens", { method: "POST", body: payload });

export const revokeApiToken = (id) =>
  request(`/api/auth/tokens/${encodeURIComponent(id)}`, { method: "DELETE" });
