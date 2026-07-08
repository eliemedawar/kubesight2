import { useEffect, useState } from "react";
import EmptyState from "../common/EmptyState.jsx";
import ErrorBanner from "../common/ErrorBanner.jsx";
import { getZohoSourceClusters } from "../../api/zohoApi.js";
import { IconCheck } from "./icons.jsx";

// Display order + labels for the run step chips (mirrors the backend STEP_KEYS).
const STEPS = [
  { key: "image_check", label: "Image check" },
  { key: "build", label: "Build" },
  { key: "verify", label: "Verify" },
  { key: "approval", label: "Approval" },
  { key: "deploy", label: "Deploy" },
  { key: "pods", label: "Pod health" },
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
  verifying_rollout: ["info", "Rolling out"],
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
  "verifying_rollout",
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
        <span
          className="sg-tag"
          title={
            run.ticketTag && run.ticketTag !== run.imageTag
              ? `ticket tag: ${run.ticketTag}`
              : undefined
          }
        >
          {run.imageTag}
        </span>
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
          let state = steps.get(step.key) || { status: "wait", detail: "" };
          // Runs that finished before the pod-health step existed have no
          // "pods" entry — show it as skipped rather than eternally waiting.
          if (step.key === "pods" && run.status === "deployed" && state.status === "wait") {
            state = { status: "skip", detail: "not checked (older run)" };
          }
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
          <span
            className={`status-pill ${jenkins?.autoRunTickets ? "ok" : "muted"}`}
            title={
              Object.keys(jenkins?.autoRunClusters || {}).length
                ? Object.entries(jenkins.autoRunClusters)
                    .map(([c, m]) => `${c}: ${m}`)
                    .join(", ")
                : "No per-cluster overrides"
            }
          >
            {jenkins?.autoRunTickets ? "Auto-start on" : "Auto-start off"}
            {Object.keys(jenkins?.autoRunClusters || {}).length
              ? ` · ${Object.keys(jenkins.autoRunClusters).length} override(s)`
              : ""}
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
    buildToken: "",
    routerJobPath: jenkins?.routerJobPath || "",
    verifyTls: jenkins?.verifyTls !== false,
    autoRunTickets: Boolean(jenkins?.autoRunTickets),
    autoRunClusters: { ...(jenkins?.autoRunClusters || {}) },
    imageTagTemplate: jenkins?.imageTagTemplate || "{tag}",
    buildTimeoutMinutes: jenkins?.buildTimeoutMinutes || 45,
    queueTimeoutMinutes: jenkins?.queueTimeoutMinutes || 10,
    bundleWindowHours: jenkins?.bundleWindowHours || 24,
    rolloutTimeoutMinutes: jenkins?.rolloutTimeoutMinutes || 15,
  }));
  const [error, setError] = useState("");
  const [clusters, setClusters] = useState([]);
  const set = (k, v) => setForm((prev) => ({ ...prev, [k]: v }));

  // Per-cluster auto-start overrides need the selectable cluster list.
  useEffect(() => {
    let cancelled = false;
    getZohoSourceClusters()
      .then((res) => {
        if (!cancelled) setClusters(res?.items || []);
      })
      .catch(() => {
        /* section simply stays empty */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const setClusterMode = (clusterId, mode) => {
    setForm((prev) => {
      const next = { ...prev.autoRunClusters };
      if (mode === "default") delete next[clusterId];
      else next[clusterId] = mode;
      return { ...prev, autoRunClusters: next };
    });
  };

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
      rolloutTimeoutMinutes: Number(form.rolloutTimeoutMinutes) || 15,
    };
    payload.autoRunClusters = form.autoRunClusters;
    payload.imageTagTemplate = form.imageTagTemplate.trim() || "{tag}";
    if (form.apiToken.trim()) payload.apiToken = form.apiToken.trim();
    if (form.buildToken.trim()) payload.buildToken = form.buildToken.trim();
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
              KubeSight triggers ONE router pipeline with <code>APP</code>, <code>TAG</code> and{" "}
              <code>NAMESPACE</code> (plus the job's remote-trigger <code>token</code> when set);
              the router maps the app to the right job, waits on it and propagates its result.
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
          <label>
            Build token (optional)
            <input
              type="password"
              value={form.buildToken}
              onChange={(e) => set("buildToken", e.target.value)}
              placeholder={jenkins?.buildTokenConfigured ? "•••• (leave blank to keep)" : ""}
              autoComplete="new-password"
            />
            <span className="field-hint">
              The job's “Trigger builds remotely” token — sent as the <code>token</code> field on
              every build request.
            </span>
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
          <label>
            Image tag template
            <input
              value={form.imageTagTemplate}
              onChange={(e) => set("imageTagTemplate", e.target.value)}
              placeholder="{tag}"
              className="mono"
            />
            <span className="field-hint">
              How a ticket's tag becomes the registry tag. <code>{"{tag}"}</code> is the ticket
              value — e.g. <code>{"v{tag}-prod"}</code> turns <code>1.72.1</code> into{" "}
              <code>v1.72.1-prod</code> for the Nexus check and the deploy. The Jenkins router
              always receives the raw ticket tag. Tickets already carrying the full form are used
              as-is.
            </span>
          </label>
          <label className="checkbox-label">
            <input
              type="checkbox"
              checked={form.autoRunTickets}
              onChange={(e) => set("autoRunTickets", e.target.checked)}
            />
            Auto-start by default: run automation for every resolved inbound ticket with a tag
          </label>
          <div className="field-span">
            <span className="sg-zh-pick-label">Auto-start per cluster</span>
            <p className="field-hint">
              Overrides the default above for the ticket's target cluster. On a cluster set to
              auto-start that also requires no approvals, tickets deploy fully hands-off.
            </p>
            {clusters.length === 0 ? (
              <p className="muted">No clusters available.</p>
            ) : (
              <div className="sg-zh-cluster-modes">
                {clusters.map((c) => (
                  <label key={c.id} className="sg-zh-cluster-mode">
                    <span className="mono">{c.name || c.id}</span>
                    <select
                      value={form.autoRunClusters[c.id] || "default"}
                      onChange={(e) => setClusterMode(c.id, e.target.value)}
                    >
                      <option value="default">
                        Default ({form.autoRunTickets ? "auto-start" : "manual"})
                      </option>
                      <option value="auto">Auto-start</option>
                      <option value="manual">Manual start</option>
                    </select>
                  </label>
                ))}
              </div>
            )}
          </div>
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
          <label>
            Rollout timeout (minutes)
            <input
              type="number"
              min="1"
              value={form.rolloutTimeoutMinutes}
              onChange={(e) => set("rolloutTimeoutMinutes", e.target.value)}
            />
            <span className="field-hint">
              After the image is applied, how long to wait for the pods to report ready before the
              run is marked failed.
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
