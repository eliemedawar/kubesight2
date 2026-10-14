/** Readiness — every check, grouped, with the fix next to the problem.
 *
 *  A blocker stops the plan until it passes, or until an admin waives it
 *  with a reason (the reason travels with the request to the approver and
 *  into the audit log). Warnings never stop anything.
 */

import { useMemo, useState } from "react";
import { CHECK_TONE, CHECK_WORD, Chip, IconRefresh, timeAgo } from "./upgradeUi.jsx";

const DEFAULT_GROUPS = [
  { key: "workloads", label: "Draining and workloads" },
  { key: "health", label: "Cluster health" },
  { key: "versions", label: "Versions and APIs" },
  { key: "storage", label: "Storage" },
];

function CheckRow({ check, isAdmin, waiver, onWaiver }) {
  const [open, setOpen] = useState(false);
  const [waiving, setWaiving] = useState(Boolean(waiver));
  const tone = CHECK_TONE[check.status] || "muted";
  const failed = check.status === "failed";
  return (
    <li className={`uc-check${failed ? " uc-check--blocker" : ""}${failed && waiver ? " is-waived" : ""}`}>
      <div className="uc-check__status">
        <Chip tone={failed && waiver ? "warn" : tone}>{failed && waiver ? "Waived" : CHECK_WORD[check.status] || check.status}</Chip>
      </div>
      <div className="uc-check__body">
        <span className="uc-check__name">{check.name}</span>
        {check.details ? <span className="uc-check__detail">{check.details}</span> : null}
        {check.fix ? (
          <span className="uc-check__fix">
            <strong>Fix:</strong> {check.fix}
          </span>
        ) : null}
        {check.objects?.length ? (
          <div className="uc-check__objects">
            <button type="button" className="btn-ghost uc-linkbtn" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
              {open ? "Hide" : "Show"} {check.objectCount || check.objects.length} affected
            </button>
            {open ? (
              <ul className="uc-objects">
                {check.objects.map((o) => (
                  <li key={o} className="uc-mono">{o}</li>
                ))}
                {check.objectCount > check.objects.length ? (
                  <li className="uc-sub">…and {check.objectCount - check.objects.length} more</li>
                ) : null}
              </ul>
            ) : null}
          </div>
        ) : null}
        {failed && isAdmin ? (
          waiving ? (
            <label className="uc-waiver">
              <span>Why it is safe to go ahead anyway (goes to the approver and the audit log)</span>
              <input
                type="text"
                className="uc-input"
                value={waiver || ""}
                placeholder="e.g. payments-api is scaled to 4 before the window"
                onChange={(event) => onWaiver(check.key, event.target.value)}
              />
              <button
                type="button"
                className="btn-ghost uc-linkbtn"
                onClick={() => {
                  onWaiver(check.key, "");
                  setWaiving(false);
                }}
              >
                Don’t waive
              </button>
            </label>
          ) : (
            <button type="button" className="btn-ghost uc-linkbtn" onClick={() => setWaiving(true)}>
              Waive with a reason
            </button>
          )
        ) : null}
      </div>
    </li>
  );
}

export default function ReadinessPanel({
  readiness,
  checking,
  target,
  isAdmin,
  canStart,
  waivers,
  onWaiver,
  onRecheck,
  onContinue,
  staleFor,
}) {
  const [filter, setFilter] = useState("all");
  const checks = readiness?.checks || [];
  const counts = useMemo(
    () => ({
      all: checks.length,
      failed: checks.filter((c) => c.status === "failed").length,
      warning: checks.filter((c) => c.status === "warning").length,
      passed: checks.filter((c) => c.status === "passed").length,
    }),
    [checks]
  );
  const blockers = checks.filter((c) => c.status === "failed");
  const unwaived = blockers.filter((c) => !(waivers[c.key] || "").trim());
  const groups = (readiness?.groups?.length ? readiness.groups : DEFAULT_GROUPS)
    .map((g) => {
      const all = checks.filter((c) => (c.group || "health") === g.key);
      const shown = filter === "all" ? all : all.filter((c) => c.status === filter);
      const sum = [];
      const b = all.filter((c) => c.status === "failed").length;
      const w = all.filter((c) => c.status === "warning").length;
      if (b) sum.push(`${b} blocker${b === 1 ? "" : "s"}`);
      if (w) sum.push(`${w} warning${w === 1 ? "" : "s"}`);
      sum.push(`${all.filter((c) => c.status === "passed").length} passed`);
      return { ...g, shown, sum: sum.join(" · ") };
    })
    .filter((g) => g.shown.length);

  if (!readiness) {
    return (
      <section className="uc-card uc-readiness" aria-busy={checking} aria-labelledby="uc-ready-h">
        <h2 id="uc-ready-h" className="uc-h2">Readiness for {target}</h2>
        {checking ? (
          <div className="uc-checking">
            <span className="uc-spinner" aria-hidden="true" />
            <span>Reading the cluster once and running every check…</span>
          </div>
        ) : (
          <div className="uc-checking">
            <span className="uc-muted">Not checked for {target} yet.</span>
            <button type="button" className="btn-primary uc-btn-sm" onClick={onRecheck}>
              Run readiness checks
            </button>
          </div>
        )}
      </section>
    );
  }

  let verdictTone = "ok";
  let verdictWord = "Ready";
  let headline = "Nothing blocks the upgrade.";
  let sub = "Read the warnings, then build the plan. They travel with the plan, so the approver sees them too.";
  if (unwaived.length) {
    verdictTone = "danger";
    verdictWord = `Blocked by ${unwaived.length} check${unwaived.length === 1 ? "" : "s"}`;
    headline = `${unwaived[0].name}: ${unwaived[0].details || "failed"}`;
    sub = isAdmin
      ? "Fix it and re-check, or waive it with a reason (it goes into the audit log)."
      : "Fix it and re-check. Only an admin can waive a blocker.";
  } else if (blockers.length) {
    verdictTone = "warn";
    verdictWord = `${blockers.length} blocker${blockers.length === 1 ? "" : "s"} waived`;
    headline = "Waived blockers go to the approver with your reasons.";
    sub = "";
  } else if (counts.warning) {
    verdictWord = `Ready · ${counts.warning} warning${counts.warning === 1 ? "" : "s"}`;
  }

  return (
    <section className="uc-card uc-readiness" aria-labelledby="uc-ready-h">
      <div className="uc-readiness__head">
        <div className="uc-stack uc-gap-6">
          <Chip tone={verdictTone} className="uc-chip--lg">{verdictWord}</Chip>
          <h2 id="uc-ready-h" className="uc-h2 uc-clamp2">{headline}</h2>
          {sub ? <p className="uc-muted">{sub}</p> : null}
        </div>
        <div className="uc-readiness__meta">
          <span className="uc-sub">
            Checked {timeAgo(readiness.checkedAt)} against {readiness.targetVersion}
            {readiness.durationMs ? ` · ${(readiness.durationMs / 1000).toFixed(1)} s` : ""}
          </span>
          <button type="button" className="btn-outline uc-btn-sm" onClick={onRecheck} disabled={checking}>
            <IconRefresh /> {checking ? "Checking…" : "Re-check"}
          </button>
        </div>
      </div>
      {staleFor ? (
        <p className="uc-note">These results are for {staleFor}; re-check for {target}.</p>
      ) : null}

      <div className="uc-seg" role="group" aria-label="Filter checks">
        {[
          ["all", `All ${counts.all}`],
          ["failed", `Blockers ${counts.failed}`],
          ["warning", `Warnings ${counts.warning}`],
          ["passed", `Passed ${counts.passed}`],
        ].map(([key, label]) => (
          <button key={key} type="button" className={filter === key ? "is-on" : ""} aria-pressed={filter === key}
            onClick={() => setFilter(key)}>
            {label}
          </button>
        ))}
      </div>

      {groups.length === 0 ? <p className="uc-muted">No checks in this filter.</p> : null}
      {groups.map((group) => (
        <div key={group.key} className="uc-check-group">
          <div className="uc-check-group__head">
            <span className="uc-eyebrow">{group.label}</span>
            <span className="uc-sub">{group.sum}</span>
          </div>
          <ul className="uc-checks">
            {group.shown.map((check) => (
              <CheckRow key={check.key || check.name} check={check} isAdmin={isAdmin}
                waiver={waivers[check.key]} onWaiver={onWaiver} />
            ))}
          </ul>
        </div>
      ))}

      <div className="uc-actionbar">
        <span className="uc-sub">
          {unwaived.length ? "Warnings don’t stop you. Blockers do." : "Warnings don’t stop you."}
        </span>
        <button type="button" className="btn-primary" disabled={!canStart || unwaived.length > 0 || Boolean(staleFor)}
          onClick={onContinue}
          title={!canStart ? "You need the Start upgrades permission" : undefined}>
          Continue to plan
        </button>
      </div>
    </section>
  );
}
