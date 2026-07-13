import { useEffect, useRef, useState } from "react";
import { listRegistries } from "../../api/registriesApi.js";
import { getZohoSourceClusters } from "../../api/zohoApi.js";
import { IconAlert, IconCheck } from "./icons.jsx";
import { CopyButton } from "./common.jsx";

const SECTIONS = [
  { key: "connection", label: "Connection" },
  { key: "oauth", label: "OAuth client" },
  { key: "fields", label: "Field mapping" },
  { key: "sync", label: "Sync behaviour" },
  { key: "webhook", label: "Inbound webhook" },
  { key: "writeback", label: "Ticket write-back" },
  { key: "jenkins", label: "Jenkins router" },
];

// Completeness per section, judged on the SAVED config — the rail doubles as a
// setup checklist.
function sectionDone(key, config, jenkins) {
  switch (key) {
    case "connection":
      return Boolean(config?.orgId && config?.layoutId);
    case "oauth":
      return Boolean(
        config?.clientId && config?.clientSecretConfigured && config?.refreshTokenConfigured
      );
    case "fields":
      return Boolean(config?.appFieldId);
    case "sync":
      return true;
    case "webhook":
      return Boolean(config?.inboundSecretConfigured);
    case "writeback":
      return Boolean(config?.ticketWritebackEnabled);
    case "jenkins":
      return Boolean(jenkins?.baseUrl && jenkins?.apiTokenConfigured);
    default:
      return false;
  }
}

function SettingsCard({ id, title, pill, children, refFn }) {
  return (
    <section className="card sg-zh-setsec" id={id} ref={refFn}>
      <div className="card-header-row">
        <h3>{title}</h3>
        {pill || null}
      </div>
      {children}
    </section>
  );
}

// Setup room: sticky completeness rail + grouped sections. Replaces both the
// 18-field Zoho config modal and the Jenkins connection modal.
export default function ZohoSettingsTab({
  canManage,
  config,
  form,
  setField,
  onSaveConfig,
  savingConfig,
  dirty,
  onDiscard,
  webhookUrl,
  jenkins,
  onSaveJenkins,
  savingJenkins,
  onTestJenkins,
  testingJenkins,
}) {
  const sectionRefs = useRef({});
  const [active, setActive] = useState("connection");
  const ro = !canManage;

  const jumpTo = (key) => {
    setActive(key);
    sectionRefs.current[key]?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  return (
    <div className="sg-zh-setgrid">
      <nav className="sg-zh-setrail" aria-label="Settings sections">
        {SECTIONS.map((s) => {
          const done = sectionDone(s.key, config, jenkins);
          return (
            <button
              key={s.key}
              type="button"
              className={`sg-zh-setitem ${active === s.key ? "sg-zh-setitem--on" : ""}`}
              onClick={() => jumpTo(s.key)}
            >
              {s.label}
              <span
                className={`sg-zh-setstate ${done ? "sg-zh-setstate--done" : "sg-zh-setstate--todo"}`}
                title={done ? "Configured" : "Needs attention"}
              >
                {done ? <IconCheck /> : <IconAlert />}
              </span>
            </button>
          );
        })}
        {ro ? <p className="sg-zh-setro">Read-only — you don't have the zoho:manage permission.</p> : null}
      </nav>

      <div className="sg-zh-setmain">
        <form
          onSubmit={(e) => {
            e.preventDefault();
            onSaveConfig();
          }}
        >
          <SettingsCard
            id="zh-set-connection"
            title="Connection"
            refFn={(el) => (sectionRefs.current.connection = el)}
            pill={
              <span className={`status-pill ${config?.enabled ? "ok" : "muted"}`}>
                {config?.enabled ? "Integration on" : "Integration off"}
              </span>
            }
          >
            <div className="settings-form sg-zh-setform">
              <label className="checkbox-label">
                <input
                  type="checkbox"
                  checked={form.enabled}
                  onChange={(e) => setField("enabled", e.target.checked)}
                  disabled={ro}
                />
                Enabled (allows scheduled + manual sync)
              </label>
              <label>
                Org ID
                <input
                  value={form.orgId}
                  onChange={(e) => setField("orgId", e.target.value)}
                  placeholder="854214247"
                  disabled={ro}
                />
              </label>
              <label>
                Layout ID (DevOps Request)
                <input
                  value={form.layoutId}
                  onChange={(e) => setField("layoutId", e.target.value)}
                  placeholder="999149000010342586"
                  disabled={ro}
                />
              </label>
              <label>
                Department ID (optional)
                <input
                  value={form.departmentId}
                  onChange={(e) => setField("departmentId", e.target.value)}
                  disabled={ro}
                />
              </label>
              <label>
                Desk API base
                <input
                  value={form.apiBase}
                  onChange={(e) => setField("apiBase", e.target.value)}
                  disabled={ro}
                />
              </label>
              <label>
                Token endpoint
                <input
                  value={form.tokenEndpoint}
                  onChange={(e) => setField("tokenEndpoint", e.target.value)}
                  disabled={ro}
                />
              </label>
            </div>
          </SettingsCard>

          <SettingsCard
            id="zh-set-oauth"
            title="OAuth client"
            refFn={(el) => (sectionRefs.current.oauth = el)}
            pill={<span className="sg-zh-count">server-to-server, acts as zagent</span>}
          >
            <p className="muted">
              Secrets are stored encrypted and never shown again — leave a field blank to keep the
              current value.
            </p>
            <div className="settings-form sg-zh-setform">
              <label>
                Client ID
                <input
                  value={form.clientId}
                  onChange={(e) => setField("clientId", e.target.value)}
                  autoComplete="off"
                  disabled={ro}
                />
              </label>
              <label>
                Client Secret
                <input
                  type="password"
                  value={form.clientSecret}
                  onChange={(e) => setField("clientSecret", e.target.value)}
                  placeholder={config?.clientSecretConfigured ? "•••• (leave blank to keep)" : ""}
                  autoComplete="new-password"
                  disabled={ro}
                />
              </label>
              <label>
                Refresh Token
                <input
                  type="password"
                  value={form.refreshToken}
                  onChange={(e) => setField("refreshToken", e.target.value)}
                  placeholder={config?.refreshTokenConfigured ? "•••• (leave blank to keep)" : ""}
                  autoComplete="new-password"
                  disabled={ro}
                />
              </label>
            </div>
          </SettingsCard>

          <SettingsCard
            id="zh-set-fields"
            title="Field mapping"
            refFn={(el) => (sectionRefs.current.fields = el)}
          >
            <p className="muted">
              Field IDs come from DEVOPS-REQUEST-FIELD-SYNC-CONFIG.md — they bind the sync to the
              exact dropdowns on the DevOps Request layout.
            </p>
            <div className="settings-form sg-zh-setform sg-zh-fieldmap">
              <label>
                Application field ID (deployments dropdown)
                <input
                  value={form.appFieldId}
                  onChange={(e) => setField("appFieldId", e.target.value)}
                  placeholder="999149000010343250"
                  disabled={ro}
                />
              </label>
              <label>
                Application field API name
                <input
                  value={form.appFieldApiName}
                  onChange={(e) => setField("appFieldApiName", e.target.value)}
                  placeholder="cf_application"
                  disabled={ro}
                />
              </label>
              <label>
                Environment field ID (namespaces dropdown — optional)
                <input
                  value={form.environmentFieldId}
                  onChange={(e) => setField("environmentFieldId", e.target.value)}
                  placeholder="999149000010343580"
                  disabled={ro}
                />
                <span className="field-hint">Leave blank to not publish the namespace list.</span>
              </label>
              <label>
                Environment field API name
                <input
                  value={form.environmentFieldApiName}
                  onChange={(e) => setField("environmentFieldApiName", e.target.value)}
                  placeholder="cf_environment"
                  disabled={ro}
                />
              </label>
              <label>
                Tag field API name (inbound version)
                <input
                  value={form.tagFieldApiName}
                  onChange={(e) => setField("tagFieldApiName", e.target.value)}
                  placeholder="cf_tag"
                  disabled={ro}
                />
              </label>
              <label>
                Variable field ID (env-var picklist — optional)
                <input
                  value={form.variableFieldId}
                  onChange={(e) => setField("variableFieldId", e.target.value)}
                  disabled={ro}
                />
                <span className="field-hint">
                  Must be a Picklist field on the layout. Leave blank to not manage it.
                </span>
              </label>
              <label>
                Variable field API name
                <input
                  value={form.variableFieldApiName}
                  onChange={(e) => setField("variableFieldApiName", e.target.value)}
                  placeholder="cf_variable"
                  disabled={ro}
                />
              </label>
              <label>
                Value field API name (inbound variable value)
                <input
                  value={form.valueFieldApiName}
                  onChange={(e) => setField("valueFieldApiName", e.target.value)}
                  placeholder="cf_value"
                  disabled={ro}
                />
              </label>
            </div>
          </SettingsCard>

          <SettingsCard
            id="zh-set-sync"
            title="Sync behaviour"
            refFn={(el) => (sectionRefs.current.sync = el)}
          >
            <p className="muted">
              Choose which fields KubeSight publishes. A field left off here is yours to edit
              manually in the layout editor — the sync won't touch it.
            </p>
            <div className="settings-form sg-zh-setform">
              <label className="checkbox-label">
                <input
                  type="checkbox"
                  checked={form.syncApplication}
                  onChange={(e) => setField("syncApplication", e.target.checked)}
                  disabled={ro}
                />
                Publish deployments → Application field
              </label>
              <label className="checkbox-label">
                <input
                  type="checkbox"
                  checked={form.syncEnvironment}
                  onChange={(e) => setField("syncEnvironment", e.target.checked)}
                  disabled={ro}
                />
                Publish namespaces → Environment field
              </label>
              <label className="checkbox-label">
                <input
                  type="checkbox"
                  checked={form.syncVariables}
                  onChange={(e) => setField("syncVariables", e.target.checked)}
                  disabled={ro}
                />
                Publish deployment env-var names → Variable field (needs the Variable field ID;
                tickets carrying a Variable + Value become variable-change runs)
              </label>
              <label className="checkbox-label">
                <input
                  type="checkbox"
                  checked={form.cascadeEnabled}
                  onChange={(e) => setField("cascadeEnabled", e.target.checked)}
                  disabled={ro}
                />
                Cascade: filter Application by the selected Environment — and, when variables are
                published, filter Variable by the selected Application (needs the fields published;
                requires <code>Desk.settings.CREATE</code> on the Zoho token)
              </label>
              <label>
                Auto-sync interval (minutes)
                <input
                  type="number"
                  min="1"
                  value={form.syncIntervalMinutes}
                  onChange={(e) => setField("syncIntervalMinutes", e.target.value)}
                  disabled={ro}
                />
              </label>
            </div>
          </SettingsCard>

          <SettingsCard
            id="zh-set-webhook"
            title="Inbound webhook"
            refFn={(el) => (sectionRefs.current.webhook = el)}
            pill={
              config?.inboundSecretConfigured ? (
                <span className="status-pill ok">Secret configured</span>
              ) : (
                <span className="status-pill warn">Open — no secret set</span>
              )
            }
          >
            <p className="muted">
              Configure a Zoho Desk workflow rule to POST new DevOps Request tickets to this URL,
              sending the shared secret in the <code>X-Zoho-Secret</code> header.
            </p>
            <div className="sg-zh-url">
              <input readOnly value={webhookUrl} onFocus={(e) => e.target.select()} className="mono" />
              <CopyButton text={webhookUrl} />
            </div>
            <div className="settings-form sg-zh-setform">
              <label>
                Shared secret (X-Zoho-Secret header)
                <input
                  type="password"
                  value={form.inboundSecret}
                  onChange={(e) => setField("inboundSecret", e.target.value)}
                  placeholder={config?.inboundSecretConfigured ? "•••• (leave blank to keep)" : ""}
                  autoComplete="new-password"
                  disabled={ro}
                />
              </label>
            </div>
          </SettingsCard>

          <SettingsCard
            id="zh-set-writeback"
            title="Ticket write-back"
            refFn={(el) => (sectionRefs.current.writeback = el)}
            pill={
              <span className={`status-pill ${config?.ticketWritebackEnabled ? "ok" : "muted"}`}>
                {config?.ticketWritebackEnabled ? "On" : "Off"}
              </span>
            }
          >
            <p className="muted">
              When a deploy-automation run finishes, update its Desk ticket: set the status, post a
              comment + resolution describing the result, and reassign the ticket to the service
              account. Needs the token minted with <code>Desk.tickets.ALL</code>. Status labels must
              match your Desk config exactly (they're matched literally).
            </p>
            <div className="settings-form sg-zh-jform sg-zh-setform">
              <label className="checkbox-label">
                <input
                  type="checkbox"
                  checked={form.ticketWritebackEnabled}
                  onChange={(e) => setField("ticketWritebackEnabled", e.target.checked)}
                  disabled={ro}
                />
                Update the ticket when a run finishes
              </label>
              <label title="The agent every updated ticket is reassigned to">
                Owner email
                <input
                  value={form.ticketOwnerEmail}
                  onChange={(e) => setField("ticketOwnerEmail", e.target.value)}
                  placeholder="zagent@areeba.com"
                  disabled={ro}
                />
              </label>
              <div className="sg-zh-jrow4">
                <label>
                  Status — started
                  <input
                    value={form.ticketStatusStarted}
                    onChange={(e) => setField("ticketStatusStarted", e.target.value)}
                    placeholder="Open"
                    disabled={ro}
                  />
                </label>
                <label>
                  Status — deployed
                  <input
                    value={form.ticketStatusDeployed}
                    onChange={(e) => setField("ticketStatusDeployed", e.target.value)}
                    placeholder="Closed"
                    disabled={ro}
                  />
                </label>
                <label>
                  Status — failed
                  <input
                    value={form.ticketStatusFailed}
                    onChange={(e) => setField("ticketStatusFailed", e.target.value)}
                    placeholder="Failed"
                    disabled={ro}
                  />
                </label>
                <label>
                  Status — canceled
                  <input
                    value={form.ticketStatusCancelled}
                    onChange={(e) => setField("ticketStatusCancelled", e.target.value)}
                    placeholder="Canceled"
                    disabled={ro}
                  />
                </label>
              </div>
            </div>
          </SettingsCard>

          {dirty && canManage ? (
            <div className="sg-zh-savebar">
              <span>Unsaved configuration changes</span>
              <div className="sg-zh-savebar-actions">
                <button type="button" className="secondary" onClick={onDiscard}>
                  Discard
                </button>
                <button type="submit" className="primary" disabled={savingConfig}>
                  {savingConfig ? "Saving…" : "Save configuration"}
                </button>
              </div>
            </div>
          ) : null}
        </form>

        <JenkinsSection
          canManage={canManage}
          jenkins={jenkins}
          onSave={onSaveJenkins}
          saving={savingJenkins}
          onTest={onTestJenkins}
          testing={testingJenkins}
          refFn={(el) => (sectionRefs.current.jenkins = el)}
        />
      </div>
    </div>
  );
}

// Jenkins router connection — same fields as the old modal, inline as a group.
function JenkinsSection({ canManage, jenkins, onSave, saving, onTest, testing, refFn }) {
  const ro = !canManage;
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
        autoRunTickets: Boolean(jenkins.autoRunTickets),
        autoRunClusters: { ...(jenkins.autoRunClusters || {}) },
        imageTagTemplate: jenkins.imageTagTemplate || "{tag}",
        buildTimeoutMinutes: jenkins.buildTimeoutMinutes || 45,
        queueTimeoutMinutes: jenkins.queueTimeoutMinutes || 10,
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
    getZohoSourceClusters()
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
            <code>NAMESPACE</code> — the router runs the right build and reports back.
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
          Auto-rollback on rollout failure — admins are emailed on every failure either way
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
