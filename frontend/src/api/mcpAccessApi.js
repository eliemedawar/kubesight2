import { request } from "./client";

/**
 * MCP access — which of KubeSight's tools an agent may call at all.
 *
 * The switches are a ceiling over every token: a tool that is on still needs
 * the permission of whoever's token calls it. Saving sends the whole list of
 * tools that are OFF, never a diff, so two people saving at once cannot leave
 * half of each other's change behind.
 */

export const getMcpAccess = () => request("/api/mcp/access");

export const updateMcpAccess = (payload) =>
  request("/api/mcp/access", { method: "PUT", body: payload });
