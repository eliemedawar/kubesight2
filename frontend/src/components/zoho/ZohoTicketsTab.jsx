import { useMemo, useState } from "react";
import EmptyState from "../common/EmptyState.jsx";
import { useTicketing } from "../ticketing/TicketingContext.jsx";
import { IconChevronRight, IconPlay, IconRefresh, IconTrash } from "./icons.jsx";
import AgentTaskCard, { AgentTaskPill } from "./AgentTaskCard.jsx";
import { CopyButton } from "./common.jsx";
import { ACTIVE_RUN_STATUSES, RunDetail, RunStatusPill } from "./ZohoRunDetail.jsx";

const FILTERS = [
  { key: "all", label: "All" },
  { key: "unresolved", label: "Unresolved" },
  { key: "active", label: "Running" },
  { key: "approval", label: "Awaiting approval" },
  { key: "impediment", label: "Impediment" },
];

// The ticket's first Hermes reading (the handle task) — what the column shows.
const handleTask = (t) => (t.agentTasks || []).find((task) => task.kind === "handle");
const agentWaiting = (t) => (t.agentTasks || []).some((task) => task.status === "awaiting_approval");
// With the agent on, tickets are free text: the target and change are what
// Hermes worked out (from the task that executed or asked for approval), not
// dropdown fields.
const agentAction = (t) =>
  (t.agentTasks || []).find((task) => task.kind === "handle" && task.deploymentName);
const agentChangeLabel = (task) => {
  if (!task) return null;
  if (task.changeType === "env_var") return `${task.variableName}=${task.variableValue}`;
  if (task.changeType === "restart") return "restart";
  return task.tag || null;
};
// Every application the task changes — one for most tickets, several when the
// ticket named several (each with its own run).
const agentChanges = (task) => {
  if (!task) return [];
  if (task.changes?.length) return task.changes;
  return [{ ...task, variable: task.variableName, value: task.variableValue }];
};
const changeLabel = (c) => {
  if (c.changeType === "env_var") return `${c.variable}=${c.value}`;
  if (c.changeType === "restart") return "restart";
  return c.tag || null;
};
// A ticket for several applications has several runs going at once — the
// newest one finishing first does not make the ticket idle.
const anyRunActive = (ticketRuns) => (ticketRuns || []).some((r) => ACTIVE_RUN_STATUSES.has(r.status));
// Shown in a table cell: the first few, then a count.
const CELL_MAX = 3;

// Hermes can be asked again once it has settled on something other than a run.
const REHANDLE = new Set(["impediment", "on_hold", "error", "superseded", "done"]);

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
  agentActive = false,
  onApproveTask,
  onRejectTask,
  decidingTaskId,
  onHandleAgain,
  handlingTicketId,
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
    const runsOf = (t) => runsByTicket.get(t.id) || [];
    return {
      all: tickets.length,
      unresolved: tickets.filter((t) => !t.resolved).length,
      active: tickets.filter((t) => anyRunActive(runsOf(t))).length,
      approval: tickets.filter(
        (t) => runsOf(t).some((r) => r.status === "awaiting_approval") || agentWaiting(t)
      ).length,
      impediment: tickets.filter((t) => handleTask(t)?.status === "impediment").length,
    };
  }, [tickets, runsByTicket]);

  const query = search.trim().toLowerCase();
  const visible = tickets.filter((t) => {
    const ticketRuns = runsByTicket.get(t.id) || [];
    if (filter === "unresolved" && t.resolved) return false;
    if (filter === "active" && !anyRunActive(ticketRuns)) return false;
    if (
      filter === "approval" &&
      !ticketRuns.some((r) => r.status === "awaiting_approval") &&
      !agentWaiting(t)
    )
      return false;
    if (filter === "impediment" && handleTask(t)?.status !== "impediment") return false;
    if (!query) return true;
    return [
      t.ticketNumber,
      t.ticketId,
      t.deploymentName,
      t.targetName,
      t.rawAppValue,
      t.subject,
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

  const canRun = (ticket, ticketRuns) =>
    canManage && ticket.resolved && ticketChange(ticket) && !anyRunActive(ticketRuns);

  return (
    <>
      <div className="sg-zh-hookstrip">
        {inboundSecretConfigured ? (
          <span className="status-pill ok">Webhook secret set</span>
        ) : (
          <span
            className="status-pill warn"
            title="Inbound secret not configured — webhooks are rejected. Set one in Settings → Inbound webhook."
          >
            Webhooks rejected — no secret
          </span>
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
                  {agentActive ? <th>Subject</th> : null}
                  <th>Deployment</th>
                  <th>Change</th>
                  {agentActive ? null : <th>Resolution</th>}
                  <th>Hermes</th>
                  <th>Automation</th>
                  {canManage ? <th aria-label="Actions" /> : null}
                </tr>
              </thead>
              <tbody>
                {visible.map((t) => {
                  const ticketRuns = runsByTicket.get(t.id) || [];
                  // The pill follows a run still going (several apps run side by
                  // side) before the newest one.
                  const latest =
                    ticketRuns.find((r) => ACTIVE_RUN_STATUSES.has(r.status)) || ticketRuns[0];
                  const tasks = t.agentTasks || [];
                  const hasDetail = ticketRuns.length > 0 || tasks.length > 0;
                  const isOpen = expanded.has(t.id) && hasDetail;
                  const cols = canManage ? 9 : 8;
                  const firstReading = handleTask(t);
                  return [
                    <tr
                      key={t.id}
                      className={`sg-zh-trow ${hasDetail ? "sg-zh-trow--exp" : ""} ${
                        isOpen ? "sg-zh-trow--open" : ""
                      }`}
                      onClick={hasDetail ? () => toggle(t.id) : undefined}
                    >
                      <td className="sg-zh-tdchev">
                        {hasDetail ? (
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
                      {agentActive ? (
                        <td className="sg-zh-tsubject" title={t.subject || ""}>
                          {t.subject || <span className="muted">—</span>}
                        </td>
                      ) : null}
                      <td>
                        {agentActive && agentAction(t) ? (
                          <AgentCell
                            items={agentChanges(agentAction(t))}
                            render={(c) => (
                              <span className="mono">
                                {c.deploymentName} ({c.namespace})
                              </span>
                            )}
                          />
                        ) : agentActive && !t.resolved ? (
                          <span className="muted">—</span>
                        ) : t.resolved ? (
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
                          if (agentActive) {
                            const task = agentAction(t);
                            if (!task || !agentChangeLabel(task)) return "—";
                            return (
                              <AgentCell
                                items={agentChanges(task)}
                                render={(c) => (
                                  <span className="sg-tag mono" title="what Hermes understood">
                                    {changeLabel(c) || "—"}
                                  </span>
                                )}
                              />
                            );
                          }
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
                      {agentActive ? null : (
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
                      )}
                      <td>
                        <AgentTaskPill task={firstReading} />
                        {agentWaiting(t) && firstReading?.status !== "awaiting_approval" ? (
                          <span className="status-pill warn">Needs approval</span>
                        ) : null}
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
                          {agentActive &&
                          REHANDLE.has(firstReading?.status) &&
                          !anyRunActive(ticketRuns) ? (
                            <button
                              type="button"
                              className="btn-ghost sg-zh-trun"
                              onClick={() => onHandleAgain(t)}
                              disabled={handlingTicketId === t.id}
                              title="Hand this ticket to Hermes again (e.g. after the requester fixed it)"
                              aria-label={`Ask Hermes again about ticket ${
                                t.ticketNumber || t.ticketId || t.id
                              }`}
                            >
                              <IconRefresh />
                            </button>
                          ) : null}
                          {canRun(t, ticketRuns) ? (
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
                            {tasks.map((task) => (
                              <AgentTaskCard
                                key={`task-${task.id}`}
                                task={task}
                                canManage={canManage}
                                deciding={decidingTaskId === task.id}
                                onApprove={onApproveTask}
                                onReject={onRejectTask}
                              />
                            ))}
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

// A Deployment / Change cell: one line per application Hermes is changing,
// the first few shown and the rest counted (all of them in the tooltip).
function AgentCell({ items, render }) {
  if (items.length <= 1) return items[0] ? render(items[0]) : "—";
  const shown = items.slice(0, CELL_MAX);
  const rest = items.length - shown.length;
  const title = items.map((c) => `${c.deploymentName} (${c.namespace}) ${changeLabel(c) || ""}`).join("\n");
  return (
    <span className="sg-zh-agentcell" title={title}>
      {shown.map((c, index) => (
        <span key={`${c.namespace}/${c.deploymentName}/${index}`}>{render(c)}</span>
      ))}
      {rest > 0 ? <span className="muted">+{rest} more</span> : null}
    </span>
  );
}
