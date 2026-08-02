import { Link } from "react-router-dom";
import FreshnessIndicator from "../components/common/FreshnessIndicator.jsx";

/**
 * The answer to "what needs attention?", at the top of the dashboard.
 *
 * Rows are ordered by the feed composer, not by source, so the most serious
 * thing is first regardless of which system reported it. Each row states what
 * is wrong, where, since when, and what to do — an operator should be able to
 * act from the row without opening the page it links to first.
 */

const TONE = {
  critical: "danger",
  warning: "warn",
  info: "info",
};

function AttentionRow({ entry }) {
  const body = (
    <>
      <span className={`attention-dot attention-dot--${TONE[entry.severity] || "info"}`} aria-hidden="true" />
      <span className="attention-main">
        <span className="attention-title">{entry.title}</span>
        {entry.detail ? <span className="attention-detail muted">{entry.detail}</span> : null}
        <span className="attention-meta muted">
          {entry.scope ? <span className="attention-scope">{entry.scope}</span> : null}
          {entry.detectedAt ? (
            <FreshnessIndicator timestamp={entry.detectedAt} prefix="since" />
          ) : (
            <span>time not recorded</span>
          )}
          {entry.owner ? <span className="attention-owner">{entry.owner}</span> : null}
        </span>
      </span>
      <span className="attention-action">{entry.action}</span>
    </>
  );

  if (!entry.href) {
    return <li className="attention-row">{body}</li>;
  }

  return (
    <li className="attention-row attention-row--link">
      <Link to={entry.href} className="attention-link">
        {body}
      </Link>
    </li>
  );
}

export default function AttentionFeed({ feed, loading = false }) {
  const { items = [], total = 0, counts = {}, unavailableSources = [] } = feed || {};

  return (
    <section className="ov-card attention-feed" aria-labelledby="attention-heading">
      <div className="ov-card-h">
        <h3 id="attention-heading">Needs attention</h3>
        <span className="ov-card-sub">
          {loading
            ? "Checking…"
            : total === 0
              ? "Nothing outstanding"
              : `${total} item${total === 1 ? "" : "s"}`}
          {counts.critical ? ` · ${counts.critical} critical` : ""}
        </span>
      </div>

      {/*
        A short feed because a source failed is not the same as a short feed
        because nothing is wrong, and an operator must not read the second when
        the first is true.
      */}
      {unavailableSources.length ? (
        <p className="banner-message banner-message--warn attention-partial" role="status">
          Could not check: {unavailableSources.join(", ")}. This list may be incomplete.
        </p>
      ) : null}

      {!loading && total === 0 && !unavailableSources.length ? (
        <p className="attention-clear muted">
          No firing alerts, degraded integrations, pending approvals, or unhealthy nodes.
        </p>
      ) : null}

      {items.length ? (
        <ol className="attention-list">
          {items.map((entry) => (
            <AttentionRow key={entry.id} entry={entry} />
          ))}
        </ol>
      ) : null}
    </section>
  );
}
