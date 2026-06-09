import { request } from "./client";

export const listAlerts = (query = {}) => request("/api/alerts", { query });

export const testAlertEmail = () =>
  request("/api/alerts/notifications/email/test", { method: "POST" });
