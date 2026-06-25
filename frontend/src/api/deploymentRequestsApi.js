import { request } from "./client";

export const createDeploymentRequest = (payload) =>
  request("/api/deployment-requests", { method: "POST", body: payload });

export const listDeploymentRequests = (query = {}) =>
  request("/api/deployment-requests", { query });

export const listMyDeploymentRequests = (query = {}) =>
  request("/api/deployment-requests/mine", { query });

export const approveDeploymentRequest = (requestId) =>
  request(`/api/deployment-requests/${encodeURIComponent(requestId)}/approve`, {
    method: "POST",
  });

export const declineDeploymentRequest = (requestId) =>
  request(`/api/deployment-requests/${encodeURIComponent(requestId)}/decline`, {
    method: "POST",
  });

export const getClusterDeployEligibility = (clusterId) =>
  request("/api/deployment-requests/eligibility", { query: { clusterId } });

export const getDeploymentRequestRecipients = () =>
  request("/api/deployment-requests/recipients");

export const updateDeploymentRequestRecipients = (payload) =>
  request("/api/deployment-requests/recipients", {
    method: "PUT",
    body: {
      ...payload,
      clusterApprovals: payload.clusterApprovals,
    },
  });
