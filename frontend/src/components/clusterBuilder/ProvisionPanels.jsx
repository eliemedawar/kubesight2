/** What OpenTofu is doing to a build's VMs, and the decisions it waits on.
 *
 *  One card follows the build's latest provisioning job through its life:
 *  a plan being made, a plan to review and apply, VMs being created and
 *  reached, a failure with the next step, a destroy waiting for a second
 *  person. The panels below it (grow, destroy, save as template) only start
 *  jobs; the card is where every job is watched.
 */

import { useEffect, useRef, useState } from "react";
import { LiveBadge } from "./common.jsx";
import { parseApiTime } from "../../lib/apiTime";
import { formatClock, timeAgo } from "../../utils/clusterBuilder.js";
import {
  ACTION_SIGN,
  ROLE_ONE,
  ROLE_TITLE,
  destroyStance,
  shapesForVms,
  growthState,
  shapeLabel,
  planGroups,
  provisionRail,
  vmRows,
} from "../../utils/clusterProvisioning.js";
import {
  applyProvisionPlan,
  approveClusterDestroy,
  createClusterTemplate,
  discardProvisionJob,
  getProvisionJobConfig,
  installKubernetesOnVms,
  stopProvisionWait,
  planClusterVms,
  planMoreWorkers,
  rejectClusterDestroy,
  requestClusterDestroy,
  retryProvisionConnect,
} from "../../api/clusterBuildsApi.js";

/** The server's own words when it refused (a 403 otherwise reads as a bare
    "no access"): an approval rule or a second-person rule explains itself. */
export function refusalText(error) {
  if (error?.status === 403 && error.serverMessage) return error.serverMessage;
  return error?.message || String(error);
}

function useTicker(active) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!active) return undefined;
    const id = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(id);
  }, [active]);
  return now;
}

function Checks({ checks = [] }) {
  if (!checks.length) return null;
  return (
    <ul className="sg-cb-pv-checks">
      {checks.map((check) => (
        <li key={`${check.label}-${check.detail}`} className={`is-${check.status}`}>
          <i aria-hidden="true">{check.status === "ok" ? "✓" : check.status === "warn" ? "!" : check.status === "bad" ? "✕" : "i"}</i>
          <span><b>{check.label}</b> {check.detail}</span>
        </li>
      ))}
    </ul>
  );
}

/** A plan, as a decision: what is created, changed and deleted, and why it is safe. */
export function PlanReview({
  job, title, lede, applyLabel, onApply, onDiscard, onBack, busy, canApply = true, children,
}) {
  const summary = job?.summary || {};
  const groups = planGroups(summary);
  const tiles = [
    ["add", "to create", "is-add"],
    ["change", "to change", "is-change"],
    ["destroy", "to delete", "is-del"],
  ];
  return (
    <div className="card sg-cb-card sg-cb-pv-plan">
      <div className="sg-cb-sect">
        <h2>{title}</h2>
        <span className="sg-cb-sect-right sg-cb-mono">
          plan #{job.id}{job.requestedBy ? ` · ${job.requestedBy}` : ""}
          {job.createdAt ? ` · ${timeAgo(job.createdAt)}` : ""}
        </span>
      </div>
      {lede ? <p className="muted sg-cb-pv-lede">{lede}</p> : null}
      <div className="sg-cb-pv-tally">
        {tiles.map(([key, label, cls]) => (
          <div key={key} className={`sg-cb-pv-tile ${summary[key] ? cls : "is-zero"}`}>
            <b>{summary[key] || 0}</b>
            <span>{label}</span>
          </div>
        ))}
      </div>
      {summary.blocked ? <div className="sg-cb-pv-note is-bad">{summary.blocked}</div> : null}
      {groups.map((group) => (
        <div className="sg-cb-pv-group" key={group.kind}>
          <h4>{group.label}</h4>
          <ul className="sg-cb-pv-res">
            {group.items.map((item) => (
              <li key={item.address} className={`is-${item.action}`}>
                <span className="sign" aria-label={item.action}>{ACTION_SIGN[item.action] || "·"}</span>
                <span className="what">
                  <b className={item.kind === "vm" ? "sg-cb-mono" : ""}>{item.title}</b>
                  <span className="addr sg-cb-mono">{item.address}</span>
                </span>
                <span className="detail sg-cb-mono">{item.detail}</span>
              </li>
            ))}
          </ul>
        </div>
      ))}
      {!groups.length ? (
        <p className="muted">Nothing to change: everything in this plan already exists.</p>
      ) : null}
      <Checks checks={summary.checks} />
      {job.planText ? (
        <details className="sg-cb-pv-raw">
          <summary>Show OpenTofu&apos;s own plan</summary>
          <pre className="sg-cb-log">{job.planText}</pre>
        </details>
      ) : null}
      {children}
      <div className="sg-cb-actions sg-cb-pv-acts">
        {onBack ? <button className="btn-outline" type="button" onClick={onBack}>Change something</button> : null}
        {onDiscard ? (
          <button className="btn-ghost" type="button" disabled={busy} onClick={onDiscard}>
            Discard plan
          </button>
        ) : null}
        {onApply ? (
          <button
            className="primary"
            type="button"
            disabled={busy || !canApply || Boolean(summary.blocked)}
            onClick={onApply}
          >
            {applyLabel}
          </button>
        ) : null}
      </div>
    </div>
  );
}

function PlanningState({ job, now }) {
  const started = parseApiTime(job.startedAt || job.createdAt);
  const phase = job.progress?.phase;
  const label = job.operation === "destroy"
    ? "Planning what to delete"
    : phase === "checking" ? "Checking vCenter, the template and the addresses" : "OpenTofu is making the plan";
  return (
    <div className="card sg-cb-card sg-cb-pv-wait">
      <LiveBadge label="Planning" />
      <div>
        <b>{label}</b>
        <p className="muted">
          Nothing is created or deleted while planning. This usually takes under a minute.
          {Number.isFinite(started) ? ` · ${formatClock(now - started)}` : ""}
        </p>
      </div>
    </div>
  );
}

/** The live view of an apply: four phases, a row per VM, OpenTofu's log.
    ``compact`` once Kubernetes has taken over: the rail and one line. */
export function ProvisionProgress({ build, now, compact = false }) {
  const job = build.provisioning?.job;
  const rows = vmRows(build).filter((row) => (
    job?.operation !== "grow" || Object.prototype.hasOwnProperty.call(job?.progress?.vms || {}, row.name)
  ));
  const destroy = job?.operation === "destroy";
  const cells = destroy ? null : provisionRail(build);
  const started = parseApiTime(job?.progress?.applyStartedAt || job?.startedAt);
  const running = ["applying", "connecting"].includes(job?.status);
  const [logOpen, setLogOpen] = useState(running);
  const logRef = useRef(null);
  // Follow the newest lines while OpenTofu writes them.
  useEffect(() => {
    if (running && logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [running, job?.logTail]);
  if (compact) {
    const ready = rows.filter((row) => row.state === "ready").length;
    return (
      <div className="card sg-cb-card sg-cb-pv-progress">
        <div className="sg-cb-sect">
          <h2>VMs created by OpenTofu</h2>
          <span className="sg-cb-sect-right">
            {ready} of {rows.length} answer SSH · Kubernetes is being installed below
          </span>
        </div>
        {cells ? (
          <ol className="sg-cb-pv-rail">
            {cells.map((cell) => (
              <li key={cell.key} className={`is-${cell.state}`}>
                <i aria-hidden="true">{cell.state === "done" ? "✓" : cell.state === "fail" ? "✕" : ""}</i>
                {cell.label}
              </li>
            ))}
          </ol>
        ) : null}
        {job?.progress?.handoffNote ? (
          <div className="sg-cb-pv-note is-warn">{job.progress.handoffNote}</div>
        ) : null}
      </div>
    );
  }
  return (
    <div className="card sg-cb-card sg-cb-pv-progress">
      <div className="sg-cb-sect">
        <h2>
          {destroy ? "Deleting the VMs"
            : job?.status === "connecting" ? "Waiting for the new VMs to answer"
              : build.status === "vms_ready" ? "VMs ready"
              : job?.status === "succeeded" ? "VMs created"
                : job?.operation === "grow" ? "Creating workers in vCenter" : "Creating VMs in vCenter"}
        </h2>
        <span className="sg-cb-sect-right">
          {running ? <LiveBadge label={destroy ? "Destroying" : "Running"} /> : null}
          {running && Number.isFinite(started)
            ? <span className="sg-cb-mono"> {formatClock(now - started)}</span> : null}
        </span>
      </div>
      {cells ? (
        <ol className="sg-cb-pv-rail">
          {cells.map((cell) => (
            <li key={cell.key} className={`is-${cell.state}`}>
              <i aria-hidden="true">{cell.state === "done" ? "✓" : cell.state === "fail" ? "✕" : ""}</i>
              {cell.label}
            </li>
          ))}
        </ol>
      ) : null}
      <div className="table-wrap">
        <table className="sg-cb-pv-vms">
          <thead>
            <tr><th>VM</th><th>Address</th><th>Size</th><th>Now</th></tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.name}>
                <td className="sg-cb-mono">{row.name}<span className="muted"> · {ROLE_ONE[row.role] || row.role}</span></td>
                <td className="sg-cb-mono">{row.ip || "—"}</td>
                <td className="sg-cb-mono muted">{row.size}</td>
                <td>
                  <span className={`sg-cb-pv-state is-${row.tone}`}>
                    <i />{row.label}{row.elapsed ? ` · ${row.elapsed}` : ""}
                  </span>
                  {row.error ? <div className="sg-cb-pv-vmerr">{row.error}</div> : null}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {job?.progress?.handoffNote ? (
        <div className="sg-cb-pv-note is-warn">{job.progress.handoffNote}</div>
      ) : null}
      {job?.logTail ? (
        <details className="sg-cb-pv-raw" open={logOpen} onToggle={(e) => setLogOpen(e.currentTarget.open)}>
          <summary>OpenTofu log</summary>
          <pre className="sg-cb-log sg-cb-pv-log" ref={logRef}>{job.logTail}</pre>
        </details>
      ) : null}
    </div>
  );
}

/** Second-person approval for a destroy, seen from either side. */
function DestroyDecision({ build, job, currentUserId, canExecute, busy, act, refusal }) {
  const stance = destroyStance(job, currentUserId);
  const [note, setNote] = useState("");
  return (
    <PlanReview
      job={job}
      title={`Destroy ${build.name}?`}
      lede={stance === "requester"
        ? "Waiting for a second person who can run cluster builds. You asked, so you cannot approve it. The cluster keeps running until someone does."
        : `${job.requestedBy || "Someone"} asked to delete this cluster's VMs and remove it from KubeSight. Read the plan, then decide.`}
      busy={busy}
    >
      <div className="sg-cb-pv-ask">
        <div className="who">
          <span className="sg-cb-pv-avatar" aria-hidden="true">
            {(job.requestedBy || "?").slice(0, 2).toUpperCase()}
          </span>
          <span>
            <b>{job.requestedBy || "Unknown"}</b> asked {timeAgo(job.createdAt)}
            {job.reason ? <> · <span className="muted">“{job.reason}”</span></> : null}
          </span>
        </div>
        {stance === "approver" && canExecute ? (
          <>
            <label className="sg-cb-field-label" htmlFor="pv-note">Note (optional)</label>
            <input id="pv-note" className="sg-cb-input" value={note}
                   onChange={(event) => setNote(event.target.value)} placeholder="Checked with the UAT team" />
            <div className="sg-cb-actions">
              <button className="btn-outline" type="button" disabled={busy}
                      onClick={() => act(() => rejectClusterDestroy(build.id, job.id, note))}>
                Reject
              </button>
              <button className="btn-danger" type="button" disabled={busy}
                      onClick={() => act(() => approveClusterDestroy(build.id, job.id, note))}>
                Approve and destroy
              </button>
            </div>
          </>
        ) : stance === "requester" ? (
          <div className="sg-cb-actions">
            <button className="btn-outline" type="button" disabled={busy}
                    onClick={() => act(() => discardProvisionJob(build.id, job.id))}>
              Withdraw request
            </button>
          </div>
        ) : (
          <p className="muted">Someone with permission to run cluster builds decides.</p>
        )}
        {refusal}
      </div>
    </PlanReview>
  );
}

/** Install Kubernetes on a VMs-only build: the shapes that fit its VMs. */
function InstallChooser({ build, catalog, k8sVersions, busy, onInstall, onClose }) {
  const machines = build.provisioning?.spec?.machines || [];
  const shapes = shapesForVms(machines.length, catalog);
  const [key, setKey] = useState(shapes[0]?.key || "");
  const versions = k8sVersions.includes(build.k8sVersion) || !build.k8sVersion
    ? k8sVersions : [build.k8sVersion, ...k8sVersions];
  const [version, setVersion] = useState(build.k8sVersion || versions[0] || "");
  const chosen = shapes.find((shape) => shape.key === key);
  // Roles go to the VMs in order: balancers first, then control planes, then workers.
  const roles = chosen
    ? [
      ...Array(chosen.counts.loadbalancer).fill("loadbalancer"),
      ...Array(chosen.counts.controlPlane).fill("controlPlane"),
      ...Array(chosen.counts.worker).fill("worker"),
    ]
    : [];
  return (
    <div className="card sg-cb-card sg-cb-pv-panel">
      <div className="sg-cb-sect">
        <h2>Install Kubernetes on {machines.length} VM{machines.length === 1 ? "" : "s"}</h2>
        <button className="btn-ghost btn-sm" type="button" onClick={onClose}>Close</button>
      </div>
      <p className="muted sg-cb-pv-lede">
        Shapes that fit exactly {machines.length} VM{machines.length === 1 ? "" : "s"}. Nothing is cloned again: the VMs
        keep their names and addresses and take the roles below. Preflight runs first, and the build starts on its own
        when it is clean.
      </p>
      {shapes.length ? (
        <div className="sg-cb-choices">
          {shapes.map((shape) => (
            <button key={shape.key} type="button" className="sg-cb-choice" aria-pressed={shape.key === key}
                    onClick={() => setKey(shape.key)}>
              <span className="ct">
                {shape.name || (shape.counts.worker ? "Custom" : "Single node")}
                {shape.name ? <span className="sg-cb-pill is-muted">template</span> : null}
              </span>
              <span className="cd sg-cb-mono">{shape.label}</span>
              {shape.note ? <span className="cd">{shape.note}</span> : null}
            </button>
          ))}
        </div>
      ) : <p className="sg-cb-field-error">No Kubernetes shape fits {machines.length} VMs.</p>}
      {chosen ? (
        <div className="table-wrap">
          <table className="sg-cb-pv-vms">
            <thead><tr><th>VM</th><th>Address</th><th>Becomes</th></tr></thead>
            <tbody>
              {machines.map((machine, index) => (
                <tr key={machine.name}>
                  <td className="sg-cb-mono">{machine.name}</td>
                  <td className="sg-cb-mono">{machine.ip || "—"}</td>
                  <td>{ROLE_ONE[roles[index]] || "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ) : null}
      {chosen?.counts.loadbalancer ? (
        <p className="muted">The API address (VIP) is reserved from the network&apos;s range when you install.</p>
      ) : null}
      <div className="sg-cb-actions">
        {versions.length ? (
          <label className="sg-cb-inlinecheck">
            Kubernetes
            <select className="sg-cb-input" value={version} onChange={(event) => setVersion(event.target.value)}>
              {versions.map((item) => <option key={item} value={item}>v{item}</option>)}
            </select>
          </label>
        ) : null}
        <button className="primary" type="button" disabled={busy || !chosen}
                onClick={() => onInstall({ counts: chosen.counts, k8sVersion: version || undefined })}>
          {busy ? "Running preflight…" : `Install Kubernetes${chosen?.name ? ` (${chosen.name})` : ""}`}
        </button>
      </div>
    </div>
  );
}

/** Download the main.tf.json this job's OpenTofu ran, to read or share. */
function ExportConfig({ build, job, notify }) {
  const [busy, setBusy] = useState(false);
  if (!job?.id) return null;
  const download = async () => {
    setBusy(true);
    try {
      const { filename, content } = await getProvisionJobConfig(build.id, job.id);
      const url = URL.createObjectURL(new Blob([content], { type: "application/json" }));
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.appendChild(link);
      link.click();
      link.remove();
      // Revoking in the same tick as the click can cancel the download.
      setTimeout(() => URL.revokeObjectURL(url), 30000);
    } catch (error) {
      if (notify) notify(refusalText(error), true);
    } finally {
      setBusy(false);
    }
  };
  return (
    <button className="btn-ghost btn-sm" type="button" disabled={busy} onClick={download}
            title="The OpenTofu configuration for this job. It holds no passwords.">
      {busy ? "Preparing…" : "Download main.tf.json"}
    </button>
  );
}

function Failure({ title, error, children }) {
  return (
    <div className="card sg-cb-blowup sg-cb-pv-fail">
      <div><h3>{title}</h3></div>
      {error ? <pre className="sg-cb-cmdline">{error}</pre> : null}
      {children ? <div className="sg-cb-blowup-acts">{children}</div> : null}
    </div>
  );
}

/**
 * The card for a VMware build: whatever its latest OpenTofu job needs.
 * Returns null for builds whose machines KubeSight did not create.
 */
export function ProvisionCard({
  build, canExecute, canCreate, currentUserId, notify, onChanged, onRequestDestroy,
  templateCatalog = null, k8sVersions = [],
}) {
  const [busy, setBusy] = useState(false);
  const [refusal, setRefusal] = useState("");
  const [installOpen, setInstallOpen] = useState(false);
  const job = build.provisioning?.job;
  const active = ["planning", "applying", "connecting"].includes(job?.status);
  const now = useTicker(active);
  if (!build.provisioning) return null;

  const act = async (fn) => {
    setBusy(true);
    setRefusal("");
    try {
      await fn();
      await onChanged();
    } catch (error) {
      const text = refusalText(error);
      setRefusal(text);
      notify(text, true);
    } finally {
      setBusy(false);
    }
  };
  const refusalNote = refusal ? <div className="sg-cb-pv-note is-bad" role="alert">{refusal}</div> : null;
  const planAgain = canExecute ? (
    <button className="primary" type="button" disabled={busy} onClick={() => act(() => planClusterVms(build.id))}>
      Plan again
    </button>
  ) : null;

  if (!job) {
    if (build.status !== "draft") return null;
    return (
      <div className="card sg-cb-card sg-cb-pv-wait">
        <div>
          <b>No VMs yet</b>
          <p className="muted">
            KubeSight creates this build&apos;s machines in vCenter. Make a plan to see exactly what it will create.
          </p>
        </div>
        {canExecute ? (
          <button className="primary" type="button" disabled={busy} onClick={() => act(() => planClusterVms(build.id))}>
            Make the plan
          </button>
        ) : null}
      </div>
    );
  }

  if (job.status === "planning") return <PlanningState job={job} now={now} />;

  if (job.operation === "destroy") {
    if (job.status === "awaiting_approval") {
      return (
        <DestroyDecision build={build} job={job} currentUserId={currentUserId}
                         canExecute={canExecute} busy={busy} act={act} refusal={refusalNote} />
      );
    }
    if (job.status === "applying") return <ProvisionProgress build={build} now={now} />;
    if (job.status === "plan_failed" || job.status === "apply_failed") {
      return (
        <Failure title={job.status === "plan_failed" ? "The destroy could not be planned" : "The destroy stopped part-way"} error={job.error}>
          {canExecute && onRequestDestroy ? (
            <button className="primary" type="button" onClick={onRequestDestroy}>Ask to destroy again</button>
          ) : null}
          <span className="sg-cb-safe">
            Whatever OpenTofu deleted is recorded; asking again plans only what is left.
          </span>
        </Failure>
      );
    }
    if (job.status === "rejected") {
      return (
        <div className="card sg-cb-addonline">
          <span className="sg-cb-config-label">Destroy rejected</span>
          <span>
            {job.approvedBy || "Someone"} rejected the request from {job.requestedBy || "someone"}
            {job.decisionNote ? `: “${job.decisionNote}”` : "."}
          </span>
        </div>
      );
    }
    if (job.status === "succeeded" && build.status === "destroyed") {
      return (
        <div className="card sg-cb-receipt sg-cb-pv-tomb">
          <div className="sg-cb-okring is-muted" aria-hidden="true">✕</div>
          <div>
            <h3>{build.name} was destroyed</h3>
            <p className="muted">
              Asked by {job.requestedBy || "someone"}{job.reason ? ` (“${job.reason}”)` : ""}, approved by{" "}
              {job.approvedBy || "someone"} {timeAgo(job.approvedAt)}. Its VMs are gone from vCenter, its
              addresses are free, and the cluster no longer appears in KubeSight. This record stays for audit.
            </p>
          </div>
        </div>
      );
    }
    return null;
  }

  // create / grow
  const grow = job.operation === "grow";
  if (job.status === "planned") {
    // The button names VMs; folders and keep-apart rules ride along in the plan.
    const add = (job.summary?.resources || [])
      .filter((r) => r.kind === "vm" && ["create", "replace"].includes(r.action)).length;
    return (
      <PlanReview
        job={job}
        title={grow ? `Add ${add} machine${add === 1 ? "" : "s"} to ${build.name}` : `Plan for ${build.name}`}
        lede={grow
          ? "The existing VMs must stay untouched — the plan is refused if it would change one. The new machines join once they answer SSH."
          : "Applying runs exactly this plan. If anything in vCenter changes first, OpenTofu stops and asks for a new one."}
        applyLabel={grow
          ? `Create ${add} VM${add === 1 ? "" : "s"} and join them`
          : add ? `Create ${add} VM${add === 1 ? "" : "s"}` : "Continue"}
        onApply={canExecute ? () => act(() => applyProvisionPlan(build.id, job.id)) : null}
        onDiscard={canCreate ? () => act(() => discardProvisionJob(build.id, job.id)) : null}
        busy={busy}
      >
        {refusalNote}
        <ExportConfig build={build} job={job} notify={notify} />
      </PlanReview>
    );
  }
  if (job.status === "plan_failed") {
    return (
      <Failure title="The plan could not be made" error={job.error}>
        {planAgain}
        <ExportConfig build={build} job={job} notify={notify} />
        <span className="sg-cb-safe">Nothing was created. Fix what the message says, then plan again.</span>
      </Failure>
    );
  }
  if (["applying", "connecting", "interrupted"].includes(job.status)) {
    return (
      <>
        {job.status === "connecting" && canExecute ? (
          <div className="card sg-cb-addonline">
            <span className="sg-cb-config-label">Waiting for SSH</span>
            <span className="muted">
              Each failed login and its reason is in the log below. Stopping changes nothing in vCenter;
              the VMs stay and you can try SSH again.
            </span>
            <button className="btn-outline btn-sm" type="button" disabled={busy}
                    onClick={() => act(() => stopProvisionWait(build.id, job.id))}>
              Stop waiting
            </button>
          </div>
        ) : null}
        {refusalNote}
        <ProvisionProgress build={build} now={now} />
      </>
    );
  }
  if (job.status === "apply_failed" || job.status === "connect_failed") {
    const created = build.provisioning?.state?.vmCount || 0;
    return (
      <>
        <Failure
          title={job.status === "connect_failed"
            ? "The VMs exist but did not answer SSH"
            : `${created} VM${created === 1 ? "" : "s"} created, then OpenTofu stopped`}
          error={job.error}
        >
          {job.status === "connect_failed" && canExecute ? (
            <button className="primary" type="button" disabled={busy}
                    onClick={() => act(() => retryProvisionConnect(build.id, job.id))}>
              Try SSH again
            </button>
          ) : !grow ? planAgain : null}
          {created && canExecute && onRequestDestroy && !grow ? (
            <button className="btn-outline" type="button" onClick={onRequestDestroy}>
              Remove the created VMs…
            </button>
          ) : null}
          <ExportConfig build={build} job={job} notify={notify} />
          <span className="sg-cb-safe">
            OpenTofu&apos;s state was saved and its lock released. A new plan creates only what is missing.
          </span>
        </Failure>
        <ProvisionProgress build={build} now={now} />
      </>
    );
  }
  if (job.status === "succeeded" && build.status === "vms_ready") {
    const count = build.provisioning?.state?.vmCount || 0;
    return (
      <>
        <div className="card sg-cb-card sg-cb-pv-wait sg-cb-pv-vmsonly">
          <div>
            <b>{count} VM{count === 1 ? "" : "s"} running · Kubernetes not installed</b>
            <p className="muted">
              This build creates VMs only, and stopped once every VM answered SSH. Log in with the
              build&apos;s SSH route to test them. Install Kubernetes lets you pick a shape that fits
              these VMs (Lab, Small, Standard HA …), then preflights and builds the cluster on them —
              nothing is cloned again. When you are done, Destroy VMs frees them and their addresses.
            </p>
          </div>
          <div className="sg-cb-pv-vmsonly-acts">
            {canExecute && !installOpen ? (
              <button className="primary" type="button" disabled={busy} onClick={() => setInstallOpen(true)}>
                Install Kubernetes…
              </button>
            ) : null}
          </div>
        </div>
        {installOpen && canExecute ? (
          <InstallChooser
            build={build}
            catalog={templateCatalog}
            k8sVersions={k8sVersions}
            busy={busy}
            onClose={() => setInstallOpen(false)}
            onInstall={(payload) => act(() => installKubernetesOnVms(build.id, payload))}
          />
        ) : null}
        {refusalNote}
        <ProvisionProgress build={build} now={now} />
      </>
    );
  }
  if (job.status === "succeeded" && !["completed", "destroyed"].includes(build.status)) {
    // Kubernetes has the machines now; its own phase rail and log tell the story.
    return <ProvisionProgress build={build} now={now} compact={build.status !== "draft"} />;
  }
  return null;
}

const GROW_ROLES = [
  // [spec role, node role, payload key]
  ["worker", "worker", "workers"],
  ["controlPlane", "control_plane", "controlPlanes"],
  ["loadbalancer", "loadbalancer", "loadBalancers"],
];

/** Day two: ask OpenTofu for more machines. The card above takes over from the plan. */
export function ProvisionGrowPanel({ build, canExecute, notify, onChanged, onClose }) {
  const spec = build.provisioning?.spec || {};
  const growth = growthState(build);
  const [counts, setCounts] = useState({ worker: 1, controlPlane: 0, loadbalancer: 0 });
  const [sizes, setSizes] = useState(() => JSON.parse(JSON.stringify(spec.sizes || {})));
  const [busy, setBusy] = useState(false);
  const total = counts.worker + counts.controlPlane + counts.loadbalancer;
  // Control planes in steps of two, balancers up to the pair.
  const options = {
    worker: Array.from({ length: 21 }, (_, n) => n),
    controlPlane: growth.controlPlane.allowed ? [0, 2] : [0],
    loadbalancer: growth.loadbalancer.allowed
      ? Array.from({ length: Math.max(0, 2 - growth.totals.loadbalancer) + 1 }, (_, n) => n)
      : [0],
  };
  const reasons = {
    worker: null,
    controlPlane: growth.controlPlane.allowed ? null : growth.controlPlane.reason,
    loadbalancer: growth.loadbalancer.allowed ? null : growth.loadbalancer.reason,
  };
  const step = (role, delta) => setCounts((current) => {
    const list = options[role];
    const index = Math.min(Math.max(list.indexOf(current[role]) + delta, 0), list.length - 1);
    return { ...current, [role]: list[index] };
  });
  const submit = async () => {
    setBusy(true);
    try {
      await planMoreWorkers(build.id, {
        workers: counts.worker,
        controlPlanes: counts.controlPlane,
        loadBalancers: counts.loadbalancer,
        sizes,
      });
      await onChanged();
      onClose();
    } catch (error) {
      notify(refusalText(error), true);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="card sg-cb-card sg-cb-pv-panel">
      <div className="sg-cb-sect">
        <h2>Add machines to {build.name}</h2>
        <button className="btn-ghost btn-sm" type="button" onClick={onClose}>Close</button>
      </div>
      <p className="muted sg-cb-pv-lede">
        New VMs are cloned from the same template, in <span className="sg-cb-mono">{spec.datastoreName}</span> on{" "}
        <span className="sg-cb-mono">{spec.networkName}</span>, then join the running cluster. The plan is refused if it
        would change a VM that already runs. New control planes join two at a time, one after another with an etcd
        health check between them; a new balancer becomes keepalived&apos;s backup and both balancers are reloaded, not
        restarted.
      </p>
      <div className="table-wrap">
        <table className="sg-cb-sizes">
          <thead>
            <tr><th>Role</th><th>Now</th><th>Add</th><th>vCPU</th><th>Memory GB</th><th>Disk GB</th></tr>
          </thead>
          <tbody>
            {GROW_ROLES.map(([role, nodeRole]) => (
              <tr key={role}>
                <td><span className={`sg-cb-rolechip is-${role}`}><i />{ROLE_TITLE[role]}</span></td>
                <td className="sg-cb-mono">{growth.running[nodeRole]}</td>
                <td>
                  {options[role].length > 1 ? (
                    <div className="sg-cb-stepper" role="group" aria-label={`${ROLE_TITLE[role]} to add`}>
                      <button type="button" className="btn-ghost" aria-label={`Fewer ${ROLE_TITLE[role].toLowerCase()}`}
                              onClick={() => step(role, -1)}>−</button>
                      <output>{counts[role]}</output>
                      <button type="button" className="btn-ghost" aria-label={`More ${ROLE_TITLE[role].toLowerCase()}`}
                              onClick={() => step(role, 1)}>+</button>
                    </div>
                  ) : <span className="muted sg-cb-growrole-note">{reasons[role] || "—"}</span>}
                </td>
                {["cpu", "memoryGb", "diskGb"].map((key) => (
                  <td key={key}>
                    <input type="number" min={1} className="sg-cb-input sg-cb-mono sg-cb-pv-num"
                           aria-label={`${ROLE_TITLE[role]} ${key}`}
                           disabled={!counts[role]}
                           value={sizes[role]?.[key] ?? ""}
                           onChange={(e) => setSizes({ ...sizes, [role]: { ...sizes[role], [key]: Number(e.target.value) } })} />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {counts.controlPlane && growth.totals.loadbalancer + counts.loadbalancer < 2 ? (
        <p className="sg-cb-topowarn">
          ⚠ With one load balancer the API address still has a single point of failure. Add the second balancer too.
        </p>
      ) : null}
      {counts.controlPlane ? (
        <p className="muted sg-cb-growrole-note">
          Before the new control planes join, an etcd snapshot is saved on the first control plane. If it cannot be taken, none of them is added.
        </p>
      ) : null}
      <div className="sg-cb-actions">
        <button className="primary" type="button" disabled={busy || !canExecute || !total} onClick={submit}>
          {busy ? "Planning…" : `Preview plan for ${total} VM${total === 1 ? "" : "s"}`}
        </button>
      </div>
    </div>
  );
}

/** Ask to destroy: typed name, a reason, and a second person decides. */
export function DestroyPanel({ build, notify, onChanged, onClose }) {
  const [typed, setTyped] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const vmCount = build.provisioning?.state?.vmCount || 0;
  const submit = async () => {
    setBusy(true);
    try {
      await requestClusterDestroy(build.id, { confirmName: typed, reason });
      await onChanged();
      onClose();
    } catch (error) {
      notify(error.message || String(error), true);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="card sg-cb-pv-danger">
      <div className="sg-cb-sect">
        <h2>Destroy {build.name}</h2>
        <button className="btn-ghost btn-sm" type="button" onClick={onClose}>Close</button>
      </div>
      <p className="sg-cb-pv-lede">
        Deletes the {vmCount} VM{vmCount === 1 ? "" : "s"} OpenTofu created for this cluster, its keep-apart rules and its
        folder{build.resultClusterId ? ", and removes the cluster from Clusters, Dashboard and Inventory — its kubeconfig stops working" : ""}.
        This cannot be undone. Only what is in this cluster&apos;s OpenTofu state is touched: the VM template, datastore,
        network and every other VM stay as they are.
      </p>
      <p className="muted">
        KubeSight first shows the exact plan. A second person who can run cluster builds has to approve it; you cannot approve your own request.
      </p>
      <div className="sg-cb-qgrid">
        <div className="sg-cb-field">
          <label className="sg-cb-field-label" htmlFor="destroy-name">
            Type <span className="sg-cb-mono">{build.name}</span> to confirm
          </label>
          <input id="destroy-name" className="sg-cb-input sg-cb-mono" autoComplete="off" value={typed}
                 onChange={(event) => setTyped(event.target.value)} />
        </div>
        <div className="sg-cb-field">
          <label className="sg-cb-field-label" htmlFor="destroy-why">Why</label>
          <input id="destroy-why" className="sg-cb-input" value={reason}
                 onChange={(event) => setReason(event.target.value)} placeholder="UAT moved to a new cluster" />
        </div>
      </div>
      <div className="sg-cb-actions">
        <button className="btn-danger" type="button" disabled={busy || typed !== build.name} onClick={submit}>
          {busy ? "Planning…" : "Plan the destroy and ask for approval"}
        </button>
      </div>
    </div>
  );
}

/** Save this build's shape, sizes, networking and add-ons as a template. */
export function SaveTemplatePanel({ build, notify, onClose, onSaved }) {
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [busy, setBusy] = useState(false);
  const counts = build.nodeCounts || {};
  const submit = async () => {
    setBusy(true);
    try {
      const saved = await createClusterTemplate({ name, description, fromBuildId: build.id });
      notify(`Saved “${saved.name}”. It is offered under Saved by admins in New build.`);
      if (onSaved) onSaved(saved);
      onClose();
    } catch (error) {
      notify(error.message || String(error), true);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="card sg-cb-card sg-cb-pv-panel">
      <div className="sg-cb-sect">
        <h2>Save as template</h2>
        <button className="btn-ghost btn-sm" type="button" onClick={onClose}>Close</button>
      </div>
      <p className="muted sg-cb-pv-lede">
        Keeps the shape ({shapeLabel(counts)}), the machine sizes, networking and add-ons. Not the
        vCenter placement or the addresses, so the template works on any vCenter.
      </p>
      <div className="sg-cb-qgrid">
        <div className="sg-cb-field">
          <label className="sg-cb-field-label" htmlFor="tpl-name">Name</label>
          <input id="tpl-name" className="sg-cb-input" value={name} onChange={(e) => setName(e.target.value)}
                 placeholder="Payments UAT" />
        </div>
        <div className="sg-cb-field">
          <label className="sg-cb-field-label" htmlFor="tpl-desc">What it is for</label>
          <input id="tpl-desc" className="sg-cb-input" value={description}
                 onChange={(e) => setDescription(e.target.value)} placeholder="Small, with bigger workers" />
        </div>
      </div>
      <div className="sg-cb-actions">
        <button className="primary" type="button" disabled={busy || !name.trim()} onClick={submit}>
          Save template
        </button>
      </div>
    </div>
  );
}
