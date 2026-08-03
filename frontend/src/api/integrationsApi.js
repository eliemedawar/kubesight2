import { request } from "./client.js";

/** The provider-neutral integrations contract — see lib/integrations.js. */

export const listIntegrations = () => request("/api/integrations");

export const getIntegration = (key) => request(`/api/integrations/${key}`);

export const testIntegration = (key) => request(`/api/integrations/${key}/test`, { method: "POST" });

export const setIntegrationEnabled = (key, enabled) =>
  request(`/api/integrations/${key}/enabled`, { method: "PUT", body: { enabled } });

export const listIntegrationActivity = (key, { limit = 50 } = {}) =>
  request(`/api/integrations/${key}/activity`, { query: { limit } });
