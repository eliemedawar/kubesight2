/** Preflight results, grouped by check rather than by machine. Shared by the
 *  wizard's Verify step and the build page, which shows the same verdict when
 *  preflight ran on its own (OpenTofu-created VMs, "Run preflight").
 */

import { StatusPill } from "./common.jsx";

export function CheckGroup({ group, open }) {
  return (
    <details className="sg-cb-chk" open={open}>
      <summary>
        <StatusPill status={group.status} />
        <span className="sg-cb-chk-id">
          <span className="ct">{group.label}</span>
          {group.machines[0]?.detail
            ? <span className="cw">{group.machines[0].detail}</span>
            : null}
        </span>
        <span className="sg-cb-chk-n">
          {group.machines.length} machine{group.machines.length === 1 ? "" : "s"}
        </span>
        <span className="sg-cb-chev" aria-hidden="true">›</span>
      </summary>
      <div className="sg-cb-chk-body">
        <div className="sg-cb-whos">
          {group.machines.map((machine) => (
            <span className="sg-cb-who sg-cb-mono" key={`${machine.nodeId}-${machine.name}`}>
              {machine.name}
              {machine.detail && machine.detail !== group.machines[0].detail
                ? ` — ${machine.detail}`
                : ""}
            </span>
          ))}
        </div>
        {group.hint ? (
          <p className={`sg-cb-fix is-${group.status}`}>
            <b>Fix</b>
            <span>{group.hint}</span>
          </p>
        ) : null}
      </div>
    </details>
  );
}

export default function PreflightReport({
  grouped,
  preflightResult,
  busy = false,
  onRerun = null,
  warnNote = "Warnings can be acknowledged below, and the acknowledgement is recorded against the build.",
}) {
  const { counts, total, attention, passSummary, verdict } = grouped;
  const good = total ? (counts.pass / total) * 100 : 0;
  const ringStyle = {
    background: `conic-gradient(var(--ok) 0 ${good}%, var(--warn) ${good}% ${
      ((counts.pass + counts.warn) / (total || 1)) * 100
    }%, var(--danger) ${((counts.pass + counts.warn) / (total || 1)) * 100}% 100%)`,
  };

  const headline = counts.fail
    ? `${counts.pass} of ${total} checks pass — ${counts.fail} must be fixed`
    : counts.warn
      ? `${counts.pass} of ${total} checks pass — ${counts.warn} warning${
        counts.warn === 1 ? "" : "s"}, nothing blocking`
      : `All ${total} checks pass`;

  return (
    <>
      <div className="card sg-cb-verdict">
        <div className="sg-cb-vring" style={ringStyle} role="img"
             aria-label={`${counts.pass} of ${total} checks pass`}>
          <b>{total}</b>
        </div>
        <div>
          <h3>{headline}</h3>
          <p className="muted">
            {verdict === "fail"
              ? "Fix the failures and re-run — failures are never acknowledgeable."
              : verdict === "warn"
                ? warnNote
                : "Ready to build."}
          </p>
        </div>
        {onRerun ? (
          <button className="btn-outline sg-cb-rerun" type="button" disabled={busy} onClick={onRerun}>
            Re-run preflight
          </button>
        ) : null}
      </div>

      {(preflightResult.topologyWarnings || []).map((warning) => (
        <p key={warning} className="sg-cb-topowarn">⚠ {warning}</p>
      ))}

      {/* One row per check, not per machine: a kernel module missing on three
          machines is one finding with one fix, and the count is the severity. */}
      {attention.map((group) => (
        <CheckGroup key={group.key} group={group} open={group.status === "fail"} />
      ))}

      {passSummary.checkCount ? (
        <details className="sg-cb-chk">
          <summary>
            <StatusPill status="pass" />
            <span className="sg-cb-chk-id">
              <span className="ct">
                {passSummary.checkCount} check{passSummary.checkCount === 1 ? "" : "s"} passed
              </span>
              <span className="cw">
                grouped so the page stays about what needs attention
              </span>
            </span>
            <span className="sg-cb-chk-n">
              {passSummary.machines.length} machine{passSummary.machines.length === 1 ? "" : "s"}
            </span>
            <span className="sg-cb-chev" aria-hidden="true">›</span>
          </summary>
          <div className="sg-cb-chk-body">
            <div className="sg-cb-whos">
              {grouped.passing.map((group) => (
                // A pass can still say what it found ("kubectl v1.30 from the
                // template, replaced") — keep that one hover away.
                <span className="sg-cb-who" key={group.key} title={group.machines[0]?.detail || undefined}>
                  {group.label}
                </span>
              ))}
            </div>
          </div>
        </details>
      ) : null}
    </>
  );
}
