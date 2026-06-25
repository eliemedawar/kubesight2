import { useEffect, useMemo, useState } from "react";
import { listPermissions } from "../../api/usersApi";
import {
  PERMISSION_GROUPS as FALLBACK_GROUPS,
  permissionLabel,
} from "../../lib/permissionCatalog";

export default function RoleFormModal({
  open,
  mode = "edit",
  role,
  roles = [],
  onClose,
  onSave,
  saving,
}) {
  const readOnly = mode === "view";
  const [form, setForm] = useState({
    name: "",
    description: "",
    permissions: [],
    template: "custom",
  });
  const [formError, setFormError] = useState("");
  // Backend-driven permission catalog: { groups: [{id,label,keys}], items: [{key,description,dangerous}] }
  const [catalog, setCatalog] = useState(null);

  useEffect(() => {
    if (!open) {
      return;
    }
    let cancelled = false;
    listPermissions()
      .then((res) => {
        if (!cancelled) setCatalog(res || null);
      })
      .catch(() => {
        // Fall back to the static catalog if the API is unavailable.
        if (!cancelled) setCatalog(null);
      });
    return () => {
      cancelled = true;
    };
  }, [open]);

  useEffect(() => {
    if (!open) {
      return;
    }
    if (role) {
      setForm({
        name: role.name || "",
        description: role.description || "",
        permissions: [...(role.permissions || [])],
        template: "custom",
      });
    } else {
      setForm({ name: "", description: "", permissions: [], template: "custom" });
    }
    setFormError("");
  }, [open, role]);

  // Group + label metadata sourced from the backend (falls back to static).
  const groups = useMemo(() => {
    if (catalog?.groups?.length) {
      return catalog.groups;
    }
    return FALLBACK_GROUPS.map((g) => ({ id: g.id, label: g.label, keys: g.keys }));
  }, [catalog]);

  const itemMeta = useMemo(() => {
    const map = {};
    (catalog?.items || []).forEach((item) => {
      map[item.key] = { label: item.description || item.key, dangerous: Boolean(item.dangerous) };
    });
    return map;
  }, [catalog]);

  const labelFor = (key) => itemMeta[key]?.label || permissionLabel(key);
  const isDangerous = (key) => Boolean(itemMeta[key]?.dangerous);
  const keysForGroup = (group) => group.keys || [];

  // Quick templates are the actual roles in the system — copy their permission
  // set as a starting point. Fully dynamic: new roles appear here automatically.
  const templateRoles = useMemo(() => {
    const copyable = (roles || []).filter((r) => (r.permissions || []).length);
    // System roles first (admin, operator, ...), then custom roles, by name.
    return [...copyable].sort((a, b) => {
      if (Boolean(b.isSystemRole) !== Boolean(a.isSystemRole)) {
        return b.isSystemRole ? 1 : -1;
      }
      return String(a.name).localeCompare(String(b.name));
    });
  }, [roles]);

  const selectedPermissions = useMemo(() => new Set(form.permissions), [form.permissions]);

  const togglePermission = (key) => {
    if (readOnly) return;
    setForm((prev) => {
      const next = new Set(prev.permissions);
      next.has(key) ? next.delete(key) : next.add(key);
      return { ...prev, permissions: [...next], template: "custom" };
    });
  };

  const toggleGroup = (group, checked) => {
    if (readOnly) return;
    const keys = keysForGroup(group);
    setForm((prev) => {
      const next = new Set(prev.permissions);
      keys.forEach((key) => (checked ? next.add(key) : next.delete(key)));
      return { ...prev, permissions: [...next], template: "custom" };
    });
  };

  const applyTemplate = (templateRole) => {
    if (readOnly) return;
    setForm((prev) => ({
      ...prev,
      template: `role:${templateRole.id}`,
      permissions: [...(templateRole.permissions || [])],
    }));
  };

  const handleSubmit = async () => {
    setFormError("");
    if (!form.name.trim() && !role?.isSystemRole) {
      setFormError("Role name is required.");
      return;
    }
    if (!form.permissions.length) {
      setFormError("Select at least one permission.");
      return;
    }
    const payload = {
      description: form.description.trim(),
      permissions: form.permissions,
    };
    if (!role?.isSystemRole) {
      payload.name = form.name.trim();
    }
    try {
      await onSave(payload);
    } catch (err) {
      setFormError(err.message);
    }
  };

  if (!open) {
    return null;
  }

  const title = mode === "view" ? "View Role" : role ? "Edit Role" : "Create Role";

  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <div className="modal-card modal-card--wide" role="dialog" onClick={(e) => e.stopPropagation()}>
        <div className="modal-card__header">
          <h3>{title}</h3>
          <p className="muted">
            {readOnly
              ? "Review role permissions and assignment details."
              : "Define role capabilities using grouped permission checkboxes."}
          </p>
        </div>

        {formError ? <p className="banner-message error">{formError}</p> : null}
        {role?.isSystemRole ? (
          <p className="banner-message">
            System roles are protected. You can update the description and permissions, but not the
            name or delete the role.
          </p>
        ) : null}

        <section className="form-section">
          <h4>Role details</h4>
          <div className="form-grid">
            <label>
              Role name
              <input
                value={form.name}
                onChange={(e) => setForm((prev) => ({ ...prev, name: e.target.value }))}
                disabled={readOnly || Boolean(role?.isSystemRole)}
                placeholder="e.g. platform_operator"
              />
            </label>
            <label className="form-grid__full">
              Description
              <input
                value={form.description}
                onChange={(e) => setForm((prev) => ({ ...prev, description: e.target.value }))}
                disabled={readOnly}
                placeholder="What this role is for"
              />
            </label>
          </div>
        </section>

        {!readOnly && templateRoles.length ? (
          <section className="form-section">
            <h4>Start from an existing role</h4>
            <div className="role-template-bar">
              {templateRoles.map((templateRole) => (
                <button
                  key={templateRole.id}
                  type="button"
                  className={`btn-outline ${form.template === `role:${templateRole.id}` ? "is-selected" : ""}`}
                  onClick={() => applyTemplate(templateRole)}
                  title={`Copy the ${templateRole.permissions?.length || 0} permission(s) from ${templateRole.name}`}
                >
                  {templateRole.name}
                  {templateRole.isSystemRole ? " (system)" : ""}
                </button>
              ))}
            </div>
          </section>
        ) : null}

        <section className="form-section role-permissions-editor">
          <h4>Permissions</h4>
          <div className="role-permission-groups">
            {groups.map((group) => {
              const keys = keysForGroup(group);
              const checkedCount = keys.filter((key) => selectedPermissions.has(key)).length;
              const allChecked = checkedCount === keys.length && keys.length > 0;
              const someChecked = checkedCount > 0 && !allChecked;

              return (
                <div key={group.id} className="role-permission-group">
                  <div className="role-permission-group__header">
                    <label className="checkbox-row">
                      <input
                        type="checkbox"
                        checked={allChecked}
                        ref={(input) => {
                          if (input) input.indeterminate = someChecked;
                        }}
                        disabled={readOnly}
                        onChange={(e) => toggleGroup(group, e.target.checked)}
                      />
                      <strong>{group.label}</strong>
                    </label>
                    <span className="muted">
                      {checkedCount}/{keys.length}
                    </span>
                  </div>
                  <div className="role-permission-group__items">
                    {keys.map((key) => (
                      <label key={key} className="checkbox-row">
                        <input
                          type="checkbox"
                          checked={selectedPermissions.has(key)}
                          disabled={readOnly}
                          onChange={() => togglePermission(key)}
                        />
                        <span>
                          {labelFor(key)}
                          {isDangerous(key) ? (
                            <span
                              title="Privileged / destructive permission"
                              style={{ color: "#dc2626", marginLeft: 6, fontSize: "0.7rem", fontWeight: 600 }}
                            >
                              ●
                            </span>
                          ) : null}
                        </span>
                      </label>
                    ))}
                  </div>
                </div>
              );
            })}
          </div>
        </section>

        <div className="modal-actions">
          <button type="button" className="btn-outline" onClick={onClose}>
            {readOnly ? "Close" : "Cancel"}
          </button>
          {!readOnly ? (
            <button type="button" className="primary" onClick={handleSubmit} disabled={saving}>
              {saving ? "Saving…" : role ? "Save Role" : "Create Role"}
            </button>
          ) : null}
        </div>
      </div>
    </div>
  );
}
