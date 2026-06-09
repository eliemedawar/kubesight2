import { request } from "./client";

export const getDashboardSummary = (clusterId) =>
  request(`/api/dashboard/summary?clusterId=${encodeURIComponent(clusterId)}`);
