import { useCallback, useEffect, useState } from "react";
import PageTitle from "../components/common/PageTitle.jsx";
import ErrorBanner from "../components/common/ErrorBanner.jsx";
import DataTable from "../components/common/DataTable.jsx";
import EmptyState from "../components/common/EmptyState.jsx";
import ReceiverGroupsPanel from "../components/alerts/ReceiverGroupsPanel.jsx";
import {
  createReceiver,
  deleteReceiver,
  getSmtpSettings,
  listDeliveryLogs,
  listReceiverGroups,
  listReceivers,
  saveSmtpSettings,
  testReceiver,
  testSmtpSettings,
  updateReceiver,
} from "../api/alertRoutingApi.js";

const TABS = [
  { key: "smtp", label: "SMTP Settings" },
  { key: "receivers", label: "Receivers" },
  { key: "logs", label: "Delivery Logs" },
];

const EMPTY_SMTP = {
  host: "",
  port: 587,
  username: "",
  password: "",
  fromEmail: "",
  fromName: "KubeSight",
  useTls: true,
  useSsl: false,
};

const EMPTY_RECEIVER = {
  name: "",
  type: "email",
  emailAddress: "",
  url: "",
  httpMethod: "POST",
  headers: {},
  secret: "",
  enabled: true,
};

const RECEIVER_TYPE_LABELS = {
  email: "Email",
  slack: "Slack",
  webhook: "Webhook",
};

function receiverDestination(receiver) {
  if (!receiver) {
    return "—";
  }
  if (receiver.type === "email") {
    return receiver.emailAddress || "—";
  }
  return receiver.url || "—";
}

function ReceiverTypeBadge({ type }) {
  const label = RECEIVER_TYPE_LABELS[type] || type || "Unknown";
  return <span className={`receiver-type-badge receiver-type-${type || "unknown"}`}>{label}</span>;
}

function StatusBadge({ ok, label }) {
  return <span className={`routing-status-badge ${ok ? "ok" : "warn"}`}>{label}</span>;
}

function TestResult({ status, message, at }) {
  if (!status && !message) {
    return <span className="muted">No test run yet</span>;
  }
  return (
    <span className={status === "success" ? "routing-test-ok" : "routing-test-fail"}>
      {status === "success" ? "Success" : "Failed"}
      {message ? ` — ${message}` : ""}
      {at ? ` (${new Date(at).toLocaleString()})` : ""}
    </span>
  );
}

function SmtpTab({ smtp, onSaved }) {
  const [draft, setDraft] = useState(EMPTY_SMTP);
  const [testRecipient, setTestRecipient] = useState("");
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    if (smtp) {
      setDraft({
        host: smtp.host || "",
        port: smtp.port || 587,
        username: smtp.username || "",
        password: "",
        fromEmail: smtp.fromEmail || "",
        fromName: smtp.fromName || "KubeSight",
        useTls: smtp.useTls !== false,
        useSsl: Boolean(smtp.useSsl),
      });
      setTestRecipient(smtp.fromEmail || "");
    }
  }, [smtp]);

  const save = async () => {
    setSaving(true);
    setError("");
    setMessage("");
    try {
      const payload = { ...draft };
      if (!payload.password) {
        delete payload.password;
      }
      await saveSmtpSettings(payload);
      setMessage("SMTP settings saved.");
      onSaved();
    } catch (err) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  };

  const sendTest = async () => {
    setTesting(true);
    setError("");
    setMessage("");
    try {
      const payload = { ...draft };
      if (!payload.password) {
        delete payload.password;
      }
      await saveSmtpSettings(payload);
      const result = await testSmtpSettings(testRecipient || draft.fromEmail);
      setMessage(result.message || "Test email sent.");
      onSaved();
    } catch (err) {
      setError(err.message);
      onSaved();
    } finally {
      setTesting(false);
    }
  };

  return (
    <section className="card alert-routing-panel">
      <header className="alert-routing-panel-header">
        <div>
          <h3>SMTP sender</h3>
          <p className="muted">Configure the outbound email server used for alert notifications.</p>
        </div>
        <StatusBadge
          ok={smtp?.configured}
          label={smtp?.configured ? "SMTP configured" : "SMTP not configured"}
        />
      </header>

      <div className="settings-form alert-routing-form">
        <label>
          SMTP host
          <input
            value={draft.host}
            onChange={(e) => setDraft((p) => ({ ...p, host: e.target.value }))}
            placeholder="smtp.company.com"
          />
        </label>
        <label>
          SMTP port
          <input
            type="number"
            value={draft.port}
            onChange={(e) => setDraft((p) => ({ ...p, port: Number(e.target.value) || 587 }))}
          />
        </label>
        <label>
          Username
          <input
            value={draft.username}
            onChange={(e) => setDraft((p) => ({ ...p, username: e.target.value }))}
          />
        </label>
        <label>
          Password / app password
          <input
            type="password"
            value={draft.password}
            placeholder={smtp?.passwordConfigured ? "•••••••• (leave blank to keep)" : ""}
            onChange={(e) => setDraft((p) => ({ ...p, password: e.target.value }))}
          />
        </label>
        <label>
          From email
          <input
            type="email"
            value={draft.fromEmail}
            onChange={(e) => setDraft((p) => ({ ...p, fromEmail: e.target.value }))}
          />
        </label>
        <label>
          From name
          <input
            value={draft.fromName}
            onChange={(e) => setDraft((p) => ({ ...p, fromName: e.target.value }))}
          />
        </label>
        <label className="checkbox-label settings-checkbox">
          <input
            type="checkbox"
            checked={draft.useTls}
            onChange={(e) => setDraft((p) => ({ ...p, useTls: e.target.checked }))}
          />
          Use TLS
        </label>
        <label className="checkbox-label settings-checkbox">
          <input
            type="checkbox"
            checked={draft.useSsl}
            onChange={(e) => setDraft((p) => ({ ...p, useSsl: e.target.checked }))}
          />
          Use SSL
        </label>
        <label>
          Test recipient
          <input
            type="email"
            value={testRecipient}
            onChange={(e) => setTestRecipient(e.target.value)}
            placeholder="admin@company.com"
          />
        </label>
      </div>

      <p className="muted">
        Last test: <TestResult status={smtp?.lastTestStatus} message={smtp?.lastTestMessage} at={smtp?.lastTestAt} />
      </p>

      {error ? <ErrorBanner message={error} /> : null}
      {message ? <p className="routing-test-message">{message}</p> : null}

      <div className="alert-routing-actions">
        <button type="button" onClick={save} disabled={saving || testing}>
          {saving ? "Saving..." : "Save SMTP Settings"}
        </button>
        <button type="button" onClick={sendTest} disabled={saving || testing}>
          {testing ? "Sending..." : "Send Test Email"}
        </button>
      </div>
    </section>
  );
}

function ReceiverModal({ open, mode, initial, onClose, onSave, saving, error }) {
  const [form, setForm] = useState(initial);
  const [headersText, setHeadersText] = useState("{}");
  const [localError, setLocalError] = useState("");

  useEffect(() => {
    if (open) {
      setForm(initial);
      setHeadersText(JSON.stringify(initial.headers || {}, null, 2));
      setLocalError("");
    }
  }, [open, initial]);

  if (!open) {
    return null;
  }

  const handleSave = () => {
    setLocalError("");
    const payload = { ...form };
    if (payload.type === "webhook") {
      const trimmed = headersText.trim();
      if (!trimmed) {
        payload.headers = {};
      } else {
        try {
          const parsed = JSON.parse(trimmed);
          if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
            throw new Error("Headers must be a JSON object.");
          }
          payload.headers = parsed;
        } catch (parseError) {
          setLocalError(parseError.message || "Headers must be valid JSON.");
          return;
        }
      }
    }
    onSave(payload);
  };

  return (
    <div className="modal-overlay" role="presentation" onClick={onClose}>
      <section className="card modal-panel routing-modal" onClick={(e) => e.stopPropagation()}>
        <header className="modal-header">
          <h3>{mode === "edit" ? "Edit receiver" : "Add receiver"}</h3>
          <button type="button" className="modal-close" onClick={onClose}>
            ×
          </button>
        </header>

        <div className="settings-form alert-routing-form receiver-modal-form">
          <label>
            Name
            <input value={form.name} onChange={(e) => setForm((p) => ({ ...p, name: e.target.value }))} />
          </label>
          <label>
            Receiver type
            <select
              value={form.type}
              onChange={(e) => setForm((p) => ({ ...p, type: e.target.value }))}
              disabled={mode === "edit"}
            >
              <option value="email">Email</option>
              <option value="slack">Slack</option>
              <option value="webhook">Webhook</option>
            </select>
          </label>

          {form.type === "email" ? (
            <label className="full-width">
              Email address
              <input
                type="email"
                value={form.emailAddress}
                onChange={(e) => setForm((p) => ({ ...p, emailAddress: e.target.value }))}
                placeholder="devops@company.com"
              />
            </label>
          ) : null}

          {form.type === "slack" ? (
            <label className="full-width">
              Slack webhook URL
              <input
                type="url"
                value={form.url}
                onChange={(e) => setForm((p) => ({ ...p, url: e.target.value }))}
                placeholder="https://hooks.slack.com/services/..."
              />
            </label>
          ) : null}

          {form.type === "webhook" ? (
            <>
              <label className="full-width">
                Webhook URL
                <input
                  type="url"
                  value={form.url}
                  onChange={(e) => setForm((p) => ({ ...p, url: e.target.value }))}
                  placeholder="https://api.company.com/alerts"
                />
              </label>
              <label>
                HTTP method
                <select
                  value={form.httpMethod || "POST"}
                  onChange={(e) => setForm((p) => ({ ...p, httpMethod: e.target.value }))}
                >
                  <option value="POST">POST</option>
                  <option value="PUT">PUT</option>
                  <option value="PATCH">PATCH</option>
                </select>
              </label>
              <label className="full-width">
                Headers (optional JSON)
                <textarea
                  rows={4}
                  value={headersText}
                  onChange={(e) => setHeadersText(e.target.value)}
                  placeholder='{"X-Custom-Header": "value"}'
                />
              </label>
              <label className="full-width">
                Secret (optional)
                <input
                  type="password"
                  placeholder={form.secretConfigured ? "•••••••• (leave blank to keep)" : ""}
                  value={form.secret || ""}
                  onChange={(e) => setForm((p) => ({ ...p, secret: e.target.value }))}
                />
              </label>
            </>
          ) : null}

          <label className="checkbox-label settings-checkbox full-width">
            <input
              type="checkbox"
              checked={form.enabled}
              onChange={(e) => setForm((p) => ({ ...p, enabled: e.target.checked }))}
            />
            Enabled
          </label>
        </div>

        {error || localError ? <p className="routing-error">{error || localError}</p> : null}

        <footer className="modal-actions">
          <button type="button" onClick={onClose} disabled={saving}>
            Cancel
          </button>
          <button type="button" onClick={handleSave} disabled={saving}>
            {saving ? "Saving..." : "Save receiver"}
          </button>
        </footer>
      </section>
    </div>
  );
}

function ReceiverDetailsModal({ open, receiver, onClose }) {
  if (!open || !receiver) {
    return null;
  }

  const policyNames = receiver.assignedPolicyNames || [];
  const groupNames = receiver.groupNames || [];

  return (
    <div className="modal-overlay" role="presentation" onClick={onClose}>
      <section className="card modal-panel routing-modal" onClick={(e) => e.stopPropagation()}>
        <header className="modal-header">
          <h3>Receiver details</h3>
          <button type="button" className="modal-close" onClick={onClose}>
            ×
          </button>
        </header>

        <dl className="receiver-details-grid">
          <div>
            <dt>Name</dt>
            <dd>{receiver.name}</dd>
          </div>
          <div>
            <dt>Type</dt>
            <dd>
              <ReceiverTypeBadge type={receiver.type} />
            </dd>
          </div>
          <div>
            <dt>Destination</dt>
            <dd>{receiverDestination(receiver)}</dd>
          </div>
          <div>
            <dt>Status</dt>
            <dd>
              <StatusBadge ok={receiver.enabled} label={receiver.enabled ? "Enabled" : "Disabled"} />
            </dd>
          </div>
          <div className="full-width">
            <dt>Receiver groups</dt>
            <dd>
              {groupNames.length ? (
                <ul className="receiver-assigned-policies">
                  {groupNames.map((name) => (
                    <li key={name}>{name}</li>
                  ))}
                </ul>
              ) : (
                <span className="muted">Not a member of any group.</span>
              )}
            </dd>
          </div>
          <div className="full-width">
            <dt>Assigned policies</dt>
            <dd>
              {policyNames.length ? (
                <ul className="receiver-assigned-policies">
                  {policyNames.map((name) => (
                    <li key={name}>{name}</li>
                  ))}
                </ul>
              ) : (
                <span className="muted">No policies assigned. Assign receivers from Alert Policies.</span>
              )}
            </dd>
          </div>
          <div className="full-width">
            <dt>Last test</dt>
            <dd>
              <TestResult
                status={receiver.lastTestStatus}
                message={receiver.lastTestMessage}
                at={receiver.lastTestAt}
              />
            </dd>
          </div>
        </dl>

        <footer className="modal-actions">
          <button type="button" onClick={onClose}>
            Close
          </button>
        </footer>
      </section>
    </div>
  );
}

function ReceiversTab({ receivers, groups, onChanged }) {
  const [receiverView, setReceiverView] = useState("individual");
  const [modalOpen, setModalOpen] = useState(false);
  const [modalMode, setModalMode] = useState("create");
  const [editing, setEditing] = useState(null);
  const [detailsReceiver, setDetailsReceiver] = useState(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [testingId, setTestingId] = useState(null);
  const [testMessage, setTestMessage] = useState("");

  const openCreate = () => {
    setModalMode("create");
    setEditing(EMPTY_RECEIVER);
    setError("");
    setModalOpen(true);
  };

  const openEdit = (receiver) => {
    setModalMode("edit");
    setEditing({
      ...receiver,
      secret: "",
      secretConfigured: receiver.secretConfigured,
    });
    setError("");
    setModalOpen(true);
  };

  const save = async (form) => {
    setSaving(true);
    setError("");
    try {
      const payload = { ...form };
      if (!payload.secret) {
        delete payload.secret;
      }
      delete payload.secretConfigured;
      delete payload.id;
      delete payload.lastTestAt;
      delete payload.lastTestStatus;
      delete payload.lastTestMessage;
      delete payload.createdAt;
      delete payload.updatedAt;
      delete payload.assignedPolicies;
      delete payload.assignedPolicyNames;

      if (modalMode === "edit" && editing?.id) {
        await updateReceiver(editing.id, payload);
      } else {
        await createReceiver(payload);
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
    if (!window.confirm("Delete this receiver?")) {
      return;
    }
    await deleteReceiver(id);
    onChanged();
  };

  const runTest = async (id) => {
    setTestingId(id);
    setTestMessage("");
    try {
      const result = await testReceiver(id);
      setTestMessage(result.message || "Test sent.");
      onChanged();
    } catch (err) {
      setTestMessage(err.message);
      onChanged();
    } finally {
      setTestingId(null);
    }
  };

  const formatGroups = (receiver) => {
    const names = receiver.groupNames || [];
    return names.length ? names.join(", ") : "—";
  };

  const tableRows = receivers.map((receiver) => ({
    id: receiver.id,
    name: receiver.name,
    typeDisplay: <ReceiverTypeBadge type={receiver.type} />,
    groups: formatGroups(receiver),
    statusDisplay: (
      <StatusBadge ok={receiver.enabled} label={receiver.enabled ? "Enabled" : "Disabled"} />
    ),
    lastTestDisplay: (
      <TestResult
        status={receiver.lastTestStatus}
        message={receiver.lastTestMessage}
        at={receiver.lastTestAt}
      />
    ),
    actionsDisplay: (
      <div className="table-actions">
        <button type="button" onClick={() => setDetailsReceiver(receiver)}>
          Details
        </button>
        <button type="button" onClick={() => runTest(receiver.id)} disabled={testingId === receiver.id}>
          {testingId === receiver.id ? "Testing..." : "Test"}
        </button>
        <button type="button" onClick={() => openEdit(receiver)}>
          Edit
        </button>
        <button type="button" className="danger" onClick={() => remove(receiver.id)}>
          Delete
        </button>
      </div>
    ),
  }));

  const columns = [
    { key: "name", label: "Name" },
    { key: "typeDisplay", label: "Type" },
    { key: "groups", label: "Groups" },
    { key: "statusDisplay", label: "Enabled" },
    { key: "lastTestDisplay", label: "Last test" },
    { key: "actionsDisplay", label: "" },
  ];

  return (
    <div className="receivers-tab-shell">
      <nav className="tab-bar receiver-sub-tabs" aria-label="Receiver views">
        <button
          type="button"
          className={receiverView === "individual" ? "active" : ""}
          onClick={() => setReceiverView("individual")}
        >
          Individual Receivers
        </button>
        <button
          type="button"
          className={receiverView === "groups" ? "active" : ""}
          onClick={() => setReceiverView("groups")}
        >
          Receiver Groups
        </button>
      </nav>

      {receiverView === "groups" ? (
        <ReceiverGroupsPanel groups={groups} receivers={receivers} onChanged={onChanged} />
      ) : (
        <section className="card alert-routing-panel">
          <header className="alert-routing-panel-header">
            <div>
              <h3>Individual receivers</h3>
              <p className="muted">Email inboxes and webhook endpoints that can receive alerts.</p>
            </div>
            <button type="button" onClick={openCreate}>
              Add receiver
            </button>
          </header>

          {testMessage ? <p className="routing-test-message">{testMessage}</p> : null}

          {receivers.length ? (
            <DataTable columns={columns} rows={tableRows} />
          ) : (
            <EmptyState message="Add an email, Slack, or webhook receiver to get started." />
          )}

          <ReceiverModal
            open={modalOpen}
            mode={modalMode}
            initial={editing || EMPTY_RECEIVER}
            onClose={() => setModalOpen(false)}
            onSave={save}
            saving={saving}
            error={error}
          />

          <ReceiverDetailsModal
            open={Boolean(detailsReceiver)}
            receiver={detailsReceiver}
            onClose={() => setDetailsReceiver(null)}
          />
        </section>
      )}
    </div>
  );
}

function LogsTab({ logs, onRefresh, loading }) {
  const logRows = logs.map((log) => ({
    ...log,
    groupName: log.groupName || "—",
    deliveredAtDisplay: log.deliveredAt ? new Date(log.deliveredAt).toLocaleString() : "—",
    typeDisplay: <ReceiverTypeBadge type={log.receiverType} />,
    statusDisplay: (
      <StatusBadge
        ok={log.status === "success"}
        label={log.status === "success" ? "Success" : log.status}
      />
    ),
    errorDisplay: log.errorMessage || "—",
  }));

  const columns = [
    { key: "alertName", label: "Alert" },
    { key: "policyName", label: "Policy" },
    { key: "groupName", label: "Group" },
    { key: "receiverName", label: "Receiver" },
    { key: "typeDisplay", label: "Type" },
    { key: "statusDisplay", label: "Status" },
    { key: "deliveredAtDisplay", label: "Timestamp" },
    { key: "errorDisplay", label: "Error" },
  ];

  return (
    <section className="card alert-routing-panel">
      <header className="alert-routing-panel-header">
        <div>
          <h3>Delivery logs</h3>
          <p className="muted">Recent alert notification delivery attempts.</p>
        </div>
        <button type="button" onClick={onRefresh} disabled={loading}>
          {loading ? "Refreshing..." : "Refresh"}
        </button>
      </header>

      {logs.length ? (
        <DataTable columns={columns} rows={logRows} />
      ) : (
        <EmptyState message="Logs appear when policy alerts are delivered to receivers." />
      )}
    </section>
  );
}

export default function AlertRoutingPage() {
  const [activeTab, setActiveTab] = useState("smtp");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [smtp, setSmtp] = useState(null);
  const [receivers, setReceivers] = useState([]);
  const [receiverGroups, setReceiverGroups] = useState([]);
  const [logs, setLogs] = useState([]);

  const loadAll = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [smtpRes, receiversRes, groupsRes, logsRes] = await Promise.all([
        getSmtpSettings(),
        listReceivers(),
        listReceiverGroups(),
        listDeliveryLogs(),
      ]);
      setSmtp(smtpRes);
      setReceivers(receiversRes.items || []);
      setReceiverGroups(groupsRes.items || []);
      setLogs(logsRes.items || []);
    } catch (err) {
      setError(err.message);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  const refreshPartial = async () => {
    try {
      const [smtpRes, receiversRes, groupsRes] = await Promise.all([
        getSmtpSettings(),
        listReceivers(),
        listReceiverGroups(),
      ]);
      setSmtp(smtpRes);
      setReceivers(receiversRes.items || []);
      setReceiverGroups(groupsRes.items || []);
    } catch (err) {
      setError(err.message);
    }
  };

  const refreshLogs = async () => {
    try {
      const logsRes = await listDeliveryLogs();
      setLogs(logsRes.items || []);
    } catch (err) {
      setError(err.message);
    }
  };

  return (
    <>
      <PageTitle
        title="Alert Routing"
        subtitle="Configure SMTP, notification receivers, and review delivery logs."
      />

      <nav className="alert-routing-tabs" aria-label="Alert routing sections">
        {TABS.map((tab) => (
          <button
            key={tab.key}
            type="button"
            className={activeTab === tab.key ? "active" : ""}
            onClick={() => setActiveTab(tab.key)}
          >
            {tab.label}
          </button>
        ))}
      </nav>

      {error ? <ErrorBanner message={error} /> : null}
      {loading && !smtp ? <p className="muted">Loading alert routing configuration…</p> : null}

      {activeTab === "smtp" ? <SmtpTab smtp={smtp} onSaved={refreshPartial} /> : null}
      {activeTab === "receivers" ? (
        <ReceiversTab receivers={receivers} groups={receiverGroups} onChanged={refreshPartial} />
      ) : null}
      {activeTab === "logs" ? <LogsTab logs={logs} onRefresh={refreshLogs} loading={loading} /> : null}
    </>
  );
}
