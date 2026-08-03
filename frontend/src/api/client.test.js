import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../authStorage", () => ({
  getStoredToken: () => "stored-token",
  clearStoredSession: vi.fn(),
}));

const { request, setUnauthorizedHandler, resetSessionState } = await import("./client.js");

/**
 * The 401 → refresh → retry path, contract 4.
 *
 * The failure mode worth designing against: a non-auth failure *during* the
 * retry must not be reported as an auth failure. On the one path where a silent
 * failure is indistinguishable from a working session, "something went wrong"
 * must not arrive dressed as "your session expired" or "you lack permission".
 */

const jsonResponse = (status, payload = {}) => ({
  ok: status >= 200 && status < 300,
  status,
  json: async () => payload,
});

const envelope = (data) => ({ success: true, data, error: null });

let fetchMock;

beforeEach(() => {
  resetSessionState();
  setUnauthorizedHandler(null);
  fetchMock = vi.fn();
  vi.stubGlobal("fetch", fetchMock);
  vi.stubGlobal("window", { location: { protocol: "http:", origin: "http://x" }, APP_CONFIG: {} });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
});

const urlOf = (call) => String(call[0]);
const calls = () => fetchMock.mock.calls.map(urlOf);

describe("a failure during the retry keeps its own identity", () => {
  // The case the flag was about. If this ever reports a permission problem, an
  // operator goes looking at their access instead of at the 500.
  it("surfaces a 500 as a 500, not as a session or permission message", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse(401, { error: "expired" }))
      .mockResolvedValueOnce(jsonResponse(200, {}))
      .mockResolvedValueOnce(jsonResponse(500, { error: "Database unavailable" }));

    const error = await request("/api/clusters").catch((e) => e);

    expect(error.status).toBe(500);
    expect(error.message).toBe("Database unavailable");
    expect(error.message).not.toMatch(/session|sign in|access/i);
  });

  it("surfaces a 404 during the retry as a 404", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse(401, {}))
      .mockResolvedValueOnce(jsonResponse(200, {}))
      .mockResolvedValueOnce(jsonResponse(404, { error: "Unknown cluster." }));

    const error = await request("/api/clusters/gone").catch((e) => e);
    expect(error.status).toBe(404);
  });

  // A 500 mid-retry is not a reason to sign the operator out.
  it("does not log out when the retry fails for a non-auth reason", async () => {
    const onUnauthorized = vi.fn();
    setUnauthorizedHandler(onUnauthorized);
    fetchMock
      .mockResolvedValueOnce(jsonResponse(401, {}))
      .mockResolvedValueOnce(jsonResponse(200, {}))
      .mockResolvedValueOnce(jsonResponse(500, { error: "boom" }));

    await request("/api/clusters").catch(() => {});
    expect(onUnauthorized).not.toHaveBeenCalled();
  });
});

describe("the refresh itself", () => {
  it("retries once after a successful refresh and returns the data", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse(401, {}))
      .mockResolvedValueOnce(jsonResponse(200, {}))
      .mockResolvedValueOnce(jsonResponse(200, envelope({ items: [1] })));

    await expect(request("/api/clusters")).resolves.toEqual({ items: [1] });
    expect(calls()[1]).toContain("/api/auth/refresh");
  });

  // Contract 4: a second 401 means log out. Without the guard this recurses.
  it("logs out on a second 401 rather than refreshing again", async () => {
    const onUnauthorized = vi.fn();
    setUnauthorizedHandler(onUnauthorized);
    fetchMock
      .mockResolvedValueOnce(jsonResponse(401, {}))
      .mockResolvedValueOnce(jsonResponse(200, {}))
      .mockResolvedValueOnce(jsonResponse(401, {}));

    await request("/api/clusters").catch(() => {});

    expect(onUnauthorized).toHaveBeenCalledTimes(1);
    expect(calls().filter((u) => u.includes("/api/auth/refresh"))).toHaveLength(1);
  });

  it("logs out without retrying when the refresh is refused", async () => {
    const onUnauthorized = vi.fn();
    setUnauthorizedHandler(onUnauthorized);
    fetchMock
      .mockResolvedValueOnce(jsonResponse(401, {}))
      .mockResolvedValueOnce(jsonResponse(401, {}));

    await request("/api/clusters").catch(() => {});

    expect(onUnauthorized).toHaveBeenCalledTimes(1);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  // A3 lands the server half in three steps; this is the middle one. Until the
  // endpoint exists a 404 must degrade to today's behaviour, not to a loop.
  it("behaves like today when the refresh endpoint does not exist yet", async () => {
    const onUnauthorized = vi.fn();
    setUnauthorizedHandler(onUnauthorized);
    fetchMock
      .mockResolvedValueOnce(jsonResponse(401, {}))
      .mockResolvedValueOnce(jsonResponse(404, {}));

    const error = await request("/api/clusters").catch((e) => e);

    expect(onUnauthorized).toHaveBeenCalledTimes(1);
    expect(error.message).toMatch(/sign in/i);
  });

  it("does not refresh for an unauthenticated request", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(401, {}));
    await request("/api/auth/login", { method: "POST", auth: false }).catch(() => {});
    expect(calls().some((u) => u.includes("/api/auth/refresh"))).toBe(false);
  });
});

describe("cookies and CSRF", () => {
  it("sends credentials on every request", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, envelope({})));
    await request("/api/clusters");
    expect(fetchMock.mock.calls[0][1].credentials).toBe("include");
  });

  it("sends the CSRF token on a mutation", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse(200, { data: { token: "csrf-abc" } }))
      .mockResolvedValueOnce(jsonResponse(200, envelope({})));

    await request("/api/clusters", { method: "POST", body: {} });

    const mutation = fetchMock.mock.calls[1][1];
    expect(mutation.headers["X-CSRF-Token"]).toBe("csrf-abc");
  });

  it("does not fetch a CSRF token for a read", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(200, envelope({})));
    await request("/api/clusters");
    expect(calls().some((u) => u.includes("/api/auth/csrf"))).toBe(false);
  });

  // Inert until A3 lands the endpoint: a 404 costs one request and changes
  // nothing, rather than blocking every mutation in the app.
  it("proceeds without the header when the endpoint does not exist yet", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse(404, {}))
      .mockResolvedValueOnce(jsonResponse(200, envelope({ ok: true })));

    await expect(request("/api/clusters", { method: "POST", body: {} })).resolves.toEqual({
      ok: true,
    });
    expect(fetchMock.mock.calls[1][1].headers["X-CSRF-Token"]).toBeUndefined();
  });

  it("fetches the token once and reuses it", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse(200, { data: { token: "csrf-abc" } }))
      .mockResolvedValueOnce(jsonResponse(200, envelope({})))
      .mockResolvedValueOnce(jsonResponse(200, envelope({})));

    await request("/api/a", { method: "POST", body: {} });
    await request("/api/b", { method: "POST", body: {} });

    expect(calls().filter((u) => u.includes("/api/auth/csrf"))).toHaveLength(1);
  });
});

describe("errors that are not 401", () => {
  it("keeps a 403 distinguishable by status", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(403, { error: "Forbidden" }));
    const error = await request("/api/clusters").catch((e) => e);
    expect(error.status).toBe(403);
  });

  it("does not attempt a refresh for a 403", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse(403, {}));
    await request("/api/clusters").catch(() => {});
    expect(calls().some((u) => u.includes("/api/auth/refresh"))).toBe(false);
  });
});
