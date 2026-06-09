import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
import PolicyScopeFields, {
  ALL_RESOURCES_VALUE,
  normalizeScope,
} from "../components/alerts/PolicyScopeFields.jsx";
import { EMPTY_MESSAGES } from "../utils/authz.js";
import {
  applyLogPolicyDefaults,
  LOG_DEFAULT_PATTERN,
  LOG_TOUCH_KEYS,
  logWindowSecondsForEvaluationInterval,
  shouldShowLogReceiverWarning,
} from "../lib/logPolicyDefaults.js";

const RECEIVER_TYPE_LABELS = {
  email: "Email",
  slack: "Slack",
  webhook: "Webhook",
};

function ReceiverTypeBadge({ type }) {
  const label = RECEIVER_TYPE_LABELS[type] || type || "Unknown";
  return <span className={`receiver-type-badge receiver-type-${type || "unknown"}`}>{label}</span>;
}

function formatReceiverList(names) {
  if (!names?.length) {
    return "—";
  }
  if (names.length <= 2) {
    return names.join(", ");
  }
  return `${names.slice(0, 2).join(", ")} +${names.length - 2} more`;
}

function formatNotificationDestinations(policy) {
  const groups = policy?.receiverGroupNames || [];
  const receivers = policy?.receiverNames || [];
  const combined = [...groups, ...receivers];
  return formatReceiverList(combined);
}

const DEFAULT_EVALUATION_INTERVAL_SECONDS = 300;

const EVALUATION_INTERVAL_OPTIONS = [
  { seconds: 60, label: "1 minute" },
  { seconds: 300, label: "5 minutes" },
  { seconds: 600, label: "10 minutes" },
  { seconds: 900, label: "15 minutes" },
  { seconds: 1800, label: "30 minutes" },
  { seconds: 3600, label: "1 hour" },
];

function formatEvaluationInterval(policy) {
  return policy?.evaluationIntervalLabel || "Every 5 min";
}

function formatEvaluationIntervalShort(policy) {
  return formatEvaluationInterval(policy).replace(/^Every\s+/i, "");
}

function PolicyLastEvalCell({ policy }) {
  const checked = formatLastChecked(policy);
  const value = formatLastValue(policy);
  return (
    <div className="policy-last-eval-cell">
      <span className="policy-last-eval-time">{checked}</span>
      {value !== "—" ? <span className="policy-last-eval-value muted">{value}</span> : null}
      <PolicyLastResult policy={policy} />
    </div>
  );
}

function PolicyRowActions({ policy, onEdit, onToggle, onDelete }) {
  return (
    <div className="inventory-actions-cell policy-table-actions" onClick={(e) => e.stopPropagation()}>
      <button type="button" className="btn-text" onClick={() => onEdit(policy)}>
        Edit
      </button>
      <button type="button" className="btn-text" onClick={() => onToggle(policy)}>
        {policy.enabled ? "Disable" : "Enable"}
      </button>
      <button type="button" className="btn-text danger-text" onClick={() => onDelete(policy)}>
        Delete
      </button>
    </div>
  );
}

function formatLastChecked(policy) {
  if (!policy?.lastEvaluatedAt) {
    return "—";
  }
  const raw = policy.lastEvaluatedAt;
  const normalized = /[Zz]|[+-]\d{2}:\d{2}$/.test(raw) ? raw : `${raw}Z`;
  return new Date(normalized).toLocaleString();
}

function formatLastValue(policy) {
  const measured = policy?.lastMeasuredValue;
  const threshold = policy?.lastThreshold;
  if (!measured && !threshold) {
    return "—";
  }
  if (measured && threshold) {
    return `${measured} (${threshold})`;
  }
  return measured || threshold || "—";
}

const LAST_RESULT_LABELS = {
  met: "Met",
  not_met: "Not met",
  error: "Error",
};

function PolicyLastResult({ policy }) {
  const result = policy?.lastResult;
  if (!result) {
    return <span className="muted">—</span>;
  }
  const label = LAST_RESULT_LABELS[result] || result;
  const title = result === "error" ? policy?.lastEvaluationError || undefined : undefined;
  return (
    <span className={`policy-eval-result policy-eval-result-${result}`} title={title}>
      {label}
    </span>
  );
}

const DEFAULT_LOG_CONFIG = {
  matchType: "contains",
  pattern: LOG_DEFAULT_PATTERN,
  caseSensitive: false,
  logWindowSeconds: 60,
  contextLinesBefore: 5,
  contextLinesAfter: 5,
  maxLines: 20,
};

const EMPTY_POLICY = {
  name: "",
  clusterId: "",
  description: "",
  enabled: true,
  alertType: "metric",
  severity: "warning",
  conditionLogic: "any",
  conditions: [{ metricKey: "cpu_usage_percent", operator: ">", threshold: 70 }],
  logConfig: { ...DEFAULT_LOG_CONFIG },
  scope: { type: "deployment", namespace: "", resourceName: ALL_RESOURCES_VALUE },
  showOnDashboard: true,
  receiverIds: [],
  receiverGroupIds: [],
  evaluationIntervalSeconds: DEFAULT_EVALUATION_INTERVAL_SECONDS,
};

function PolicyFormModal({
  open,
  mode,
  initial,
  catalog,
  clusterOptions,
  activeNamespace,
  namespaceOptions,
  onClose,
  onSave,
  saving,
  error,
}) {
  const [form, setForm] = useState(initial);
  const touchedRef = useRef(new Set());

  const touch = (key) => {
    touchedRef.current.add(key);
  };

  const logDefaultsContext = useMemo(
    () => ({
      activeNamespace,
      namespaceOptions,
      receiverGroups: catalog?.receiverGroups || [],
      defaultEvaluationIntervalSeconds: DEFAULT_EVALUATION_INTERVAL_SECONDS,
    }),
    [activeNamespace, catalog?.receiverGroups, namespaceOptions]
  );

  useEffect(() => {
    if (open) {
      setForm(initial);
      touchedRef.current = new Set();
    }
  }, [open, initial]);

  const applyLogDefaults = (previous) =>
    applyLogPolicyDefaults(previous, logDefaultsContext, touchedRef.current);

  const handleAlertTypeChange = (nextType) => {
    if (nextType === "log" && mode === "create") {
      setForm((previous) => applyLogDefaults({ ...previous, alertType: "log" }));
      return;
    }
    setForm((previous) => ({
      ...previous,
      alertType: nextType,
      logConfig: previous.logConfig || { ...DEFAULT_LOG_CONFIG },
    }));
  };

  const handleEvaluationIntervalChange = (seconds) => {
    touch(LOG_TOUCH_KEYS.evaluationInterval);
    setForm((previous) => {
      const next = { ...previous, evaluationIntervalSeconds: seconds };
      if (
        previous.alertType === "log" &&
        mode === "create" &&
        !touchedRef.current.has(LOG_TOUCH_KEYS.logWindow) &&
        !touchedRef.current.has(LOG_TOUCH_KEYS.logConfig)
      ) {
        next.logConfig = {
          ...(previous.logConfig || {}),
          logWindowSeconds: logWindowSecondsForEvaluationInterval(seconds),
        };
      }
      return next;
    });
  };

  if (!open) {
    return null;
  }

  const metrics = catalog?.metrics || [];
  const isLogPolicy = form.alertType === "log";
  const logConfig = form.logConfig || catalog?.defaultLogConfig || DEFAULT_LOG_CONFIG;
  const logWindowOptions = catalog?.logWindowOptions || [
    { seconds: 60, label: "Last 1 minute" },
    { seconds: 300, label: "Last 5 minutes" },
    { seconds: 900, label: "Last 15 minutes" },
    { seconds: 1800, label: "Last 30 minutes" },
  ];
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

  const handleSubmit = (event) => {
    event.preventDefault();
    onSave(form);
  };

  const receivers = catalog?.receivers || [];
  const receiverGroups = catalog?.receiverGroups || [];
  const showReceiverWarning = mode === "create" && shouldShowLogReceiverWarning(form);

  const toggleReceiver = (receiverId) => {
    touch(LOG_TOUCH_KEYS.receivers);
    setForm((prev) => {
      const current = prev.receiverIds || [];
      const next = current.includes(receiverId)
        ? current.filter((id) => id !== receiverId)
        : [...current, receiverId];
      return { ...prev, receiverIds: next };
    });
  };

  const toggleReceiverGroup = (groupId) => {
    touch(LOG_TOUCH_KEYS.receivers);
    setForm((prev) => {
      const current = prev.receiverGroupIds || [];
      const next = current.includes(groupId)
        ? current.filter((id) => id !== groupId)
        : [...current, groupId];
      return { ...prev, receiverGroupIds: next };
    });
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
          <p className="muted">Define when alerts should fire based on metrics or pod log patterns.</p>
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
              onChange={(e) => {
                touch(LOG_TOUCH_KEYS.scope);
                setForm((p) => ({
                  ...p,
                  clusterId: e.target.value,
                  scope: {
                    ...normalizeScope(p.scope),
                    namespace: "",
                    resourceName: ALL_RESOURCES_VALUE,
                  },
                }));
              }}
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
            Alert Type
            <select
              value={form.alertType || "metric"}
              onChange={(e) => handleAlertTypeChange(e.target.value)}
            >
              <option value="metric">Metric</option>
              <option value="log">Log</option>
            </select>
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
          <label>
            Evaluation Interval
            <select
              value={form.evaluationIntervalSeconds ?? DEFAULT_EVALUATION_INTERVAL_SECONDS}
              onChange={(e) => handleEvaluationIntervalChange(Number(e.target.value))}
            >
              {(catalog?.evaluationIntervals || EVALUATION_INTERVAL_OPTIONS).map((option) => (
                <option key={option.seconds} value={option.seconds}>
                  Every {option.label}
                </option>
              ))}
            </select>
          </label>
        </div>

        <section className="alert-policy-section">
          <h3>Scope</h3>
          <p className="muted alert-policy-routing-hint">
            Choose a namespace and deployment or pod. Use the all-resources option to evaluate every
            workload in the namespace.
          </p>
          <PolicyScopeFields
            clusterId={form.clusterId}
            scope={form.scope}
            onChange={(scope) => {
              touch(LOG_TOUCH_KEYS.scope);
              setForm((p) => ({ ...p, scope }));
            }}
          />
        </section>

        {!isLogPolicy ? (
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
        ) : (
        <section className="alert-policy-section">
          <h3>Pattern Matching</h3>
          <div className="alert-policy-form-grid">
            <label>
              Match Type
              <select
                value={logConfig.matchType || "contains"}
                onChange={(e) => {
                  touch(LOG_TOUCH_KEYS.logMatchType);
                  setForm((p) => ({
                    ...p,
                    logConfig: { ...(p.logConfig || logConfig), matchType: e.target.value },
                  }));
                }}
              >
                <option value="contains">Contains</option>
                <option value="regex">Regex</option>
              </select>
            </label>
            <label>
              Pattern
              <input
                value={logConfig.pattern || ""}
                onChange={(e) => {
                  touch(LOG_TOUCH_KEYS.logPattern);
                  setForm((p) => ({
                    ...p,
                    logConfig: { ...(p.logConfig || logConfig), pattern: e.target.value },
                  }));
                }}
                placeholder="ERROR, Exception, failed, timeout"
                required
              />
            </label>
            <label className="full-width checkbox-label settings-checkbox">
              <input
                type="checkbox"
                checked={Boolean(logConfig.caseSensitive)}
                onChange={(e) => {
                  touch(LOG_TOUCH_KEYS.logCaseSensitive);
                  setForm((p) => ({
                    ...p,
                    logConfig: { ...(p.logConfig || logConfig), caseSensitive: e.target.checked },
                  }));
                }}
              />
              Case Sensitive
            </label>
          </div>

          <h3 className="alert-policy-subheading">Log Window</h3>
          <label>
            Scan Logs From
            <select
              value={logConfig.logWindowSeconds ?? 60}
              onChange={(e) => {
                touch(LOG_TOUCH_KEYS.logWindow);
                setForm((p) => ({
                  ...p,
                  logConfig: { ...(p.logConfig || logConfig), logWindowSeconds: Number(e.target.value) },
                }));
              }}
            >
              {logWindowOptions.map((option) => (
                <option key={option.seconds} value={option.seconds}>
                  {option.label}
                </option>
              ))}
            </select>
          </label>

          <h3 className="alert-policy-subheading">Context Lines</h3>
          <div className="alert-policy-form-grid">
            <label>
              Lines before match
              <input
                type="number"
                min={0}
                max={50}
                value={logConfig.contextLinesBefore ?? 5}
                onChange={(e) => {
                  touch(LOG_TOUCH_KEYS.logContext);
                  setForm((p) => ({
                    ...p,
                    logConfig: { ...(p.logConfig || logConfig), contextLinesBefore: Number(e.target.value) },
                  }));
                }}
              />
            </label>
            <label>
              Lines after match
              <input
                type="number"
                min={0}
                max={50}
                value={logConfig.contextLinesAfter ?? 5}
                onChange={(e) => {
                  touch(LOG_TOUCH_KEYS.logContext);
                  setForm((p) => ({
                    ...p,
                    logConfig: { ...(p.logConfig || logConfig), contextLinesAfter: Number(e.target.value) },
                  }));
                }}
              />
            </label>
            <label>
              Max lines in notification
              <input
                type="number"
                min={1}
                max={200}
                value={logConfig.maxLines ?? 20}
                onChange={(e) => {
                  touch(LOG_TOUCH_KEYS.logContext);
                  setForm((p) => ({
                    ...p,
                    logConfig: { ...(p.logConfig || logConfig), maxLines: Number(e.target.value) },
                  }));
                }}
              />
            </label>
          </div>
        </section>
        )}

        <section className="alert-policy-section">
          <h3>Notification Receivers</h3>
          <p className="muted alert-policy-routing-hint">
            Choose where to send notifications when this policy fires. Configure receivers under
            Administration → Alert Routing.
          </p>
          {showReceiverWarning ? (
            <p className="alert-policy-receiver-warning" role="status">
              Select at least one receiver or receiver group.
            </p>
          ) : null}
          {receiverGroups.length ? (
            <>
              <h4 className="alert-policy-subheading">Receiver Groups</h4>
              <div className="routing-rule-receiver-list">
                {receiverGroups.map((group) => (
                  <label key={group.id} className="routing-rule-receiver-option checkbox-label settings-checkbox">
                    <input
                      type="checkbox"
                      checked={(form.receiverGroupIds || []).includes(group.id)}
                      onChange={() => toggleReceiverGroup(group.id)}
                    />
                    <span className="routing-rule-receiver-meta">
                      <strong>{group.name}</strong>
                      <span className="muted">
                        {(group.memberCount || 0)} member{(group.memberCount || 0) === 1 ? "" : "s"}
                      </span>
                    </span>
                  </label>
                ))}
              </div>
            </>
          ) : null}

          {receivers.length ? (
            <>
              <h4 className="alert-policy-subheading">Individual Receivers</h4>
              <div className="routing-rule-receiver-list">
                {receivers.map((receiver) => (
                  <label key={receiver.id} className="routing-rule-receiver-option checkbox-label settings-checkbox">
                    <input
                      type="checkbox"
                      checked={(form.receiverIds || []).includes(receiver.id)}
                      onChange={() => toggleReceiver(receiver.id)}
                    />
                    <span className="routing-rule-receiver-meta">
                      <strong>{receiver.name}</strong>
                      <ReceiverTypeBadge type={receiver.type} />
                      <span className="muted">{receiver.destination || "—"}</span>
                    </span>
                  </label>
                ))}
              </div>
            </>
          ) : null}

          {!receiverGroups.length && !receivers.length ? (
            <p className="muted">No receivers configured yet. Add receivers or groups in Alert Routing.</p>
          ) : null}
        </section>

        <section className="alert-policy-section">
          <h3>Dashboard</h3>
          <label className="alert-policy-channel checkbox-label settings-checkbox">
            <input
              type="checkbox"
              checked={form.showOnDashboard !== false}
              onChange={(e) => setForm((p) => ({ ...p, showOnDashboard: e.target.checked }))}
            />
            Create dashboard alert
          </label>
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
  selectedNamespace = "",
  allowedNamespaces = [],
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

  useEffect(() => {
    if (activeTab !== "policies" || !clusterId) {
      return undefined;
    }
    const timer = window.setInterval(() => {
      loadData();
    }, 30000);
    return () => window.clearInterval(timer);
  }, [activeTab, clusterId, loadData]);

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
      alertType: policy.alertType || "metric",
      logConfig: policy.logConfig || { ...DEFAULT_LOG_CONFIG },
      scope: normalizeScope(policy.scope),
      showOnDashboard: policy.showOnDashboard !== false,
      receiverIds: policy.receiverIds || [],
      receiverGroupIds: policy.receiverGroupIds || [],
      evaluationIntervalSeconds:
        policy.evaluationIntervalSeconds ?? DEFAULT_EVALUATION_INTERVAL_SECONDS,
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
        alertType: policy.alertType === "log" ? "Log" : "Metric",
        severity: policy.severity,
        status: policy.enabled ? "Enabled" : "Disabled",
        logic: policy.alertType === "log" ? "—" : policy.conditionLogic === "all" ? "ALL" : "ANY",
        conditions:
          policy.alertType === "log"
            ? policy.logConfig?.pattern || "—"
            : `${(policy.conditions || []).length} rule(s)`,
        receivers: formatNotificationDestinations(policy),
        evaluationInterval: formatEvaluationIntervalShort(policy),
        lastEval: <PolicyLastEvalCell policy={policy} />,
        actions: policy,
      })),
    [policies]
  );

  const policyTableColumns = useMemo(() => {
    const columns = [
      { key: "name", label: "Policy" },
      ...(clusterId ? [] : [{ key: "cluster", label: "Cluster" }]),
      { key: "alertType", label: "Type" },
      { key: "severity", label: "Severity" },
      { key: "status", label: "Status" },
      { key: "conditions", label: "Conditions" },
      { key: "receivers", label: "Receivers" },
      { key: "evaluationInterval", label: "Interval" },
      { key: "lastEval", label: "Last evaluation" },
    ];
    if (canManage) {
      columns.push({ key: "actions", label: "Actions" });
    }
    return columns;
  }, [canManage, clusterId]);

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
        conditions: row.alertType === "log"
          ? row.matchedPattern || "—"
          : (row.triggeredConditions || [])
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
              columns={policyTableColumns}
              rows={policyRows.map((row) => ({
                ...row,
                actions: canManage ? (
                  <PolicyRowActions
                    policy={row.actions}
                    onEdit={openEdit}
                    onToggle={handleToggle}
                    onDelete={handleDelete}
                  />
                ) : null,
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
        activeNamespace={selectedNamespace}
        namespaceOptions={allowedNamespaces}
        onClose={() => setModalOpen(false)}
        onSave={handleSave}
        saving={saving}
        error={modalError}
      />
    </div>
  );
}
