import { Fragment, useEffect, useRef, useState } from "react";

import { getPromotionHistory } from "../../api/promotionsApi.js";
import { PrIcon } from "./icons.jsx";
import { STATE_META, formatMinutes, formatRelative } from "./promotionModel.js";

/**
 * One application across the ladder: its version track (what runs where and
 * what each hop can do), the workloads behind each environment, and every
 * version the ladder has seen with how far it got.
 */
export default function AppPanel({ app, overview, canDeploy, onClose, onPromote }) {
  const { environments } = overview;
  const [history, setHistory] = useState({ loading: true, items: [], error: "" });
  const closeRef = useRef(null);

  useEffect(() => {
    closeRef.current?.focus();
    const onKey = (event) => event.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  useEffect(() => {
    let cancelled = false;
    setHistory({ loading: true, items: [], error: "" });
    getPromotionHistory(app.repository)
      .then((data) => !cancelled && setHistory({ loading: false, items: data?.items || [], error: "" }))
      .catch((err) => !cancelled && setHistory({ loading: false, items: [], error: err.message }));
    return () => {
      cancelled = true;
    };
  }, [app.repository]);

  return (
    <div className="pr-drawer-root" role="presentation">
      <div className="pr-scrim" onClick={onClose} />
      <aside className="pr-drawer" role="dialog" aria-modal="true" aria-labelledby="pr-app-title">
        <header className="pr-drawer-head">
          <div className="pr-drawer-titles">
            <span className="pr-eyebrow">{app.system}</span>
            <h3 id="pr-app-title">{app.name}</h3>
            <p className="pr-drawer-meta">
              <code>{app.repository}</code>
              {app.team && <span className="pr-team">{app.team}</span>}
              {app.ciService && (
                <a className="pr-link" href={`#/service-catalog/${app.ciService.id}`}>
                  CI service
                </a>
              )}
            </p>
          </div>
          <button ref={closeRef} type="button" className="icon-button pr-x" onClick={onClose} aria-label="Close">
            <PrIcon.X />
          </button>
        </header>

        <div className="pr-drawer-body">
          <section className="pr-section">
            <h4>Version track</h4>
            <ol className="pr-track">
              {app.cells.map((cell, index) => {
                const env = environments[index];
                const step = app.steps[index];
                const next = environments[index + 1];
                const ready = cell.workloads.reduce((n, w) => n + (w.ready || 0), 0);
                const desired = cell.workloads.reduce((n, w) => n + (w.desired || 0), 0);
                return (
                  <Fragment key={env.id}>
                    <li className={`pr-track-stop${cell.workloads.length ? "" : " is-empty"}${cell.drift.length ? " is-drift" : ""}`}>
                      <span className="pr-track-env">{env.name}</span>
                      {cell.workloads.length ? (
                        <>
                          <span className="pr-track-tags">
                            {cell.tags.map((tag) => (
                              <span key={tag} className="pr-ver pr-ver--new">
                                {tag}
                              </span>
                            ))}
                          </span>
                          <span className={`pr-track-health pr-track-health--${cell.state}`}>
                            <i aria-hidden="true" />
                            {cell.state === "progressing" ? `rolling out ${ready}/${desired}` : `${ready}/${desired} ready`}
                          </span>
                          {cell.healthySince && <span className="pr-track-since">since {formatRelative(cell.healthySince)}</span>}
                          {cell.drift.length > 0 && (
                            <span className="pr-skip">never ran in {cell.drift[0].skipped}</span>
                          )}
                        </>
                      ) : (
                        <span className="pr-muted">not deployed</span>
                      )}
                    </li>
                    {step && (
                      <li className={`pr-track-hop pr-track-hop--${step.state}`} aria-label={`${env.name} to ${next.name}`}>
                        <span className="pr-track-line" aria-hidden="true" />
                        {step.state === "ready" && canDeploy ? (
                          <button type="button" className="primary pr-track-go" onClick={() => onPromote(index, step.image)}>
                            Promote {step.tag} to {next.name}
                            <PrIcon.Arrow />
                          </button>
                        ) : (
                          <span className={`pr-chip pr-chip--${STATE_META[step.state]?.tone || "muted"}`} title={step.detail}>
                            {step.state === "soaking"
                              ? `soaking · ${formatMinutes(step.soakMinutesLeft)} left`
                              : STATE_META[step.state]?.label || (step.state === "idle" ? "—" : step.state)}
                          </span>
                        )}
                        {step.state !== "ready" && step.state !== "idle" && step.state !== "in_sync" && (
                          <span className="pr-track-detail">{step.detail}</span>
                        )}
                      </li>
                    )}
                  </Fragment>
                );
              })}
            </ol>
          </section>

          <section className="pr-section">
            <h4>Workloads</h4>
            <table className="pr-wl-table">
              <thead>
                <tr>
                  <th>Environment</th>
                  <th>Workload</th>
                  <th>Cluster</th>
                  <th>Version</th>
                  <th>Ready</th>
                </tr>
              </thead>
              <tbody>
                {app.cells.flatMap((cell, index) =>
                  cell.workloads.map((w) => (
                    <tr key={`${index}-${w.clusterId}-${w.namespace}-${w.name}-${w.container}`}>
                      <td>{environments[index].name}</td>
                      <td>
                        <code>
                          {w.namespace}/{w.name}
                        </code>
                      </td>
                      <td>{overview.clusters?.[w.clusterId]?.name || w.clusterId}</td>
                      <td>
                        <span className="pr-ver">{w.tag}</span>
                      </td>
                      <td className={w.state === "healthy" ? "pr-ok" : "pr-warn-text"}>
                        {w.ready}/{w.desired}
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
          </section>

          <section className="pr-section">
            <h4>Versions the ladder has seen</h4>
            {history.loading ? (
              <p className="pr-muted">Loading…</p>
            ) : history.error ? (
              <p className="pr-danger-text">{history.error}</p>
            ) : history.items.length === 0 ? (
              <p className="pr-muted">No version has been seen healthy in the ladder yet.</p>
            ) : (
              <ol className="pr-history">
                {history.items.map((item) => {
                  const reached = new Map(item.environments.map((e) => [e.environmentId, e]));
                  return (
                    <li key={item.image}>
                      <span className="pr-ver pr-ver--new">{item.tag || item.image}</span>
                      <span className="pr-history-track">
                        {environments.map((env) => {
                          const hit = reached.get(env.id);
                          return (
                            <span
                              key={env.id}
                              className={`pr-history-dot${hit ? " is-hit" : ""}`}
                              title={
                                hit
                                  ? `${env.name}: healthy since ${new Date(hit.firstHealthyAt).toLocaleString()}${hit.where ? ` (${hit.where})` : ""}`
                                  : `${env.name}: never`
                              }
                            >
                              {env.name}
                            </span>
                          );
                        })}
                      </span>
                      <span className="pr-history-when">first seen {formatRelative(item.firstSeenAt)}</span>
                    </li>
                  );
                })}
              </ol>
            )}
          </section>
        </div>
      </aside>
    </div>
  );
}
