import { request } from "./client";

export const getAlertPolicyCatalog = () => request("/api/alert-policies/catalog");

export const listAlertPolicies = (params = {}) => {
  const query = new URLSearchParams();
  if (params.cluster) query.set("cluster", params.cluster);
  const suffix = query.toString() ? `?${query}` : "";
  return request(`/api/alert-policies${suffix}`);
};

export const getAlertPolicy = (policyId) => request(`/api/alert-policies/${policyId}`);

export const createAlertPolicy = (payload) =>
  request("/api/alert-policies", { method: "POST", body: payload });

export const updateAlertPolicy = (policyId, payload) =>
  request(`/api/alert-policies/${policyId}`, { method: "PUT", body: payload });

export const deleteAlertPolicy = (policyId) =>
  request(`/api/alert-policies/${policyId}`, { method: "DELETE" });

export const setAlertPolicyEnabled = (policyId, enabled) =>
  request(`/api/alert-policies/${policyId}/status`, {
    method: "PATCH",
    body: { enabled },
  });

export const listAlertHistory = (params = {}) => {
  const query = new URLSearchParams();
  if (params.cluster) query.set("cluster", params.cluster);
  if (params.status) query.set("status", params.status);
  if (params.limit) query.set("limit", String(params.limit));
  const suffix = query.toString() ? `?${query}` : "";
  return request(`/api/alert-policies/history${suffix}`);
};

export const getAlertPolicyStats = (params = {}) => {
  const query = new URLSearchParams();
  if (params.cluster) query.set("cluster", params.cluster);
  const suffix = query.toString() ? `?${query}` : "";
  return request(`/api/alert-policies/stats${suffix}`);
};

export const evaluateAlertPolicies = (clusterId) =>
  request("/api/alert-policies/evaluate", {
    method: "POST",
    body: { clusterId },
  });
