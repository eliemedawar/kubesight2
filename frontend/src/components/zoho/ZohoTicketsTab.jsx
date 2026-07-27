import { useMemo, useState } from "react";
import EmptyState from "../common/EmptyState.jsx";
import { useTicketing } from "../ticketing/TicketingContext.jsx";
import { IconChevronRight, IconPlay, IconTrash } from "./icons.jsx";
import { CopyButton } from "./common.jsx";
import { ACTIVE_RUN_STATUSES, RunDetail, RunStatusPill } from "./ZohoRunDetail.jsx";

const FILTERS = [
  { key: "all", label: "All" },
  { key: "unresolved", label: "Unresolved" },
  { key: "active", label: "Running" },
  { key: "approval", label: "Awaiting approval" },
];

// Inbound room: webhook setup strip, triage filters, and one table where each
// ticket owns its automation runs (expand a row to see the pipeline).
export default function ZohoTicketsTab({
  canManage,
  tickets,
  ticketsLoading,
  runs,
  runsLoading,
  webhookUrl,
  inboundSecretConfigured,
  onRunTicket,
  startingTicketId,
  onCancelRun,
  cancellingRunId,
  onDeleteTicket,
  deletingTicketId,
}) {
  const { name: providerName } = useTicketing();
  const [filter, setFilter] = useState("all");
  const [search, setSearch] = useState("");
  const [expanded, setExpanded] = useState(() => new Set());

  // All runs per ticket, newest-first (runs arrive newest-first from the API).
  const runsByTicket = useMemo(() => {
    const map = new Map();
    for (const r of runs) {
      if (r.ticketRecordId == null) continue;
      if (!map.has(r.ticketRecordId)) map.set(r.ticketRecordId, []);
      map.get(r.ticketRecordId).push(r);
    }
    return map;
  }, [runs]);

  // Runs whose ticket is no longer in the log (deleted entries) stay visible.
  const orphanRuns = useMemo(() => {
    const ids = new Set(tickets.map((t) => t.id));
    return runs.filter((r) => r.ticketRecordId == null || !ids.has(r.ticketRecordId));
  }, [runs, tickets]);

  const counts = useMemo(() => {
    const latest = (t) => runsByTicket.get(t.id)?.[0];
    return {
      all: tickets.length,
      unresolved: tickets.filter((t) => !t.resolved).length,
      active: tickets.filter((t) => ACTIVE_RUN_STATUSES.has(latest(t)?.status)).length,
      approval: tickets.filter((t) => latest(t)?.status === "awaiting_approval").length,
    };
  }, [tickets, runsByTicket]);

  const query = search.trim().toLowerCase();
  const visible = tickets.filter((t) => {
    const latest = runsByTicket.get(t.id)?.[0];
    if (filter === "unresolved" && t.resolved) return false;
    if (filter === "active" && !ACTIVE_RUN_STATUSES.has(latest?.status)) return false;
    if (filter === "approval" && latest?.status !== "awaiting_approval") return false;
    if (!query) return true;
    return [
      t.ticketNumber,
      t.ticketId,
      t.deploymentName,
      t.targetName,
      t.rawAppValue,
      t.namespace,
      t.tag,
      t.variableName,
    ].some((v) => (v || "").toString().toLowerCase().includes(query));
  });

  const toggle = (id) => {
    setExpanded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  // A runnable ticket carries exactly one change: a tag OR a variable+value.
  const ticketChange = (t) => {
    const tag = (t.tag || "").trim();
    const variable = (t.variableName || "").trim();
    const value = (t.variableValue || "").trim();
    if (tag && !variable) return { kind: "image", label: tag };
    if (variable && value && !tag) return { kind: "env_var", label: `${variable}=${value}` };
    return null;
  };

  const canRun = (ticket, latest) =>
    canManage && ticket.resolved && ticketChange(ticket) && !ACTIVE_RUN_STATUSES.has(latest?.status);

  return (
    <>
      <div className="sg-zh-hookstrip">
        {inboundSecretConfigured ? (
          <span className="status-pill ok">Webhook secret set</span>
        ) : (
          <span className="status-pill warn">Webhook open — no secret</span>
        )}
        <span className="sg-zh-hookstrip-text">{providerName} posts new tickets to</span>
        <span className="sg-zh-hookurl mono">{webhookUrl}</span>
        <CopyButton text={webhookUrl} label="Copy" className="sg-zh-hookcopy" />
      </div>

      <section className="card">
        <div className="sg-zh-tfilters">
          {FILTERS.map((f) => (
            <button
              key={f.key}
              type="button"
              className={`sg-zh-fpill ${filter === f.key ? "sg-zh-fpill--on" : ""}`}
              onClick={() => setFilter(f.key)}
            >
              {f.label} <span className="sg-zh-fpill-n">{counts[f.key]}</span>
            </button>
          ))}
          <input
            type="search"
            className="sg-zh-filter sg-zh-tsearch"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
            placeholder="Search ticket, app, tag…"
            aria-label="Search tickets"
          />
        </div>

        {ticketsLoading ? (
          <p className="muted">Loading…</p>
        ) : tickets.length === 0 ? (
          <EmptyState
            message={`No tickets received yet — configure the ${providerName} webhook to POST to the URL above.`}
          />
        ) : visible.length === 0 ? (
          <EmptyState message="No tickets match the current filter." />
        ) : (
          <div className="table-wrap sg-zh-tscroll">
            <table className="data-table sg-zh-ttable">
              <thead>
                <tr>
                  <th aria-label="Expand" className="sg-zh-thchev" />
                  <th>Ticket</th>
                  <th>Received</th>
                  <th>Deployment</th>
                  <th>Change</th>
                  <th>Resolution</th>
                  <th>Automation</th>
                  {canManage ? <th aria-label="Actions" /> : null}
                </tr>
              </thead>
              <tbody>
                {visible.map((t) => {
                  const ticketRuns = runsByTicket.get(t.id) || [];
                  const latest = ticketRuns[0];
                  const isOpen = expanded.has(t.id) && ticketRuns.length > 0;
                  const cols = canManage ? 8 : 7;
                  return [
                    <tr
                      key={t.id}
                      className={`sg-zh-trow ${ticketRuns.length ? "sg-zh-trow--exp" : ""} ${
                        isOpen ? "sg-zh-trow--open" : ""
                      }`}
                      onClick={ticketRuns.length ? () => toggle(t.id) : undefined}
                    >
                      <td className="sg-zh-tdchev">
                        {ticketRuns.length ? (
                          <button
                            type="button"
                            className="btn-ghost sg-zh-chev"
                            aria-expanded={isOpen}
                            aria-label={`${isOpen ? "Collapse" : "Expand"} runs for ticket ${
                              t.ticketNumber || t.ticketId || t.id
                            }`}
                            onClick={(e) => {
                              e.stopPropagation();
                              toggle(t.id);
                            }}
                          >
                            <IconChevronRight />
                          </button>
                        ) : null}
                      </td>
                      <td>{t.ticketNumber || t.ticketId || "—"}</td>
                      <td className="sg-zh-htime">
                        {t.receivedAt ? new Date(t.receivedAt).toLocaleString() : ""}
                      </td>
                      <td>
                        {t.resolved ? (
                          <span className="mono" title={t.targetName || ""}>
                            {t.deploymentName || t.targetName || `#${t.targetId}`}
                            {t.namespace ? ` (${t.namespace})` : ""}
                          </span>
                        ) : (
                          <span className="muted">{t.rawAppValue || "—"}</span>
                        )}
                      </td>
                      <td>
                        {(() => {
                          const change = ticketChange(t);
                          if (change) {
                            return (
                              <span
                                className={`sg-tag ${change.kind === "env_var" ? "mono" : ""}`}
                                title={change.kind === "env_var" ? "variable change" : "image tag"}
                              >
                                {change.label}
                              </span>
                            );
                          }
                          return t.tag || t.variableName ? (
                            <span className="muted" title={t.error || ""}>
                              {[t.tag, t.variableName].filter(Boolean).join(" / ")}
                            </span>
                          ) : (
                            "—"
                          );
                        })()}
                      </td>
                      <td>
                        {t.resolved && t.error ? (
                          <span className="status-pill warn" title={t.error}>
                            Needs attention
                          </span>
                        ) : t.resolved ? (
                          <span className="status-pill ok">Resolved</span>
                        ) : (
                          <span className="status-pill danger" title={t.error || ""}>
                            Unresolved
                          </span>
                        )}
                      </td>
                      <td>
                        {latest ? (
                          <>
                            <RunStatusPill status={latest.status} />
                            {ticketRuns.length > 1 ? (
                              <span className="sg-zh-runcount">×{ticketRuns.length}</span>
                            ) : null}
                          </>
                        ) : (
                          <span className="muted">—</span>
                        )}
                      </td>
                      {canManage ? (
                        <td className="sg-zh-tactions" onClick={(e) => e.stopPropagation()}>
                          {canRun(t, latest) ? (
                            <button
                              type="button"
                              className="btn-ghost sg-zh-trun"
                              onClick={() => onRunTicket(t)}
                              disabled={startingTicketId === t.id}
                              title="Run deploy automation for this ticket"
                              aria-label={`Run automation for ticket ${
                                t.ticketNumber || t.ticketId || t.id
                              }`}
                            >
                              <IconPlay />
                            </button>
                          ) : null}
                          <button
                            type="button"
                            className="btn-ghost sg-zh-tdel"
                            onClick={() => onDeleteTicket(t)}
                            disabled={deletingTicketId === t.id}
                            title="Delete this entry from the inbound log"
                            aria-label={`Delete ticket ${t.ticketNumber || t.ticketId || t.id}`}
                          >
                            <IconTrash />
                          </button>
                        </td>
                      ) : null}
                    </tr>,
                    isOpen ? (
                      <tr key={`${t.id}-detail`} className="sg-zh-tdetail">
                        <td colSpan={cols}>
                          <div className="sg-zh-tdetail-runs">
                            {ticketRuns.map((run) => (
                              <RunDetail
                                key={run.id}
                                run={run}
                                canManage={canManage}
                                cancelling={cancellingRunId === run.id}
                                onCancel={onCancelRun}
                              />
                            ))}
                          </div>
                        </td>
                      </tr>
                    ) : null,
                  ];
                })}
              </tbody>
            </table>
          </div>
        )}
      </section>

      {!runsLoading && orphanRuns.length > 0 ? (
        <section className="card">
          <div className="card-header-row">
            <h3>Runs without a ticket in this log</h3>
            <span className="sg-zh-count">{orphanRuns.length}</span>
          </div>
          <p className="muted">
            Automation runs whose inbound-log entry was deleted (only the 10 newest tickets are
            kept — older ones are pruned automatically with their finished runs).
          </p>
          <div className="sg-zh-runs">
            {orphanRuns.map((run) => (
              <RunDetail
                key={run.id}
                run={run}
                canManage={canManage}
                cancelling={cancellingRunId === run.id}
                onCancel={onCancelRun}
              />
            ))}
          </div>
        </section>
      ) : null}
    </>
  );
}
