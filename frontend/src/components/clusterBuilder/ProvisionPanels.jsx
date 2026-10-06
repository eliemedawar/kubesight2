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
  destroyStance,
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
}) {
  const [busy, setBusy] = useState(false);
  const [refusal, setRefusal] = useState("");
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
    const add = job.summary?.add || 0;
    return (
      <PlanReview
        job={job}
        title={grow ? `Add ${add} worker${add === 1 ? "" : "s"} to ${build.name}` : `Plan for ${build.name}`}
        lede={grow
          ? "The existing VMs must stay untouched — the plan is refused if it would change one. The new workers join once they answer SSH."
          : "Applying runs exactly this plan. If anything in vCenter changes first, OpenTofu stops and asks for a new one."}
        applyLabel={grow
          ? `Create ${add} VM${add === 1 ? "" : "s"} and join them`
          : add ? `Create ${add} resource${add === 1 ? "" : "s"}` : "Continue"}
        onApply={canExecute ? () => act(() => applyProvisionPlan(build.id, job.id)) : null}
        onDiscard={canCreate ? () => act(() => discardProvisionJob(build.id, job.id)) : null}
        busy={busy}
      >
        {refusalNote}
      </PlanReview>
    );
  }
  if (job.status === "plan_failed") {
    return (
      <Failure title="The plan could not be made" error={job.error}>
        {planAgain}
        <span className="sg-cb-safe">Nothing was created. Fix what the message says, then plan again.</span>
      </Failure>
    );
  }
  if (["applying", "connecting", "interrupted"].includes(job.status)) {
    return <ProvisionProgress build={build} now={now} />;
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
          <span className="sg-cb-safe">
            OpenTofu&apos;s state was saved and its lock released. A new plan creates only what is missing.
          </span>
        </Failure>
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

/** Day two: ask OpenTofu for more workers. The card above takes over from the plan. */
export function ProvisionGrowPanel({ build, canExecute, notify, onChanged, onClose }) {
  const spec = build.provisioning?.spec || {};
  const [count, setCount] = useState(1);
  const [size, setSize] = useState(() => ({ ...(spec.sizes?.worker || { cpu: 4, memoryGb: 8, diskGb: 100 }) }));
  const [busy, setBusy] = useState(false);
  const workers = (spec.machines || []).filter((m) => m.role === "worker").length;
  const submit = async () => {
    setBusy(true);
    try {
      await planMoreWorkers(build.id, { count, size });
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
        <h2>Add workers to {build.name}</h2>
        <button className="btn-ghost btn-sm" type="button" onClick={onClose}>Close</button>
      </div>
      <p className="muted sg-cb-pv-lede">
        New VMs are cloned like the first {workers} worker{workers === 1 ? "" : "s"}, in{" "}
        <span className="sg-cb-mono">{spec.datastoreName}</span> on <span className="sg-cb-mono">{spec.networkName}</span>,
        then join the cluster. Running workloads are not touched. Control planes and load balancers cannot be added this way.
      </p>
      <div className="sg-cb-pv-growrow">
        <div className="sg-cb-field">
          <span className="sg-cb-field-label">How many</span>
          <div className="sg-cb-stepper" role="group" aria-label="Number of workers">
            <button type="button" className="btn-ghost" aria-label="Fewer" onClick={() => setCount((n) => Math.max(1, n - 1))}>−</button>
            <output>{count}</output>
            <button type="button" className="btn-ghost" aria-label="More" onClick={() => setCount((n) => Math.min(20, n + 1))}>+</button>
          </div>
        </div>
        {[["cpu", "vCPU"], ["memoryGb", "Memory GB"], ["diskGb", "Disk GB"]].map(([key, label]) => (
          <div className="sg-cb-field" key={key}>
            <label className="sg-cb-field-label" htmlFor={`grow-${key}`}>{label}</label>
            <input id={`grow-${key}`} type="number" min={1} className="sg-cb-input sg-cb-mono sg-cb-pv-num"
                   value={size[key]} onChange={(e) => setSize({ ...size, [key]: Number(e.target.value) })} />
          </div>
        ))}
      </div>
      <div className="sg-cb-actions">
        <button className="primary" type="button" disabled={busy || !canExecute} onClick={submit}>
          {busy ? "Planning…" : "Preview plan"}
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
