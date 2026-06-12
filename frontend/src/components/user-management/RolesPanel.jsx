import { useMemo, useState } from "react";
import { createRole, deleteRole, updateRole } from "../../api";
import AccessDeniedPage from "../auth/AccessDenied.jsx";
import LoadingState from "../common/LoadingState.jsx";
import { isAccessDeniedError } from "../../utils/authz.js";
import { permissionSummary } from "../../lib/permissionCatalog";
import RoleFormModal from "./RoleFormModal";

export default function RolesPanel({
  roles,
  loading,
  error,
  canManage,
  onRolesChanged,
  onError,
}) {
  const [search, setSearch] = useState("");
  const [modalOpen, setModalOpen] = useState(false);
  const [modalMode, setModalMode] = useState("edit");
  const [editingRole, setEditingRole] = useState(null);
  const [saving, setSaving] = useState(false);

  const filteredRoles = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) {
      return roles;
    }
    return roles.filter((role) => {
      const haystack = [role.name, role.description, ...(role.permissions || [])]
        .join(" ")
        .toLowerCase();
      return haystack.includes(q);
    });
  }, [roles, search]);

  const openCreate = () => {
    setEditingRole(null);
    setModalMode("edit");
    setModalOpen(true);
  };

  const openView = (role) => {
    setEditingRole(role);
    setModalMode("view");
    setModalOpen(true);
  };

  const openEdit = (role) => {
    setEditingRole(role);
    setModalMode("edit");
    setModalOpen(true);
  };

  const handleDelete = async (role) => {
    const userCount = role.userCount || 0;
    if (userCount > 0) {
      onError(`Cannot delete "${role.name}" while ${userCount} user(s) are assigned.`);
      return;
    }
    const confirmed = window.confirm(
      `Delete role "${role.name}"?\n\nThis cannot be undone.`
    );
    if (!confirmed) {
      return;
    }
    onError("");
    try {
      await deleteRole(role.id);
      await onRolesChanged();
    } catch (err) {
      onError(err.message);
    }
  };

  const handleSave = async (payload) => {
    setSaving(true);
    onError("");
    try {
      if (editingRole) {
        await updateRole(editingRole.id, payload);
      } else {
        await createRole(payload);
      }
      setModalOpen(false);
      setEditingRole(null);
      await onRolesChanged();
    } catch (err) {
      throw err;
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <div className="card-header-row">
        <div>
          <h2>Roles</h2>
          <p className="muted">
            Create custom roles and control what each role can do across the platform.
          </p>
        </div>
        {canManage ? (
          <button type="button" className="primary" onClick={openCreate}>
            Create Role
          </button>
        ) : null}
      </div>

      {loading ? (
        <LoadingState label="Loading roles…" />
      ) : isAccessDeniedError(error) ? (
        <AccessDeniedPage message={error} />
      ) : (
        <>
          {error ? <p className="banner-message error">{error}</p> : null}

          <div className="user-filters">
            <label className="user-filters__search">
              Search
              <input
                type="search"
                placeholder="Role name, description, or permission"
                value={search}
                onChange={(e) => setSearch(e.target.value)}
              />
            </label>
          </div>

          <p className="muted user-table-meta">
            Showing {filteredRoles.length} of {roles.length} roles
          </p>
          <div className="table-wrap">
            <table className="data-table data-table--roles">
              <thead>
                <tr>
                  <th>Role</th>
                  <th>Description</th>
                  <th>Users</th>
                  <th>Permissions</th>
                  <th>Actions</th>
                </tr>
              </thead>
              <tbody>
                {filteredRoles.map((role) => (
                  <tr key={role.id}>
                    <td>
                      <span className="role-badge">{role.name}</span>
                      {role.isSystemRole ? (
                        <span className="status-pill muted role-system-pill">System</span>
                      ) : null}
                    </td>
                    <td>{role.description || "—"}</td>
                    <td>{role.userCount ?? 0}</td>
                    <td className="role-permissions-summary">
                      {permissionSummary(role.permissions)}
                    </td>
                    <td className="table-actions-cell">
                      <div className="table-actions">
                        <button type="button" className="btn-outline btn-compact" onClick={() => openView(role)}>
                          View
                        </button>
                        {canManage ? (
                          <>
                            <button type="button" className="btn-outline btn-compact" onClick={() => openEdit(role)}>
                              Edit
                            </button>
                            <button
                              type="button"
                              className="btn-outline btn-compact danger"
                              onClick={() => handleDelete(role)}
                              disabled={role.isSystemRole || (role.userCount || 0) > 0}
                              title={
                                role.isSystemRole
                                  ? "System roles cannot be deleted"
                                  : (role.userCount || 0) > 0
                                    ? "Reassign users before deleting this role"
                                    : undefined
                              }
                            >
                              Delete
                            </button>
                          </>
                        ) : null}
                      </div>
                    </td>
                  </tr>
                ))}
                {!filteredRoles.length ? (
                  <tr>
                    <td colSpan={5} className="muted">
                      No roles match the current filters.
                    </td>
                  </tr>
                ) : null}
              </tbody>
            </table>
          </div>
        </>
      )}

      <RoleFormModal
        open={modalOpen}
        mode={modalMode}
        role={editingRole}
        onClose={() => {
          setModalOpen(false);
          setEditingRole(null);
        }}
        onSave={handleSave}
        saving={saving}
      />
    </>
  );
}
