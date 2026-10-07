import { Fragment, useMemo, useState } from "react";

import { ModeIcon, PrIcon } from "./icons.jsx";
import SystemFilter from "./SystemFilter.jsx";
import { APP_FILTERS, filterApps, groupApps, systemsOf } from "./promotionModel.js";

/**
 * Every application across the ladder, one compact row each: the version in
 * every environment, coloured only where something differs from the
 * environment below. Built for hundreds of rows — grouped, filterable, and
 * each group renders lazily.
 */
export default function ApplicationsView({ overview, search, onSearch, onOpenApp }) {
  const { environments, apps } = overview;
  const [filter, setFilter] = useState("all");
  const [systems, setSystems] = useState([]);
  const [groupBy, setGroupBy] = useState("system");
  const [collapsed, setCollapsed] = useState(() => new Set());

  const counts = useMemo(() => {
    const out = {};
    for (const f of APP_FILTERS) out[f.key] = filterApps(apps, { filter: f.key }).length;
    return out;
  }, [apps]);
  const shown = useMemo(() => filterApps(apps, { search, systems, filter }), [apps, search, systems, filter]);
  const groups = useMemo(() => groupApps(shown, groupBy), [shown, groupBy]);
  const columns = `minmax(13rem, 1.5fr) repeat(${environments.length}, minmax(6.5rem, 1fr)) 6.5rem`;
  const minWidth = `${13 + environments.length * 6.5 + 6.5 + 2}rem`;

  const toggleGroup = (key) => {
    const next = new Set(collapsed);
    if (next.has(key)) next.delete(key);
    else next.add(key);
    setCollapsed(next);
  };

  return (
    <div className="pr-apps">
      <div className="pr-filter-chips" role="group" aria-label="Show">
        {APP_FILTERS.map((f) => (
          <button
            key={f.key}
            type="button"
            className={`btn-ghost pr-fchip${filter === f.key ? " is-on" : ""}`}
            onClick={() => setFilter(f.key)}
            aria-pressed={filter === f.key}
          >
            {f.label}
            <b>{counts[f.key]}</b>
          </button>
        ))}
      </div>
      <div className="pr-toolbar">
        <label className="pr-search">
          <PrIcon.Search />
          <input
            type="search"
            value={search}
            onChange={(event) => onSearch(event.target.value)}
            placeholder="Search applications, systems, teams, namespaces…"
            aria-label="Search applications"
          />
        </label>
        <SystemFilter systems={systemsOf(apps)} selected={systems} onChange={setSystems} />
        <label className="pr-select">
          <span>Group</span>
          <select value={groupBy} onChange={(event) => setGroupBy(event.target.value)}>
            <option value="system">by system</option>
            <option value="team">by team</option>
            <option value="none">none</option>
          </select>
        </label>
        {groupBy !== "none" && groups.length > 1 && (
          <button
            type="button"
            className="btn-ghost pr-select-all"
            onClick={() => setCollapsed(collapsed.size ? new Set() : new Set(groups.map((g) => g.key)))}
          >
            {collapsed.size ? "Expand all" : "Collapse all"}
          </button>
        )}
        <span className="pr-toolbar-meta">
          {shown.length} of {apps.length} applications
        </span>
      </div>

      {shown.length === 0 ? (
        <div className="pr-empty pr-empty--small">
          <p>No application matches.</p>
        </div>
      ) : (
        <div className="pr-grid" role="table" aria-label="Versions per environment" style={{ "--pr-cols": columns, "--pr-min": minWidth }}>
          <div className="pr-grid-inner">
          <div className="pr-grid-row pr-grid-row--head" role="row">
            <span role="columnheader">Application</span>
            {environments.map((env, index) => (
              <span key={env.id} role="columnheader" className="pr-grid-env">
                <ModeIcon mode={index === 0 ? "entry" : env.mode} />
                {env.name}
              </span>
            ))}
            <span role="columnheader">Lag</span>
          </div>
          {groups.map((group) => {
            const isCollapsed = collapsed.has(group.key);
            const behind = group.items.filter((a) => a.lag > 0).length;
            return (
              <Fragment key={group.key}>
                {group.label && (
                  <button
                    type="button"
                    className="btn-ghost pr-grid-group"
                    onClick={() => toggleGroup(group.key)}
                    aria-expanded={!isCollapsed}
                  >
                    {isCollapsed ? <PrIcon.ChevronRight /> : <PrIcon.ChevronDown />}
                    <b>{group.label}</b>
                    <em>
                      {group.items.length} {group.items.length === 1 ? "application" : "applications"}
                      {behind > 0 && ` · ${behind} behind`}
                    </em>
                  </button>
                )}
                {!isCollapsed && (
                  <div className="pr-grid-body">
                    {group.items.map((app) => (
                      <AppRow key={app.key} app={app} environments={environments} showSystem={groupBy !== "system"} onOpen={() => onOpenApp(app.key)} />
                    ))}
                  </div>
                )}
              </Fragment>
            );
          })}
          </div>
        </div>
      )}
    </div>
  );
}

function AppRow({ app, environments, showSystem, onOpen }) {
  return (
    <button type="button" className="btn-ghost pr-grid-row pr-grid-row--app" role="row" onClick={onOpen}>
      <span role="cell" className="pr-grid-app">
        <span className="pr-app-name">{app.name}</span>
        <span className="pr-app-sub">
          {showSystem && <span className="pr-sys">{app.system}</span>}
          {app.team && <span className="pr-team">{app.team}</span>}
          <span className="pr-repo">{app.repository}</span>
        </span>
      </span>
      {app.cells.map((cell, index) => {
        const prev = index > 0 ? app.cells[index - 1] : null;
        const same = prev && prev.images.length && cell.images.join() === prev.images.join();
        const step = index < app.steps.length ? app.steps[index] : null;
        const readyUp = step?.state === "ready";
        const env = environments[index];
        if (!cell.workloads.length) {
          return (
            <span key={env.id} role="cell" className="pr-grid-cell">
              <span className="pr-pill pr-pill--empty">—</span>
            </span>
          );
        }
        const cls = [
          "pr-pill",
          same ? "is-same" : "is-new",
          cell.drift.length ? "is-drift" : "",
          cell.state === "progressing" ? "is-rolling" : "",
        ].join(" ");
        const title = [
          `${env.name}: ${cell.tags.join(", ")}`,
          cell.state === "progressing" ? "rolling out" : null,
          cell.drift.length ? `never ran in ${cell.drift[0].skipped}` : null,
          readyUp ? `ready to promote to ${environments[index + 1].name}` : null,
        ]
          .filter(Boolean)
          .join(" · ");
        return (
          <span key={env.id} role="cell" className="pr-grid-cell" title={title}>
            <span className={cls}>
              {cell.state === "progressing" && <i className="pr-pill-dot" aria-hidden="true" />}
              {cell.drift.length > 0 && <PrIcon.Warn />}
              {cell.tags[0]}
              {cell.tags.length > 1 && <em>+{cell.tags.length - 1}</em>}
            </span>
            {readyUp && <span className="pr-pill-up" aria-label="ready to promote" />}
          </span>
        );
      })}
      <span role="cell" className="pr-grid-lag">
        {app.lag > 0 ? (
          <span className="pr-lag">{app.lag} behind</span>
        ) : (
          <span className="pr-lag pr-lag--ok">
            <PrIcon.Check /> in sync
          </span>
        )}
      </span>
    </button>
  );
}
