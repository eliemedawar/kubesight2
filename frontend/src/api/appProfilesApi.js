import { request } from "./client";

// App profiles: the applications a ticket may ask for (an Inventory Helm chart,
// its version and values, the namespace and release). Hermes picks them by
// name and never sees the values.

export const listAppProfiles = () => request("/api/app-profiles");

export const createAppProfile = (payload) =>
  request("/api/app-profiles", { method: "POST", body: payload });

export const updateAppProfile = (id, payload) =>
  request(`/api/app-profiles/${id}`, { method: "PUT", body: payload });

export const deleteAppProfile = (id) =>
  request(`/api/app-profiles/${id}`, { method: "DELETE" });
