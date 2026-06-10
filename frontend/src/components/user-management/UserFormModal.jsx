import { useEffect, useMemo, useRef, useState } from "react";
import { getUser } from "../../api";
import AccessRulesEditor, {
  accessRulesToGrants,
  countSelectedResources,
  grantsToAccessRules,
} from "./AccessRulesEditor";
import EffectiveAccessPreview from "./EffectiveAccessPreview";
import {
  allowedClusterSnapshot,
  buildEffectiveAccessPreview,
  emptyClusterGrant,
  sanitizeClusterGrantsForRole,
} from "../../lib/accessRulesForm";
import { isFullAccessRole, roleDescription } from "../../lib/rolePresets";

export default function UserFormModal({
  open,
  editingUser,
  roles,
  clusters,
  currentUser,
  onClose,
  onSave,
  saving,
}) {
  const [form, setForm] = useState({
    username: "",
    fullName: "",
    email: "",
    password: "",
    roleId: "",
    isActive: true,
  });
  const [clusterGrants, setClusterGrants] = useState({});
  const [formError, setFormError] = useState("");
  const [loadingProfile, setLoadingProfile] = useState(false);
  const savedAccessRulesRef = useRef([]);

  const selectedRole = useMemo(
    () => roles.find((r) => String(r.id) === String(form.roleId)),
    [roles, form.roleId]
  );

  const isEditingSelf = editingUser && currentUser && editingUser.id === currentUser.id;
  const isAdminRole = isFullAccessRole(selectedRole);

  const clustersById = useMemo(
    () => Object.fromEntries(clusters.map((c) => [c.id, c])),
    [clusters]
  );

  const clusterIdsKey = useMemo(
    () => clusters.map((c) => c.id).sort().join("|"),
    [clusters]
  );

  const allowedClustersKey = useMemo(
    () => allowedClusterSnapshot(clusterGrants, clusters),
    [clusterGrants, clusters]
  );

  const effectivePreview = useMemo(
    () =>
      buildEffectiveAccessPreview(clusterGrants, clustersById, selectedRole, clusters),
    [clusterGrants, clustersById, clusters, selectedRole, form.roleId, allowedClustersKey]
  );

  const applyAccessRulesToForm = (profile, clusterIds) => {
    const rules = profile?.accessRules?.length
      ? profile.accessRules
      : legacyToRules(profile || {});
    savedAccessRulesRef.current = rules;
    const role = roles.find((r) => String(r.id) === String(profile?.roleId));
    const grants = accessRulesToGrants(rules, clusterIds);
    const pruned = Object.fromEntries(
      clusterIds.map((id) => [id, grants[id] || emptyClusterGrant(id)])
    );
    setClusterGrants(sanitizeClusterGrantsForRole(pruned, role));
  };

  useEffect(() => {
    if (!open) {
      savedAccessRulesRef.current = [];
      setClusterGrants({});
      setLoadingProfile(false);
      return;
    }

    const clusterIds = clusters.map((c) => c.id);

    if (!editingUser?.id) {
      setForm({
        username: "",
        fullName: "",
        email: "",
        password: "",
        roleId: roles[0]?.id || "",
        isActive: true,
      });
      setClusterGrants(
        Object.fromEntries(clusterIds.map((id) => [id, emptyClusterGrant(id)]))
      );
      savedAccessRulesRef.current = [];
      setFormError("");
      return;
    }

    let cancelled = false;
    setLoadingProfile(true);
    setFormError("");

    (async () => {
      let profile = editingUser;
      try {
        profile = await getUser(editingUser.id);
      } catch {
        // Fall back to the list row snapshot if the detail request fails.
      }
      if (cancelled) {
        return;
      }

      setForm({
        username: profile.username,
        fullName: profile.fullName || "",
        email: profile.email || "",
        password: "",
        roleId: profile.roleId || "",
        isActive: profile.isActive !== false,
      });
      applyAccessRulesToForm(profile, clusterIds);
      setLoadingProfile(false);
    })();

    return () => {
      cancelled = true;
    };
  }, [open, editingUser?.id]);

  useEffect(() => {
    if (!open || !clusterIdsKey) {
      return;
    }
    setClusterGrants((prev) => {
      const next = { ...prev };
      let changed = false;
      clusters.forEach((c) => {
        if (!next[c.id]) {
          next[c.id] = emptyClusterGrant(c.id);
          changed = true;
        }
      });
      return changed ? next : prev;
    });
  }, [open, clusterIdsKey, clusters]);

  const handleRoleChange = (roleId) => {
    const role = roles.find((r) => String(r.id) === String(roleId));
    if (isEditingSelf && isFullAccessRole(selectedRole) && !isFullAccessRole(role)) {
      setFormError("You cannot remove your own admin access.");
      return;
    }
    setFormError("");
    setForm((prev) => ({ ...prev, roleId }));
    if (role) {
      setClusterGrants((prev) => sanitizeClusterGrantsForRole(prev, role));
    }
  };

  const handleSubmit = async () => {
    setFormError("");
    if (!editingUser && !form.username.trim()) {
      setFormError("Username is required.");
      return;
    }
    if (!editingUser && !form.password) {
      setFormError("Password is required for new users.");
      return;
    }

    if (!isAdminRole) {
      const hasCluster = Object.values(clusterGrants).some((g) => g?.allowed);
      if (!hasCluster) {
        setFormError("Assign at least one cluster or choose the Admin role.");
        return;
      }
      for (const grant of Object.values(clusterGrants)) {
        if (!grant?.allowed) continue;
        if (grant.mode === "namespaces" && !grant.namespaces?.length) {
          setFormError("Select at least one namespace, or choose full cluster access.");
          return;
        }
        if (grant.mode === "resources") {
          const counts = countSelectedResources(grant.resourceAccess);
          if (counts.total === 0) {
            setFormError(
              `Select at least one pod, deployment, or service in ${grant.clusterId}.`
            );
            return;
          }
        }
      }
    }

    const payload = {
      username: form.username.trim(),
      fullName: form.fullName.trim(),
      email: form.email.trim(),
      roleId: Number(form.roleId),
      isActive: form.isActive,
      accessRules: isAdminRole
        ? []
        : grantsToAccessRules(clusterGrants, selectedRole?.permissions || []),
    };
    if (form.password) payload.password = form.password;

    try {
      await onSave(payload, editingUser);
    } catch (err) {
      setFormError(err.message);
    }
  };

  if (!open) return null;

  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <div
        className="modal-card modal-card--wide"
        role="dialog"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="modal-card__header">
          <h3>{editingUser ? "Edit User" : "Add User"}</h3>
          <p className="muted">Assign a role and choose what clusters and resources this person can use.</p>
        </div>

        {formError ? <p className="banner-message error">{formError}</p> : null}
        {isEditingSelf ? (
          <p className="banner-message">
            You are editing your own account. Admin role and active status are protected.
          </p>
        ) : null}

        <section className="form-section">
          <h4>Account</h4>
          <div className="form-grid">
            <label>
              Username
              <input
                value={form.username}
                onChange={(e) => setForm((p) => ({ ...p, username: e.target.value }))}
                disabled={Boolean(editingUser)}
              />
            </label>
            <label>
              Full Name
              <input
                value={form.fullName}
                onChange={(e) => setForm((p) => ({ ...p, fullName: e.target.value }))}
              />
            </label>
            <label>
              Email
              <input
                type="email"
                value={form.email}
                onChange={(e) => setForm((p) => ({ ...p, email: e.target.value }))}
              />
            </label>
            <label>
              Password {editingUser ? "(optional)" : "(required)"}
              <input
                type="password"
                value={form.password}
                onChange={(e) => setForm((p) => ({ ...p, password: e.target.value }))}
              />
            </label>
            <label className="role-select-label">
              Role
              <select
                value={form.roleId}
                onChange={(e) => handleRoleChange(e.target.value)}
                disabled={isEditingSelf}
              >
                {roles.map((role) => (
                  <option key={role.id} value={role.id}>
                    {role.name}
                    {role.description ? ` — ${role.description}` : ""}
                  </option>
                ))}
              </select>
              {selectedRole ? (
                <span className="muted role-preset-desc">
                  {selectedRole.description || roleDescription(selectedRole.name)}
                </span>
              ) : null}
            </label>
            <label className="checkbox-row">
              <input
                type="checkbox"
                checked={form.isActive}
                disabled={isEditingSelf}
                onChange={(e) => setForm((p) => ({ ...p, isActive: e.target.checked }))}
              />
              Active account
            </label>
          </div>
        </section>

        {loadingProfile ? <p className="muted">Loading access settings…</p> : null}

        {isAdminRole ? (
          <p className="muted form-section">
            Admin users have full access to the platform. Cluster restrictions below are not required.
          </p>
        ) : !loadingProfile ? (
          <>
            <AccessRulesEditor
              clusters={clusters}
              clusterGrants={clusterGrants}
              onClusterGrantsChange={setClusterGrants}
              selectedRole={selectedRole}
              disabled={false}
            />
            <EffectiveAccessPreview preview={effectivePreview} />
          </>
        ) : null}

        <div className="modal-actions">
          <button type="button" className="btn-outline" onClick={onClose}>
            Cancel
          </button>
          <button type="button" className="primary" onClick={handleSubmit} disabled={saving}>
            {saving ? "Saving…" : "Save User"}
          </button>
        </div>
      </div>
    </div>
  );
}

function legacyToRules(user) {
  const rules = [];
  (user.clusterAccess || []).forEach((clusterId) => {
    rules.push({
      clusterId,
      resourceType: "cluster",
      permissionKey: "clusters:view",
      effect: "allow",
    });
  });
  (user.namespaceAccess || []).forEach((row) => {
    rules.push({
      clusterId: row.clusterId,
      namespace: row.namespace,
      resourceType: "namespace",
      permissionKey: "namespaces:view",
      effect: "allow",
    });
  });
  return rules;
}
