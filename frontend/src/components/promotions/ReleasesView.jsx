import { useEffect, useMemo, useState } from "react";

import { getPromotionActivity, listPromotionReleases } from "../../api/promotionsApi.js";
import ErrorBanner from "../common/ErrorBanner.jsx";
import LoadingState from "../common/LoadingState.jsx";
import { PrIcon } from "./icons.jsx";
import { RELEASE_STATUS, formatRelative, groupByDay, plural, tagOf } from "./promotionModel.js";

const STATUS_FILTERS = [
  { key: "", label: "All" },
  { key: "pending_approval", label: "Awaiting approval" },
  { key: "applied", label: "Deployed" },
  { key: "partial", label: "Partly" },
  { key: "refused", label: "Refused" },
];

const EVENT_META = {
  blocked: { label: "Stopped", icon: <PrIcon.Stop />, tone: "danger" },
  warned: { label: "Skipped (warn)", icon: <PrIcon.Warn />, tone: "warn" },
  exception_requested: { label: "Exception asked", icon: <PrIcon.Hand />, tone: "info" },
  exception_applied: { label: "Exception deployed", icon: <PrIcon.Check />, tone: "info" },
};

const PATHS = {
  promote: "Promotions",
  deploy: "Deploy",
  ci: "CI Deploy stage",
  ticket: "Ticket automation",
  helm: "Helm",
  bundle: "Change bundle",
  ui: "Promotions",
  mcp: "Agent",
};

/**
 * What moved and what was stopped: releases (a set of applications promoted
 * together, with each one's fate read live from its change bundle), and the
 * deploys the ladder refused or let through with a warning — from any path.
 */
export default function ReleasesView({ environments, refreshKey }) {
  const [state, setState] = useState({ loading: true, releases: [], events: [], error: "" });
  const [env, setEnv] = useState("");
  const [status, setStatus] = useState("");
  const [term, setTerm] = useState("");
  const [open, setOpen] = useState(() => new Set());

  useEffect(() => {
    let cancelled = false;
    Promise.all([
      listPromotionReleases({ limit: 200 }),
      getPromotionActivity({ limit: 200, kinds: "blocked,warned,exception_requested,exception_applied" }),
    ])
      .then(([releases, events]) => {
        if (cancelled) return;
        setState({ loading: false, releases: releases?.items || [], events: (events?.items || []).filter((e) => !e.releaseId), error: "" });
      })
      .catch((err) => !cancelled && setState({ loading: false, releases: [], events: [], error: err.message }));
    return () => {
      cancelled = true;
    };
  }, [refreshKey]);

  const entries = useMemo(() => {
    const t = term.trim().toLowerCase();
    const releases = state.releases
      .filter((r) => !env || String(r.environmentId) === env)
      .filter((r) => !status || r.status === status)
      .filter(
        (r) =>
          !t ||
          r.name.toLowerCase().includes(t) ||
          (r.reference || "").toLowerCase().includes(t) ||
          r.items.some((i) => i.name.toLowerCase().includes(t))
      )
      .map((r) => ({ type: "release", createdAt: r.createdAt, id: `r${r.id}`, release: r }));
    const events = status
      ? []
      : state.events
          .filter((e) => !env || String(e.environmentId) === env)
          .filter((e) => !t || JSON.stringify(e).toLowerCase().includes(t))
          .map((e) => ({ type: "event", createdAt: e.createdAt, id: `e${e.id}`, event: e }));
    return [...releases, ...events].sort((a, b) => (b.createdAt || "").localeCompare(a.createdAt || ""));
  }, [state, env, status, term]);

  if (state.loading) return <LoadingState label="Loading releases…" />;

  const toggle = (id) => {
    const next = new Set(open);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    setOpen(next);
  };

  return (
    <div className="pr-releases">
      {state.error && <ErrorBanner message={state.error} />}
      <div className="pr-toolbar">
        <label className="pr-search">
          <PrIcon.Search />
          <input
            type="search"
            value={term}
            onChange={(event) => setTerm(event.target.value)}
            placeholder="Search releases, references, applications…"
            aria-label="Search releases"
          />
        </label>
        <div className="pr-seg" role="group" aria-label="Environment">
          <button type="button" className={`btn-ghost pr-seg-btn${env === "" ? " is-on" : ""}`} onClick={() => setEnv("")}>
            All
          </button>
          {environments.slice(1).map((e) => (
            <button
              key={e.id}
              type="button"
              className={`btn-ghost pr-seg-btn${env === String(e.id) ? " is-on" : ""}`}
              onClick={() => setEnv(String(e.id))}
            >
              → {e.name}
            </button>
          ))}
        </div>
        <label className="pr-select">
          <span>Status</span>
          <select value={status} onChange={(event) => setStatus(event.target.value)}>
            {STATUS_FILTERS.map((s) => (
              <option key={s.key} value={s.key}>
                {s.label}
              </option>
            ))}
          </select>
        </label>
      </div>

      {entries.length === 0 ? (
        <div className="pr-empty pr-empty--small">
          <PrIcon.Ladder />
          <p>
            Nothing yet. Releases you send from Promote land here, with each application&apos;s fate — and so does every
            deploy the ladder stopped, from any path.
          </p>
        </div>
      ) : (
        groupByDay(entries).map((day) => (
          <section key={day.key} className="pr-day">
            <h4 className="pr-day-label">{day.label}</h4>
            <ol className="pr-feed">
              {day.items.map((entry) =>
                entry.type === "release" ? (
                  <ReleaseCard key={entry.id} release={entry.release} open={open.has(entry.id)} onToggle={() => toggle(entry.id)} />
                ) : (
                  <EventRow key={entry.id} event={entry.event} />
                )
              )}
            </ol>
          </section>
        ))
      )}
    </div>
  );
}

function ReleaseCard({ release, open, onToggle }) {
  const meta = RELEASE_STATUS[release.status] || RELEASE_STATUS.refused;
  const workloads = Object.values(release.counts).reduce((a, b) => a + b, 0);
  return (
    <li className={`pr-rel pr-rel--${meta.tone}${open ? " is-open" : ""}`}>
      <button type="button" className="btn-ghost pr-rel-head" onClick={onToggle} aria-expanded={open}>
        <span className="pr-rel-icon">{release.kind === "exception" ? <PrIcon.Hand /> : <PrIcon.Up />}</span>
        <span className="pr-rel-title">
          <b>{release.name}</b>
          {release.reference && <code className="pr-ref">{release.reference}</code>}
          <span className="pr-rel-route">
            {release.fromEnvironmentName ? `${release.fromEnvironmentName} → ` : "→ "}
            {release.environmentName}
          </span>
        </span>
        <span className="pr-rel-meta">
          {plural(release.applications, "app")} · {plural(workloads, "workload")}
          {release.actor && ` · ${release.actor}`}
        </span>
        <span className={`pr-chip pr-chip--${meta.tone}`}>{meta.label}</span>
        <time dateTime={release.createdAt} title={new Date(release.createdAt).toLocaleString()}>
          {formatRelative(release.createdAt)}
        </time>
        {open ? <PrIcon.ChevronDown /> : <PrIcon.ChevronRight />}
      </button>
      {open && (
        <div className="pr-rel-body">
          {release.note && <p className="pr-rel-note">“{release.note}”</p>}
          {release.exceptionReason && (
            <p className="pr-rel-note">
              <b>Exception:</b> {release.exceptionReason}
            </p>
          )}
          <ul className="pr-rel-items">
            {release.items.map((item) => {
              const im = RELEASE_STATUS[item.status] || RELEASE_STATUS.refused;
              return (
                <li key={item.repository}>
                  <span className="pr-rel-app">{item.name}</span>
                  <span className="pr-rel-ver">
                    <span className="pr-ver pr-ver--old">{(item.fromTags || []).join(", ") || "—"}</span>
                    <PrIcon.Arrow />
                    <span className="pr-ver pr-ver--new">{item.tag}</span>
                    {item.exception && <span className="pr-skip">exception</span>}
                  </span>
                  <span className={`pr-chip pr-chip--${im.tone}`}>{im.label}</span>
                  {item.targets
                    .filter((t) => t.status === "refused")
                    .map((t) => (
                      <p key={`${t.namespace}/${t.name}`} className="pr-rel-msg">
                        <code>
                          {t.namespace}/{t.name}
                        </code>{" "}
                        {t.message}
                      </p>
                    ))}
                </li>
              );
            })}
          </ul>
          {release.bundleIds.length > 0 && (
            <p className="pr-rel-links">
              {release.bundleIds.map((id) => (
                <a key={id} className="pr-link" href="#/change-bundles/all">
                  Change bundle #{id}
                </a>
              ))}
            </p>
          )}
        </div>
      )}
    </li>
  );
}

function EventRow({ event }) {
  const meta = EVENT_META[event.kind] || { label: event.kind, icon: <PrIcon.Dot />, tone: "muted" };
  const images = (event.images || []).map((i) => i.image).filter(Boolean);
  const name = images[0] ? images[0].split(":")[0].split("/").pop() : event.workloadName;
  return (
    <li className={`pr-evt pr-evt--${meta.tone}`}>
      <span className="pr-evt-icon">{meta.icon}</span>
      <div className="pr-evt-body">
        <p className="pr-evt-title">
          <b>{meta.label}</b> — <code>{name} {images.map(tagOf).join(", ")}</code> into <b>{event.environmentName}</b>
          {event.fromEnvironmentName && <> (not passed {event.fromEnvironmentName})</>}
        </p>
        {event.message && <p className="pr-evt-msg">{event.message}</p>}
        <p className="pr-evt-meta">
          <span className="pr-evt-path">{PATHS[event.path] || event.path}</span>
          {event.namespace && (
            <span>
              {event.clusterName ? `${event.clusterName} · ` : ""}
              {event.namespace}
              {event.workloadName ? `/${event.workloadName}` : ""}
            </span>
          )}
          {event.actor && <span>{event.actor}</span>}
          {event.bundleId && (
            <a className="pr-link" href="#/change-bundles/all">
              bundle #{event.bundleId}
            </a>
          )}
          <time dateTime={event.createdAt}>{formatRelative(event.createdAt)}</time>
        </p>
      </div>
    </li>
  );
}
