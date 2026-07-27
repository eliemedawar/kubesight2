/** The Floor — what is happening now, and everything that has happened.
 *
 *  A running build is not a card the same size as one from three weeks ago: it
 *  takes the full width with its own phase rail. Everything finished collapses
 *  into compact rows, grouped by what each build wants from a person rather
 *  than by date.
 */

import EmptyState from "../common/EmptyState.jsx";
import PhaseRail from "./PhaseRail.jsx";
import ReadinessBar from "./ReadinessBar.jsx";
import { AddonChips, LiveBadge, SectionHead, ShapeGlyph, StatusPill } from "./common.jsx";
import { parseApiTime } from "../../lib/apiTime";
import {
  PHASE_LABELS,
  PHASE_NOTES,
  buildDuration,
  groupBuilds,
  formatClock,
  railFromCurrentPhase,
  timeAgo,
} from "../../utils/clusterBuilder.js";

function shapeSummary(build) {
  const counts = build.nodeCounts || {};
  return [
    counts.loadbalancer ? `${counts.loadbalancer} LB` : "",
    `${counts.controlPlane || 0} CP`,
    `${counts.worker || 0} worker${counts.worker === 1 ? "" : "s"}`,
  ].filter(Boolean).join(" · ");
}

function InFlightStrip({ build, now, onOpen }) {
  const rail = railFromCurrentPhase(build);
  const started = parseApiTime(build.startedAt);
  const elapsed = Number.isFinite(started) ? now - started : null;
  const position = rail.findIndex((cell) => cell.state === "now") + 1;
  const phaseLabel = build.currentPhase
    ? PHASE_LABELS[build.currentPhase] || build.currentPhase
    : "Starting…";
  return (
    <div className="card sg-cb-flight" aria-live="polite">
      <div className="sg-cb-flight-top">
        <LiveBadge label={build.status === "preflighting" ? "Preflighting" : "Building"} />
        <h3>{build.name}</h3>
        <span className="muted sg-cb-mono sg-cb-flight-meta">
          v{build.k8sVersion} · {build.topologyType === "stacked_ha" ? "HA" : "single CP"}
          {build.controlPlaneEndpoint ? ` · ${build.controlPlaneEndpoint}` : ""} · {build.cniPlugin}
        </span>
        <span className="sg-cb-flight-el">
          {elapsed !== null ? <>elapsed <b className="sg-cb-mono">{formatClock(elapsed)}</b></> : null}
          {position ? <> · phase {position} of {rail.length}</> : null}
        </span>
      </div>
      <PhaseRail timeline={rail} />
      <div className="sg-cb-flight-now">
        <b>{phaseLabel}</b>
        {build.currentPhase && PHASE_NOTES[build.currentPhase]
          ? <span className="muted">{PHASE_NOTES[build.currentPhase]}</span>
          : null}
        <span className="muted sg-cb-mono">{shapeSummary(build)}</span>
        <button className="btn-outline sg-cb-flight-cta" type="button" onClick={() => onOpen(build.id)}>
          Watch
        </button>
      </div>
    </div>
  );
}

function LibraryRow({ build, catalog, now, onOpen }) {
  const duration = buildDuration(build);
  const age = timeAgo(build.finishedAt || build.createdAt, now);
  let middle = null;
  if (build.status === "failed" && build.currentPhase) {
    middle = <>Stopped at <b>{PHASE_LABELS[build.currentPhase] || build.currentPhase}</b></>;
  } else if (build.status === "draft") {
    middle = build.nodeCounts?.controlPlane ? shapeSummary(build) : "No machines assigned yet";
  } else if (build.status === "preflight_passed") {
    middle = "Preflight passed — not launched";
  } else if ((build.addons || []).length) {
    middle = <AddonChips addons={build.addons} catalog={catalog} />;
  } else {
    middle = <span className="muted">No add-ons</span>;
  }

  return (
    <button className="sg-cb-librow" type="button" onClick={() => onOpen(build.id)}>
      <ShapeGlyph shape={build.nodeShape} buildStatus={build.status} />
      <span className="sg-cb-librow-id">
        <span className="nm">{build.name}</span>
        <span className="sub">
          v{build.k8sVersion} · {build.topologyType === "stacked_ha" ? "HA" : "single CP"}
          {build.vipAddress ? <> · VIP <span className="sg-cb-mono">{build.vipAddress}</span></> : null}
        </span>
      </span>
      <StatusPill status={build.status} />
      <span className="sg-cb-librow-mid">{middle}</span>
      <span className="sg-cb-librow-when sg-cb-mono">
        {duration ? `${duration} · ` : ""}{age}
      </span>
      <span className="sg-cb-librow-go" aria-hidden="true">›</span>
    </button>
  );
}

export default function BuildsFloor({
  builds,
  readiness,
  catalog,
  canCreate,
  now,
  onOpenBuild,
  onNewBuild,
  onOpenSources,
}) {
  const groups = groupBuilds(builds);
  const hasLibrary = groups.attention.length > 0 || groups.done.length > 0;

  return (
    <div className="sg-cb-vstack">
      <ReadinessBar readiness={readiness} onOpenSources={onOpenSources} />

      {groups.inFlight.map((build) => (
        <InFlightStrip key={build.id} build={build} now={now} onOpen={onOpenBuild} />
      ))}

      {hasLibrary ? (
        <div className="sg-cb-vstack-tight">
          <SectionHead
            title="Library"
            right={`${builds.length} build${builds.length === 1 ? "" : "s"} · ${
              groups.done.filter((build) => build.resultClusterId).length
            } registered as clusters`}
          />
          <div className="card sg-cb-lib">
            {groups.attention.length ? (
              <div className="sg-cb-lib-head"><span>Needs you</span></div>
            ) : null}
            {groups.attention.map((build) => (
              <LibraryRow
                key={build.id} build={build} catalog={catalog} now={now} onOpen={onOpenBuild}
              />
            ))}
            {groups.done.length ? (
              <div className="sg-cb-lib-head"><span>Done</span></div>
            ) : null}
            {groups.done.map((build) => (
              <LibraryRow
                key={build.id} build={build} catalog={catalog} now={now} onOpen={onOpenBuild}
              />
            ))}
          </div>
        </div>
      ) : null}

      {!builds.length ? (
        canCreate ? (
          <button className="card sg-cb-newcard" type="button" onClick={onNewBuild}>
            <span className="sg-cb-newcard-plus">+</span>
            <span className="sg-cb-newcard-t">Build your first cluster</span>
            <span className="muted">
              Two load balancers, three control planes and any number of workers is the
              usual production shape.
            </span>
          </button>
        ) : (
          <EmptyState title="No cluster builds yet" message="No builds have been created." />
        )
      ) : null}
    </div>
  );
}
