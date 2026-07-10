import { HealthRow, healthTone } from "./ZohoHealthRow.jsx";
import { StatusPill, timeAgo } from "./common.jsx";
import { ACTIVE_RUN_STATUSES } from "./ZohoRunDetail.jsx";

const RUN_EVENT_TEXT = {
  awaiting_approval: ["warn", "awaiting approval"],
  deployed: ["ok", "deployed"],
  failed: ["danger", "failed"],
  cancelled: ["muted", "cancelled"],
};

// Merge runs, tickets and the last sync into one newest-first feed.
function buildFeed({ config, tickets, runs }) {
  const events = [];
  for (const run of runs) {
    if (!run.createdAt) continue;
    const [tone, verb] = RUN_EVENT_TEXT[run.status] ||
      (ACTIVE_RUN_STATUSES.has(run.status) ? ["info", "in progress"] : ["muted", run.status]);
    events.push({
      time: run.updatedAt || run.createdAt,
      tone,
      text: (
        <>
          <b>Run for {run.ticketNumber || `#${run.id}`}</b> {verb} — {run.deploymentName}{" "}
          <span className="mono">
            {run.changeType === "env_var"
              ? `${run.variableName}=${run.variableValue}`
              : run.imageTag}
          </span>{" "}
          to {run.namespace}
        </>
      ),
    });
  }
  for (const t of tickets) {
    if (!t.receivedAt) continue;
    events.push({
      time: t.receivedAt,
      tone: t.resolved ? "ok" : "danger",
      text: t.resolved ? (
        <>
          Ticket <b>{t.ticketNumber || t.ticketId}</b> resolved to{" "}
          <span className="mono">{t.deploymentName || t.targetName}</span>
          {t.namespace ? <> · {t.namespace}</> : null}
        </>
      ) : (
        <>
          Ticket <b>{t.ticketNumber || t.ticketId}</b> could not be resolved
          {t.rawAppValue ? <> — “{t.rawAppValue}” matches no deployment</> : null}
        </>
      ),
    });
  }
  if (config?.lastSyncAt) {
    events.push({
      time: config.lastSyncAt,
      tone: config.lastSyncStatus === "ok" ? "ok" : "danger",
      text: (
        <>
          <b>Field sync</b> {config.lastSyncMessage || "ran"}
        </>
      ),
    });
  }
  events.sort((a, b) => new Date(b.time) - new Date(a.time));
  return events.slice(0, 8);
}

const TONE_COLOR = {
  ok: "var(--ok)",
  warn: "var(--warn)",
  danger: "var(--danger)",
  info: "var(--info)",
  muted: "var(--border-strong)",
};

export default function ZohoOverviewTab({ config, tickets, runs }) {
  const feed = buildFeed({ config, tickets, runs });
  const checks = [
    config?.lastTestStatus === "ok",
    config?.lastSyncStatus === "ok",
    config?.cascadeEnabled === false || config?.lastDependencyStatus === "ok",
    Boolean(config?.sourceClusterId),
  ];
  const passing = checks.filter(Boolean).length;

  return (
    <div className="sg-zh-col2">
      <section className="card">
        <div className="card-header-row">
          <h3>Health checks</h3>
          <span className={`status-pill ${passing === checks.length ? "ok" : "warn"}`}>
            {passing} of {checks.length} passing
          </span>
        </div>
        <div className="sg-zh-health">
          <HealthRow
            tone={healthTone(config?.lastTestStatus)}
            title="Connection to Zoho Desk"
            message={
              config?.lastTestMessage ||
              (config?.lastTestStatus ? "" : "Never tested — run a connection test.")
            }
            right={<StatusPill status={config?.lastTestStatus} />}
          />
          <HealthRow
            tone={healthTone(config?.lastSyncStatus)}
            title="Last field sync"
            message={
              config?.lastSyncMessage || (config?.lastSyncStatus ? "" : "No sync has run yet.")
            }
            right={<StatusPill status={config?.lastSyncStatus} />}
            time={config?.lastSyncAt ? new Date(config.lastSyncAt).toLocaleString() : ""}
          />
          <HealthRow
            tone={
              config?.cascadeEnabled === false ? "muted" : healthTone(config?.lastDependencyStatus)
            }
            title="Cascade — Environment filters Application"
            message={
              config?.cascadeEnabled === false
                ? "Cascade disabled in the configuration."
                : config?.lastDependencyMessage || ""
            }
            right={
              config?.cascadeEnabled === false ? (
                <span className="status-pill muted">Off</span>
              ) : (
                <StatusPill status={config?.lastDependencyStatus} neverLabel="Pending" />
              )
            }
          />
          <HealthRow
            tone={config?.sourceClusterId ? "ok" : "warn"}
            title="Dropdown source"
            message={
              config?.sourceClusterId
                ? ""
                : "No source cluster selected yet — use “Choose namespaces” on the Environment field in Field sync."
            }
            tags={
              config?.sourceClusterId ? (
                <>
                  <span className="sg-tag">{config.sourceClusterId}</span>
                  <span className="sg-zh-count">
                    {(config?.selectedNamespaces || []).length} namespace(s)
                  </span>
                </>
              ) : null
            }
          />
        </div>
      </section>

      <section className="card">
        <div className="card-header-row">
          <h3>Activity</h3>
        </div>
        {feed.length === 0 ? (
          <p className="muted">Nothing yet — activity appears once syncs run and tickets arrive.</p>
        ) : (
          <ul className="sg-zh-feed">
            {feed.map((e, i) => (
              <li key={i}>
                <span className="sg-zh-ftime">{timeAgo(e.time)}</span>
                <span className="sg-zh-fled" style={{ background: TONE_COLOR[e.tone] }} />
                <span className="sg-zh-fbody">{e.text}</span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}
