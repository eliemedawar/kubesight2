import { useState } from "react";
import EmptyState from "../common/EmptyState.jsx";
import ErrorBanner from "../common/ErrorBanner.jsx";
import { IconCheck } from "./icons.jsx";

// Display order + labels for the run step chips (mirrors the backend STEP_KEYS).
const STEPS = [
  { key: "image_check", label: "Image check" },
  { key: "build", label: "Build" },
  { key: "verify", label: "Verify" },
  { key: "approval", label: "Approval" },
  { key: "deploy", label: "Deploy" },
];

const STEP_CHIP_CLASS = {
  done: "sg-pstep--done",
  run: "sg-pstep--run",
  fail: "sg-pstep--fail",
  wait: "sg-pstep--wait",
  skip: "sg-pstep--wait sg-zh-pstep-skip",
};

const RUN_STATUS_PILL = {
  queued: ["info", "Queued"],
  checking_image: ["info", "Checking image"],
  building: ["info", "Building"],
  verifying_image: ["info", "Verifying"],
  awaiting_approval: ["warn", "Awaiting approval"],
  deployed: ["ok", "Deployed"],
  failed: ["danger", "Failed"],
  cancelled: ["muted", "Cancelled"],
};

export const ACTIVE_RUN_STATUSES = new Set([
  "queued",
  "checking_image",
  "building",
  "verifying_image",
  "awaiting_approval",
]);

export function RunStatusPill({ status }) {
  const [tone, label] = RUN_STATUS_PILL[status] || ["muted", status || "—"];
  return <span className={`status-pill ${tone}`}>{label}</span>;
}

function RunRow({ run, canManage, cancelling, onCancel }) {
  const active = ACTIVE_RUN_STATUSES.has(run.status);
  const steps = new Map((run.steps || []).map((s) => [s.key, s]));
  return (
    <div className="sg-zh-run">
      <div className="sg-zh-run-head">
        <b>{run.ticketNumber || `run #${run.id}`}</b>
        <span className="mono sg-zh-run-target">{run.deploymentName}</span>
        <span className="sg-tag">{run.namespace}</span>
        <span className="sg-tag">{run.imageTag}</span>
        {run.auto ? <span className="sg-zh-count">auto</span> : null}
        <span className="sg-zh-run-spacer" />
        <span className="sg-zh-htime">
          {run.createdAt ? new Date(run.createdAt).toLocaleString() : ""}
        </span>
        <RunStatusPill status={run.status} />
        {active && canManage ? (
          <button
            type="button"
            className="btn-ghost sg-zh-run-cancel"
            onClick={() => onCancel(run)}
            disabled={cancelling}
          >
            Cancel
          </button>
        ) : null}
      </div>

      <div className="sg-pipe sg-zh-run-pipe">
        {STEPS.map((step, index) => {
          const state = steps.get(step.key) || { status: "wait", detail: "" };
          const chip = (
            <span
              key={step.key}
              className={`sg-pstep ${STEP_CHIP_CLASS[state.status] || "sg-pstep--wait"}`}
              title={state.detail || step.label}
            >
              {state.status === "done" ? <IconCheck /> : null}
              {step.label}
              {state.status === "skip" ? " (skipped)" : ""}
            </span>
          );
          return index === 0 ? (
            chip
          ) : (
            <span key={step.key} className="sg-zh-pipe-seg">
              <span className="sg-pline" />
              {chip}
            </span>
          );
        })}
      </div>

      {run.jenkinsBuildUrl ? (
        <p className="sg-zh-fhint">
          Router build{" "}
          <a href={run.jenkinsBuildUrl} target="_blank" rel="noreferrer">
            #{run.jenkinsBuildNumber}
          </a>
          {run.bundleId ? <> · Change bundle #{run.bundleId} ({run.bundleStatus || "…"})</> : null}
        </p>
      ) : run.bundleId ? (
        <p className="sg-zh-fhint">
          Change bundle #{run.bundleId} ({run.bundleStatus || "…"})
        </p>
      ) : null}
      {run.error ? <p className="sg-zh-inline-error">{run.error}</p> : null}
    </div>
  );
}

export default function ZohoAutomationCard({
  canManage = false,
  jenkins,
  runs = [],
  runsLoading = false,
  onSaveJenkins,
  onTestJenkins,
  onCancelRun,
  testing = false,
  saving = false,
  cancellingRunId = null,
}) {
  const [showForm, setShowForm] = useState(false);
  const enabled = Boolean(jenkins?.enabled);

  return (
    <section className="card">
      <div className="card-header-row">
        <div>
          <h3>Deploy automation</h3>
          <p className="muted">
            Tickets become deployments: registry-gated builds through the Jenkins router pipeline,
            then a Change Bundle on approval-gated clusters or an immediate image update elsewhere.
          </p>
        </div>
        <div className="actions">
          <span className={`status-pill ${enabled ? "ok" : "muted"}`}>
            {enabled ? "Jenkins connected" : "Jenkins off"}
          </span>
          <span className={`status-pill ${jenkins?.autoRunTickets ? "ok" : "muted"}`}>
            {jenkins?.autoRunTickets ? "Auto-run on" : "Auto-run off"}
          </span>
          {canManage ? (
            <>
              <button
                type="button"
                className="secondary"
                onClick={onTestJenkins}
                disabled={testing || !jenkins?.apiTokenConfigured}
                title={jenkins?.apiTokenConfigured ? "" : "Configure Jenkins first"}
              >
                {testing ? "Testing…" : "Test Jenkins"}
              </button>
              <button type="button" className="secondary" onClick={() => setShowForm(true)}>
                Configure Jenkins
              </button>
            </>
          ) : null}
        </div>
      </div>

      {jenkins?.lastTestMessage ? (
        <p className={jenkins.lastTestStatus === "ok" ? "field-hint" : "sg-zh-inline-error"}>
          {jenkins.lastTestMessage}
        </p>
      ) : null}

      {runsLoading ? (
        <p className="muted">Loading runs…</p>
      ) : runs.length === 0 ? (
        <EmptyState message="No automation runs yet — use “Run” on a resolved inbound ticket below, or turn on auto-run." />
      ) : (
        <div className="sg-zh-runs">
          {runs.map((run) => (
            <RunRow
              key={run.id}
              run={run}
              canManage={canManage}
              cancelling={cancellingRunId === run.id}
              onCancel={onCancelRun}
            />
          ))}
        </div>
      )}

      {showForm && canManage ? (
        <JenkinsConfigModal
          jenkins={jenkins}
          saving={saving}
          onClose={() => setShowForm(false)}
          onSave={async (payload) => {
            const ok = await onSaveJenkins(payload);
            if (ok) setShowForm(false);
          }}
        />
      ) : null}
    </section>
  );
}

function JenkinsConfigModal({ jenkins, saving, onClose, onSave }) {
  const [form, setForm] = useState(() => ({
    enabled: Boolean(jenkins?.enabled),
    baseUrl: jenkins?.baseUrl || "",
    username: jenkins?.username || "",
    apiToken: "",
    routerJobPath: jenkins?.routerJobPath || "",
    verifyTls: jenkins?.verifyTls !== false,
    autoRunTickets: Boolean(jenkins?.autoRunTickets),
    buildTimeoutMinutes: jenkins?.buildTimeoutMinutes || 45,
    queueTimeoutMinutes: jenkins?.queueTimeoutMinutes || 10,
    bundleWindowHours: jenkins?.bundleWindowHours || 24,
  }));
  const [error, setError] = useState("");
  const set = (k, v) => setForm((prev) => ({ ...prev, [k]: v }));

  const submit = async (e) => {
    e.preventDefault();
    setError("");
    const payload = {
      enabled: form.enabled,
      baseUrl: form.baseUrl,
      username: form.username,
      routerJobPath: form.routerJobPath,
      verifyTls: form.verifyTls,
      autoRunTickets: form.autoRunTickets,
      buildTimeoutMinutes: Number(form.buildTimeoutMinutes) || 45,
      queueTimeoutMinutes: Number(form.queueTimeoutMinutes) || 10,
      bundleWindowHours: Number(form.bundleWindowHours) || 24,
    };
    if (form.apiToken.trim()) payload.apiToken = form.apiToken.trim();
    try {
      await onSave(payload);
    } catch (err) {
      setError(err.message || "Failed to save the Jenkins connection.");
    }
  };

  return (
    <div className="modal-overlay" role="presentation" onClick={onClose}>
      <section
        className="card modal-panel sg-zh-config"
        role="dialog"
        aria-label="Jenkins router connection"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="modal-header">
          <div>
            <h3>Jenkins router connection</h3>
            <p className="muted">
              KubeSight triggers ONE router pipeline with <code>APP_NAME</code>,{" "}
              <code>NAMESPACE</code>, <code>IMAGE_TAG</code> and <code>TICKET</code>; the router
              maps the app to the right job, waits on it and propagates its result.
            </p>
          </div>
          <button type="button" className="modal-close" onClick={onClose} aria-label="Close">
            ✕
          </button>
        </header>

        {error ? <ErrorBanner message={error} onDismiss={() => setError("")} /> : null}

        <form className="settings-form" onSubmit={submit}>
          <label className="checkbox-label">
            <input
              type="checkbox"
              checked={form.enabled}
              onChange={(e) => set("enabled", e.target.checked)}
            />
            Enabled (allows automation runs to trigger builds)
          </label>

          <h4>Connection</h4>
          <label>
            Jenkins base URL
            <input
              value={form.baseUrl}
              onChange={(e) => set("baseUrl", e.target.value)}
              placeholder="https://jenkins.areeba.com"
            />
          </label>
          <label>
            Router job path
            <input
              value={form.routerJobPath}
              onChange={(e) => set("routerJobPath", e.target.value)}
              placeholder="kubesight/deploy-router"
            />
            <span className="field-hint">
              Folder-style path — <code>folder/job-name</code> becomes{" "}
              <code>/job/folder/job/job-name</code>.
            </span>
          </label>
          <label>
            Username
            <input
              value={form.username}
              onChange={(e) => set("username", e.target.value)}
              autoComplete="off"
            />
          </label>
          <label>
            API token
            <input
              type="password"
              value={form.apiToken}
              onChange={(e) => set("apiToken", e.target.value)}
              placeholder={jenkins?.apiTokenConfigured ? "•••• (leave blank to keep)" : ""}
              autoComplete="new-password"
            />
          </label>
          <label className="checkbox-label">
            <input
              type="checkbox"
              checked={form.verifyTls}
              onChange={(e) => set("verifyTls", e.target.checked)}
            />
            Verify TLS certificates
          </label>

          <h4>Automation behaviour</h4>
          <label className="checkbox-label">
            <input
              type="checkbox"
              checked={form.autoRunTickets}
              onChange={(e) => set("autoRunTickets", e.target.checked)}
            />
            Auto-run: start a run for every resolved inbound ticket that carries a tag
          </label>
          <label>
            Build timeout (minutes)
            <input
              type="number"
              min="1"
              value={form.buildTimeoutMinutes}
              onChange={(e) => set("buildTimeoutMinutes", e.target.value)}
            />
          </label>
          <label>
            Queue timeout (minutes)
            <input
              type="number"
              min="1"
              value={form.queueTimeoutMinutes}
              onChange={(e) => set("queueTimeoutMinutes", e.target.value)}
            />
          </label>
          <label>
            Approval window (hours)
            <input
              type="number"
              min="1"
              value={form.bundleWindowHours}
              onChange={(e) => set("bundleWindowHours", e.target.value)}
            />
            <span className="field-hint">
              How long an auto-created Change Bundle stays deployable while approvals come in.
            </span>
          </label>

          <div className="modal-actions">
            <button type="button" className="secondary" onClick={onClose}>
              Cancel
            </button>
            <button type="submit" className="primary" disabled={saving}>
              {saving ? "Saving…" : "Save connection"}
            </button>
          </div>
        </form>
      </section>
    </div>
  );
}
