import { useEffect, useRef, useState } from "react";
import { listRegistries } from "../../api/registriesApi.js";
import { useTicketingApi } from "./TicketingContext.jsx";

// The Jenkins router that turns a ticket into a build + deploy. It is ONE
// connection shared by every ticketing provider — a build is a build whoever
// raised the ticket — so this section is appended to each provider's settings
// room rather than duplicated per provider, and editing it from the Jira tab
// changes the same record the Zoho tab shows.

// Jenkins router connection — same fields as the old modal, inline as a group.
export default function JenkinsSection({ canManage, jenkins, onSave, saving, onTest, testing, refFn }) {
  const api = useTicketingApi();
  const ro = !canManage;
  const [form, setForm] = useState(() => ({
    enabled: Boolean(jenkins?.enabled),
    baseUrl: jenkins?.baseUrl || "",
    username: jenkins?.username || "",
    apiToken: "",
    buildToken: "",
    routerJobPath: jenkins?.routerJobPath || "",
    verifyTls: jenkins?.verifyTls !== false,
    sendParamApp: jenkins?.sendParamApp !== false,
    sendParamNamespace: jenkins?.sendParamNamespace !== false,
    sendParamTag: jenkins?.sendParamTag !== false,
    autoRunTickets: Boolean(jenkins?.autoRunTickets),
    autoRunClusters: { ...(jenkins?.autoRunClusters || {}) },
    imageTagTemplate: jenkins?.imageTagTemplate || "{tag}",
    buildTimeoutMinutes: jenkins?.buildTimeoutMinutes || 45,
    queueTimeoutMinutes: jenkins?.queueTimeoutMinutes || 30,
    bundleWindowHours: jenkins?.bundleWindowHours || 24,
    rolloutTimeoutMinutes: jenkins?.rolloutTimeoutMinutes || 15,
    rollbackOnFailure: jenkins?.rollbackOnFailure !== false,
    registryConnectionId: jenkins?.registryConnectionId || "",
  }));
  const [clusters, setClusters] = useState([]);
  const [registries, setRegistries] = useState([]);
  const set = (k, v) => setForm((prev) => ({ ...prev, [k]: v }));

  // The tab mounts fresh each visit; jenkins may still be loading then — seed
  // the form once it arrives (but never over untouched local edits).
  const seeded = useRef(Boolean(jenkins));
  useEffect(() => {
    if (jenkins && !seeded.current) {
      seeded.current = true;
      setForm((prev) => ({
        ...prev,
        enabled: Boolean(jenkins.enabled),
        baseUrl: jenkins.baseUrl || "",
        username: jenkins.username || "",
        routerJobPath: jenkins.routerJobPath || "",
        verifyTls: jenkins.verifyTls !== false,
        sendParamApp: jenkins.sendParamApp !== false,
        sendParamNamespace: jenkins.sendParamNamespace !== false,
        sendParamTag: jenkins.sendParamTag !== false,
        autoRunTickets: Boolean(jenkins.autoRunTickets),
        autoRunClusters: { ...(jenkins.autoRunClusters || {}) },
        imageTagTemplate: jenkins.imageTagTemplate || "{tag}",
        buildTimeoutMinutes: jenkins.buildTimeoutMinutes || 45,
        queueTimeoutMinutes: jenkins.queueTimeoutMinutes || 30,
        bundleWindowHours: jenkins.bundleWindowHours || 24,
        rolloutTimeoutMinutes: jenkins.rolloutTimeoutMinutes || 15,
        rollbackOnFailure: jenkins.rollbackOnFailure !== false,
        registryConnectionId: jenkins.registryConnectionId || "",
      }));
    }
  }, [jenkins]);

  // Per-cluster auto-start overrides need the selectable cluster list; the
  // image-check pin needs the linked registries.
  useEffect(() => {
    let cancelled = false;
    api
      .getSourceClusters()
      .then((res) => {
        if (!cancelled) setClusters(res?.items || []);
      })
      .catch(() => {
        /* section simply stays empty */
      });
    listRegistries()
      .then((res) => {
        if (!cancelled) setRegistries(res?.items || []);
      })
      .catch(() => {
        /* dropdown falls back to Auto only */
      });
    return () => {
      cancelled = true;
    };
  }, [api]);

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
    const payload = {
      enabled: form.enabled,
      baseUrl: form.baseUrl,
      username: form.username,
      routerJobPath: form.routerJobPath,
      verifyTls: form.verifyTls,
      sendParamApp: form.sendParamApp,
      sendParamNamespace: form.sendParamNamespace,
      sendParamTag: form.sendParamTag,
      autoRunTickets: form.autoRunTickets,
      buildTimeoutMinutes: Number(form.buildTimeoutMinutes) || 45,
      queueTimeoutMinutes: Number(form.queueTimeoutMinutes) || 30,
      bundleWindowHours: Number(form.bundleWindowHours) || 24,
      rolloutTimeoutMinutes: Number(form.rolloutTimeoutMinutes) || 15,
      rollbackOnFailure: form.rollbackOnFailure,
      autoRunClusters: form.autoRunClusters,
      imageTagTemplate: form.imageTagTemplate.trim() || "{tag}",
      registryConnectionId: form.registryConnectionId ? Number(form.registryConnectionId) : null,
    };
    if (form.apiToken.trim()) payload.apiToken = form.apiToken.trim();
    if (form.buildToken.trim()) payload.buildToken = form.buildToken.trim();
    const ok = await onSave(payload);
    if (ok) setForm((prev) => ({ ...prev, apiToken: "", buildToken: "" }));
  };

  return (
    <section className="card sg-zh-setsec" id="zh-set-jenkins" ref={refFn}>
      <div className="card-header-row">
        <div>
          <h3>Jenkins router</h3>
          <p className="muted">
            Triggers the router job with <code>APP</code>, <code>TAG</code> and{" "}
            <code>NAMESPACE</code> (each optional below) — the router runs the right build and
            reports back.
          </p>
        </div>
        <div className="actions">
          <span className={`status-pill ${jenkins?.enabled ? "ok" : "muted"}`}>
            {jenkins?.enabled ? "Connected" : "Off"}
          </span>
          {canManage ? (
            <button
              type="button"
              className="secondary"
              onClick={onTest}
              disabled={testing || !jenkins?.apiTokenConfigured}
              title={jenkins?.apiTokenConfigured ? "" : "Save the connection first"}
            >
              {testing ? "Testing…" : "Test Jenkins"}
            </button>
          ) : null}
        </div>
      </div>

      {jenkins?.lastTestMessage ? (
        <p className={jenkins.lastTestStatus === "ok" ? "field-hint" : "sg-zh-inline-error"}>
          {jenkins.lastTestMessage}
        </p>
      ) : null}

      <form className="settings-form sg-zh-jform sg-zh-setform" onSubmit={submit}>
        <label className="checkbox-label">
          <input
            type="checkbox"
            checked={form.enabled}
            onChange={(e) => set("enabled", e.target.checked)}
            disabled={ro}
          />
          Enabled
        </label>

        <h4>Connection</h4>
        <label>
          Jenkins base URL
          <input
            value={form.baseUrl}
            onChange={(e) => set("baseUrl", e.target.value)}
            placeholder="https://jenkins.areeba.com"
            disabled={ro}
          />
        </label>
        <label>
          Router job path
          <input
            value={form.routerJobPath}
            onChange={(e) => set("routerJobPath", e.target.value)}
            placeholder="folder/job-name"
            disabled={ro}
          />
        </label>
        <label>
          Username
          <input
            value={form.username}
            onChange={(e) => set("username", e.target.value)}
            autoComplete="off"
            disabled={ro}
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
            disabled={ro}
          />
        </label>
        <label title="The job's “Trigger builds remotely” token, sent as the token field">
          Build token (optional)
          <input
            type="password"
            value={form.buildToken}
            onChange={(e) => set("buildToken", e.target.value)}
            placeholder={jenkins?.buildTokenConfigured ? "•••• (leave blank to keep)" : ""}
            autoComplete="new-password"
            disabled={ro}
          />
        </label>
        <label className="checkbox-label sg-zh-jcheck">
          <input
            type="checkbox"
            checked={form.verifyTls}
            onChange={(e) => set("verifyTls", e.target.checked)}
            disabled={ro}
          />
          Verify TLS certificates
        </label>

        <h4>Router parameters</h4>
        <p className="muted field-span">
          Only the ticked parameters are sent on <code>buildWithParameters</code> — untick one if
          the router job isn't parameterized with it. At least one must stay on.
        </p>
        <label className="checkbox-label sg-zh-jcheck">
          <input
            type="checkbox"
            checked={form.sendParamApp}
            onChange={(e) => set("sendParamApp", e.target.checked)}
            disabled={ro}
          />
          Send <code>APP</code> — the Kubernetes deployment name
        </label>
        <label className="checkbox-label sg-zh-jcheck">
          <input
            type="checkbox"
            checked={form.sendParamNamespace}
            onChange={(e) => set("sendParamNamespace", e.target.checked)}
            disabled={ro}
          />
          Send <code>NAMESPACE</code> — the target namespace
        </label>
        <label className="checkbox-label sg-zh-jcheck">
          <input
            type="checkbox"
            checked={form.sendParamTag}
            onChange={(e) => set("sendParamTag", e.target.checked)}
            disabled={ro}
          />
          Send <code>TAG</code> — the raw ticket tag
        </label>

        <h4>Automation</h4>
        <label>
          Image tag template
          <input
            value={form.imageTagTemplate}
            onChange={(e) => set("imageTagTemplate", e.target.value)}
            placeholder="{tag}"
            className="mono"
            disabled={ro}
          />
          <span className="field-hint">
            <code>{"v{tag}-prod"}</code> ⇒ ticket <code>1.72.1</code> checks/deploys{" "}
            <code>v1.72.1-prod</code>; Jenkins always gets the raw tag.
          </span>
        </label>
        <label>
          Image-check registry
          <select
            value={String(form.registryConnectionId || "")}
            onChange={(e) => set("registryConnectionId", e.target.value)}
            disabled={ro}
          >
            <option value="">Auto — first linked registry matching the image host</option>
            {registries.map((r) => (
              <option key={r.id} value={String(r.id)}>
                {r.name} ({r.host}){r.enabled ? "" : " — disabled"}
              </option>
            ))}
          </select>
          <span className="field-hint">
            When several linked registries claim the same image host (same DNS, different
            servers), the automation checks this one. Auto keeps the default first-created
            match.
          </span>
        </label>
        <label className="checkbox-label sg-zh-jcheck">
          <input
            type="checkbox"
            checked={form.autoRunTickets}
            onChange={(e) => set("autoRunTickets", e.target.checked)}
            disabled={ro}
          />
          Auto-start new tickets by default
        </label>
        <div className="field-span">
          <span className="sg-zh-pick-label">Auto-start per cluster</span>
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
                    disabled={ro}
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
        <div className="sg-zh-jrow4">
          <label>
            Build timeout (min)
            <input
              type="number"
              min="1"
              value={form.buildTimeoutMinutes}
              onChange={(e) => set("buildTimeoutMinutes", e.target.value)}
              disabled={ro}
            />
          </label>
          <label>
            Queue timeout (min)
            <input
              type="number"
              min="1"
              value={form.queueTimeoutMinutes}
              onChange={(e) => set("queueTimeoutMinutes", e.target.value)}
              disabled={ro}
            />
          </label>
          <label title="How long an auto-created Change Bundle stays deployable while approvals come in">
            Approval window (h)
            <input
              type="number"
              min="1"
              value={form.bundleWindowHours}
              onChange={(e) => set("bundleWindowHours", e.target.value)}
              disabled={ro}
            />
          </label>
          <label title="How long to wait for pods to report ready before the run fails">
            Rollout timeout (min)
            <input
              type="number"
              min="1"
              value={form.rolloutTimeoutMinutes}
              onChange={(e) => set("rolloutTimeoutMinutes", e.target.value)}
              disabled={ro}
            />
          </label>
        </div>
        <label className="checkbox-label sg-zh-jcheck">
          <input
            type="checkbox"
            checked={form.rollbackOnFailure}
            onChange={(e) => set("rollbackOnFailure", e.target.checked)}
            disabled={ro}
          />
          Auto-rollback on rollout failure — admins are emailed on every failure either way; to
          notify others, add a Deploy Automation policy under Alerts → Policies
        </label>

        {canManage ? (
          <div className="modal-actions">
            <button type="submit" className="primary" disabled={saving}>
              {saving ? "Saving…" : "Save connection"}
            </button>
          </div>
        ) : null}
      </form>
    </section>
  );
}
