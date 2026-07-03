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

/* ── Inline stroke icons (Signal: SVG only, currentColor) ────────── */
function SvgIcon({ children }) {
  return (
    <svg
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
    >
      {children}
    </svg>
  );
}

const IconPlus = () => (
  <SvgIcon>
    <path d="M12 5v14M5 12h14" />
  </SvgIcon>
);

const IconUsers = () => (
  <SvgIcon>
    <path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" />
    <circle cx="9" cy="7" r="4" />
    <path d="M23 21v-2a4 4 0 0 0-3-3.87" />
    <path d="M16 3.13a4 4 0 0 1 0 7.75" />
  </SvgIcon>
);

const IconClock = () => (
  <SvgIcon>
    <circle cx="12" cy="12" r="10" />
    <path d="M12 6v6l4 2" />
  </SvgIcon>
);

const IconLock = () => (
  <SvgIcon>
    <rect x="3" y="11" width="18" height="11" rx="2" />
    <path d="M7 11V7a5 5 0 0 1 10 0v4" />
  </SvgIcon>
);

const IconKey = () => (
  <SvgIcon>
    <path d="M21 2l-2 2m-7.61 7.61a5.5 5.5 0 1 1-7.778 7.778 5.5 5.5 0 0 1 7.777-7.777zm0 0L15.5 7.5l3 3L22 7l-3-3" />
  </SvgIcon>
);

const IconMore = () => (
  <SvgIcon>
    <circle cx="5" cy="12" r="1" />
    <circle cx="12" cy="12" r="1" />
    <circle cx="19" cy="12" r="1" />
  </SvgIcon>
);

/* ── Presentation helpers ────────────────────────────────────────── */
const AVATAR_TINT_COUNT = 7; // users-av-0..6 → var(--chart-2..8)

const avatarTintClass = (seed) => {
  let hash = 0;
  const str = String(seed || "");
  for (let i = 0; i < str.length; i += 1) {
    hash = (hash * 31 + str.charCodeAt(i)) >>> 0;
  }
  return `users-av-${hash % AVATAR_TINT_COUNT}`;
};

const initialsOf = (name) => {
  const parts = String(name || "").trim().split(/\s+/).filter(Boolean);
  if (!parts.length) {
    return "?";
  }
  if (parts.length === 1) {
    return parts[0].slice(0, 2);
  }
  return `${parts[0][0]}${parts[parts.length - 1][0]}`;
};

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

  // KPI strip — computed from the already-loaded list, no extra API calls.
  const stats = useMemo(() => {
    const onboarding = users.filter((u) => u.accountStatus === "first_login_pending");
    const locked = users.filter((u) => u.isLocked);
    return {
      active: users.filter((u) => u.accountStatus === "active").length,
      onboarding: onboarding.length,
      awaitingTotp: onboarding.filter((u) => !u.mustChangePassword).length,
      locked: locked.length,
      adminLocked: locked.filter((u) => u.requiresAdminUnlock).length,
    };
  }, [users]);

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

  // Concept-style relative timestamp ("2 min ago"); full date lives in the title attr.
  const formatRelative = (value) => {
    if (!value) {
      return "—";
    }
    const then = new Date(value);
    if (Number.isNaN(then.getTime())) {
      return value;
    }
    const diffMs = Date.now() - then.getTime();
    if (diffMs < 60_000) {
      return "just now";
    }
    const minutes = Math.floor(diffMs / 60_000);
    if (minutes < 60) {
      return `${minutes} min ago`;
    }
    const hours = Math.floor(minutes / 60);
    if (hours < 24) {
      return `${hours} h ago`;
    }
    const days = Math.floor(hours / 24);
    if (days === 1) {
      return "yesterday";
    }
    if (days < 30) {
      return `${days} d ago`;
    }
    return then.toLocaleDateString();
  };

  // Lock reason: failed_password / failed_mfa (locks at 5) or admin.
  const lockReasonLabel = (user) => {
    switch (user.lockReason) {
      case "failed_password":
        return user.failedLoginAttempts
          ? `password ×${user.failedLoginAttempts}`
          : "failed password";
      case "failed_mfa":
        return user.mfaFailedAttempts ? `TOTP ×${user.mfaFailedAttempts}` : "failed TOTP";
      case "admin":
        return "by admin";
      default:
        return null;
    }
  };

  // Staged onboarding progress: temp password → change password → TOTP.
  const onboardingStep = (user) => {
    if (user.mustChangePassword) {
      return 1;
    }
    if (!user.mfaEnabled) {
      return 2;
    }
    return 3;
  };

  const renderMfa = (user) =>
    user.mfaEnabled ? (
      <span className="status-pill ok">TOTP enrolled</span>
    ) : (
      <span className="status-pill warn">Awaiting TOTP</span>
    );

  const renderState = (user) => {
    const status = user.accountStatus;
    if (status === "admin_locked" || status === "temp_locked") {
      const reason = lockReasonLabel(user);
      const title =
        status === "admin_locked"
          ? "Requires an admin unlock"
          : "Temporarily locked after repeated failures";
      return (
        <span className="status-pill danger" title={title}>
          {reason ? `Locked — ${reason}` : "Locked"}
        </span>
      );
    }
    if (status === "first_login_pending") {
      const step = onboardingStep(user);
      const stageTitle =
        step === 1
          ? "Temporary password issued — awaiting password change"
          : step === 2
            ? "Password changed — awaiting TOTP enrollment"
            : "Finishing first sign-in";
      return (
        <span className="status-pill warn" title={stageTitle}>
          Onboarding {step}/3
        </span>
      );
    }
    if (status === "disabled") {
      return <span className="status-pill unknown">Disabled</span>;
    }
    return <span className="status-pill ok">Active</span>;
  };

  const renderRoleTag = (user) => {
    if (!user.role) {
      return <span className="muted">—</span>;
    }
    const role = rolesByName[user.role];
    const isAdminRole = isFullAccessRole(role) || user.isAdmin || user.role === "admin";
    return <span className={`sg-tag${isAdminRole ? " sg-tag--prod" : ""}`}>{user.role}</span>;
  };

  const renderIdentity = (user) => {
    const displayName = (user.fullName || "").trim() || user.username;
    const subParts = [];
    if (displayName !== user.username) {
      subParts.push(user.username);
    }
    if (user.email) {
      subParts.push(user.email);
    }
    return (
      <div className="users-id-cell" title={`Created ${formatDate(user.createdAt)}`}>
        <span
          className={`sg-avatar sg-avatar--sm ${avatarTintClass(user.username || user.id)}`}
          aria-hidden="true"
        >
          {initialsOf(displayName)}
        </span>
        <div className="users-id-text">
          <p className="users-id-name">{displayName}</p>
          <p className="users-id-sub">{subParts.join(" · ") || "—"}</p>
        </div>
      </div>
    );
  };

  const renderActions = (user) => {
    const isSelf = currentUser?.id === user.id;
    const busy = busyUserId === user.id;
    const isOnboarding = user.accountStatus === "first_login_pending";
    const hasMenu = canUpdate || canDisable || canDelete;
    return (
      <div className="users-actions">
        {canUpdate && user.isLocked ? (
          <button
            type="button"
            className="primary users-btn-sm"
            onClick={() =>
              runAction(user, unlockUser, {
                confirm: `Unlock ${user.username}?`,
              })
            }
            disabled={busy}
          >
            <IconKey />
            Unlock
          </button>
        ) : null}
        {canUpdate && user.isActive && isOnboarding && !user.isLocked ? (
          <button
            type="button"
            className="btn-outline users-btn-sm"
            onClick={() =>
              runAction(user, resendTemporaryPassword, {
                confirm: `Send a new temporary password to ${user.username}?`,
                onResult: (r) => showTemporaryPasswordNotice(r, "updated"),
              })
            }
            disabled={busy}
            title="Resend temporary password"
          >
            Resend
          </button>
        ) : null}
        {hasMenu ? (
          <details className="row-actions-menu">
            <summary className="btn-ghost users-more" aria-label={`Actions for ${user.username}`}>
              <IconMore />
            </summary>
            <div className="row-actions-menu__panel">
              {canUpdate ? (
                <button type="button" onClick={() => openEdit(user.id)}>
                  Edit user
                </button>
              ) : null}
              {canUpdate ? (
                <button
                  type="button"
                  onClick={() => runAction(user, resetFailedAttempts, {})}
                  disabled={busy}
                >
                  Reset failed attempts
                </button>
              ) : null}
              {canUpdate && user.mfaEnabled ? (
                <button
                  type="button"
                  onClick={() =>
                    runAction(user, resetUserMfa, {
                      confirm: `Reset MFA for ${user.username}? They must re-enrol at next sign-in.`,
                    })
                  }
                  disabled={busy}
                >
                  Reset MFA
                </button>
              ) : null}
              {canUpdate && user.isActive && !isOnboarding ? (
                <button
                  type="button"
                  onClick={() =>
                    runAction(user, resendTemporaryPassword, {
                      confirm: `Send a new temporary password to ${user.username}?`,
                      onResult: (r) => showTemporaryPasswordNotice(r, "updated"),
                    })
                  }
                  disabled={busy}
                >
                  Resend temporary password
                </button>
              ) : null}
              {canUpdate && user.isActive ? (
                <button
                  type="button"
                  onClick={() =>
                    runAction(user, forcePasswordReset, {
                      confirm: `Force ${user.username} to reset their password?`,
                      onResult: (r) => showTemporaryPasswordNotice(r, "updated"),
                    })
                  }
                  disabled={busy}
                >
                  Force password reset
                </button>
              ) : null}
              {canUpdate && user.isActive && !user.requiresAdminUnlock && !isSelf ? (
                <button
                  type="button"
                  onClick={() =>
                    runAction(user, lockUser, {
                      confirm: `Lock ${user.username} until an admin unlocks the account?`,
                    })
                  }
                  disabled={busy}
                >
                  Lock account
                </button>
              ) : null}
              {canUpdate && !user.isActive ? (
                <button
                  type="button"
                  onClick={() => runAction(user, enableUser, {})}
                  disabled={busy}
                >
                  Enable account
                </button>
              ) : null}
              {canDisable && user.isActive ? (
                <button
                  type="button"
                  className="users-menu-danger"
                  onClick={() => handleDisable(user)}
                  disabled={isSelf}
                  title={isSelf ? "You cannot disable your own account" : undefined}
                >
                  Disable
                </button>
              ) : null}
              {canDelete ? (
                <button
                  type="button"
                  className="users-menu-danger"
                  onClick={() => handleDelete(user)}
                  disabled={isSelf}
                  title={
                    isSelf ? "You cannot delete your own account" : "Permanently delete this user"
                  }
                >
                  Delete
                </button>
              ) : null}
            </div>
          </details>
        ) : (
          <span className="muted">—</span>
        )}
      </div>
    );
  };

  return (
    <div className="ops-page users-page">
      <header className="sg-ph">
        <div>
          <h2>Users &amp; access</h2>
          <p className="sg-ph-sub">
            {loading
              ? "Staged onboarding"
              : `${users.length} ${users.length === 1 ? "user" : "users"} · staged onboarding`}
            : temporary password → change → TOTP enrollment
          </p>
        </div>
        <div className="sg-ph-actions">
          {activeTab === "users" && canCreate ? (
            <button type="button" className="primary users-invite" onClick={openCreate}>
              <IconPlus />
              Invite user
            </button>
          ) : null}
        </div>
      </header>

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
            <div className="sg-kpi-grid users-kpis">
              <div className="sg-kpi">
                <p className="sg-kpi-label">
                  <IconUsers />
                  Active users
                </p>
                <div className="sg-kpi-value">
                  <b>{stats.active}</b>
                  <span className="sg-delta sg-delta--flat">of {users.length} total</span>
                </div>
              </div>
              <div className="sg-kpi">
                <p className="sg-kpi-label">
                  <IconClock />
                  In onboarding
                </p>
                <div className="sg-kpi-value">
                  <b>{stats.onboarding}</b>
                  {stats.onboarding ? (
                    <span className="sg-delta sg-delta--flat">
                      {stats.awaitingTotp} awaiting TOTP
                    </span>
                  ) : null}
                </div>
              </div>
              <div className={`sg-kpi${stats.locked ? " users-kpi--locked" : ""}`}>
                <p className="sg-kpi-label">
                  <IconLock />
                  Locked out
                </p>
                <div className="sg-kpi-value">
                  <b>{stats.locked}</b>
                  {stats.adminLocked ? (
                    <span className="sg-delta sg-delta--down">
                      {stats.adminLocked} need admin unlock
                    </span>
                  ) : null}
                </div>
              </div>
            </div>
          ) : null}

          {!loading && !isAccessDeniedError(error) ? (
            <section className="card ops-section users-table-card">
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

              <p className="muted user-table-meta">
                Showing {filteredUsers.length} of {users.length} users
              </p>
              <div className="table-wrap">
                <table className="data-table users-table">
                  <thead>
                    <tr>
                      <th>User</th>
                      <th>Role</th>
                      <th>MFA</th>
                      <th>State</th>
                      <th className="r">Last sign-in</th>
                      <th className="r">Actions</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredUsers.map((user) => (
                      <tr key={user.id} className={!user.isActive ? "row-disabled" : ""}>
                        <td>{renderIdentity(user)}</td>
                        <td>{renderRoleTag(user)}</td>
                        <td>{renderMfa(user)}</td>
                        <td>{renderState(user)}</td>
                        <td
                          className="r users-last-login"
                          title={user.lastLoginAt ? formatDate(user.lastLoginAt) : undefined}
                        >
                          {formatRelative(user.lastLoginAt)}
                        </td>
                        <td className="r">{renderActions(user)}</td>
                      </tr>
                    ))}
                    {!filteredUsers.length ? (
                      <tr>
                        <td colSpan={6} className="muted">
                          No users match the current filters.
                        </td>
                      </tr>
                    ) : null}
                  </tbody>
                </table>
              </div>
            </section>
          ) : null}
        </>
      ) : (
        <section className="card ops-section">
          <RolesPanel
            roles={roles}
            loading={rolesLoading}
            error={rolesError}
            canManage={canManageRoles}
            onRolesChanged={loadRoles}
            onError={setRolesError}
          />
        </section>
      )}

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
