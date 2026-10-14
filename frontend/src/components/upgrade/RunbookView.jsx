/** Guided runbook — you run the commands, KubeSight watches and ticks steps off. */

import { useCallback, useEffect, useMemo, useState } from "react";
import { controlUpgrade, getUpgradeRunbook, startGuidedUpgrade } from "../../api/upgradesApi.js";
import { setVisibleInterval } from "../../lib/visibleInterval";
import { Chip, IconCheck, IconCopy, ModeChip, timeAgo } from "./upgradeUi.jsx";

const POLL_MS = 15000;
const STATE = {
  done: { tone: "ok", word: "Seen" },
  waiting: { tone: "run", word: "Watching" },
  blocked: { tone: "muted", word: "After the step above" },
};
const FIELD_LABEL = { clusterName: "Cluster name at the provider", resourceGroup: "Resource group", location: "Location (region or zone)" };

function fill(text, values) {
  return text.replace(/\{(\w+)\}/g, (match, key) => (values[key] ? values[key] : `<${key}>`));
}

export default function RunbookView({ ws, target, onTargetChange, canStart, onChanged }) {
  const [book, setBook] = useState(null);
  const [error, setError] = useState("");
  const [values, setValues] = useState({});
  const [copied, setCopied] = useState("");
  const [busy, setBusy] = useState(false);
  const [manualDone, setManualDone] = useState({});

  const load = useCallback(async () => {
    try {
      const data = await getUpgradeRunbook(ws.clusterId, target);
      setBook(data);
      setError("");
      setValues((prev) => ({ ...(data.placeholders || {}), ...prev }));
      if (data.job?.status === "completed") onChanged?.();
    } catch (err) {
      setError(err.message);
    }
  }, [ws.clusterId, target, onChanged]);

  useEffect(() => {
    load();
    return setVisibleInterval(load, POLL_MS);
  }, [load]);

  const copy = async (key, text) => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(key);
      setTimeout(() => setCopied((c) => (c === key ? "" : c)), 1600);
    } catch {
      setCopied("");
    }
  };

  const track = async () => {
    setBusy(true);
    try {
      await startGuidedUpgrade(ws.clusterId, target);
      await load();
      onChanged?.();
    } catch (err) {
      setError(err.serverMessage || err.message);
    } finally {
      setBusy(false);
    }
  };
  const cancel = async () => {
    setBusy(true);
    try {
      await controlUpgrade(book.job.jobId, "cancel-guided");
      await load();
      onChanged?.();
    } catch (err) {
      setError(err.serverMessage || err.message);
    } finally {
      setBusy(false);
    }
  };

  const job = book?.job && book.job.status === "guided" ? book.job : null;
  const hops = (ws.path || []).filter((h) => h.version);
  const fields = Object.keys(book?.placeholders || {});
  const steps = book?.steps || [];
  const doneCount = useMemo(
    () => steps.filter((s) => (s.observe === "auto" ? s.state === "done" : manualDone[s.key])).length,
    [steps, manualDone]
  );

  return (
    <div className="uc-runbook">
      <section className="uc-card uc-runbook__intro">
        <div className="uc-run__head">
          <div className="uc-stack uc-gap-6">
            <ModeChip mode={ws.mode} />
            <h2 className="uc-h2">
              {ws.mode === "manual" ? "The platform upgrades this cluster; KubeSight watches the version." : "You run these steps; KubeSight follows along."}
            </h2>
            <p className="uc-muted">{ws.modeReason}</p>
          </div>
          {ws.mode === "guided" && canStart ? (
            job ? (
              <button type="button" className="btn-outline uc-btn-sm" onClick={cancel} disabled={busy}>Stop tracking</button>
            ) : (
              <button type="button" className="btn-primary uc-btn-sm" onClick={track} disabled={busy || !target}>
                Start tracking this upgrade
              </button>
            )
          ) : null}
        </div>
        {job ? (
          <p className="uc-note">
            Tracking since {timeAgo(job.startedAt)}: it ends in History on its own when every step is seen.
          </p>
        ) : null}
        {hops.length && ws.mode !== "manual" ? (
          <div className="uc-field">
            <span className="uc-field__label" id="uc-rb-target">Target</span>
            <div className="uc-seg" role="group" aria-labelledby="uc-rb-target">
              {hops.slice(0, 1).map((hop) => (
                <button key={hop.version} type="button" className={target === hop.version ? "is-on" : ""}
                  aria-pressed={target === hop.version} onClick={() => onTargetChange(hop.version)} disabled={!!job}>
                  {hop.version}
                </button>
              ))}
            </div>
          </div>
        ) : null}
        {fields.length ? (
          <div className="uc-fields-row">
            {fields.map((field) => (
              <label key={field} className="uc-field">
                <span className="uc-field__label">{FIELD_LABEL[field] || field}</span>
                <input className="uc-input uc-mono" value={values[field] || ""}
                  onChange={(e) => setValues((v) => ({ ...v, [field]: e.target.value }))} />
              </label>
            ))}
          </div>
        ) : null}
        {error ? <p className="uc-error" role="alert">{error}</p> : null}
      </section>

      <section className="uc-card" aria-labelledby="uc-rb-h">
        <div className="uc-card__head">
          <h3 id="uc-rb-h" className="uc-h3">
            {ws.mode === "manual" ? "How to upgrade" : `Runbook · ${book?.targetVersion || target}`}
          </h3>
          <span className="uc-sub">
            {steps.length ? `${doneCount} of ${steps.length} done` : ""}
            {book?.checkedAt ? ` · cluster read ${timeAgo(book.checkedAt)}` : ""}
          </span>
        </div>
        {!book && !error ? <p className="uc-muted">Writing the runbook…</p> : null}
        <ol className="uc-rb-steps">
          {steps.map((step, index) => {
            const auto = step.observe === "auto";
            const isDone = auto ? step.state === "done" : manualDone[step.key];
            const state = auto ? STATE[step.state] || STATE.waiting : null;
            return (
              <li key={step.key} className={`uc-rb-step${isDone ? " is-done" : ""}`}>
                <span className="uc-rb-step__n" aria-hidden="true">{isDone ? <IconCheck /> : index + 1}</span>
                <div className="uc-rb-step__body">
                  <div className="uc-rb-step__head">
                    <span className="uc-strong">{step.title}</span>
                    {auto ? (
                      <Chip tone={state.tone} title={step.seen ? `Ticked when KubeSight sees ${step.seen}` : undefined}>
                        {state.word}{step.seen && step.state !== "done" ? ` · ${step.seen}` : ""}
                      </Chip>
                    ) : (
                      <label className="uc-check-line uc-check-line--tight">
                        <input type="checkbox" checked={!!manualDone[step.key]}
                          onChange={(e) => setManualDone((m) => ({ ...m, [step.key]: e.target.checked }))} />
                        <span>I did this</span>
                      </label>
                    )}
                  </div>
                  {step.body ? <span className="uc-muted">{step.body}</span> : null}
                  {step.commands?.map((cmd, i) => {
                    const text = fill(cmd.text, values);
                    const key = `${step.key}:${i}`;
                    if (cmd.where === "note") {
                      return <span key={key} className="uc-rb-note">{text}</span>;
                    }
                    return (
                      <div key={key} className="uc-cmd">
                        <span className="uc-cmd__where">{cmd.where === "workstation" ? "kubectl / CLI" : `on ${cmd.where}`}{cmd.label ? ` · ${cmd.label}` : ""}</span>
                        <code>{text}</code>
                        <button type="button" className="btn-ghost uc-iconbtn" aria-label="Copy command" onClick={() => copy(key, text)}>
                          {copied === key ? <IconCheck /> : <IconCopy />}
                        </button>
                      </div>
                    );
                  })}
                </div>
              </li>
            );
          })}
        </ol>
      </section>
    </div>
  );
}
