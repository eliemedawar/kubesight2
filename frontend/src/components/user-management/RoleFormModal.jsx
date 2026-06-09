import { useEffect, useMemo, useState } from "react";
import {
  PERMISSION_GROUPS,
  catalogEntriesForGroup,
} from "../../lib/permissionCatalog";
import {
  ROLE_TEMPLATE_OPTIONS,
  permissionsForTemplate,
} from "../../lib/roleTemplates";

export default function RoleFormModal({
  open,
  mode = "edit",
  role,
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
      setForm({
        name: "",
        description: "",
        permissions: permissionsForTemplate("viewer"),
        template: "viewer",
      });
    }
    setFormError("");
  }, [open, role]);

  const selectedPermissions = useMemo(() => new Set(form.permissions), [form.permissions]);

  const togglePermission = (key) => {
    if (readOnly) {
      return;
    }
    setForm((prev) => {
      const next = new Set(prev.permissions);
      if (next.has(key)) {
        next.delete(key);
      } else {
        next.add(key);
      }
      return { ...prev, permissions: [...next], template: "custom" };
    });
  };

  const toggleGroup = (group, checked) => {
    if (readOnly) {
      return;
    }
    const entries = catalogEntriesForGroup(group);
    setForm((prev) => {
      const next = new Set(prev.permissions);
      entries.forEach((entry) => {
        if (checked) {
          next.add(entry.key);
        } else {
          next.delete(entry.key);
        }
      });
      return { ...prev, permissions: [...next], template: "custom" };
    });
  };

  const applyTemplate = (templateId) => {
    if (readOnly || templateId === "custom") {
      setForm((prev) => ({ ...prev, template: templateId }));
      return;
    }
    setForm((prev) => ({
      ...prev,
      template: templateId,
      permissions: permissionsForTemplate(templateId),
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

  const title =
    mode === "view" ? "View Role" : role ? "Edit Role" : "Create Role";

  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <div
        className="modal-card modal-card--wide"
        role="dialog"
        onClick={(e) => e.stopPropagation()}
      >
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
            System roles are protected. You can update the description and permissions, but not the name or delete the role.
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

        {!readOnly ? (
          <section className="form-section">
            <h4>Quick template</h4>
            <div className="role-template-bar">
              {ROLE_TEMPLATE_OPTIONS.map((option) => (
                <button
                  key={option.id}
                  type="button"
                  className={`btn-outline ${form.template === option.id ? "is-selected" : ""}`}
                  onClick={() => applyTemplate(option.id)}
                >
                  {option.label}
                </button>
              ))}
            </div>
          </section>
        ) : null}

        <section className="form-section role-permissions-editor">
          <h4>Permissions</h4>
          <div className="role-permission-groups">
            {PERMISSION_GROUPS.map((group) => {
              const entries = catalogEntriesForGroup(group);
              const checkedCount = entries.filter((entry) => selectedPermissions.has(entry.key)).length;
              const allChecked = checkedCount === entries.length && entries.length > 0;
              const someChecked = checkedCount > 0 && !allChecked;

              return (
                <div key={group.id} className="role-permission-group">
                  <div className="role-permission-group__header">
                    <label className="checkbox-row">
                      <input
                        type="checkbox"
                        checked={allChecked}
                        ref={(input) => {
                          if (input) {
                            input.indeterminate = someChecked;
                          }
                        }}
                        disabled={readOnly}
                        onChange={(e) => toggleGroup(group, e.target.checked)}
                      />
                      <strong>{group.label}</strong>
                    </label>
                    <span className="muted">
                      {checkedCount}/{entries.length}
                    </span>
                  </div>
                  <div className="role-permission-group__items">
                    {entries.map((entry) => (
                      <label key={entry.key} className="checkbox-row">
                        <input
                          type="checkbox"
                          checked={selectedPermissions.has(entry.key)}
                          disabled={readOnly}
                          onChange={() => togglePermission(entry.key)}
                        />
                        <span>{entry.label}</span>
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
