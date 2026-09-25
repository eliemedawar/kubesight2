import { request } from "./client";

// The Hermes ticket agent: Hermes handles each inbound ticket through
// KubeSight's MCP tools. These endpoints are the operator side — configure it,
// approve or reject what Hermes asked for, hand a ticket back to it. Not bound
// to a provider: the agent serves Zoho and Jira alike.

export const getTicketAgentSettings = () => request("/api/ticket-agent/settings");

export const updateTicketAgentSettings = (payload) =>
  request("/api/ticket-agent/settings", { method: "PUT", body: payload });

export const testTicketAgentTelegram = () =>
  request("/api/ticket-agent/telegram/test", { method: "POST" });

export const approveAgentTask = (taskId) =>
  request(`/api/ticket-agent/tasks/${encodeURIComponent(taskId)}/approve`, { method: "POST" });

export const rejectAgentTask = (taskId, note = "") =>
  request(`/api/ticket-agent/tasks/${encodeURIComponent(taskId)}/reject`, {
    method: "POST",
    body: { note },
  });

export const handleTicketAgain = (ticketRecordId) =>
  request(`/api/ticket-agent/tickets/${encodeURIComponent(ticketRecordId)}/handle`, {
    method: "POST",
  });
