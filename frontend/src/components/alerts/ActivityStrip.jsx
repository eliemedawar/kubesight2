import { useState } from "react";

function hourLabel(startTs) {
  const date = new Date(startTs);
  return `${String(date.getHours()).padStart(2, "0")}:00`;
}

/**
 * 24h firing-activity strip: hourly columns stacked by severity.
 * Severity is encoded by fixed stack position (critical always grows from the
 * baseline, warning above it, info on top) — hue is a redundant channel, so
 * red-green colorblind users read position + the tooltip's dot-plus-word rows.
 */
export default function ActivityStrip({ buckets = [], maxTotal = 0, total = 0, loading = false }) {
  const [hover, setHover] = useState(null);

  const ariaSummary = total
    ? `Hourly alert firings over the last 24 hours, stacked by severity. ${total} firings in total.`
    : "No alert firings in the last 24 hours.";

  return (
    <section className="card al-activity" aria-label="Firing activity, last 24 hours">
      <div className="al-activity-head">
        <h4>Firing activity · last 24 h</h4>
        <div className="al-legend" aria-hidden="true">
          <span><i className="al-legend-dot--critical" />critical</span>
          <span><i className="al-legend-dot--warning" />warning</span>
          <span><i className="al-legend-dot--info" />info</span>
        </div>
      </div>

      {loading ? (
        <div className="al-strip-loading" aria-hidden="true">
          <span className="al-skeleton" />
        </div>
      ) : (
        <div className="al-strip-wrap">
          <div className="al-strip" role="img" aria-label={ariaSummary}>
            {buckets.map((bucket, index) => {
              const isNow = index === buckets.length - 1;
              const scale = maxTotal || 1;
              return (
                <div
                  key={bucket.startTs}
                  className={`al-col${isNow ? " al-col--now" : ""}`}
                  onMouseEnter={() => setHover(index)}
                  onMouseLeave={() => setHover(null)}
                >
                  {["critical", "warning", "info"].map((severity) =>
                    bucket[severity] ? (
                      <i
                        key={severity}
                        className={`al-bar al-bar--${severity}`}
                        style={{ height: `${Math.max(6, (bucket[severity] / scale) * 100)}%` }}
                      />
                    ) : null
                  )}
                </div>
              );
            })}
          </div>

          {hover != null && buckets[hover] ? (
            <div
              className="al-tip"
              role="presentation"
              style={{ left: `${((hover + 0.5) / buckets.length) * 100}%` }}
            >
              <b>{hourLabel(buckets[hover].startTs)} – {hourLabel(buckets[hover].startTs).replace(":00", ":59")}</b>
              {buckets[hover].total ? (
                <>
                  {buckets[hover].critical ? (
                    <span className="al-tip-row"><i className="al-legend-dot--critical" />{buckets[hover].critical} critical</span>
                  ) : null}
                  {buckets[hover].warning ? (
                    <span className="al-tip-row"><i className="al-legend-dot--warning" />{buckets[hover].warning} warning</span>
                  ) : null}
                  {buckets[hover].info ? (
                    <span className="al-tip-row"><i className="al-legend-dot--info" />{buckets[hover].info} info</span>
                  ) : null}
                </>
              ) : (
                <span className="al-tip-row">no firings</span>
              )}
            </div>
          ) : null}

          {!total ? <p className="al-strip-empty">No firings in the last 24 hours.</p> : null}

          <div className="al-strip-x" aria-hidden="true">
            <span>-24h</span><span>-18h</span><span>-12h</span><span>-6h</span><span>now</span>
          </div>
        </div>
      )}
    </section>
  );
}
