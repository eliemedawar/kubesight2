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
import { useState } from "react";
import { parseApiTime } from "../../lib/apiTime";
import { cancelClusterBuild, deleteClusterBuild, stopProvisionWait } from "../../api/clusterBuildsApi.js";
import { PROVISION_STATUS_LABELS } from "../../utils/clusterProvisioning.js";
import {
  PHASE_LABELS,
  PHASE_NOTES,
  buildDuration,
  deletePolicy,
  groupBuilds,
  formatClock,
  isGrowing,
  railFromCurrentPhase,
  runStartedAt,
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

/** Delete a build's record after the right confirmation. Returns true when deleted. */
export async function confirmAndDelete(build, notify) {
  const policy = deletePolicy(build);
  if (!policy.allowed) {
    notify(policy.reason, true);
    return false;
  }
  let confirmName;
  if (policy.needsName) {
    confirmName = window.prompt(
      `${build.name} made a cluster. Deleting removes this build's record only — the cluster `
      + "stays in Clusters, and the audit log keeps what the build was.\n\n"
      + `Type ${build.name} to delete it:`
    );
    if (confirmName === null) return false;
    if (confirmName.trim() !== build.name) {
      notify("The name did not match; nothing was deleted.", true);
      return false;
    }
  } else if (!window.confirm(`Delete build "${build.name}"? This cannot be undone.`)) {
    return false;
  }
  try {
    await deleteClusterBuild(build.id, confirmName);
    notify(`Build ${build.name} deleted.`);
    return true;
  } catch (error) {
    notify(error.message || String(error), true);
    return false;
  }
}

function InFlightStrip({ build, now, onOpen, canExecute, notify, onChanged }) {
  const [busy, setBusy] = useState(false);
  const cancel = async () => {
    if (!window.confirm(`Cancel the build "${build.name}"? You can delete or retry it afterwards.`)) return;
    setBusy(true);
    try {
      await cancelClusterBuild(build.id);
      notify(`Build ${build.name} cancelled.`);
      if (onChanged) onChanged();
    } catch (error) {
      notify(error.message || String(error), true);
    } finally {
      setBusy(false);
    }
  };
  // A VMware build that is still creating VMs (or waiting for them to answer
  // SSH) has no Kubernetes phase yet: say what OpenTofu is doing instead.
  const job = build.provisioning?.job;
  if (["provisioning", "destroying"].includes(build.status)) {
    const stopWait = async () => {
      if (!window.confirm(`Stop waiting for ${build.name}'s VMs to answer SSH? The VMs stay; you can try SSH again later.`)) return;
      setBusy(true);
      try {
        await stopProvisionWait(build.id, job.id);
        notify(`Stopped waiting for ${build.name}.`);
        if (onChanged) onChanged();
      } catch (error) {
        notify(error.message || String(error), true);
      } finally {
        setBusy(false);
      }
    };
    const vms = build.nodeShape?.length || 0;
    return (
      <div className="card sg-cb-flight" aria-live="polite">
        <div className="sg-cb-flight-top">
          <LiveBadge label={PROVISION_STATUS_LABELS[build.provisionStatus] || "Creating VMs"} />
          <h3>{build.name}</h3>
          <span className="muted sg-cb-mono sg-cb-flight-meta">
            {build.vmsOnly ? "VMs only" : `v${build.k8sVersion}`} · {vms} VM{vms === 1 ? "" : "s"} · OpenTofu
          </span>
        </div>
        <div className="sg-cb-flight-now">
          <b>{PROVISION_STATUS_LABELS[build.provisionStatus] || "Creating VMs"}</b>
          <span className="muted">
            {job?.status === "connecting"
              ? "The VMs exist; KubeSight is logging in to each with the build's SSH route."
              : "OpenTofu is working in vCenter."}
          </span>
          <span className="sg-cb-flight-acts">
            {canExecute && job?.status === "connecting" ? (
              <button className="btn-ghost" type="button" disabled={busy} onClick={stopWait}>
                {busy ? "Stopping…" : "Stop"}
              </button>
            ) : null}
            <button className="btn-outline sg-cb-flight-cta" type="button" onClick={() => onOpen(build.id)}>
              Watch
            </button>
          </span>
        </div>
      </div>
    );
  }
  const rail = railFromCurrentPhase(build);
  const started = parseApiTime(runStartedAt(build));
  const elapsed = Number.isFinite(started) ? now - started : null;
  const position = rail.findIndex((cell) => cell.state === "now") + 1;
  const phaseLabel = build.currentPhase
    ? PHASE_LABELS[build.currentPhase] || build.currentPhase
    : "Starting…";
  return (
    <div className="card sg-cb-flight" aria-live="polite">
      <div className="sg-cb-flight-top">
        <LiveBadge label={build.status === "preflighting" ? "Preflighting"
          : isGrowing(build) ? "Adding machines" : "Building"} />
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
        <span className="sg-cb-flight-acts">
          {canExecute && ["building", "preflighting"].includes(build.status) ? (
            <button className="btn-ghost" type="button" disabled={busy} onClick={cancel}>
              {busy ? "Cancelling…" : "Cancel"}
            </button>
          ) : null}
          <button className="btn-outline sg-cb-flight-cta" type="button" onClick={() => onOpen(build.id)}>
            Watch
          </button>
        </span>
      </div>
    </div>
  );
}

function LibraryRow({ build, catalog, now, onOpen, canCreate, notify, onChanged }) {
  const [busy, setBusy] = useState(false);
  const policy = deletePolicy(build);
  const remove = async () => {
    setBusy(true);
    const deleted = await confirmAndDelete(build, notify);
    setBusy(false);
    if (deleted && onChanged) onChanged();
  };
  const duration = buildDuration(build);
  const age = timeAgo(build.finishedAt || build.createdAt, now);
  let middle = null;
  if (build.status === "failed" && build.currentPhase) {
    middle = <>Stopped at <b>{PHASE_LABELS[build.currentPhase] || build.currentPhase}</b></>;
  } else if (build.status === "draft") {
    middle = build.nodeCounts?.controlPlane ? shapeSummary(build) : "No machines assigned yet";
  } else if (build.status === "preflight_passed") {
    middle = "Preflight passed — not launched";
  } else if (build.status === "vms_ready") {
    middle = "VMs answer SSH — no Kubernetes";
  } else if ((build.addons || []).length) {
    middle = <AddonChips addons={build.addons} catalog={catalog} />;
  } else {
    middle = <span className="muted">No add-ons</span>;
  }

  return (
    <div className="sg-cb-librow-wrap">
    <button className="sg-cb-librow" type="button" onClick={() => onOpen(build.id)}>
      <ShapeGlyph shape={build.nodeShape} buildStatus={build.status} />
      <span className="sg-cb-librow-id">
        <span className="nm">{build.name}</span>
        <span className="sub">
          {build.vmsOnly
            ? `VMs only · ${(build.nodeShape || []).length} VM${(build.nodeShape || []).length === 1 ? "" : "s"}`
            : <>v{build.k8sVersion} · {build.topologyType === "stacked_ha" ? "HA" : "single CP"}</>}
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
    {canCreate && policy.destroyFirst ? (
      // Its VMs must go first: open the build, where Destroy VMs is.
      <button className="btn-ghost btn-sm sg-cb-librow-del" type="button"
              title={policy.reason} onClick={() => onOpen(build.id)}>
        Destroy VMs…
      </button>
    ) : canCreate ? (
      <button
        className="btn-ghost btn-sm sg-cb-librow-del"
        type="button"
        disabled={busy || !policy.allowed}
        title={policy.allowed ? `Delete ${build.name}` : policy.reason}
        aria-label={`Delete ${build.name}`}
        onClick={remove}
      >
        Delete
      </button>
    ) : null}
    </div>
  );
}

export default function BuildsFloor({
  builds,
  readiness,
  catalog,
  canCreate,
  canExecute = false,
  now,
  onOpenBuild,
  onNewBuild,
  onOpenSources,
  notify = () => {},
  onChanged = null,
}) {
  const groups = groupBuilds(builds);
  const hasLibrary = groups.attention.length > 0 || groups.done.length > 0;

  return (
    <div className="sg-cb-vstack">
      <ReadinessBar readiness={readiness} onOpenSources={onOpenSources} />

      {groups.inFlight.map((build) => (
        <InFlightStrip key={build.id} build={build} now={now} onOpen={onOpenBuild}
                       canExecute={canExecute} notify={notify} onChanged={onChanged} />
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
                canCreate={canCreate} notify={notify} onChanged={onChanged}
              />
            ))}
            {groups.done.length ? (
              <div className="sg-cb-lib-head"><span>Done</span></div>
            ) : null}
            {groups.done.map((build) => (
              <LibraryRow
                key={build.id} build={build} catalog={catalog} now={now} onOpen={onOpenBuild}
                canCreate={canCreate} notify={notify} onChanged={onChanged}
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
