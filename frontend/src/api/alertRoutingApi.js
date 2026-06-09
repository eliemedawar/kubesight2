import { request } from "./client";

export const getSmtpSettings = () => request("/api/alert-routing/smtp");

export const saveSmtpSettings = (payload) =>
  request("/api/alert-routing/smtp", { method: "POST", body: payload });

export const testSmtpSettings = (recipient) =>
  request("/api/alert-routing/smtp/test", {
    method: "POST",
    body: recipient ? { recipient } : {},
  });

export const listReceivers = () => request("/api/alert-routing/receivers");

export const createReceiver = (payload) =>
  request("/api/alert-routing/receivers", { method: "POST", body: payload });

export const updateReceiver = (id, payload) =>
  request(`/api/alert-routing/receivers/${id}`, { method: "PUT", body: payload });

export const deleteReceiver = (id) =>
  request(`/api/alert-routing/receivers/${id}`, { method: "DELETE" });

export const testReceiver = (id) =>
  request(`/api/alert-routing/receivers/${id}/test`, { method: "POST" });

export const listDeliveryLogs = (limit = 100) =>
  request("/api/alert-routing/delivery-logs", { query: { limit } });
