/** Plan & approval — what will happen in order, how it runs, when, and who approves. */

import { useMemo, useState } from "react";
import { requestUpgrade } from "../../api/upgradesApi.js";
import { Chip } from "./upgradeUi.jsx";

const pad = (n) => String(n).padStart(2, "0");
const localInput = (date) =>
  `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`;

function nextSaturdayAt(hour) {
  const d = new Date();
  d.setDate(d.getDate() + ((6 - d.getDay() + 7) % 7 || 7));
  d.setHours(hour, 0, 0, 0);
  return d;
}

export default function PlanPanel({ ws, target, readiness, waivers, onBack, onSubmitted }) {
  const nodes = ws.nodes || [];
  const cps = nodes.filter((n) => n.role === "control-plane");
  const workers = nodes.filter((n) => n.role !== "control-plane");
  const maxWave = Math.max(1, Math.min(3, workers.length));
  const pods = readiness?.baseline?.podsByNode || {};

  const [waveSize, setWaveSize] = useState(1);
  const [drain, setDrain] = useState(10);
  const [snapshot, setSnapshot] = useState(true);
  const [pauseBetween, setPauseBetween] = useState(false);
  const [when, setWhen] = useState("asap");
  const [start, setStart] = useState(() => localInput(nextSaturdayAt(2)));
  const [end, setEnd] = useState(() => localInput(nextSaturdayAt(5)));
  const [note, setNote] = useState("");
  const [phrase, setPhrase] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState("");

  const expected = `UPGRADE ${ws.name} TO ${target}`;
  const waves = useMemo(() => {
    const out = [];
    for (let i = 0; i < workers.length; i += waveSize) out.push(workers.slice(i, i + waveSize));
    return out;
  }, [workers, waveSize]);
  // Rough: first control plane ~8 min, others ~5, a wave ~6, plus snapshot and verify.
  const minutes = (snapshot ? 2 : 0) + (cps.length ? 8 + 5 * (cps.length - 1) : 0) + waves.length * 6 + 3;
  const podsToMove = nodes.reduce((sum, n) => sum + (pods[n.name] || 0), 0);
  const singleReplica = readiness?.checks?.find((c) => c.key === "single_replica" && c.status === "warning");
  const warnings = (readiness?.checks || []).filter((c) => c.status === "warning");
  const waived = Object.entries(waivers).filter(([, reason]) => (reason || "").trim());
  const windowHours =
    when === "window" && start && end ? (new Date(end) - new Date(start)) / 3600000 : null;
  const fits = windowHours === null || minutes / 60 <= windowHours;
  const required = ws.requiredApprovals ?? 1;
  const submitLabel = required > 0 ? "Request approval" : when === "window" ? "Schedule upgrade" : "Start upgrade";

  const submit = async () => {
    setSubmitting(true);
    setError("");
    try {
      const job = await requestUpgrade({
        clusterId: ws.clusterId,
        targetVersion: target,
        options: { waveSize, drainTimeoutMinutes: drain, etcdSnapshot: snapshot, pauseBetweenWaves: pauseBetween },
        schedule:
          when === "window"
            ? {
                mode: "window",
                start: new Date(start).toISOString(),
                end: end ? new Date(end).toISOString() : null,
                timezone: Intl.DateTimeFormat().resolvedOptions().timeZone,
              }
            : { mode: "asap" },
        waivers: waived.map(([key, reason]) => ({ key, reason })),
        confirmation: phrase.trim(),
        note: note.trim() || undefined,
      });
      onSubmitted(job);
    } catch (err) {
      setError(err.serverMessage || err.message);
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="uc-plan">
      <section className="uc-card uc-plan__steps" aria-labelledby="uc-plan-h">
        <div className="uc-card__head">
          <h2 id="uc-plan-h" className="uc-h2">What will happen, in order</h2>
          <span className="uc-sub">About {minutes} min (rough) · stops at the first failure</span>
        </div>
        <ol className="uc-stages">
          <li className="uc-stage">
            <span className="uc-stage__n">0</span>
            <div className="uc-stage__body">
              <span className="uc-stage__title">Safety net</span>
              <span className="uc-muted">
                {snapshot
                  ? `An etcd snapshot inside ${cps[0]?.name || "the first control plane"}'s etcd pod, and a baseline of ready pods to compare against at the end.`
                  : "A baseline of ready pods to compare against at the end. No etcd snapshot: there is no way back if the control plane breaks."}
              </span>
            </div>
          </li>
          <li className="uc-stage">
            <span className="uc-stage__n">1</span>
            <div className="uc-stage__body">
              <span className="uc-stage__title">First control plane · {cps[0]?.name || "—"}</span>
              <span className="uc-muted">Packages, <code>kubeadm upgrade apply {target}</code>, then kubelet.</span>
            </div>
          </li>
          {cps.length > 1 ? (
            <li className="uc-stage">
              <span className="uc-stage__n">2</span>
              <div className="uc-stage__body">
                <span className="uc-stage__title">Other control planes · {cps.slice(1).map((n) => n.name).join(", then ")}</span>
                <span className="uc-muted">One at a time with <code>kubeadm upgrade node</code>, so the API keeps a majority throughout.</span>
              </div>
            </li>
          ) : null}
          {workers.length ? (
            <li className="uc-stage">
              <span className="uc-stage__n">{cps.length > 1 ? 3 : 2}</span>
              <div className="uc-stage__body">
                <span className="uc-stage__title">Workers · {waves.length} wave{waves.length === 1 ? "" : "s"} of {waveSize}</span>
                <span className="uc-muted">
                  At most {waveSize} of {workers.length} workers out at a time. A wave starts only when the last one’s nodes are Ready on the new kubelet.
                </span>
                <div className="uc-waves">
                  {waves.map((wave, i) => (
                    <div key={i} className="uc-wave">
                      <span className="uc-eyebrow">Wave {i + 1}</span>
                      {wave.map((n) => (
                        <span key={n.name} className="uc-wave__node">
                          <span>{n.name}</span>
                          {pods[n.name] !== undefined ? <span className="uc-sub uc-mono">{pods[n.name]} pods</span> : null}
                        </span>
                      ))}
                    </div>
                  ))}
                </div>
                <span className="uc-steps">
                  {["cordon", `drain · ${drain} min max`, "kubeadm upgrade node", "kubelet", "uncordon", "wait for Ready"].map((s) => (
                    <span key={s}>{s}</span>
                  ))}
                </span>
              </div>
            </li>
          ) : null}
          <li className="uc-stage">
            <span className="uc-stage__n">{(cps.length > 1 ? 3 : 2) + (workers.length ? 1 : 0)}</span>
            <div className="uc-stage__body">
              <span className="uc-stage__title">Verify</span>
              <span className="uc-muted">API server and every kubelet report {target}; ready pods are compared with the baseline.</span>
            </div>
          </li>
        </ol>
        <div className="uc-callout">
          <strong>If a step fails</strong>
          <span>
            The run stops on that node and says why. A drain that times out uncordons its node (nothing changed on it);
            a failed upgrade leaves the node cordoned for you to look at. Nodes already done stay on {target}, within the
            supported version skew. kubeadm can’t downgrade a control plane, so the step 0 snapshot is the way back.
          </span>
        </div>
      </section>

      <aside className="uc-plan__side">
        <section className="uc-card" aria-labelledby="uc-how-h">
          <h3 id="uc-how-h" className="uc-h3">How it runs</h3>
          {workers.length > 1 ? (
            <div className="uc-field">
              <span className="uc-field__label" id="uc-wave-l">Workers at a time</span>
              <div className="uc-seg" role="group" aria-labelledby="uc-wave-l">
                {Array.from({ length: maxWave }, (_, i) => i + 1).map((n) => (
                  <button key={n} type="button" className={waveSize === n ? "is-on" : ""} aria-pressed={waveSize === n}
                    onClick={() => setWaveSize(n)}>
                    {n}
                  </button>
                ))}
              </div>
            </div>
          ) : null}
          <label className="uc-field">
            <span className="uc-field__label">Drain timeout per node</span>
            <select className="uc-input" value={drain} onChange={(e) => setDrain(Number(e.target.value))}>
              {[5, 10, 15, 20, 30].map((m) => (
                <option key={m} value={m}>{m} minutes</option>
              ))}
            </select>
            <span className="uc-sub">A drain that runs out of time uncordons its node and stops the run.</span>
          </label>
          <label className="uc-check-line">
            <input type="checkbox" checked={snapshot} onChange={(e) => setSnapshot(e.target.checked)} />
            <span>Take an etcd snapshot first</span>
          </label>
          {workers.length > waveSize ? (
            <label className="uc-check-line">
              <input type="checkbox" checked={pauseBetween} onChange={(e) => setPauseBetween(e.target.checked)} />
              <span>Wait for my go-ahead after each wave</span>
            </label>
          ) : null}
        </section>

        <section className="uc-card" aria-labelledby="uc-when-h">
          <h3 id="uc-when-h" className="uc-h3">When</h3>
          <label className="uc-check-line">
            <input type="radio" name="uc-when" checked={when === "asap"} onChange={() => setWhen("asap")} />
            <span>{required > 0 ? "As soon as it is approved" : "Now"}</span>
          </label>
          <label className="uc-check-line">
            <input type="radio" name="uc-when" checked={when === "window"} onChange={() => setWhen("window")} />
            <span>In a maintenance window</span>
          </label>
          {when === "window" ? (
            <div className="uc-window">
              <label className="uc-field">
                <span className="uc-field__label">Starts</span>
                <input type="datetime-local" className="uc-input" value={start} onChange={(e) => setStart(e.target.value)} />
              </label>
              <label className="uc-field">
                <span className="uc-field__label">Ends</span>
                <input type="datetime-local" className="uc-input" value={end} onChange={(e) => setEnd(e.target.value)} />
              </label>
              <span className="uc-sub">
                Times are in {Intl.DateTimeFormat().resolvedOptions().timeZone}. If the window ends mid-run, KubeSight
                finishes the node it is on and pauses.
              </span>
              <Chip tone={fits ? "ok" : "warn"}>
                {fits ? `~${minutes} min fits the ${Math.round(windowHours * 10) / 10} h window` : `~${minutes} min may not fit`}
              </Chip>
            </div>
          ) : null}
        </section>

        <section className="uc-card" aria-labelledby="uc-impact-h">
          <h3 id="uc-impact-h" className="uc-h3">Impact</h3>
          <div className="uc-stats">
            <div><b>{podsToMove}</b><span className="uc-sub">pods rescheduled</span></div>
            <div><b>{singleReplica ? singleReplica.objectCount || singleReplica.objects?.length || 0 : 0}</b><span className="uc-sub">single-replica apps briefly down</span></div>
            <div><b>{warnings.length}</b><span className="uc-sub">warnings shown to the approver</span></div>
          </div>
          {waived.length ? (
            <p className="uc-note">{waived.length} waived blocker{waived.length === 1 ? "" : "s"} go to the approver with your reasons.</p>
          ) : null}
        </section>

        <section className="uc-card uc-approval" aria-labelledby="uc-appr-h">
          <div className="uc-card__head">
            <h3 id="uc-appr-h" className="uc-h3">Approval</h3>
            {required > 0 ? <Chip tone="info">Needs {required} approval{required === 1 ? "" : "s"}</Chip> : <Chip tone="ok">No approval needed</Chip>}
          </div>
          <p className="uc-muted">
            {required > 0
              ? "It goes to the Approvals inbox for people with the Start upgrades permission. You can’t approve your own. The plan, the warnings and the window travel with it."
              : `${ws.isAdmin && ws.clusterApprovals > 0 ? "You’re an admin, so no approval is needed" : `Approvals for ${ws.name} are set to 0`}: it ${when === "window" ? "starts when the window opens" : "starts as soon as you confirm"}.`}
          </p>
          <label className="uc-field">
            <span className="uc-field__label">Note for the approver (optional)</span>
            <textarea className="uc-input" rows={2} value={note} onChange={(e) => setNote(e.target.value)}
              placeholder="Why now, who is on call…" />
          </label>
          <label className="uc-field">
            <span className="uc-field__label">
              Type <code className="uc-phrase">{expected}</code> to confirm
            </span>
            <input className="uc-input uc-mono" type="text" autoComplete="off" spellCheck="false" value={phrase}
              onChange={(e) => setPhrase(e.target.value)} placeholder={expected} />
          </label>
          {error ? <p className="uc-error" role="alert">{error}</p> : null}
          <div className="uc-row">
            <button type="button" className="btn-outline" onClick={onBack}>Back to readiness</button>
            <button type="button" className="btn-primary" disabled={phrase.trim() !== expected || submitting} onClick={submit}>
              {submitting ? "Sending…" : submitLabel}
            </button>
          </div>
        </section>
      </aside>
    </div>
  );
}
