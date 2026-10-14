/** Fleet — every cluster, where it sits on the release line, and its next step. */

import { useMemo, useState } from "react";
import {
  ACTIVE_STATUSES,
  Chip,
  ModeChip,
  PENDING_STATUSES,
  StatusChip,
  SupportChip,
  formatDay,
  readinessTone,
  readinessWords,
  timeAgo,
} from "./upgradeUi.jsx";

const minorOf = (version) => {
  const match = /^v?(\d+)\.(\d+)/.exec(version || "");
  return match ? `${match[1]}.${match[2]}` : null;
};

/** What a person should do next on this cluster, in one button. */
export function nextStep(row) {
  const job = row.openJob;
  if (!row.reachable) return { label: "Open", tone: "", why: "Unreachable" };
  if (job) {
    if (ACTIVE_STATUSES.includes(job.status)) return { label: "Watch run", tone: "primary" };
    if (job.status === "awaiting_approval") return { label: "Open request", tone: "" };
    if (job.status === "scheduled") return { label: "Open plan", tone: "" };
    if (job.status === "guided") return { label: "Continue runbook", tone: "primary" };
  }
  if (row.mode === "manual") return { label: "How to upgrade", tone: "" };
  if (!row.hops && !(row.patchTarget && row.patchTarget !== row.version)) return { label: "Open", tone: "" };
  if (row.readiness?.summary?.failed) return { label: "Resolve blockers", tone: "primary" };
  if (row.mode === "guided") return { label: "Open runbook", tone: "" };
  if (row.support?.state !== "supported") return { label: "Plan upgrade", tone: "primary" };
  return { label: "Plan upgrade", tone: "" };
}

function category(row) {
  const cats = new Set(["all"]);
  if (row.support?.state === "ended") cats.add("out");
  if (row.support?.state === "ending") cats.add("ending");
  if (row.openJob && [...ACTIVE_STATUSES, ...PENDING_STATUSES, "guided"].includes(row.openJob.status)) {
    cats.add("progress");
  }
  if (row.reachable && !row.hops && row.support?.state === "supported") cats.add("current");
  return cats;
}

const TILES = [
  { key: "all", tone: "muted", label: "All clusters", hint: (n) => `${n} reachable or not` },
  { key: "out", tone: "danger", label: "Out of support", hint: () => "No upstream security patches" },
  { key: "ending", tone: "warn", label: "Support ending", hint: () => "Within 60 days" },
  { key: "progress", tone: "run", label: "Upgrading or waiting", hint: () => "Running, scheduled or awaiting approval" },
  { key: "current", tone: "ok", label: "On the latest release", hint: () => "Nothing to do" },
];

function headline(rows) {
  const out = rows.filter((r) => r.support?.state === "ended").length;
  const ending = rows.filter((r) => r.support?.state === "ending").length;
  if (!out && !ending) return "Every cluster is inside upstream support.";
  const parts = [];
  if (out) parts.push(`${out} cluster${out === 1 ? " is" : "s are"} out of support.`);
  if (ending) parts.push(`${ending} ${out ? "more " : ""}lose${ending === 1 ? "s" : ""} it within 60 days.`);
  return parts.join(" ");
}

function versionCell(row) {
  if (!row.reachable) {
    return (
      <div className="uc-stack">
        <span className="uc-mono">{row.version || "—"}</span>
        <span className="uc-sub uc-sub--danger" title={row.error}>Could not reach the cluster</span>
      </div>
    );
  }
  const hop = row.nextHop;
  const patch = row.patchTarget && row.patchTarget !== row.version ? row.patchTarget : null;
  let next = null;
  let sub = "On the newest release";
  if (hop) {
    next = hop.label;
    sub = row.hops > 1 ? `Hop 1 of ${row.hops} to the latest` : "One minor up";
  } else if (patch) {
    next = patch;
    sub = "Patch release";
  }
  const skewed = (row.nodeVersions || []).length > 1;
  return (
    <div className="uc-stack">
      <span className="uc-mono uc-strong">
        {row.version}
        {next ? (
          <>
            <span className="uc-arrow" aria-hidden="true"> → </span>
            <span className="uc-sr">upgrade to </span>
            {next}
          </>
        ) : null}
      </span>
      <span className="uc-sub">{skewed ? `Nodes on ${row.nodeVersions.join(", ")}` : sub}</span>
    </div>
  );
}

function readinessCell(row) {
  const job = row.openJob;
  if (job) {
    let detail = "";
    if (ACTIVE_STATUSES.includes(job.status) && job.nodesTotal) {
      detail = `${job.activeNode ? `${job.activeNode} · ` : ""}${job.nodesDone} of ${job.nodesTotal} nodes`;
    } else if (job.status === "awaiting_approval" && job.approvals) {
      detail = `${job.approvals.approved} of ${job.approvals.required} approvals`;
    } else if (job.status === "scheduled" && job.schedule?.mode === "window") {
      detail = `Window ${new Date(job.schedule.start).toLocaleString(undefined, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" })}`;
    }
    return (
      <div className="uc-stack">
        <StatusChip status={job.status} />
        <span className="uc-sub">{detail || `to ${job.targetVersion}`}</span>
      </div>
    );
  }
  if (!row.readiness) {
    return (
      <div className="uc-stack">
        <Chip tone="muted">Not checked</Chip>
        <span className="uc-sub">{row.mode === "manual" ? "Not needed" : "Run readiness from the cluster"}</span>
      </div>
    );
  }
  return (
    <div className="uc-stack">
      <Chip tone={readinessTone(row.readiness.summary)}>{readinessWords(row.readiness.summary)}</Chip>
      <span className="uc-sub">
        For {row.readiness.targetVersion} · {timeAgo(row.readiness.checkedAt)}
      </span>
    </div>
  );
}

export default function FleetView({ data, onOpenCluster }) {
  const [filter, setFilter] = useState("all");
  const rows = data?.clusters || [];
  const counts = useMemo(() => {
    const out = {};
    TILES.forEach((t) => {
      out[t.key] = rows.filter((r) => category(r).has(t.key)).length;
    });
    return out;
  }, [rows]);
  const shown = rows.filter((r) => category(r).has(filter));
  const byMinor = useMemo(() => {
    const map = {};
    rows.forEach((r) => {
      const minor = minorOf(r.version);
      if (minor) (map[minor] = map[minor] || []).push(r);
    });
    return map;
  }, [rows]);
  const activeTile = TILES.find((t) => t.key === filter);

  return (
    <div className="uc-fleet">
      <section className="uc-pulse" aria-labelledby="uc-pulse-h">
        <div className="uc-pulse__text">
          <h2 id="uc-pulse-h" className="uc-display">{headline(rows)}</h2>
          <p className="uc-muted">
            Upstream Kubernetes patches the three newest minor versions
            {data?.calendar?.length ? `, now ${data.calendar.filter((m) => m.state !== "ended").map((m) => m.minor).slice(-3).join(", ")}` : ""}.
            Each cluster shows how far it is from that window and the next step KubeSight can take.
          </p>
          {data && !data.autoUpgradeEnabled && rows.some((r) => r.provider === "kubeadm" && r.mode === "guided") ? (
            <p className="uc-note">
              Automatic upgrades are off on this KubeSight (<code>KUBESIGHT_AUTO_UPGRADE</code>), so kubeadm
              clusters use the guided runbook.
            </p>
          ) : null}
        </div>
        <div className="uc-tiles" role="group" aria-label="Filter clusters">
          {TILES.map((tile) => (
            <button
              key={tile.key}
              type="button"
              className={`uc-tile${filter === tile.key ? " is-on" : ""}`}
              aria-pressed={filter === tile.key}
              onClick={() => setFilter(tile.key)}
            >
              <Chip tone={tile.tone}>{tile.label}</Chip>
              <span className="uc-tile__n">{counts[tile.key] ?? 0}</span>
              <span className="uc-sub">{tile.hint(rows.length)}</span>
            </button>
          ))}
        </div>
      </section>

      {data?.calendar?.length ? (
        <section className="uc-card uc-lane-card" aria-labelledby="uc-lane-h">
          <div className="uc-card__head">
            <h3 id="uc-lane-h" className="uc-h3">Where the fleet sits on the release line</h3>
            <span className="uc-sub">End-of-life dates from the Kubernetes release calendar; newer minors are estimates.</span>
          </div>
          <div className="uc-lane-scroll">
            <ol className="uc-lane" style={{ gridTemplateColumns: `repeat(${data.calendar.length}, minmax(9.5rem, 1fr))` }}>
              {data.calendar.map((minor) => (
                <li key={minor.minor} className={`uc-lane__col uc-lane__col--${minor.state}`}>
                  <div className="uc-lane__head">
                    <span className="uc-lane__minor">{minor.minor}</span>
                    {minor.latest ? <span className="uc-sub">latest</span> : null}
                  </div>
                  <SupportChip
                    support={{ state: minor.state, endOfLife: minor.endOfLife, daysLeft: minor.daysLeft, estimated: minor.estimated }}
                  />
                  <span className="uc-sub">
                    {minor.state === "ended" ? "Ended" : "End of life"} {formatDay(minor.endOfLife)}
                    {minor.estimated ? " (est.)" : ""}
                  </span>
                  <ul className="uc-lane__pills">
                    {(byMinor[minor.minor] || []).map((r) => (
                      <li key={r.id}>
                        <button type="button" className="uc-pill" onClick={() => onOpenCluster(r.id)}>
                          <span className="uc-pill__name">{r.name}</span>
                          <span className="uc-mono uc-sub">
                            {r.version}
                            {r.openJob && ["queued", "running", "paused"].includes(r.openJob.status) ? " → " + r.openJob.targetVersion : ""}
                          </span>
                        </button>
                      </li>
                    ))}
                  </ul>
                </li>
              ))}
            </ol>
          </div>
        </section>
      ) : null}

      <section className="uc-clusters" aria-labelledby="uc-clusters-h">
        <div className="uc-section-head">
          <h3 id="uc-clusters-h" className="uc-h3">
            Clusters <span className="uc-sub">· {filter === "all" ? `all ${rows.length}` : `${activeTile.label.toLowerCase()} (${shown.length})`}</span>
          </h3>
          {filter !== "all" ? (
            <button type="button" className="btn-ghost uc-linkbtn" onClick={() => setFilter("all")}>
              Show all
            </button>
          ) : null}
        </div>
        <div className="uc-table-wrap">
          <table className="uc-table">
            <thead>
              <tr>
                <th scope="col">Cluster</th>
                <th scope="col">Version · next stop</th>
                <th scope="col">Support</th>
                <th scope="col">How it upgrades</th>
                <th scope="col">Readiness · progress</th>
                <th scope="col" className="uc-right">Next step</th>
              </tr>
            </thead>
            <tbody>
              {shown.length === 0 ? (
                <tr>
                  <td colSpan={6} className="uc-empty">No cluster matches this filter.</td>
                </tr>
              ) : (
                shown.map((row) => {
                  const step = nextStep(row);
                  return (
                    <tr key={row.id}>
                      <td>
                        <div className="uc-stack">
                          <button type="button" className="uc-namebtn" onClick={() => onOpenCluster(row.id)}>
                            {row.name}
                          </button>
                          <span className="uc-sub">
                            {row.reachable
                              ? `${row.providerDisplay} · ${row.controlPlanes} control plane${row.controlPlanes === 1 ? "" : "s"} · ${row.workers} worker${row.workers === 1 ? "" : "s"}`
                              : row.id}
                            {row.notReady?.length ? ` · ${row.notReady.length} not Ready` : ""}
                          </span>
                        </div>
                      </td>
                      <td>{versionCell(row)}</td>
                      <td>{row.reachable ? <SupportChip support={row.support} /> : <Chip tone="muted">Unknown</Chip>}</td>
                      <td>
                        {row.reachable ? (
                          <div className="uc-stack">
                            <ModeChip mode={row.mode} />
                            <span className="uc-sub uc-clamp" title={row.modeReason}>{row.modeReason}</span>
                          </div>
                        ) : (
                          "—"
                        )}
                      </td>
                      <td>{row.reachable ? readinessCell(row) : <Chip tone="danger">Unreachable</Chip>}</td>
                      <td className="uc-right">
                        <button
                          type="button"
                          className={step.tone === "primary" ? "btn-primary uc-btn-sm" : "btn-outline uc-btn-sm"}
                          onClick={() => onOpenCluster(row.id)}
                        >
                          {step.label}
                        </button>
                      </td>
                    </tr>
                  );
                })
              )}
            </tbody>
          </table>
        </div>
        <p className="uc-legend">
          <span><strong>Automatic</strong> — KubeSight runs every step, after approval.</span>
          <span><strong>Guided</strong> — KubeSight writes the commands and ticks them off as the cluster changes.</span>
          <span><strong>Manual</strong> — the platform upgrades itself; KubeSight tracks the clock.</span>
        </p>
      </section>
    </div>
  );
}
