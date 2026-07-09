import { getBaseUrl, request } from "./client";
import { getStoredToken } from "../authStorage";

export const listUsers = () => request("/api/users");
export const getUser = (id) => request(`/api/users/${id}`);
export const createUser = (payload) => request("/api/users", { method: "POST", body: payload });
export const updateUser = (id, payload) =>
  request(`/api/users/${id}`, { method: "PUT", body: payload });
export const disableUser = (id) => request(`/api/users/${id}`, { method: "DELETE" });
export const deleteUser = (id) =>
  request(`/api/users/${id}/permanent`, { method: "DELETE" });

export const resendTemporaryPassword = (id) =>
  request(`/api/users/${id}/resend-temporary-password`, { method: "POST" });

export const resetUserMfa = (id) =>
  request(`/api/users/${id}/reset-mfa`, { method: "POST" });

export const unlockUser = (id) => request(`/api/users/${id}/unlock`, { method: "POST" });

export const resetFailedAttempts = (id) =>
  request(`/api/users/${id}/reset-failed-attempts`, { method: "POST" });

export const forcePasswordReset = (id) =>
  request(`/api/users/${id}/force-password-reset`, { method: "POST" });

export const lockUser = (id) => request(`/api/users/${id}/lock`, { method: "POST" });

export const enableUser = (id) => request(`/api/users/${id}/enable`, { method: "POST" });

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

// Downloads a filtered CSV of audit entries. Uses a raw fetch (not `request`,
// which parses JSON) so the response comes back as a Blob, and carries the
// bearer token since the download isn't a plain navigation.
export const exportAuditLogs = async (query = {}) => {
  const params = new URLSearchParams();
  Object.entries(query).forEach(([key, value]) => {
    if (value !== undefined && value !== null && String(value) !== "") {
      params.append(key, String(value));
    }
  });
  const qs = params.toString();
  const token = getStoredToken();
  const response = await fetch(
    `${getBaseUrl()}/api/audit-logs/export${qs ? `?${qs}` : ""}`,
    { headers: token ? { Authorization: `Bearer ${token}` } : {} }
  );
  if (!response.ok) {
    let message = `Export failed (${response.status})`;
    try {
      const payload = await response.json();
      message = payload.error || payload.message || message;
    } catch {
      /* non-JSON error body */
    }
    throw new Error(message);
  }
  return response.blob();
};
