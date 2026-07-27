/** Small shared pieces of the Cluster Builder vocabulary. */

import { addonDisplayName } from "../../utils/clusterBuilder.js";

const STATUS_PILLS = {
  draft: ["Draft", "is-muted"],
  preflighting: ["Preflighting…", "is-live"],
  preflight_passed: ["Preflight passed", "is-ok"],
  preflight_failed: ["Preflight failed", "is-bad"],
  building: ["Building…", "is-live"],
  completed: ["Completed", "is-ok"],
  failed: ["Failed", "is-bad"],
  cancelled: ["Cancelled", "is-muted"],
  pending: ["Pending", "is-muted"],
  running: ["Running…", "is-live"],
  skipped: ["Skipped", "is-muted"],
  preparing: ["Preparing…", "is-live"],
  ready: ["Prepared", "is-ok"],
  joined: ["Joined", "is-ok"],
  removed: ["Removed", "is-muted"],
  pass: ["Pass", "is-ok"],
  warn: ["Warning", "is-warn"],
  fail: ["Fail", "is-bad"],
  ok: ["OK", "is-ok"],
};

export function StatusPill({ status, children }) {
  const [label, cls] = STATUS_PILLS[status] || [status || "—", "is-muted"];
  return <span className={`sg-cb-pill ${cls}`}>{children || label}</span>;
}

export function LiveBadge({ label = "Live" }) {
  return <span className="sg-cb-live"><i />{label}</span>;
}

/** A labelled control. Groups of controls (a segmented control, choice cards)
    pass no `htmlFor` and get a plain caption, because a <label> pointing at
    nothing is worse for a screen reader than no label at all — those groups
    carry their own role="group" and aria-label. */
export function Field({ label, htmlFor, children, hint, error }) {
  return (
    <div className="sg-cb-field">
      {label
        ? (htmlFor
          ? <label className="sg-cb-field-label" htmlFor={htmlFor}>{label}</label>
          : <span className="sg-cb-field-label">{label}</span>)
        : null}
      {children}
      {error
        ? <span className="sg-cb-field-error">{error}</span>
        : hint ? <span className="sg-cb-field-hint">{hint}</span> : null}
    </div>
  );
}

/** The build's fingerprint at row scale: circles are load balancers, red
    squares control planes, plain squares workers; filled means joined.
    A build with no machines yet still renders the wrapper — it occupies a grid
    cell, and collapsing it would shift every following column left. */
export function ShapeGlyph({ shape = [], buildStatus }) {
  if (!shape.length) return <span className="sg-cb-glyph is-empty" aria-hidden="true" />;
  const done = new Set(["joined", "ready"]);
  let previousRole = null;
  return (
    <span className="sg-cb-glyph" aria-hidden="true">
      {shape.map((node, index) => {
        const separator = previousRole && previousRole !== node.role;
        previousRole = node.role;
        const cls = [
          node.role === "loadbalancer" ? "is-lb" : "",
          node.role === "control_plane" ? "is-cp" : "",
          buildStatus === "completed" || done.has(node.status) ? "is-done" : "",
          node.status === "failed" ? "is-fail" : "",
        ].filter(Boolean).join(" ");
        return (
          <span className="sg-cb-glyph-cell" key={index}>
            {separator ? <s /> : null}
            <i className={cls} />
          </span>
        );
      })}
    </span>
  );
}

export function AddonChips({ addons = [], catalog = [], empty = "None" }) {
  if (!addons.length) return <span className="muted">{empty}</span>;
  return (
    <span className="sg-cb-chiprow">
      {addons.map((addon) => {
        const id = typeof addon === "string" ? addon : addon.id;
        const version = typeof addon === "object" ? addon.version : "";
        // Configured values are part of what was chosen — a MetalLB pool
        // decides which addresses the cluster will hand out.
        const settings = Object.values((typeof addon === "object" && addon.config) || {})
          .map((entry) => (Array.isArray(entry) ? entry.join(", ") : String(entry).trim()))
          .filter(Boolean);
        return (
          <span className="sg-cb-chip" key={`${id}-${version}`}>
            {addonDisplayName(addon, catalog)}
            {version ? <span className="sg-cb-chip-sub">v{version}</span> : null}
            {settings.map((entry) => (
              <span className="sg-cb-chip-sub" key={entry}>{entry}</span>
            ))}
          </span>
        );
      })}
    </span>
  );
}

export function HostChip({ host, conflict = false }) {
  if (!host) return <span className="muted">—</span>;
  return <span className={`sg-cb-hostchip ${conflict ? "is-conflict" : ""}`}>{host}</span>;
}

export function SectionHead({ title, right, children }) {
  return (
    <div className="sg-cb-sect">
      <h2>{title}</h2>
      {children}
      {right ? <span className="sg-cb-sect-right">{right}</span> : null}
    </div>
  );
}
