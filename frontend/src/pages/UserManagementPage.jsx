import { lazy, Suspense, useEffect, useMemo, useState } from "react";
import {
  createUser,
  deleteUser,
  disableUser,
  enableUser,
  forcePasswordReset,
  getUser,
  listRoles,
  listUsers,
  lockUser,
  resendTemporaryPassword,
  resetFailedAttempts,
  resetUserMfa,
  unlockUser,
  updateUser,
} from "../api";
import { useAuth } from "../context/AuthContext";
import SearchableSelect from "../components/common/SearchableSelect.jsx";
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
  const canDelete = hasPermission("users:delete") || hasPermission("users:manage");
  const readOnlyUsers =
    hasPermission("users:view") && !canCreate && !canUpdate && !canDisable && !canDelete;
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
  const [notice, setNotice] = useState(null);
  const [busyUserId, setBusyUserId] = useState(null);

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
      if (statusFilter === "active" && user.accountStatus !== "active") {
        return false;
      }
      if (statusFilter === "inactive" && user.isActive) {
        return false;
      }
      if (statusFilter === "locked" && !user.isLocked) {
        return false;
      }
      if (statusFilter === "pending" && user.accountStatus !== "first_login_pending") {
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
        showTemporaryPasswordNotice(savedUser, "created");
      }
      setModalOpen(false);
      setEditingUser(editing ? savedUser || null : null);
      await loadData();
    } catch (err) {
      throw err;
    } finally {
      setSaving(false);
    }
  };

  // Surface the outcome of a temporary-password action. When SMTP is configured
  // the password was emailed and never returned; otherwise we show the plaintext
  // once so the admin can pass it along out of band.
  const showTemporaryPasswordNotice = (result, verb) => {
    if (!result) {
      return;
    }
    const who = result.username ? ` for ${result.username}` : "";
    if (result.temporaryPassword) {
      setNotice({
        tone: "warn",
        title: `User ${verb}. SMTP is not configured, so share this temporary password securely${who}:`,
        password: result.temporaryPassword,
      });
    } else if (result.temporaryPasswordEmailed) {
      setNotice({
        tone: "ok",
        title: `User ${verb}. A temporary password has been emailed${who}.`,
      });
    } else {
      setNotice({ tone: "ok", title: `User ${verb}.` });
    }
  };

  const runAction = async (user, actionFn, { confirm, onResult } = {}) => {
    if (confirm && !window.confirm(confirm)) {
      return;
    }
    setError("");
    setNotice(null);
    setBusyUserId(user.id);
    try {
      const result = await actionFn(user.id);
      if (onResult) {
        onResult(result);
      }
      await loadData();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusyUserId(null);
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

  const handleDelete = async (user) => {
    if (currentUser?.id === user.id) {
      setError("You cannot delete your own account.");
      return;
    }
    const label = user.fullName || user.username;
    const confirmed = window.confirm(
      `Permanently delete user "${label}" (${user.username})?\n\n` +
        "This cannot be undone. Their access grants and API tokens are removed; " +
        "past audit/history entries are kept but no longer linked to an account."
    );
    if (!confirmed) {
      return;
    }
    setError("");
    try {
      await deleteUser(user.id);
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

  const STATUS_META = {
    active: { label: "Active", tone: "ok" },
    disabled: { label: "Disabled", tone: "warn" },
    temp_locked: { label: "Temporarily locked", tone: "warn" },
    admin_locked: { label: "Locked (admin unlock)", tone: "danger" },
    first_login_pending: { label: "First login pending", tone: "info" },
  };

  const renderStatus = (user) => {
    const meta = STATUS_META[user.accountStatus] || STATUS_META.active;
    return (
      <div className="user-status-cell">
        <span className={`status-pill ${meta.tone}`}>{meta.label}</span>
        {user.mfaEnabled ? <span className="status-pill info">MFA</span> : null}
      </div>
    );
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

            {notice ? (
              <div className={`banner-message ${notice.tone === "warn" ? "error" : ""} user-notice`}>
                <div className="user-notice__row">
                  <span>{notice.title}</span>
                  <button type="button" className="btn-link" onClick={() => setNotice(null)}>
                    Dismiss
                  </button>
                </div>
                {notice.password ? (
                  <code className="user-notice__password">{notice.password}</code>
                ) : null}
              </div>
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
                <SearchableSelect value={roleFilter} onChange={(e) => setRoleFilter(e.target.value)}>
                  <option value="all">All roles</option>
                  {roles.map((role) => (
                    <option key={role.id} value={role.name}>
                      {role.name}
                    </option>
                  ))}
                </SearchableSelect>
              </label>
              <label>
                Status
                <SearchableSelect value={statusFilter} onChange={(e) => setStatusFilter(e.target.value)}>
                  <option value="all">All</option>
                  <option value="active">Active</option>
                  <option value="inactive">Disabled</option>
                  <option value="locked">Locked</option>
                  <option value="pending">First login pending</option>
                </SearchableSelect>
              </label>
              <label>
                Cluster access
                <SearchableSelect value={clusterFilter} onChange={(e) => setClusterFilter(e.target.value)}>
                  <option value="all">Any cluster</option>
                  {clusterFilterOptions.map((id) => (
                    <option key={id} value={id}>
                      {id}
                    </option>
                  ))}
                </SearchableSelect>
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
                          <td>{renderStatus(user)}</td>
                          <td>{formatDate(user.lastLoginAt)}</td>
                          <td>{formatDate(user.createdAt)}</td>
                          <td className="table-actions">
                            {canUpdate ? (
                              <button type="button" className="btn-outline" onClick={() => openEdit(user.id)}>
                                Edit
                              </button>
                            ) : null}
                            {canUpdate ? (
                              <details className="row-actions-menu">
                                <summary className="btn-outline">Security ▾</summary>
                                <div className="row-actions-menu__panel">
                                  {user.isLocked ? (
                                    <button
                                      type="button"
                                      onClick={() =>
                                        runAction(user, unlockUser, {
                                          confirm: `Unlock ${user.username}?`,
                                        })
                                      }
                                      disabled={busyUserId === user.id}
                                    >
                                      Unlock account
                                    </button>
                                  ) : null}
                                  <button
                                    type="button"
                                    onClick={() =>
                                      runAction(user, resetFailedAttempts, {})
                                    }
                                    disabled={busyUserId === user.id}
                                  >
                                    Reset failed attempts
                                  </button>
                                  {user.mfaEnabled ? (
                                    <button
                                      type="button"
                                      onClick={() =>
                                        runAction(user, resetUserMfa, {
                                          confirm: `Reset MFA for ${user.username}? They must re-enrol at next sign-in.`,
                                        })
                                      }
                                      disabled={busyUserId === user.id}
                                    >
                                      Reset MFA
                                    </button>
                                  ) : null}
                                  {user.isActive ? (
                                    <button
                                      type="button"
                                      onClick={() =>
                                        runAction(user, resendTemporaryPassword, {
                                          confirm: `Send a new temporary password to ${user.username}?`,
                                          onResult: (r) => showTemporaryPasswordNotice(r, "updated"),
                                        })
                                      }
                                      disabled={busyUserId === user.id}
                                    >
                                      Resend temporary password
                                    </button>
                                  ) : null}
                                  {user.isActive ? (
                                    <button
                                      type="button"
                                      onClick={() =>
                                        runAction(user, forcePasswordReset, {
                                          confirm: `Force ${user.username} to reset their password?`,
                                          onResult: (r) => showTemporaryPasswordNotice(r, "updated"),
                                        })
                                      }
                                      disabled={busyUserId === user.id}
                                    >
                                      Force password reset
                                    </button>
                                  ) : null}
                                  {user.isActive && !user.requiresAdminUnlock && currentUser?.id !== user.id ? (
                                    <button
                                      type="button"
                                      onClick={() =>
                                        runAction(user, lockUser, {
                                          confirm: `Lock ${user.username} until an admin unlocks the account?`,
                                        })
                                      }
                                      disabled={busyUserId === user.id}
                                    >
                                      Lock account
                                    </button>
                                  ) : null}
                                  {!user.isActive ? (
                                    <button
                                      type="button"
                                      onClick={() => runAction(user, enableUser, {})}
                                      disabled={busyUserId === user.id}
                                    >
                                      Enable account
                                    </button>
                                  ) : null}
                                </div>
                              </details>
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
                                Disable
                              </button>
                            ) : null}
                            {canDelete ? (
                              <button
                                type="button"
                                className="btn-outline danger"
                                onClick={() => handleDelete(user)}
                                disabled={currentUser?.id === user.id}
                                title={
                                  currentUser?.id === user.id
                                    ? "You cannot delete your own account"
                                    : "Permanently delete this user"
                                }
                              >
                                Delete
                              </button>
                            ) : null}
                            {!canUpdate && !canDisable && !canDelete ? (
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
