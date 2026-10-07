import { useMemo } from "react";

import { PrIcon } from "./icons.jsx";
import { formatRelative } from "./promotionModel.js";
import { fmtWhen, journey } from "./timetableModel.js";

/**
 * "Where's my application?" — what developers ask most. Where it runs, which
 * release it is in next, and the earliest it reaches the top of the ladder.
 */
export default function Tracker({ overview, timetable, value, onChange, onOpenApp }) {
  const app = overview.apps.find((a) => a.name === value || a.repository === value) || null;
  const route = useMemo(() => (app ? journey(app, overview, timetable) : null), [app, overview, timetable]);
  const suggestions = useMemo(
    () => overview.apps.filter((a) => a.lag > 0).slice(0, 4).map((a) => a.name),
    [overview.apps]
  );
  const tz = timetable.timezone;
  const top = overview.environments[overview.environments.length - 1];

  return (
    <aside className="tt-tracker" aria-label="Where is my application">
      <div>
        <p className="tt-eyebrow">Tracking</p>
        <h2>Where&apos;s my application?</h2>
      </div>
      <p className="tt-tracker-copy">Type a name to see where it runs, which release it is in, and when it reaches {top?.name}.</p>
      <label className="tt-search" htmlFor="tt-track">
        <PrIcon.Search />
        <input
          id="tt-track"
          list="tt-apps"
          autoComplete="off"
          value={value}
          onChange={(event) => onChange(event.target.value)}
          placeholder={suggestions[0] || "application name"}
          aria-label="Application name"
        />
        <datalist id="tt-apps">
          {overview.apps.map((a) => (
            <option key={a.key} value={a.name} />
          ))}
        </datalist>
      </label>
      <div className="tt-quick">
        {suggestions.map((name) => (
          <button key={name} type="button" className="btn-ghost" onClick={() => onChange(name)}>
            {name}
          </button>
        ))}
      </div>
      {value && !app && <p className="tt-tracker-copy">No application by that name.</p>}
      {app && route && (
        <>
          <ol className="tt-journey">
            {route.stops.map((stop) => (
              <li key={stop.env.id} className={`tt-jstop tt-jstop--${stop.state}`}>
                <span className="tt-jdot" aria-hidden="true" />
                <div>
                  <b>{stop.env.name}</b>
                  <span className="tt-jver">{stop.tag}</span>
                  <small>
                    {stop.state === "done"
                      ? stop.since
                        ? `Healthy since ${formatRelative(stop.since)}`
                        : "Running"
                      : stop.state === "absent"
                        ? stop.note
                        : stop.departure
                          ? stop.departure.kind === "ondemand"
                            ? `Next: on demand into ${stop.env.name}`
                            : `${stop.state === "next" ? "In" : "Then"} ${stop.departure.code} · ${fmtWhen(stop.departure.departsAt, tz, timetable.now)}`
                          : "No release scheduled yet"}
                    {stop.state === "next" && stop.step?.state === "soaking" && ` · soaking ${stop.step.soakMinutesLeft} min more`}
                  </small>
                </div>
              </li>
            ))}
          </ol>
          <div className="tt-eta">
            <span>{route.eta ? `Earliest in ${top?.name}` : `In ${top?.name}`}</span>
            <b>{route.eta ? fmtWhen(route.eta, tz, timetable.now) : route.stops[route.stops.length - 1].state === "done" ? "Already there" : "Not scheduled"}</b>
          </div>
          <button type="button" className="btn-ghost tt-link" onClick={() => onOpenApp(app.key)}>
            Open {app.name}
          </button>
        </>
      )}
    </aside>
  );
}
