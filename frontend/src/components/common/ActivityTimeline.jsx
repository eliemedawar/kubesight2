import FreshnessIndicator from "./FreshnessIndicator.jsx";
import EmptyState from "./EmptyState.jsx";

/**
 * A chronological list of things that happened, with who and what.
 *
 * Used by the Integrations Activity tab first, then audit and deployment
 * history. Deliberately dumb: it takes already-shaped entries rather than
 * knowing about any one domain's payload, because the three sources that need
 * it disagree on field names and normalising here would put a mapping table in
 * a presentation component.
 *
 * Entry shape: { id, at, title, detail, actor, outcome }
 * `outcome` is "ok" | "warn" | "error" | undefined, and drives only the marker.
 *
 * An entry with no timestamp still renders. Activity feeds are assembled from
 * whatever the provider recorded, and dropping rows because one field is
 * missing turns a partial history into a misleading one.
 */
export default function ActivityTimeline({ entries = [], emptyMessage = "No recent activity." }) {
  if (!entries.length) {
    return <EmptyState message={emptyMessage} />;
  }

  return (
    <ol className="activity-timeline">
      {entries.map((entry, index) => (
        <li
          key={entry.id ?? index}
          className={`activity-entry activity-entry--${entry.outcome || "neutral"}`}
        >
          <span className="activity-marker" aria-hidden="true" />
          <div className="activity-body">
            <div className="activity-head">
              <span className="activity-title">{entry.title}</span>
              {entry.at ? (
                <FreshnessIndicator timestamp={entry.at} prefix="" className="activity-time" />
              ) : (
                <span className="activity-time muted">Time not recorded</span>
              )}
            </div>
            {entry.detail ? <p className="activity-detail muted">{entry.detail}</p> : null}
            {entry.actor ? <p className="activity-actor muted">by {entry.actor}</p> : null}
          </div>
        </li>
      ))}
    </ol>
  );
}
