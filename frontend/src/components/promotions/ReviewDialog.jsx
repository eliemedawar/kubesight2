import { useEffect, useRef, useState } from "react";

import { createPromotionRelease } from "../../api/promotionsApi.js";
import { PrIcon } from "./icons.jsx";
import { RELEASE_STATUS, defaultReleaseName, formatAge, plural, releasePayload } from "./promotionModel.js";

const MIN_REASON = 10;

/**
 * Review a release before it goes: what moves, from which version to which,
 * where, whether approvers are involved — then send it, and watch what
 * happened to each application. One release, one approval.
 */
export default function ReviewDialog({ plan, from, to, clusters, onRemove, onClose, onDone }) {
  const [name, setName] = useState(() => defaultReleaseName(to.name));
  const [reference, setReference] = useState("");
  const [note, setNote] = useState("");
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [release, setRelease] = useState(null);
  const dialogRef = useRef(null);

  useEffect(() => {
    dialogRef.current?.focus();
    const onKey = (event) => {
      if (event.key === "Escape" && !busy) (release ? onDone(release) : onClose());
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy, release, onClose, onDone]);

  const reasonNeeded = plan.exceptions > 0;
  const canSend = !busy && plan.items.length > 0 && name.trim() && (!reasonNeeded || reason.trim().length >= MIN_REASON);
  const gatedNames = plan.gatedClusters.map((id) => clusters?.[id]?.name || id);
  const maxApprovals = Math.max(0, ...plan.gatedClusters.map((id) => clusters?.[id]?.requiredApprovals || 0));

  const send = async () => {
    setBusy(true);
    setError("");
    try {
      const data = await createPromotionRelease(
        releasePayload(plan, {
          environmentId: to.id,
          name: name.trim(),
          reference: reference.trim(),
          note: note.trim(),
          exceptionReason: reason.trim(),
        })
      );
      setRelease(data);
    } catch (err) {
      setError(err.message || "The release could not be sent.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="pr-modal-root" role="presentation">
      <div className="pr-scrim" onClick={() => !busy && (release ? onDone(release) : onClose())} />
      <div
        className="pr-modal"
        role="dialog"
        aria-modal="true"
        aria-labelledby="pr-review-title"
        tabIndex={-1}
        ref={dialogRef}
      >
        {release ? (
          <ReleaseResult release={release} onDone={() => onDone(release)} />
        ) : (
          <>
            <header className="pr-modal-head">
              <div>
                <span className="pr-eyebrow">New release</span>
                <h3 id="pr-review-title">
                  Promote {plural(plan.items.length, "application")} to {to.name}
                </h3>
                <p className="pr-modal-sub">
                  from {from.name} · {plural(plan.workloads, "workload")} on {plural(plan.clusters.length, "cluster")}
                </p>
              </div>
              <button type="button" className="icon-button pr-x" onClick={onClose} aria-label="Close" disabled={busy}>
                <PrIcon.X />
              </button>
            </header>

            <div className="pr-modal-body">
              <div className="pr-form-grid">
                <label className="pr-field">
                  <span>Release name</span>
                  <input value={name} onChange={(event) => setName(event.target.value)} maxLength={160} />
                </label>
                <label className="pr-field">
                  <span>
                    Reference <em>optional</em>
                  </span>
                  <input
                    value={reference}
                    onChange={(event) => setReference(event.target.value)}
                    placeholder="CHG-2291, PAY-4417…"
                    maxLength={120}
                  />
                </label>
                <label className="pr-field pr-field--wide">
                  <span>
                    Note <em>optional · approvers read it</em>
                  </span>
                  <textarea
                    rows={2}
                    value={note}
                    onChange={(event) => setNote(event.target.value)}
                    placeholder="What is in this release."
                  />
                </label>
              </div>

              <div className="pr-facts">
                <div className="pr-fact">
                  <b>{plan.items.length}</b>
                  <span>{plan.items.length === 1 ? "application" : "applications"}</span>
                </div>
                <div className="pr-fact">
                  <b>{plan.workloads}</b>
                  <span>{plan.workloads === 1 ? "workload" : "workloads"}</span>
                </div>
                <div className="pr-fact">
                  <b>{plan.clusters.length}</b>
                  <span>{plan.clusters.map((id) => clusters?.[id]?.name || id).join(", ")}</span>
                </div>
                <div className={`pr-fact pr-fact--wide${plan.needsApproval ? " is-info" : " is-ok"}`}>
                  {plan.needsApproval ? <PrIcon.Clock /> : <PrIcon.Check />}
                  <span>
                    {plan.needsApproval ? (
                      <>
                        <b>Needs approval</b> on {gatedNames.join(", ")} ({maxApprovals} approval
                        {maxApprovals === 1 ? "" : "s"}) — sent as one change bundle, applied once approved.
                      </>
                    ) : (
                      <>
                        <b>Deploys now</b> — no cluster here requires approval.
                      </>
                    )}
                  </span>
                </div>
              </div>

              {reasonNeeded && (
                <section className="pr-exception">
                  <div className="pr-exception-head">
                    <PrIcon.Hand />
                    <div>
                      <h4>
                        {plan.exceptions} {plan.exceptions === 1 ? "application skips" : "applications skip"} a step
                      </h4>
                      <p>
                        {plan.exceptions === 1 ? "It goes" : "They go"} to approvers as an exception
                        {plan.items.length > plan.exceptions ? ", separate from the rest of the release" : ""}. Someone
                        other than you approves; {plan.exceptions === 1 ? "it deploys" : "they deploy"} automatically
                        once approved.
                      </p>
                    </div>
                  </div>
                  <label className="pr-field">
                    <span>Why can&apos;t {plan.exceptions === 1 ? "it" : "they"} go through the ladder?</span>
                    <textarea
                      rows={2}
                      value={reason}
                      onChange={(event) => setReason(event.target.value)}
                      placeholder="e.g. Hotfix for the card authorisation outage (INC-2291); SIT is down until Friday."
                    />
                  </label>
                </section>
              )}

              <div className="pr-review-table" role="table" aria-label="Changes">
                <div className="pr-rv-row pr-rv-row--head" role="row">
                  <span role="columnheader">Application</span>
                  <span role="columnheader">Version</span>
                  <span role="columnheader">Workloads</span>
                  <span role="columnheader">Check</span>
                  <span role="columnheader" />
                </div>
                {plan.items.map((item) => (
                  <div key={item.key} className={`pr-rv-row${item.exception ? " is-skip" : ""}`} role="row">
                    <span role="cell" className="pr-rv-app">
                      <b>{item.app.name}</b>
                      <em>{item.app.system}</em>
                    </span>
                    <span role="cell" className="pr-rv-ver">
                      <span className="pr-ver pr-ver--old">{item.fromTags.join(", ")}</span>
                      <PrIcon.Arrow />
                      <span className={`pr-ver pr-ver--new${item.exception ? " is-skip" : ""}`}>{item.tag}</span>
                    </span>
                    <span role="cell" className="pr-rv-wl" title={item.targets.map((t) => `${t.namespace}/${t.name}`).join("\n")}>
                      {item.targets.slice(0, 2).map((t) => (
                        <code key={`${t.clusterId}/${t.namespace}/${t.name}`}>
                          {t.namespace}/{t.name}
                        </code>
                      ))}
                      {item.targets.length > 2 && <em>+{item.targets.length - 2} more</em>}
                    </span>
                    <span role="cell">
                      {item.exception ? (
                        <span className="pr-skip">exception · skips {item.skips?.join(", ") || from.name}</span>
                      ) : (
                        <span className="pr-chip pr-chip--ok">
                          <PrIcon.Check /> passed {from.name}
                          {item.passedAt ? ` · ${formatAge(item.passedAt)}` : ""}
                        </span>
                      )}
                    </span>
                    <span role="cell">
                      <button
                        type="button"
                        className="icon-button pr-x pr-x--sm"
                        onClick={() => onRemove(item.key)}
                        aria-label={`Leave ${item.app.name} out`}
                        disabled={busy}
                      >
                        <PrIcon.X />
                      </button>
                    </span>
                  </div>
                ))}
              </div>

              {error && (
                <p className="pr-callout pr-callout--danger" role="alert">
                  <PrIcon.Stop />
                  <span>{error}</span>
                </p>
              )}
            </div>

            <footer className="pr-modal-foot">
              <span className="pr-foot-hint">
                Every workload still goes through the ladder and its cluster&apos;s approval rule.
              </span>
              <button type="button" className="btn-outline" onClick={onClose} disabled={busy}>
                Back
              </button>
              <button type="button" className="primary" onClick={send} disabled={!canSend}>
                {busy ? "Sending…" : `Promote ${plan.items.length} to ${to.name}`}
              </button>
            </footer>
          </>
        )}
      </div>
    </div>
  );
}

const RESULT_ICON = {
  applied: <PrIcon.Check />,
  unchanged: <PrIcon.Equal />,
  pending_approval: <PrIcon.Clock />,
  partial: <PrIcon.Warn />,
  refused: <PrIcon.Stop />,
  rejected: <PrIcon.Stop />,
  failed: <PrIcon.Stop />,
  expired: <PrIcon.Clock />,
};

const RESULT_PREVIEW = 12;

export function ReleaseResult({ release, onDone }) {
  const [showAll, setShowAll] = useState(false);
  const meta = RELEASE_STATUS[release.status] || RELEASE_STATUS.refused;
  // Problems first: what was refused is what the person has to act on.
  const ordered = [...release.items].sort(
    (a, b) => (a.status === "refused" || a.status === "partial" ? 0 : 1) - (b.status === "refused" || b.status === "partial" ? 0 : 1)
  );
  const shown = showAll ? ordered : ordered.slice(0, RESULT_PREVIEW);
  const headline = {
    applied: `Promoted to ${release.environmentName}`,
    pending_approval: "Sent for approval",
    partial: "Partly promoted",
    refused: "Nothing was promoted",
  }[release.status] || meta.label;
  const c = release.counts;
  return (
    <>
      <header className={`pr-result-head pr-result-head--${meta.tone}`}>
        <span className="pr-result-icon">{RESULT_ICON[release.status] || <PrIcon.Dot />}</span>
        <div>
          <span className="pr-eyebrow">{release.name}</span>
          <h3>{headline}</h3>
          <p>
            {[
              c.applied && `${c.applied} workload${c.applied === 1 ? "" : "s"} deployed`,
              c.pending_approval && `${c.pending_approval} waiting for approval`,
              c.refused && `${c.refused} refused`,
              c.unchanged && `${c.unchanged} already there`,
            ]
              .filter(Boolean)
              .join(" · ")}
            {c.applied > 0 && " — the overview shows them once the new pods are ready."}
          </p>
          {release.bundleIds.length > 0 && (
            <p className="pr-result-links">
              {release.bundleIds.map((id) => (
                <a key={id} className="pr-link" href="#/change-bundles/mine">
                  Change bundle #{id}
                </a>
              ))}
            </p>
          )}
        </div>
      </header>
      <div className="pr-modal-body">
        <ul className="pr-result-list">
          {shown.map((item) => (
            <li key={item.repository} className={`pr-result-item pr-result-item--${item.status}`}>
              <span className="pr-result-item-icon">{RESULT_ICON[item.status] || <PrIcon.Dot />}</span>
              <div className="pr-result-item-body">
                <div className="pr-result-item-top">
                  <b>{item.name}</b>
                  <span className="pr-ver pr-ver--old">{(item.fromTags || []).join(", ") || "—"}</span>
                  <PrIcon.Arrow />
                  <span className="pr-ver pr-ver--new">{item.tag}</span>
                  <span className={`pr-chip pr-chip--${(RELEASE_STATUS[item.status] || {}).tone || "muted"}`}>
                    {(RELEASE_STATUS[item.status] || {}).label || item.status}
                  </span>
                </div>
                {item.targets
                  .filter((t) => t.status === "refused" || t.warning)
                  .map((t) => (
                    <p key={`${t.namespace}/${t.name}`} className="pr-result-msg">
                      <code>
                        {t.namespace}/{t.name}
                      </code>{" "}
                      {t.message || t.warning}
                    </p>
                  ))}
              </div>
            </li>
          ))}
        </ul>
        {ordered.length > RESULT_PREVIEW && (
          <button type="button" className="btn-ghost pr-link pr-show-all" onClick={() => setShowAll(!showAll)}>
            {showAll ? "Show fewer" : `Show all ${ordered.length} applications`}
          </button>
        )}
      </div>
      <footer className="pr-modal-foot">
        <a className="pr-link" href="#/promotions/releases">
          See it in Releases
        </a>
        <button type="button" className="primary" onClick={onDone}>
          Done
        </button>
      </footer>
    </>
  );
}
