import { useCallback, useEffect, useMemo, useState } from "react";
import AccessScopeView from "../components/common/AccessScopeView.jsx";
import PageTitle from "../components/common/PageTitle.jsx";
import EmptyState from "../components/common/EmptyState.jsx";
import DataTable from "../components/common/DataTable.jsx";
import ErrorBanner from "../components/common/ErrorBanner.jsx";
import {
  createAlertPolicy,
  deleteAlertPolicy,
  getAlertPolicyCatalog,
  listAlertHistory,
  listAlertPolicies,
  setAlertPolicyEnabled,
  updateAlertPolicy,
} from "../api/alertPoliciesApi.js";
import { EMPTY_MESSAGES } from "../utils/authz.js";

const EMPTY_POLICY = {
  name: "",
  clusterId: "",
  description: "",
  enabled: true,
  severity: "warning",
  conditionLogic: "any",
  conditions: [{ metricKey: "cpu_usage_percent", operator: ">", threshold: 70 }],
  scope: { type: "cluster", namespace: "", resourceName: "" },
  notificationChannels: [{ channel: "dashboard" }],
};

function channelLabel(channel) {
  const labels = { dashboard: "Dashboard", email: "Email", slack: "Slack", webhook: "Webhook" };
  return labels[channel] || channel;
}

function PolicyFormModal({
  open,
  mode,
  initial,
  catalog,
  clusterOptions,
  onClose,
  onSave,
  saving,
  error,
}) {
  const [form, setForm] = useState(initial);

  useEffect(() => {
    if (open) {
      setForm(initial);
    }
  }, [open, initial]);

  if (!open) {
    return null;
  }

  const metrics = catalog?.metrics || [];
  const operatorsFor = (metricKey) => {
    const metric = metrics.find((m) => m.key === metricKey);
    return catalog?.operators?.[metric?.type] || [">"];
  };

  const updateCondition = (index, patch) => {
    setForm((prev) => {
      const conditions = [...(prev.conditions || [])];
      conditions[index] = { ...conditions[index], ...patch };
      return { ...prev, conditions };
    });
  };

  const addCondition = () => {
    setForm((prev) => ({
      ...prev,
      conditions: [
        ...(prev.conditions || []),
        { metricKey: metrics[0]?.key || "cpu_usage_percent", operator: ">", threshold: 70 },
      ],
    }));
  };

  const removeCondition = (index) => {
    setForm((prev) => ({
      ...prev,
      conditions: (prev.conditions || []).filter((_, i) => i !== index),
    }));
  };

  const toggleChannel = (channel) => {
    setForm((prev) => {
      const current = prev.notificationChannels || [];
      const exists = current.some((c) => c.channel === channel);
      const next = exists
        ? current.filter((c) => c.channel !== channel)
        : [...current, { channel }];
      return { ...prev, notificationChannels: next.length ? next : [{ channel: "dashboard" }] };
    });
  };

  const handleSubmit = (event) => {
    event.preventDefault();
    onSave(form);
  };

  return (
    <div className="modal-backdrop" role="presentation" onClick={onClose}>
      <form
        className="modal-card alert-policy-modal"
        onClick={(e) => e.stopPropagation()}
        onSubmit={handleSubmit}
      >
        <header className="modal-header">
          <h2>{mode === "edit" ? "Edit Alert Policy" : "Create Alert Policy"}</h2>
          <p className="muted">Define cluster-level conditions, logic, and notification channels.</p>
        </header>

        {error ? <ErrorBanner message={error} /> : null}

        <div className="alert-policy-form-grid">
          <label>
            Policy Name
            <input
              value={form.name}
              onChange={(e) => setForm((p) => ({ ...p, name: e.target.value }))}
              required
            />
          </label>
          <label>
            Cluster
            <select
              value={form.clusterId}
              onChange={(e) => setForm((p) => ({ ...p, clusterId: e.target.value }))}
              required
            >
              <option value="">Select cluster</option>
              {clusterOptions.map((c) => (
                <option key={c.id} value={c.id}>
                  {c.name}
                </option>
              ))}
            </select>
          </label>
          <label className="full-width">
            Description
            <textarea
              rows={2}
              value={form.description || ""}
              onChange={(e) => setForm((p) => ({ ...p, description: e.target.value }))}
            />
          </label>
          <label>
            Severity
            <select
              value={form.severity}
              onChange={(e) => setForm((p) => ({ ...p, severity: e.target.value }))}
            >
              <option value="info">Info</option>
              <option value="warning">Warning</option>
              <option value="critical">Critical</option>
            </select>
          </label>
          <label>
            Status
            <select
              value={form.enabled ? "enabled" : "disabled"}
              onChange={(e) => setForm((p) => ({ ...p, enabled: e.target.value === "enabled" }))}
            >
              <option value="enabled">Enabled</option>
              <option value="disabled">Disabled</option>
            </select>
          </label>
        </div>

        <section className="alert-policy-section">
          <h3>Scope</h3>
          <div className="alert-policy-form-grid">
            <label>
              Target
              <select
                value={form.scope?.type || "cluster"}
                onChange={(e) =>
                  setForm((p) => ({
                    ...p,
                    scope: { ...p.scope, type: e.target.value },
                  }))
                }
              >
                <option value="cluster">Entire Cluster</option>
                <option value="namespace">Namespace</option>
                <option value="deployment">Deployment</option>
                <option value="pod">Pod</option>
              </select>
            </label>
            {form.scope?.type !== "cluster" ? (
              <label>
                Namespace
                <input
                  value={form.scope?.namespace || ""}
                  onChange={(e) =>
                    setForm((p) => ({
                      ...p,
                      scope: { ...p.scope, namespace: e.target.value },
                    }))
                  }
                  required
                />
              </label>
            ) : null}
            {["deployment", "pod"].includes(form.scope?.type) ? (
              <label>
                Resource Name
                <input
                  value={form.scope?.resourceName || ""}
                  onChange={(e) =>
                    setForm((p) => ({
                      ...p,
                      scope: { ...p.scope, resourceName: e.target.value },
                    }))
                  }
                  required
                />
              </label>
            ) : null}
          </div>
        </section>

        <section className="alert-policy-section">
          <div className="alert-policy-section-header">
            <h3>Conditions</h3>
            <button type="button" className="btn-text" onClick={addCondition}>
              + Add condition
            </button>
          </div>
          <div className="alert-policy-conditions">
            {(form.conditions || []).map((condition, index) => {
              const metric = metrics.find((m) => m.key === condition.metricKey);
              const isBoolean = metric?.type === "boolean";
              return (
                <div key={index} className="alert-policy-condition-row">
                  <select
                    value={condition.metricKey}
                    onChange={(e) =>
                      updateCondition(index, {
                        metricKey: e.target.value,
                        operator: operatorsFor(e.target.value)[0],
                        threshold: metrics.find((m) => m.key === e.target.value)?.type === "boolean" ? true : 70,
                      })
                    }
                  >
                    {metrics.map((m) => (
                      <option key={m.key} value={m.key}>
                        {m.label}
                      </option>
                    ))}
                  </select>
                  <select
                    value={condition.operator}
                    onChange={(e) => updateCondition(index, { operator: e.target.value })}
                  >
                    {operatorsFor(condition.metricKey).map((op) => (
                      <option key={op} value={op}>
                        {op}
                      </option>
                    ))}
                  </select>
                  {isBoolean ? (
                    <select
                      value={String(condition.threshold)}
                      onChange={(e) =>
                        updateCondition(index, { threshold: e.target.value === "true" })
                      }
                    >
                      <option value="true">True</option>
                      <option value="false">False</option>
                    </select>
                  ) : (
                    <input
                      type="number"
                      value={condition.threshold}
                      onChange={(e) => updateCondition(index, { threshold: Number(e.target.value) })}
                    />
                  )}
                  <button
                    type="button"
                    className="btn-text danger-text"
                    onClick={() => removeCondition(index)}
                    disabled={(form.conditions || []).length <= 1}
                  >
                    Remove
                  </button>
                </div>
              );
            })}
          </div>
          <label className="alert-policy-logic">
            Logic
            <select
              value={form.conditionLogic}
              onChange={(e) => setForm((p) => ({ ...p, conditionLogic: e.target.value }))}
            >
              <option value="any">ANY condition matches (OR)</option>
              <option value="all">ALL conditions must match (AND)</option>
            </select>
          </label>
        </section>

        <section className="alert-policy-section">
          <h3>Notifications</h3>
          <div className="alert-policy-channels">
            {(catalog?.notificationChannels || ["dashboard", "email", "slack", "webhook"]).map(
              (channel) => {
                const checked = (form.notificationChannels || []).some((c) => c.channel === channel);
                return (
                  <label key={channel} className="alert-policy-channel">
                    <input
                      type="checkbox"
                      checked={checked}
                      onChange={() => toggleChannel(channel)}
                    />
                    {channelLabel(channel)}
                  </label>
                );
              }
            )}
          </div>
        </section>

        <footer className="modal-footer">
          <button type="button" className="btn-outline" onClick={onClose}>
            Cancel
          </button>
          <button type="submit" className="primary" disabled={saving}>
            {saving ? "Saving..." : "Save Policy"}
          </button>
        </footer>
      </form>
    </div>
  );
}

export default function AlertPoliciesPage({
  clusterId,
  clusterOptions = [],
  hasClusters,
  canManage = false,
  coreLoading = false,
  accessError = "",
}) {
  const [catalog, setCatalog] = useState(null);
  const [policies, setPolicies] = useState([]);
  const [history, setHistory] = useState([]);
  const [activeTab, setActiveTab] = useState("policies");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [modalOpen, setModalOpen] = useState(false);
  const [modalMode, setModalMode] = useState("create");
  const [editing, setEditing] = useState(null);
  const [saving, setSaving] = useState(false);
  const [modalError, setModalError] = useState("");

  const loadData = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [catalogRes, policiesRes, historyRes] = await Promise.all([
        getAlertPolicyCatalog(),
        listAlertPolicies(clusterId ? { cluster: clusterId } : {}),
        listAlertHistory(clusterId ? { cluster: clusterId, limit: 100 } : { limit: 100 }),
      ]);
      setCatalog(catalogRes);
      setPolicies(policiesRes.items || []);
      setHistory(historyRes.items || []);
    } catch (loadError) {
      setError(loadError.message);
      setPolicies([]);
      setHistory([]);
    } finally {
      setLoading(false);
    }
  }, [clusterId]);

  useEffect(() => {
    loadData();
  }, [loadData]);

  const openCreate = () => {
    setModalMode("create");
    setEditing({
      ...EMPTY_POLICY,
      clusterId: clusterId || clusterOptions[0]?.id || "",
    });
    setModalError("");
    setModalOpen(true);
  };

  const openEdit = (policy) => {
    setModalMode("edit");
    setEditing({
      ...policy,
      scope: policy.scope || { type: "cluster" },
      notificationChannels: policy.notificationChannels || [{ channel: "dashboard" }],
    });
    setModalError("");
    setModalOpen(true);
  };

  const handleSave = async (form) => {
    setSaving(true);
    setModalError("");
    try {
      if (modalMode === "create") {
        await createAlertPolicy(form);
      } else {
        await updateAlertPolicy(editing.id, form);
      }
      setModalOpen(false);
      await loadData();
    } catch (saveError) {
      setModalError(saveError.message);
    } finally {
      setSaving(false);
    }
  };

  const handleToggle = async (policy) => {
    try {
      await setAlertPolicyEnabled(policy.id, !policy.enabled);
      await loadData();
    } catch (toggleError) {
      setError(toggleError.message);
    }
  };

  const handleDelete = async (policy) => {
    if (!window.confirm(`Delete policy "${policy.name}"?`)) {
      return;
    }
    try {
      await deleteAlertPolicy(policy.id);
      await loadData();
    } catch (deleteError) {
      setError(deleteError.message);
    }
  };

  const policyRows = useMemo(
    () =>
      policies.map((policy) => ({
        id: policy.id,
        name: policy.name,
        cluster: policy.clusterId,
        severity: policy.severity,
        status: policy.enabled ? "Enabled" : "Disabled",
        logic: policy.conditionLogic === "all" ? "ALL" : "ANY",
        conditions: `${(policy.conditions || []).length} rule(s)`,
        channels: (policy.notificationChannels || []).map((c) => channelLabel(c.channel)).join(", "),
        actions: policy,
      })),
    [policies]
  );

  const historyRows = useMemo(
    () =>
      history.map((row) => ({
        id: row.id,
        time: row.firedAt ? new Date(row.firedAt).toLocaleString() : "—",
        cluster: row.clusterId,
        namespace: row.namespace || "—",
        resource: [row.resourceType, row.resourceName].filter(Boolean).join("/") || "—",
        policy: row.policyName || "—",
        severity: row.severity,
        status: row.status === "active" ? "Active" : "Resolved",
        conditions: (row.triggeredConditions || [])
          .filter((c) => c.matched)
          .map((c) => `${c.metricLabel || c.metricKey} ${c.operator} ${c.threshold}`)
          .join("; ") || "—",
      })),
    [history]
  );

  const header = (
    <div className="card-header-row">
      <PageTitle
        title="Alert Policies"
        subtitle="Define cluster-level alert rules evaluated continuously against metrics and workload health."
      />
      {canManage ? (
        <button type="button" className="primary" onClick={openCreate} disabled={!hasClusters}>
          Create Policy
        </button>
      ) : null}
    </div>
  );

  return (
    <div className="ops-page alert-policies-page">
      <AccessScopeView
        coreLoading={coreLoading}
        loading={loading}
        accessError={accessError || error}
        empty={!hasClusters}
        emptyMessage={EMPTY_MESSAGES.noClusters}
        header={header}
      >
        <nav className="tab-bar" aria-label="alert-policy-tabs">
          <button
            type="button"
            className={activeTab === "policies" ? "active" : ""}
            onClick={() => setActiveTab("policies")}
          >
            Policies
          </button>
          <button
            type="button"
            className={activeTab === "history" ? "active" : ""}
            onClick={() => setActiveTab("history")}
          >
            Alert History
          </button>
        </nav>

        {activeTab === "policies" ? (
          policyRows.length ? (
            <DataTable
              tableClassName="alert-policies-table"
              columns={[
                { key: "name", label: "Policy" },
                { key: "cluster", label: "Cluster" },
                { key: "severity", label: "Severity" },
                { key: "status", label: "Status" },
                { key: "logic", label: "Logic" },
                { key: "conditions", label: "Conditions" },
                { key: "channels", label: "Notifications" },
                { key: "actions", label: "Actions" },
              ]}
              rows={policyRows.map((row) => ({
                ...row,
                actions: canManage ? (
                  <div className="inventory-actions-cell">
                    <button type="button" className="btn-text" onClick={() => openEdit(row.actions)}>
                      Edit
                    </button>
                    <button type="button" className="btn-text" onClick={() => handleToggle(row.actions)}>
                      {row.actions.enabled ? "Disable" : "Enable"}
                    </button>
                    <button
                      type="button"
                      className="btn-text danger-text"
                      onClick={() => handleDelete(row.actions)}
                    >
                      Delete
                    </button>
                  </div>
                ) : (
                  <span className="muted">—</span>
                ),
              }))}
            />
          ) : (
            <EmptyState
              message="No alert policies configured yet."
              hint={canManage ? "Create a policy to start monitoring cluster metrics and workload health." : undefined}
            />
          )
        ) : historyRows.length ? (
          <DataTable
            tableClassName="alert-history-table"
            columns={[
              { key: "time", label: "Time" },
              { key: "cluster", label: "Cluster" },
              { key: "namespace", label: "Namespace" },
              { key: "resource", label: "Resource" },
              { key: "policy", label: "Policy" },
              { key: "conditions", label: "Triggered Conditions" },
              { key: "severity", label: "Severity" },
              { key: "status", label: "Status" },
            ]}
            rows={historyRows}
          />
        ) : (
          <EmptyState message="No alert history yet." hint="Triggered policies will appear here." />
        )}
      </AccessScopeView>

      <PolicyFormModal
        open={modalOpen}
        mode={modalMode}
        initial={editing || EMPTY_POLICY}
        catalog={catalog}
        clusterOptions={clusterOptions}
        onClose={() => setModalOpen(false)}
        onSave={handleSave}
        saving={saving}
        error={modalError}
      />
    </div>
  );
}
