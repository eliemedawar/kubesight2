import { useEffect, useState } from "react";
import DataTable from "../common/DataTable.jsx";
import EmptyState from "../common/EmptyState.jsx";
import {
  createReceiverGroup,
  deleteReceiverGroup,
  updateReceiverGroup,
} from "../../api/alertRoutingApi.js";

const EMPTY_GROUP = {
  name: "",
  description: "",
  enabled: true,
  receiverIds: [],
  emailList: "",
};

function StatusBadge({ ok, label }) {
  return <span className={`routing-status-badge ${ok ? "ok" : "warn"}`}>{label}</span>;
}

function ReceiverTypeBadge({ type }) {
  const labels = { email: "Email", slack: "Slack", webhook: "Webhook" };
  return <span className={`receiver-type-badge receiver-type-${type || "unknown"}`}>{labels[type] || type}</span>;
}

function ReceiverGroupModal({ open, mode, initial, receivers, onClose, onSave, saving, error }) {
  const [form, setForm] = useState(initial);

  useEffect(() => {
    if (open) {
      setForm(initial);
    }
  }, [open, initial]);

  if (!open) {
    return null;
  }

  const toggleReceiver = (id) => {
    setForm((prev) => {
      const current = prev.receiverIds || [];
      const next = current.includes(id) ? current.filter((x) => x !== id) : [...current, id];
      return { ...prev, receiverIds: next };
    });
  };

  const handleSave = () => {
    onSave(form);
  };

  return (
    <div className="modal-overlay" role="presentation" onClick={onClose}>
      <section className="card modal-panel routing-modal" onClick={(e) => e.stopPropagation()}>
        <header className="modal-header">
          <h3>{mode === "edit" ? "Edit receiver group" : "Add receiver group"}</h3>
          <button type="button" className="modal-close" onClick={onClose}>
            <svg width="14" height="14" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"><path fill-rule="evenodd" d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z" clip-rule="evenodd" /></svg>
          </button>
        </header>

        <div className="settings-form alert-routing-form receiver-modal-form">
          <label>
            Group name
            <input value={form.name} onChange={(e) => setForm((p) => ({ ...p, name: e.target.value }))} />
          </label>
          <label className="full-width">
            Description
            <textarea
              rows={2}
              value={form.description || ""}
              onChange={(e) => setForm((p) => ({ ...p, description: e.target.value }))}
            />
          </label>
          <label className="checkbox-label settings-checkbox full-width">
            <input
              type="checkbox"
              checked={form.enabled}
              onChange={(e) => setForm((p) => ({ ...p, enabled: e.target.checked }))}
            />
            Enabled
          </label>

          <fieldset className="alert-routing-fieldset full-width">
            <legend>Members</legend>
            <p className="muted alert-policy-routing-hint">
              Select existing receivers to include in this group.
            </p>
            {receivers.length ? (
              <div className="routing-rule-receiver-list">
                {receivers.map((receiver) => (
                  <label key={receiver.id} className="routing-rule-receiver-option checkbox-label settings-checkbox">
                    <input
                      type="checkbox"
                      checked={(form.receiverIds || []).includes(receiver.id)}
                      onChange={() => toggleReceiver(receiver.id)}
                    />
                    <span className="routing-rule-receiver-meta">
                      <ReceiverTypeBadge type={receiver.type} />
                      <strong>{receiver.name}</strong>
                    </span>
                  </label>
                ))}
              </div>
            ) : (
              <p className="muted">Create individual receivers first, then add them to a group.</p>
            )}
          </fieldset>

          <label className="full-width">
            Quick email list
            <textarea
              rows={4}
              value={form.emailList || ""}
              onChange={(e) => setForm((p) => ({ ...p, emailList: e.target.value }))}
              placeholder={"elie@company.com\nmarc@company.com"}
            />
          </label>
          <p className="muted alert-policy-routing-hint">
            One email per line. New email receivers are created automatically and added to this group.
          </p>
        </div>

        {error ? <p className="routing-error">{error}</p> : null}

        <footer className="modal-actions">
          <button type="button" onClick={onClose} disabled={saving}>
            Cancel
          </button>
          <button type="button" onClick={handleSave} disabled={saving}>
            {saving ? "Saving..." : "Save group"}
          </button>
        </footer>
      </section>
    </div>
  );
}

export default function ReceiverGroupsPanel({ groups, receivers, onChanged }) {
  const [modalOpen, setModalOpen] = useState(false);
  const [modalMode, setModalMode] = useState("create");
  const [editing, setEditing] = useState(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  const openCreate = () => {
    setModalMode("create");
    setEditing(EMPTY_GROUP);
    setError("");
    setModalOpen(true);
  };

  const openEdit = (group) => {
    setModalMode("edit");
    setEditing({
      ...group,
      receiverIds: group.receiverIds || [],
      emailList: "",
    });
    setError("");
    setModalOpen(true);
  };

  const save = async (form) => {
    setSaving(true);
    setError("");
    try {
      const payload = {
        name: form.name,
        description: form.description,
        enabled: form.enabled,
        receiverIds: form.receiverIds || [],
        emailList: form.emailList || "",
      };
      if (modalMode === "edit" && editing?.id) {
        await updateReceiverGroup(editing.id, payload);
      } else {
        await createReceiverGroup(payload);
      }
      setModalOpen(false);
      onChanged();
    } catch (err) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  };

  const remove = async (id) => {
    if (!window.confirm("Delete this receiver group?")) {
      return;
    }
    await deleteReceiverGroup(id);
    onChanged();
  };

  const tableRows = groups.map((group) => ({
    id: group.id,
    name: group.name,
    memberCount: group.memberCount ?? (group.receiverIds || []).length,
    assignedPolicies:
      (group.assignedPolicyNames || []).length > 0 ? group.assignedPolicyNames.join(", ") : "—",
    statusDisplay: <StatusBadge ok={group.enabled} label={group.enabled ? "Enabled" : "Disabled"} />,
    actionsDisplay: (
      <div className="table-actions">
        <button type="button" onClick={() => openEdit(group)}>
          Edit
        </button>
        <button type="button" className="danger" onClick={() => remove(group.id)}>
          Delete
        </button>
      </div>
    ),
  }));

  const columns = [
    { key: "name", label: "Group" },
    { key: "memberCount", label: "Members" },
    { key: "assignedPolicies", label: "Assigned Policies" },
    { key: "statusDisplay", label: "Status" },
    { key: "actionsDisplay", label: "" },
  ];

  return (
    <section className="card alert-routing-panel">
      <header className="alert-routing-panel-header">
        <div>
          <h3>Receiver groups</h3>
          <p className="muted">Bundle receivers into teams such as DevOps, Security, or Management.</p>
        </div>
        <button type="button" onClick={openCreate}>
          Add group
        </button>
      </header>

      {groups.length ? (
        <DataTable columns={columns} rows={tableRows} />
      ) : (
        <EmptyState message="Create a group to notify a team with one selection on alert policies." />
      )}

      <ReceiverGroupModal
        open={modalOpen}
        mode={modalMode}
        initial={editing || EMPTY_GROUP}
        receivers={receivers}
        onClose={() => setModalOpen(false)}
        onSave={save}
        saving={saving}
        error={error}
      />
    </section>
  );
}
