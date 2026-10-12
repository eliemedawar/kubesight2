/** Approvals — everything waiting on a person, what was decided, and the rules.
 *
 *  One inbox for every kind of approval KubeSight asks for: deployment
 *  requests, change bundles, Hermes ticket actions and VM requests, CI approval
 *  stages and Cluster Builder destroys. "Waiting on me" is exactly what each
 *  kind's own rule lets this person decide now; the vote goes to that kind's
 *  own route, so no rule is re-implemented here. Rules gathers every setting
 *  that decides who approves what.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import ErrorBanner from "../components/common/ErrorBanner.jsx";
import RulesTab from "../components/approvals/RulesTab.jsx";
import { decideApproval, getApprovalsCount, getApprovalsHistory, getApprovalsInbox } from "../api/approvalsApi.js";
import { useRouteParam, useRouter } from "../routes/RouterContext.jsx";
import { setVisibleInterval } from "../lib/visibleInterval";
import { parseApiTime } from "../lib/apiTime.js";
import { initialsOf, waitingFor, formatDate, IconCheck, IconClock } from "../components/clusters/RequestsTable.jsx";

const TABS = [
  { key: "waiting", label: "Waiting on me" },
  { key: "pending", label: "All pending" },
  { key: "history", label: "History" },
  { key: "rules", label: "Rules" },
];
// Links from before the inbox existed (#/deployment-requests/active|history).
const LEGACY_TAB = { active: "waiting", history: "history" };

const KIND_FILTERS = [
  { key: "", label: "Everything" },
  { key: "deployment_request", label: "Deployment requests" },
  { key: "change_bundle", label: "Change bundles" },
  { key: "hermes", label: "Hermes tickets" },
  { key: "ci_stage", label: "CI approvals" },
  { key: "cluster_destroy", label: "Cluster destroys" },
];

const OUTCOME_TONE = {
  approved: "ok", executed: "ok", completed: "ok", success: "ok",
  rejected: "danger", declined: "danger", failed: "danger", error: "danger",
  expired: "muted", withdrawn: "muted", cancelled: "muted",
};

// Approving these changes something big at once: ask first.
const CONFIRM_APPROVE = {
  cluster_destroy: (item) => `Approve: ${item.title}? The VMs are deleted in vCenter.`,
  hermes: (item) => (item.vmRequest ? `Approve and create: ${item.title}?` : null),
};

// The API sends some UTC times without a zone; a browser would read them as local.
const asUtc = (value) => {
  const ms = parseApiTime(value);
  return Number.isFinite(ms) ? new Date(ms).toISOString() : value || null;
};
const normalize = (item) => ({
  ...item,
  requestedAt: asUtc(item.requestedAt),
  expiresAt: asUtc(item.expiresAt),
  decidedAt: asUtc(item.decidedAt),
});

function untilLabel(iso) {
  if (!iso) return null;
  const ms = new Date(iso).getTime() - Date.now();
  if (Number.isNaN(ms)) return null;
  if (ms <= 0) return "expired";
  const minutes = Math.round(ms / 60000);
  if (minutes < 60) return `${minutes} min left`;
  const hours = Math.round(minutes / 60);
  return hours < 48 ? `${hours} h left` : `${Math.round(hours / 24)} days left`;
}

function ApprovalCard({ item, busy, onDecide }) {
  const required = item.required ?? null;
  const approvals = item.approvals ?? 0;
  const left = untilLabel(item.expiresAt);
  return (
    <article className="sg-rq sg-ap-card" data-kind={item.kind}>
      <header>
        <div className="sg-rq-head-tags">
          <span className={`sg-ap-kind is-${item.kind}`}>{item.kindLabel}</span>
          <b>{item.title}</b>
          {required ? (
            <span className={`status-pill ${approvals > 0 ? "warn" : "info"}`}>
              {approvals} of {required} approval{required === 1 ? "" : "s"}
            </span>
          ) : null}
          {item.confidence ? <span className="sg-tag">confidence {item.confidence}</span> : null}
        </div>
        <span className="sg-rq-wait" title={item.requestedAt ? `Asked ${formatDate(item.requestedAt)}` : ""}>
          <IconClock />
          {item.requestedAt ? waitingFor(item.requestedAt) : "—"}
          {left ? <span className={`sg-ap-left${left === "expired" ? " is-late" : ""}`}> · {left}</span> : null}
        </span>
      </header>

      <div className="sg-rq-meta">
        <span className="sg-avatar sg-avatar--sm">{initialsOf(item.requestedBy || "?")}</span>
        <span>
          Asked by <b>{item.requestedBy || "someone"}</b>
          {item.where ? <> · <span className="mono">{item.where}</span></> : null}
        </span>
      </div>

      {item.summary ? <p className="sg-rq-msg">{item.summary}</p> : null}
      {item.details?.length ? (
        <ul className="sg-ap-details">
          {item.details.map((line, index) => <li key={`${index}-${line}`}>{line}</li>)}
        </ul>
      ) : null}

      <footer>
        <div className="sg-ap-foot-left">
          {item.href ? <a className="sg-ap-open" href={item.href}>Open where it lives</a> : null}
          {!item.canDecide && item.why ? <span className="muted sg-ap-why">{item.why}</span> : null}
        </div>
        {item.canDecide ? (
          <div className="sg-rq-actions">
            <button type="button" className="btn-danger-outline btn-compact" disabled={busy}
                    onClick={() => onDecide(item, "reject")}>
              {item.actions?.rejectLabel || "Reject"}
            </button>
            <button type="button" className="primary btn-compact" disabled={busy}
                    onClick={() => onDecide(item, "approve")}>
              <IconCheck className="sg-btn-ic" />
              Approve
            </button>
          </div>
        ) : null}
      </footer>
    </article>
  );
}

function KindFilter({ items, value, onChange }) {
  const counts = useMemo(() => {
    const out = {};
    items.forEach((item) => { out[item.kind] = (out[item.kind] || 0) + 1; });
    return out;
  }, [items]);
  return (
    <div className="sg-ap-filters" role="group" aria-label="Filter by kind">
      {KIND_FILTERS.filter((f) => !f.key || counts[f.key]).map((f) => (
        <button key={f.key || "all"} type="button"
                className={`sg-ap-chip btn-ghost${value === f.key ? " is-on" : ""}`}
                aria-pressed={value === f.key} onClick={() => onChange(f.key)}>
          {f.label}
          <span className="sg-ap-chip-n">{f.key ? counts[f.key] : items.length}</span>
        </button>
      ))}
    </div>
  );
}

function Inbox({ scope, onCounts }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busyKey, setBusyKey] = useState(null);
  const [kind, setKind] = useState("");

  const load = useCallback(async ({ silent = false } = {}) => {
    try {
      const next = await getApprovalsInbox(scope);
      setData({ ...next, items: (next.items || []).map(normalize) });
      onCounts?.(next.counts);
      if (!silent) setError("");
    } catch (err) {
      if (!silent) setError(err.message || "Could not load approvals.");
    }
  }, [scope, onCounts]);

  useEffect(() => { setData(null); load(); }, [load]);
  // Others vote by email, in Telegram or on other pages: keep the list honest.
  useEffect(() => setVisibleInterval(() => load({ silent: true }), 20000), [load]);

  const decide = async (item, action) => {
    let text = "";
    if (action === "reject" && item.actions?.rejectField) {
      const answer = window.prompt(`Why reject "${item.title}"? (optional)`, "");
      if (answer === null) return;
      text = answer;
    }
    if (action === "approve") {
      const question = CONFIRM_APPROVE[item.kind]?.(item);
      if (question && !window.confirm(question)) return;
    }
    setBusyKey(item.key);
    setError("");
    setNotice("");
    try {
      await decideApproval(item, action, text);
      setNotice(`${action === "approve" ? "Approved" : "Rejected"}: ${item.title}`);
      await load({ silent: true });
    } catch (err) {
      setError(err.serverMessage || err.message || `Could not ${action}.`);
      await load({ silent: true });
    } finally {
      setBusyKey(null);
    }
  };

  if (!data && !error) return <p className="muted">Loading approvals…</p>;
  // Never read "nothing is waiting" off a list that did not load.
  if (!data) return <ErrorBanner message={error} suppressAccessDenied={false} />;
  const items = (data?.items || []).filter((item) => !kind || item.kind === kind);
  return (
    <>
      {error ? <ErrorBanner message={error} suppressAccessDenied={false} onDismiss={() => setError("")} /> : null}
      {notice ? <p className="settings-panel-notice" role="status">{notice}</p> : null}
      {data?.unavailable?.length ? (
        <p className="sg-zh-inline-error">Could not read: {data.unavailable.join(", ")}.</p>
      ) : null}
      {data?.items?.length ? <KindFilter items={data.items} value={kind} onChange={setKind} /> : null}
      {items.length ? (
        <div className="sg-rq-list">
          {items.map((item) => (
            <ApprovalCard key={item.key} item={item} busy={busyKey === item.key} onDecide={decide} />
          ))}
        </div>
      ) : (
        <div className="sg-ap-empty">
          <b>{scope === "mine" ? "Nothing is waiting on you." : "Nothing is waiting for approval."}</b>
          <span className="muted">
            {scope === "mine" && data?.counts?.pending
              ? `${data.counts.pending} pending item${data.counts.pending === 1 ? "" : "s"} wait on someone else — see All pending.`
              : "Deployment requests, change bundles, Hermes tickets, CI approval stages and Cluster Builder destroys show up here."}
          </span>
        </div>
      )}
    </>
  );
}

function History() {
  const [kind, setKind] = useState("");
  const [items, setItems] = useState(null);
  const [error, setError] = useState("");
  useEffect(() => {
    let ignore = false;
    setItems(null);
    setError("");
    getApprovalsHistory(kind)
      .then((data) => { if (!ignore) setItems((data.items || []).map(normalize)); })
      .catch((err) => {
        if (ignore) return;
        setError(err.message || "Could not load the history.");
        setItems([]);
      });
    return () => { ignore = true; };
  }, [kind]);
  return (
    <>
      <div className="sg-ap-filters" role="group" aria-label="Filter by kind">
        {KIND_FILTERS.map((f) => (
          <button key={f.key || "all"} type="button"
                  className={`sg-ap-chip btn-ghost${kind === f.key ? " is-on" : ""}`}
                  aria-pressed={kind === f.key} onClick={() => setKind(f.key)}>
            {f.label}
          </button>
        ))}
      </div>
      {error ? <ErrorBanner message={error} suppressAccessDenied={false} /> : null}
      {items === null ? <p className="muted">Loading…</p> : items.length ? (
        <div className="table-wrap">
          <table className="data-table sg-ap-history">
            <thead>
              <tr><th>What</th><th>Where</th><th>Asked by</th><th>Outcome</th><th>Decided by</th><th>When</th></tr>
            </thead>
            <tbody>
              {items.map((item) => (
                <tr key={`${item.key}-${item.decidedAt}`}>
                  <td>
                    <span className={`sg-ap-kind is-${item.kind}`}>{item.kindLabel}</span>{" "}
                    {item.href ? <a href={item.href}>{item.title}</a> : item.title}
                    {item.summary ? <div className="muted sg-ap-hsum">{item.summary}</div> : null}
                  </td>
                  <td className="mono">{item.where || "—"}</td>
                  <td>{item.requestedBy || "—"}</td>
                  <td><span className={`status-pill ${OUTCOME_TONE[item.outcome] || "muted"}`}>{item.outcome}</span></td>
                  <td>{item.decidedBy || "—"}</td>
                  <td className="sg-ap-when">{item.decidedAt ? formatDate(item.decidedAt) : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : error ? null : <p className="muted">Nothing decided yet.</p>}
    </>
  );
}

export default function ApprovalsPage({ clusters = [] }) {
  const { route, navigate } = useRouter();
  const [rawTab, setTab] = useRouteParam("tab", "waiting");
  const tab = LEGACY_TAB[rawTab] || rawTab;
  const [counts, setCounts] = useState(null);

  // Landing on History or Rules still shows how much is waiting; the inbox
  // tabs refresh the numbers themselves.
  useEffect(() => {
    let alive = true;
    getApprovalsCount()
      .then((data) => { if (alive) setCounts((prev) => prev || data); })
      .catch(() => {});
    return () => { alive = false; };
  }, []);

  // An old link (#/deployment-requests/active) lands on its new home.
  useEffect(() => {
    if (route.key === "deploymentRequestsLegacy") {
      navigate({ key: "deploymentRequests", params: { tab: LEGACY_TAB[rawTab] || "waiting" } }, { replace: true });
    }
  }, [route.key, rawTab, navigate]);

  return (
    <div className="ops-page">
      <section className="card ops-section">
        <div className="sg-ph">
          <div>
            <h2>Approvals</h2>
            <p className="sg-ph-sub">
              Everything waiting on a person — deploys, change bundles, Hermes tickets, CI approval stages and
              Cluster Builder destroys — and the rules that decide who approves.
            </p>
          </div>
        </div>

        <nav className="tab-bar" aria-label="Approval views">
          {TABS.map((item) => (
            <button key={item.key} type="button" className={tab === item.key ? "active" : ""}
                    onClick={() => setTab(item.key)}>
              {item.label}
              {item.key === "waiting" && Number.isFinite(counts?.waitingOnMe) ? (
                <span className={`sg-cnt${counts.waitingOnMe === 0 ? " sg-cnt--zero" : ""}`}>{counts.waitingOnMe}</span>
              ) : null}
              {item.key === "pending" && Number.isFinite(counts?.pending) ? (
                <span className={`sg-cnt${counts.pending === 0 ? " sg-cnt--zero" : ""}`}>{counts.pending}</span>
              ) : null}
            </button>
          ))}
        </nav>

        {tab === "waiting" ? <Inbox scope="mine" onCounts={setCounts} /> : null}
        {tab === "pending" ? <Inbox scope="all" onCounts={setCounts} /> : null}
        {tab === "history" ? <History /> : null}
        {tab === "rules" ? <RulesTab clusters={clusters} /> : null}
      </section>
    </div>
  );
}
