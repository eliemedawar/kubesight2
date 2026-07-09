import {
  formatTriggeredConditions,
  getAlertResourceName,
  getAlertTypeLabel,
} from "../../lib/alertDisplay.js";
import { formatFiringDuration } from "../../lib/alertFeed.js";

function Chevron(props) {
  return (
    <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true" focusable="false" {...props}>
      <polyline points="6 9 12 15 18 9" />
    </svg>
  );
}

function SeverityPill({ severity }) {
  return (
    <span className={`status-pill ${severity.tone}`}>{severity.label}</span>
  );
}

function TypeChip({ alert, type }) {
  const label = type || getAlertTypeLabel(alert);
  return <span className="al-type">{label}</span>;
}

function rowMeta(alert) {
  const parts = [];
  if (alert.namespace) {
    parts.push(`ns/${alert.namespace}`);
  }
  const resource = getAlertResourceName(alert);
  if (resource && resource !== "—") {
    parts.push(resource);
  }
  return parts.join(" · ");
}

/**
 * The grouped open-alerts feed: alerts sharing a policy collapse into a group
 * whose header carries the worst severity / count / oldest firing duration;
 * everything else renders as a flat row. Every row opens the detail drawer.
 */
export default function AlertFeed({ entries, nowTs, collapsedGroups, onToggleGroup, onOpenAlert }) {
  if (!entries.length) {
    return null;
  }
  return (
    <div className="al-feed">
      {entries.map((entry) => {
        if (entry.kind === "group") {
          const open = !collapsedGroups.has(entry.key);
          const oldest = formatFiringDuration(entry.alerts[0].firedAt, nowTs);
          return (
            <section
              key={entry.key}
              className={`al-group${entry.worst.tone === "danger" ? " al-critical" : ""}`}
            >
              <button
                type="button"
                className="al-ghead"
                aria-expanded={open}
                onClick={() => onToggleGroup(entry.key)}
              >
                <SeverityPill severity={entry.worst} />
                <span className="al-ghead-main">
                  <b>{entry.title}</b>
                  <span>{entry.count} alerts{oldest ? ` · oldest firing ${oldest}` : ""}</span>
                </span>
                <TypeChip type={getAlertTypeLabel(entry.alerts[0])} />
                <Chevron className={`al-chev${open ? "" : " al-chev--closed"}`} />
              </button>
              {open ? (
                <div className="al-grows">
                  {entry.alerts.map((alert) => {
                    const duration = formatFiringDuration(alert.firedAt, nowTs);
                    return (
                      <button
                        key={alert.id}
                        type="button"
                        className="al-row"
                        onClick={() => onOpenAlert(alert)}
                      >
                        <span className={`al-dot al-dot--${String(alert.severity || "info").toLowerCase()}`} aria-hidden="true" />
                        <span className="al-row-res">{getAlertResourceName(alert)}</span>
                        <span className="al-row-what">
                          {alert.namespace ? <span className="al-row-ns">ns/{alert.namespace}</span> : null}
                          {formatTriggeredConditions(alert)}
                        </span>
                        <span className="al-open-hint" aria-hidden="true">details →</span>
                        {duration ? <span className="al-dur">{duration}</span> : null}
                      </button>
                    );
                  })}
                </div>
              ) : null}
            </section>
          );
        }

        const { alert, severity } = entry;
        const duration = formatFiringDuration(alert.firedAt, nowTs);
        const meta = rowMeta(alert);
        const summary = formatTriggeredConditions(alert);
        return (
          <section
            key={entry.key}
            className={`al-flat${severity.tone === "danger" ? " al-critical" : ""}`}
          >
            <button type="button" className="al-flat-btn" onClick={() => onOpenAlert(alert)}>
              <SeverityPill severity={severity} />
              <span className="al-flat-main">
                <b>{alert.title || alert.policyName || summary}</b>
                <span>
                  {meta ? <span className="al-row-ns">{meta}</span> : null}
                  {meta && summary && summary !== "—" ? " — " : ""}
                  {summary !== "—" ? summary : ""}
                </span>
              </span>
              <TypeChip alert={alert} />
              <span className="al-open-hint" aria-hidden="true">details →</span>
              {duration ? <span className="al-dur">{duration}</span> : null}
            </button>
          </section>
        );
      })}
    </div>
  );
}
