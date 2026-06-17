import { lazy, Suspense, useEffect, useMemo, useState } from "react";
import {
  createUser,
  disableUser,
  getUser,
  listRoles,
  listUsers,
  updateUser,
} from "../api";
import { useAuth } from "../context/AuthContext";
import AccessDeniedPage from "../components/auth/AccessDenied.jsx";
import ErrorBanner from "../components/common/ErrorBanner.jsx";
import LoadingState from "../components/common/LoadingState.jsx";
import { formatAccessError, isAccessDeniedError } from "../utils/authz.js";
import RolesPanel from "../components/user-management/RolesPanel";
import { isFullAccessRole } from "../lib/rolePresets";

const UserFormModal = lazy(() => import("../components/user-management/UserFormModal.jsx"));

export default function UserManagementPage({ clusters = [] }) {
  const { user: currentUser, hasPermission } = useAuth();
  const canCreate = hasPermission("users:create") || hasPermission("users:manage");
  const canUpdate = hasPermission("users:update") || hasPermission("users:manage");
  const canDisable = hasPermission("users:disable") || hasPermission("users:manage");
  const readOnlyUsers =
    hasPermission("users:view") && !canCreate && !canUpdate && !canDisable;
  const canViewRoles = hasPermission("roles:view");
  const canManageRoles = hasPermission("roles:manage") || hasPermission("users:manage");
  const [activeTab, setActiveTab] = useState("users");
  const [users, setUsers] = useState([]);
  const [roles, setRoles] = useState([]);
  const [loading, setLoading] = useState(true);
  const [rolesLoading, setRolesLoading] = useState(true);
  const [error, setError] = useState("");
  const [rolesError, setRolesError] = useState("");
  const [modalOpen, setModalOpen] = useState(false);
  const [editingUser, setEditingUser] = useState(null);
  const [saving, setSaving] = useState(false);

  const [search, setSearch] = useState("");
  const [roleFilter, setRoleFilter] = useState("all");
  const [statusFilter, setStatusFilter] = useState("all");
  const [clusterFilter, setClusterFilter] = useState("all");

  const loadUsers = async () => {
    setLoading(true);
    setError("");
    try {
      const usersRes = await listUsers();
      setUsers(usersRes.items || []);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  };

  const loadRoles = async () => {
    if (!canViewRoles) {
      setRoles([]);
      setRolesLoading(false);
      return;
    }
    setRolesLoading(true);
    setRolesError("");
    try {
      const rolesRes = await listRoles();
      setRoles(rolesRes.items || []);
    } catch (err) {
      setRolesError(err.message);
    } finally {
      setRolesLoading(false);
    }
  };

  const loadData = async () => {
    await Promise.all([loadUsers(), loadRoles()]);
  };

  useEffect(() => {
    loadData();
  }, []);

  const rolesByName = useMemo(
    () => Object.fromEntries(roles.map((role) => [role.name, role])),
    [roles]
  );

  const clusterFilterOptions = useMemo(() => {
    const ids = new Set();
    users.forEach((u) => (u.clusterAccess || []).forEach((id) => ids.add(id)));
    clusters.forEach((c) => ids.add(c.id));
    return Array.from(ids).sort();
  }, [users, clusters]);

  const filteredUsers = useMemo(() => {
    const q = search.trim().toLowerCase();
    return users.filter((user) => {
      if (roleFilter !== "all" && user.role !== roleFilter) {
        return false;
      }
      if (statusFilter === "active" && !user.isActive) {
        return false;
      }
      if (statusFilter === "inactive" && user.isActive) {
        return false;
      }
      if (clusterFilter !== "all") {
        const access = user.clusterAccess || [];
        const role = rolesByName[user.role];
        if (isFullAccessRole(role) || user.isAdmin) {
          return true;
        }
        if (!access.includes(clusterFilter)) {
          return false;
        }
      }
      if (!q) {
        return true;
      }
      const haystack = [user.username, user.fullName, user.email].join(" ").toLowerCase();
      return haystack.includes(q);
    });
  }, [users, search, roleFilter, statusFilter, clusterFilter, rolesByName]);

  const openCreate = () => {
    setEditingUser(null);
    setModalOpen(true);
  };

  const openEdit = async (userId) => {
    try {
      const user = await getUser(userId);
      setEditingUser(user);
      setModalOpen(true);
    } catch (err) {
      setError(err.message);
    }
  };

  const handleSave = async (payload, editing) => {
    setSaving(true);
    setError("");
    try {
      let savedUser;
      if (editing) {
        savedUser = await updateUser(editing.id, payload);
      } else {
        savedUser = await createUser(payload);
      }
      setModalOpen(false);
      setEditingUser(savedUser || null);
      await loadData();
    } catch (err) {
      throw err;
    } finally {
      setSaving(false);
    }
  };

  const handleDisable = async (user) => {
    if (currentUser?.id === user.id) {
      setError("You cannot disable your own account.");
      return;
    }
    const label = user.fullName || user.username;
    const confirmed = window.confirm(
      `Disable user "${label}" (${user.username})?\n\nThey will no longer be able to sign in. This can be reversed by editing the user and marking them active again.`
    );
    if (!confirmed) {
      return;
    }
    setError("");
    try {
      await disableUser(user.id);
      await loadData();
    } catch (err) {
      setError(err.message);
    }
  };

  const formatDate = (value) => {
    if (!value) {
      return "—";
    }
    try {
      return new Date(value).toLocaleString();
    } catch {
      return value;
    }
  };

  return (
    <div className="ops-page">
      <section className="card ops-section">
        <div className="card-header-row">
          <div>
            <h2>User Management</h2>
            <p className="muted">Create users, assign roles, and scope cluster and namespace access.</p>
          </div>
          {activeTab === "users" && canCreate ? (
            <button type="button" className="primary" onClick={openCreate}>
              Add User
            </button>
          ) : null}
        </div>

        <nav className="tab-bar user-management-tabs" aria-label="user-management-tabs">
          <button
            type="button"
            className={activeTab === "users" ? "active" : ""}
            onClick={() => setActiveTab("users")}
          >
            Users
          </button>
          {canViewRoles ? (
            <button
              type="button"
              className={activeTab === "roles" ? "active" : ""}
              onClick={() => setActiveTab("roles")}
            >
              Roles
            </button>
          ) : null}
        </nav>

        {activeTab === "users" ? (
          <>
            {readOnlyUsers ? (
              <p className="banner-message">You have read-only access to user accounts.</p>
            ) : null}

            {loading ? (
              <LoadingState label="Loading users…" />
            ) : isAccessDeniedError(error) ? (
              <AccessDeniedPage message={error} />
            ) : formatAccessError(error) ? (
              <ErrorBanner message={error} suppressAccessDenied={false} />
            ) : null}

            {!loading && !isAccessDeniedError(error) ? (
            <div className="user-filters">
              <label className="user-filters__search">
                Search
                <input
                  type="search"
                  placeholder="Username, name, or email"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                />
              </label>
              <label>
                Role
                <select value={roleFilter} onChange={(e) => setRoleFilter(e.target.value)}>
                  <option value="all">All roles</option>
                  {roles.map((role) => (
                    <option key={role.id} value={role.name}>
                      {role.name}
                    </option>
                  ))}
                </select>
              </label>
              <label>
                Status
                <select value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
                  <option value="all">All</option>
                  <option value="active">Active</option>
                  <option value="inactive">Disabled</option>
                </select>
              </label>
              <label>
                Cluster access
                <select value={clusterFilter} onChange={(e) => setClusterFilter(e.target.value)}>
                  <option value="all">Any cluster</option>
                  {clusterFilterOptions.map((id) => (
                    <option key={id} value={id}>
                      {id}
                    </option>
                  ))}
                </select>
              </label>
            </div>
            ) : null}

            {!loading && !isAccessDeniedError(error) ? (
              <>
                <p className="muted user-table-meta">
                  Showing {filteredUsers.length} of {users.length} users
                </p>
                <div className="table-wrap">
                  <table className="data-table">
                    <thead>
                      <tr>
                        <th>Username</th>
                        <th>Full Name</th>
                        <th>Email</th>
                        <th>Role</th>
                        <th>Status</th>
                        <th>Last Login</th>
                        <th>Created At</th>
                        <th>Actions</th>
                      </tr>
                    </thead>
                    <tbody>
                      {filteredUsers.map((user) => (
                        <tr key={user.id} className={!user.isActive ? "row-disabled" : ""}>
                          <td>{user.username}</td>
                          <td>{user.fullName}</td>
                          <td>{user.email}</td>
                          <td>
                            <span className="role-badge">{user.role}</span>
                          </td>
                          <td>
                            <span className={`status-pill ${user.isActive ? "ok" : "warn"}`}>
                              {user.isActive ? "Active" : "Disabled"}
                            </span>
                          </td>
                          <td>{formatDate(user.lastLoginAt)}</td>
                          <td>{formatDate(user.createdAt)}</td>
                          <td className="table-actions">
                            {canUpdate ? (
                              <button type="button" className="btn-outline" onClick={() => openEdit(user.id)}>
                                Edit
                              </button>
                            ) : null}
                            {user.isActive && canDisable ? (
                              <button
                                type="button"
                                className="btn-outline danger"
                                onClick={() => handleDisable(user)}
                                disabled={currentUser?.id === user.id}
                                title={
                                  currentUser?.id === user.id
                                    ? "You cannot disable your own account"
                                    : undefined
                                }
                              >
                                Disable User
                              </button>
                            ) : null}
                            {!canUpdate && !canDisable ? (
                              <span className="muted">—</span>
                            ) : null}
                          </td>
                        </tr>
                      ))}
                      {!filteredUsers.length ? (
                        <tr>
                          <td colSpan={8} className="muted">
                            No users match the current filters.
                          </td>
                        </tr>
                      ) : null}
                    </tbody>
                  </table>
                </div>
              </>
            ) : null}
          </>
        ) : (
          <RolesPanel
            roles={roles}
            loading={rolesLoading}
            error={rolesError}
            canManage={canManageRoles}
            onRolesChanged={loadRoles}
            onError={setRolesError}
          />
        )}
      </section>

      {modalOpen ? (
        <Suspense fallback={null}>
          <UserFormModal
            open={modalOpen}
            editingUser={editingUser}
            roles={roles}
            clusters={clusters}
            currentUser={currentUser}
            onClose={() => {
              setModalOpen(false);
              setEditingUser(null);
            }}
            onSave={handleSave}
            saving={saving}
          />
        </Suspense>
      ) : null}
    </div>
  );
}
