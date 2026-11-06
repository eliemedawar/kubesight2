import { request } from "./client";

// The in-app assistant (Ctrl+G, admins only): chat with the Hermes that has the
// kubesight skill. Sending returns at once with a pending reply; poll the
// conversation until `pending` is false.

export const getAssistantStatus = () => request("/api/assistant/status");

export const listAssistantConversations = () => request("/api/assistant/conversations");

export const getAssistantConversation = (id) =>
  request(`/api/assistant/conversations/${encodeURIComponent(id)}`);

export const startAssistantConversation = (message, context) =>
  request("/api/assistant/conversations", { method: "POST", body: { message, context } });

export const sendAssistantMessage = (id, message, context) =>
  request(`/api/assistant/conversations/${encodeURIComponent(id)}/messages`, {
    method: "POST",
    body: { message, context },
  });

export const deleteAssistantConversation = (id) =>
  request(`/api/assistant/conversations/${encodeURIComponent(id)}`, { method: "DELETE" });
