import { request } from "./client";

export const listUsers = () => request("/api/users");
export const getUser = (id) => request(`/api/users/${id}`);
export const createUser = (payload) => request("/api/users", { method: "POST", body: payload });
export const updateUser = (id, payload) =>
  request(`/api/users/${id}`, { method: "PUT", body: payload });
export const disableUser = (id) => request(`/api/users/${id}`, { method: "DELETE" });
export const deleteUser = (id) =>
  request(`/api/users/${id}/permanent`, { method: "DELETE" });

export const listUserAccessRules = (userId) =>
  request(`/api/users/${userId}/access-rules`);

export const replaceUserAccessRules = (userId, accessRules) =>
  request(`/api/users/${userId}/access-rules`, {
    method: "PUT",
    body: { accessRules },
  });

export const listRoles = () => request("/api/roles");
export const getRole = (id) => request(`/api/roles/${id}`);
export const createRole = (payload) => request("/api/roles", { method: "POST", body: payload });
export const updateRole = (id, payload) =>
  request(`/api/roles/${id}`, { method: "PUT", body: payload });
export const deleteRole = (id) => request(`/api/roles/${id}`, { method: "DELETE" });
export const listPermissions = () => request("/api/permissions");
export const updateRolePermissions = (roleId, permissions) =>
  request(`/api/roles/${roleId}/permissions`, { method: "PUT", body: { permissions } });

export const listAuditLogs = (query = {}) => request("/api/audit-logs", { query });
