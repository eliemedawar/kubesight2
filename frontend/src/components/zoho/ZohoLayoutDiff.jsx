import { IconAlert } from "./icons.jsx";

const MARK = { added: "+", removed: "−", changed: "~", unchanged: "=" };

/**
 * What a whole-layout write will do, shown before it happens.
 *
 * Zoho replaces the entire layout in one call, so the operator needs to see
 * that every existing field is carried over — not just that a section is being
 * added. The raw body is available too; it's the only real trust anchor.
 */
export default function ZohoLayoutDiff({ plan }) {
  const diff = plan?.diff || {};
  const rows = diff.sections || [];
  const dropped = diff.fieldsDropped || [];

  return (
    <div className="sg-zh-diffwrap">
      <p className="sg-zh-note">
        <IconAlert />
        <span>
          This rewrites the entire <b>{plan?.layoutName || "layout"}</b> layout in one Zoho call.
          Everything below is sent together.
        </span>
      </p>

      <div className="sg-zh-tags">
        {diff.sectionsAdded ? (
          <span className="status-pill ok">{diff.sectionsAdded} section added</span>
        ) : null}
        {diff.sectionsChanged ? (
          <span className="status-pill info">{diff.sectionsChanged} section changed</span>
        ) : null}
        <span className="status-pill muted">{diff.sectionsUnchanged || 0} unchanged</span>
        <span className="status-pill muted">{diff.fieldsCarried || 0} fields carried over</span>
        {dropped.length ? (
          <span className="status-pill danger">{dropped.length} fields dropped</span>
        ) : null}
      </div>

      <ul className="sg-zh-diff">
        {rows.map((row) => (
          <li key={row.id || row.name} className={`sg-zh-diff-row sg-zh-diff-row--${row.change}`}>
            <span className="sg-zh-diff-mark">{MARK[row.change] || "="}</span>
            <span>{row.previousName ? `${row.previousName} → ${row.name}` : row.name}</span>
            <span className="muted">
              {row.change === "changed"
                ? `${row.previousFieldCount} → ${row.fieldCount} fields`
                : `${row.fieldCount} field${row.fieldCount === 1 ? "" : "s"}`}
            </span>
          </li>
        ))}
      </ul>

      {(plan?.warnings || []).map((warning) => (
        <p key={warning} className="sg-zh-note">
          <IconAlert />
          <span>{warning}</span>
        </p>
      ))}

      <details className="sg-zh-diffraw">
        <summary>Show the exact request body</summary>
        <pre className="sg-zh-diffpre mono" tabIndex={0}>
          {JSON.stringify(plan?.body ?? {}, null, 2)}
        </pre>
      </details>
    </div>
  );
}
