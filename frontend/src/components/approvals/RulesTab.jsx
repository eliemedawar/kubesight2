/** Approvals → Rules: every setting that decides who approves what, in one place.
 *
 *  Who approves (approver groups + extra emails), how many approvals each
 *  cluster needs, Hermes' approval rules (confidence bar, expiry, Telegram
 *  deploy approvers, VM approvers), Cluster Builder's fixed rule, and — read
 *  only, with links — the rules that belong to one pipeline or one promotion
 *  environment. Writes go through the settings routes the gates already read.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import ErrorBanner from "../common/ErrorBanner.jsx";
import { updateDeploymentRequestRecipients } from "../../api";
import { updateTicketAgentSettings } from "../../api/ticketAgentApi.js";
import {
  createApproverGroup,
  deleteApproverGroup,
  getApprovalRules,
  updateApproverGroup,
} from "../../api/approvalsApi.js";
import { confirmDialog } from "../../lib/dialogs.js";

const SOURCE_LABELS = {
  configuredGroups: "the approver groups marked “Approves deploys”",
  configuredGroupsAndEmails: "the approver groups plus the extra emails",
  configured: "the extra emails",
  env: "the DEPLOYMENT_REQUEST_RECIPIENTS environment variable",
  receiverGroup: "the “Deployment Approvers” group",
  allReceivers: "every enabled email receiver (nothing is chosen here yet)",
  smtpFrom: "the SMTP from-address (nothing is chosen here yet)",
  none: "nobody — requests are not emailed",
};

const MODE_TONE = { enforce: "danger", warn: "warn", off: "muted" };

function Section({ title, lede, right, message, onDismiss, children }) {
  return (
    <section className="sg-ap-rule">
      <header className="sg-ap-rule-head">
        <div>
          <h3>{title}</h3>
          {lede ? <p className="muted">{lede}</p> : null}
        </div>
        {right}
      </header>
      {/* A save's outcome shows in the section that was saved, not at the top
          of a long page where nobody looking at the Save button would see it. */}
      {message?.isError ? (
        <ErrorBanner message={message.text} suppressAccessDenied={false} onDismiss={onDismiss} />
      ) : message ? (
        <p className="settings-panel-notice" role="status">{message.text}</p>
      ) : null}
      {children}
    </section>
  );
}

const parseEmails = (value) =>
  String(value || "").split(/[\n,;]+/).map((e) => e.trim()).filter(Boolean);

// ---------------------------------------------------------------------------
// Approver groups
// ---------------------------------------------------------------------------

function GroupEditor({ initial, options, onSave, onCancel, busy }) {
  const [form, setForm] = useState(initial);
  const [search, setSearch] = useState("");
  const set = (key, value) => setForm((prev) => ({ ...prev, [key]: value }));
  const toggle = (key, id) =>
    set(key, form[key].includes(id) ? form[key].filter((x) => x !== id) : [...form[key], id]);
  const users = options.users.filter((u) =>
    !search.trim() || `${u.name} ${u.email}`.toLowerCase().includes(search.trim().toLowerCase())
  );
  return (
    <div className="sg-ap-group-edit">
      <div className="sg-ap-grid2">
        <label>
          Group name
          <input value={form.name} onChange={(e) => set("name", e.target.value)} placeholder="Release managers" />
        </label>
        <label>
          What it is for
          <input value={form.description} onChange={(e) => set("description", e.target.value)} />
        </label>
      </div>
      <div className="sg-ap-grid2">
        <fieldset className="sg-ap-pick">
          <legend>People ({form.userIds.length})</legend>
          <input type="search" placeholder="Search people" value={search} onChange={(e) => setSearch(e.target.value)} />
          <div className="sg-ap-pick-list">
            {users.map((u) => (
              <label key={u.id} className="checkbox-label">
                <input type="checkbox" checked={form.userIds.includes(u.id)} onChange={() => toggle("userIds", u.id)} />
                <span>{u.name} <span className="muted">{u.email}</span></span>
              </label>
            ))}
            {!users.length ? <p className="muted">Nobody matches.</p> : null}
          </div>
        </fieldset>
        <div className="sg-ap-stack">
          <fieldset className="sg-ap-pick">
            <legend>Roles ({form.roleIds.length})</legend>
            <p className="field-hint">Everyone active in the role, now and later.</p>
            <div className="sg-ap-pick-list is-short">
              {options.roles.map((r) => (
                <label key={r.id} className="checkbox-label">
                  <input type="checkbox" checked={form.roleIds.includes(r.id)} onChange={() => toggle("roleIds", r.id)} />
                  <span>{r.name}</span>
                </label>
              ))}
            </div>
          </fieldset>
          <label>
            Emails (people without a KubeSight account)
            <textarea rows={3} value={form.emails} onChange={(e) => set("emails", e.target.value)}
                      placeholder="cto@company.com" />
          </label>
        </div>
      </div>
      <label className="checkbox-label">
        <input type="checkbox" checked={form.approvesDeploys} onChange={(e) => set("approvesDeploys", e.target.checked)} />
        Approves deploys — deployment requests and change bundles go to this group
      </label>
      <div className="sg-ap-actions">
        <button type="button" className="primary" disabled={busy || !form.name.trim()} onClick={() => onSave(form)}>
          {busy ? "Saving…" : initial.id ? "Save group" : "Add group"}
        </button>
        <button type="button" className="btn-ghost" onClick={onCancel}>Cancel</button>
      </div>
    </div>
  );
}

function groupForm(group) {
  return {
    id: group?.id || null,
    name: group?.name || "",
    description: group?.description || "",
    userIds: (group?.users || []).map((u) => u.id),
    roleIds: (group?.roles || []).map((r) => r.id),
    emails: (group?.emails || []).join("\n"),
    approvesDeploys: group ? Boolean(group.approvesDeploys) : true,
  };
}

function ApproverGroups({ groups, canEdit, onChanged, notify }) {
  const [editing, setEditing] = useState(null);
  const [busy, setBusy] = useState(false);
  const [deletingId, setDeletingId] = useState(null);
  const save = async (form) => {
    setBusy(true);
    try {
      const payload = {
        name: form.name.trim(),
        description: form.description,
        userIds: form.userIds,
        roleIds: form.roleIds,
        emails: parseEmails(form.emails),
        approvesDeploys: form.approvesDeploys,
      };
      if (form.id) await updateApproverGroup(form.id, payload);
      else await createApproverGroup(payload);
      setEditing(null);
      notify(`Saved ${payload.name}.`);
      onChanged();
    } catch (err) {
      notify(err.serverMessage || err.message, true);
    } finally {
      setBusy(false);
    }
  };
  const remove = async (group) => {
    const used = group.alertPolicies?.length ? ` It also receives alerts for: ${group.alertPolicies.join(", ")}.` : "";
    if (!(await confirmDialog({
      title: "Delete approver group",
      message: `Delete the group ${group.name}?${used}`,
      confirmLabel: "Delete",
      tone: "danger",
    }))) return;
    setDeletingId(group.id);
    try {
      await deleteApproverGroup(group.id);
      notify(`Deleted ${group.name}.`);
      await onChanged();
    } catch (err) {
      notify(err.serverMessage || err.message, true);
    } finally {
      setDeletingId(null);
    }
  };
  const items = groups?.items || [];
  return (
    <>
      {items.map((group) => (
        editing?.id === group.id ? (
          <GroupEditor key={group.id} initial={editing} options={groups.options} busy={busy}
                       onSave={save} onCancel={() => setEditing(null)} />
        ) : (
          <div key={group.id} className={`sg-ap-group${group.enabled ? "" : " is-off"}`}>
            <div className="sg-ap-group-main">
              <div className="sg-ap-group-name">
                <b>{group.name}</b>
                {group.approvesDeploys
                  ? <span className="status-pill ok">Approves deploys</span>
                  : <span className="status-pill muted">Not approving deploys</span>}
                {group.alertPolicies?.length ? <span className="sg-tag">also alerts: {group.alertPolicies.length}</span> : null}
              </div>
              {group.description ? <span className="muted">{group.description}</span> : null}
              <div className="sg-ap-members">
                {group.users.map((u) => <span key={`u${u.id}`} className="sg-ap-member">{u.name}</span>)}
                {group.roles.map((r) => <span key={`r${r.id}`} className="sg-ap-member is-role">role · {r.name}</span>)}
                {group.emails.map((e) => <span key={e} className="sg-ap-member is-email">{e}</span>)}
                {group.otherMembers.map((m) => <span key={`o${m.id}`} className="sg-ap-member is-other">{m.type} · {m.name}</span>)}
                {!group.users.length && !group.roles.length && !group.emails.length
                  ? <span className="muted">No people yet.</span> : null}
              </div>
              <span className="field-hint">
                {group.resolvedEmails.length} address{group.resolvedEmails.length === 1 ? "" : "es"} today
              </span>
            </div>
            {canEdit ? (
              <div className="sg-ap-actions">
                <button type="button" className="btn-ghost btn-sm" onClick={() => setEditing(groupForm(group))}>Edit</button>
                <button type="button" className="btn-ghost btn-sm" disabled={deletingId === group.id}
                        onClick={() => remove(group)}>
                  {deletingId === group.id ? "Deleting…" : "Delete"}
                </button>
              </div>
            ) : null}
          </div>
        )
      ))}
      {!items.length && !editing ? <p className="muted">No approver groups yet.</p> : null}
      {editing && !editing.id ? (
        <GroupEditor initial={editing} options={groups.options} busy={busy} onSave={save} onCancel={() => setEditing(null)} />
      ) : canEdit && !editing ? (
        <div className="sg-ap-actions">
          <button type="button" className="btn-outline" onClick={() => setEditing(groupForm(null))}>Add an approver group</button>
        </div>
      ) : null}
    </>
  );
}

// ---------------------------------------------------------------------------
// Clusters
// ---------------------------------------------------------------------------

function clusterForm(config) {
  return {
    required: String(config.requiredApprovals ?? 1),
    overrides: Object.fromEntries(
      Object.entries(config.clusterApprovals || {}).map(([k, v]) => [k, String(v)])
    ),
    emails: (config.recipients || []).join("\n"),
  };
}

function ClusterRules({ config, canEdit, onSaved, notify }) {
  const [form, setForm] = useState(() => clusterForm(config));
  const [busy, setBusy] = useState(false);
  useEffect(() => setForm(clusterForm(config)), [config]);
  const initial = useMemo(() => JSON.stringify(clusterForm(config)), [config]);
  const dirty = JSON.stringify(form) !== initial;
  const globalDefault = Math.max(0, Number.parseInt(form.required, 10) || 0);
  const pool = config.resolvedRecipients || [];

  const save = async () => {
    setBusy(true);
    try {
      const clusterApprovals = {};
      Object.entries(form.overrides).forEach(([id, value]) => {
        if (value === "" || value === null || value === undefined) return;
        clusterApprovals[id] = Math.max(0, Number.parseInt(value, 10) || 0);
      });
      await updateDeploymentRequestRecipients({
        recipients: parseEmails(form.emails),
        groupIds: config.groupIds || [],
        requiredApprovals: globalDefault,
        clusterApprovals,
      });
      notify("Cluster approvals saved.");
      onSaved();
    } catch (err) {
      notify(err.serverMessage || err.message, true);
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <p className="sg-ap-pool">
        Requests go to <b>{pool.length ? `${pool.length} people` : "nobody"}</b> — {SOURCE_LABELS[config.source] || config.source}.
        {pool.length ? <span className="muted"> {pool.join(", ")}</span> : null}
        {!config.smtpConfigured ? <span className="sg-zh-inline-error"> SMTP is not configured, so no email is delivered.</span> : null}
      </p>
      <div className="sg-ap-grid2">
        <label>
          Approvals needed, by default
          <input type="number" min={0} value={form.required} disabled={!canEdit}
                 onChange={(e) => setForm((p) => ({ ...p, required: e.target.value }))} />
          <span className="field-hint">
            {globalDefault === 0
              ? "0 = no approval: changes go straight through."
              : `${globalDefault} different approver${globalDefault === 1 ? "" : "s"}; never the person who asked.`}
          </span>
        </label>
        <label>
          Extra emails (outside any group)
          <textarea rows={2} value={form.emails} disabled={!canEdit}
                    onChange={(e) => setForm((p) => ({ ...p, emails: e.target.value }))} />
        </label>
      </div>
      <div className="table-wrap">
        <table className="data-table sg-ap-clusters">
          <thead><tr><th>Cluster</th><th>Approvals needed</th><th>In effect</th></tr></thead>
          <tbody>
            {(config.clusters || []).map((row) => {
              const value = form.overrides[row.id] ?? "";
              const effective = value === "" ? globalDefault : Math.max(0, Number.parseInt(value, 10) || 0);
              return (
                <tr key={row.id}>
                  <td>{row.name}{row.missing ? <span className="muted"> (not connected)</span> : null}</td>
                  <td>
                    <input type="number" min={0} className="sg-ap-num" value={value} placeholder={`${globalDefault} (default)`}
                           disabled={!canEdit} aria-label={`Approvals needed for ${row.name}`}
                           onChange={(e) => setForm((p) => ({ ...p, overrides: { ...p.overrides, [row.id]: e.target.value } }))} />
                  </td>
                  <td>
                    <span className={`status-pill ${effective === 0 ? "muted" : "info"}`}>
                      {effective === 0 ? "No approval" : `${effective} approval${effective === 1 ? "" : "s"}`}
                    </span>
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <details className="sg-ap-gates">
        <summary>What this count gates</summary>
        <ul>{(config.gates || []).map((g) => <li key={g}>{g}</li>)}</ul>
        <p className="field-hint">
          A direct change on a gated cluster is queued as a change bundle and waits up to {config.changeApprovalTtlHours} h
          for its approvals.
        </p>
      </details>
      {canEdit && dirty ? (
        <div className="sg-ap-actions">
          <button type="button" className="primary" disabled={busy} onClick={save}>{busy ? "Saving…" : "Save cluster approvals"}</button>
          <button type="button" className="btn-ghost" onClick={() => setForm(clusterForm(config))}>Discard</button>
        </div>
      ) : null}
    </>
  );
}

// ---------------------------------------------------------------------------
// Hermes
// ---------------------------------------------------------------------------

function hermesForm(h) {
  return {
    minConfidence: h.minConfidence,
    approvalTimeoutHours: String(h.approvalTimeoutHours),
    telegramApprovers: h.telegramApprovers,
    vmRequestsEnabled: h.vmRequestsEnabled,
    vmApprovers: h.vmApprovers,
  };
}

function HermesRules({ hermes, canEdit, onSaved, notify }) {
  const [form, setForm] = useState(() => hermesForm(hermes));
  const [busy, setBusy] = useState(false);
  useEffect(() => setForm(hermesForm(hermes)), [hermes]);
  const dirty = JSON.stringify(form) !== JSON.stringify(hermesForm(hermes));
  const set = (key, value) => setForm((p) => ({ ...p, [key]: value }));
  const save = async () => {
    setBusy(true);
    try {
      await updateTicketAgentSettings({ ...form, approvalTimeoutHours: Number(form.approvalTimeoutHours) });
      notify("Hermes approval rules saved.");
      onSaved();
    } catch (err) {
      notify(err.serverMessage || err.message, true);
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <p className="sg-ap-pool">
        Hermes is <b>{hermes.active ? "handling tickets" : "off"}</b>; Telegram approvals are{" "}
        <b>{hermes.telegramReady ? "set up" : "not set up"}</b>.{" "}
        <a href={hermes.integrationHref}>Bot, chat and Hermes connection</a>
      </p>
      <div className="sg-ap-grid2">
        <label>
          A change runs without a person at
          <select value={form.minConfidence} disabled={!canEdit} onChange={(e) => set("minConfidence", e.target.value)}>
            <option value="High">High confidence only</option>
            <option value="Medium">Medium or High</option>
          </select>
          <span className="field-hint">Below it — or when Hermes disagrees with the ticket&apos;s own fields — it asks for approval.</span>
        </label>
        <label>
          An unanswered approval expires after (hours)
          <input type="number" min={1} max={168} value={form.approvalTimeoutHours} disabled={!canEdit}
                 onChange={(e) => set("approvalTimeoutHours", e.target.value)} />
        </label>
        <label>
          Deploy approvers on Telegram
          <input value={form.telegramApprovers} disabled={!canEdit} placeholder="@devops_lead, 123456789"
                 onChange={(e) => set("telegramApprovers", e.target.value)} />
          <span className="field-hint">Empty = anyone in the approval chat. In KubeSight: anyone with ticketing:manage.</span>
        </label>
        <label>
          VM approvers on Telegram
          <input value={form.vmApprovers} disabled={!canEdit} placeholder="@infra_lead"
                 onChange={(e) => set("vmApprovers", e.target.value)} />
          <span className="field-hint">
            Empty = nobody on Telegram. In KubeSight: anyone with cluster_builds:execute. VM requests always need approval.
          </span>
        </label>
      </div>
      <label className="checkbox-label">
        <input type="checkbox" checked={form.vmRequestsEnabled} disabled={!canEdit}
               onChange={(e) => set("vmRequestsEnabled", e.target.checked)} />
        Let Hermes ask for new VMs from tickets (profiles in Cluster Builder → Sources)
      </label>
      {canEdit && dirty ? (
        <div className="sg-ap-actions">
          <button type="button" className="primary" disabled={busy} onClick={save}>{busy ? "Saving…" : "Save Hermes rules"}</button>
          <button type="button" className="btn-ghost" onClick={() => setForm(hermesForm(hermes))}>Discard</button>
        </div>
      ) : null}
    </>
  );
}

// ---------------------------------------------------------------------------
// The tab
// ---------------------------------------------------------------------------

export default function RulesTab() {
  const [rules, setRules] = useState(null);
  const [error, setError] = useState("");
  // { section, text, isError } — the last save's outcome, shown in its section.
  const [message, setMessage] = useState(null);
  const load = useCallback(async () => {
    try {
      setRules(await getApprovalRules());
      setError("");
    } catch (err) {
      setError(err.message || "Could not load the approval rules.");
    }
  }, []);
  useEffect(() => { load(); }, [load]);
  const notifyIn = useCallback(
    (section) => (text, isError = false) => setMessage({ section, text, isError }),
    []
  );
  const sectionMessage = (section) => ({
    message: message?.section === section ? message : null,
    onDismiss: () => setMessage(null),
  });

  if (!rules) return error ? <ErrorBanner message={error} suppressAccessDenied={false} /> : <p className="muted">Loading rules…</p>;
  const perms = rules.permissions || {};
  const ci = rules.elsewhere?.ciStages;
  const promotions = rules.elsewhere?.promotions;
  return (
    <div className="sg-ap-rules">
      {error ? <ErrorBanner message={error} suppressAccessDenied={false} onDismiss={() => setError("")} /> : null}

      {rules.groups ? (
        <Section title="Who approves" {...sectionMessage("groups")}
                 lede="Approver groups: people, roles and emails. A group marked “Approves deploys” gets every deployment request and change bundle.">
          <ApproverGroups groups={rules.groups} canEdit={perms.editGroups} onChanged={load} notify={notifyIn("groups")} />
        </Section>
      ) : null}

      {rules.clusters ? (
        <Section title="Clusters" {...sectionMessage("clusters")} lede="How many different approvers a change to each cluster needs.">
          <ClusterRules config={rules.clusters} canEdit={perms.editClusters} onSaved={load} notify={notifyIn("clusters")} />
        </Section>
      ) : null}

      {rules.hermes ? (
        <Section title="Hermes tickets" {...sectionMessage("hermes")} lede="When a ticket action needs a person, and who that is.">
          <HermesRules hermes={rules.hermes} canEdit={perms.editHermes} onSaved={load} notify={notifyIn("hermes")} />
        </Section>
      ) : null}

      {rules.clusterBuilder ? (
        <Section title="Cluster Builder" lede="Fixed rules, shown so they are not a surprise.">
          <ul className="sg-ap-fixed">
            <li><b>Destroying VMs:</b> {rules.clusterBuilder.destroy}</li>
            <li><b>VMs from tickets:</b> {rules.clusterBuilder.vmRequests}</li>
            <li><b>Day two:</b> {rules.clusterBuilder.dayTwo}</li>
          </ul>
        </Section>
      ) : null}

      {ci || promotions ? (
        <Section title="Set where they belong"
                 lede="These rules belong to one pipeline or one environment, so they are edited there. All of them, at a glance:">
          {ci ? (
            <>
              <h4 className="sg-ap-sub">CI approval stages</h4>
              {ci.length ? (
                <div className="table-wrap">
                  <table className="data-table">
                    <thead><tr><th>Service</th><th>Pipeline</th><th>Stage</th><th>Who approves</th><th>Needs</th><th>Waits</th><th /></tr></thead>
                    <tbody>
                      {ci.map((row) => (
                        <tr key={`${row.href}-${row.pipeline}-${row.stage}`} className={row.enabled ? "" : "is-off"}>
                          <td>{row.service}</td>
                          <td>{row.pipeline}</td>
                          <td>{row.stage}</td>
                          <td>{row.approvers}{row.allowSelfApproval ? <span className="muted"> · may approve own build</span> : null}</td>
                          <td>{row.minApprovals ?? "—"}</td>
                          <td>{row.timeoutMinutes} min</td>
                          <td><a href={row.href}>Edit</a></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : <p className="muted">No pipeline has an Approval stage.</p>}
            </>
          ) : null}
          {promotions ? (
            <>
              <h4 className="sg-ap-sub">Promotion ladder</h4>
              {promotions.length ? (
                <div className="sg-ap-envs">
                  {promotions.map((env) => (
                    <a key={env.environment} className="sg-ap-env" href={env.href}>
                      <b>{env.environment}</b>
                      <span className={`status-pill ${MODE_TONE[env.mode] || "muted"}`}>{env.mode}</span>
                      {env.minSoakMinutes ? <span className="muted">soak {env.minSoakMinutes} min</span> : null}
                    </a>
                  ))}
                </div>
              ) : <p className="muted">No promotion environments yet.</p>}
              <p className="field-hint">
                Enforce: skipping an environment needs an approved exception (a change bundle, approved by the cluster&apos;s approvers).
              </p>
            </>
          ) : null}
        </Section>
      ) : null}
    </div>
  );
}
