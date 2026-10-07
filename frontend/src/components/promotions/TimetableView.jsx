import { Fragment, useMemo } from "react";

import DeparturesBoard from "./DeparturesBoard.jsx";
import { ModeIcon, PrIcon } from "./icons.jsx";
import ReleaseGraph from "./ReleaseGraph.jsx";
import ReleasePanel from "./ReleasePanel.jsx";
import Tracker from "./Tracker.jsx";
import { fmtWhen } from "./timetableModel.js";

const WEEKDAYS = [
  ["mon", "Mon"],
  ["tue", "Tue"],
  ["wed", "Wed"],
  ["thu", "Thu"],
  ["fri", "Fri"],
  ["sat", "Sat"],
  ["sun", "Sun"],
];

/** Which departure opens when nothing is chosen: the next one boarding. */
export function defaultDeparture(timetable) {
  const deps = timetable.departures;
  return (
    deps.find((d) => d.status === "boarding" && d.kind === "scheduled") ||
    deps.find((d) => ["approval", "exception", "held"].includes(d.status)) ||
    deps.find((d) => d.kind === "ondemand") ||
    deps[0] ||
    null
  );
}

/**
 * The Timetable: the environment line, the departures board with the
 * tracker, the selected release, the release graph and the schedule.
 */
export default function TimetableView({ overview, timetable, selectedKey, onSelect, tracked, onTrack, canDeploy, canManage, onChanged, onOpenApp, onOpenLadder, onToast }) {
  const selected = useMemo(
    () => timetable.departures.find((d) => d.key === selectedKey) || defaultDeparture(timetable),
    [timetable, selectedKey]
  );
  const envs = overview.environments;
  const tz = timetable.timezone;

  const nextInto = (envId) =>
    timetable.departures.find((d) => d.toEnvironmentId === envId && (d.kind === "ondemand" || ["boarding", "approval", "exception", "held", "ready", "promoting"].includes(d.status)));

  return (
    <div className="tt-root">
      <section className="tt-line" aria-label="Environments">
        <div className="tt-line-track" style={{ "--tt-stations": envs.length }}>
          {envs.map((env, i) => {
            const next = i < envs.length - 1 ? nextInto(envs[i + 1].id) : null;
            const waiting = next && ["approval", "exception", "held"].includes(next.status);
            return (
              <div key={env.id} className="tt-stn">
                <span className="tt-stn-dot" aria-hidden="true" />
                {next && (
                  <button type="button" className={`btn-ghost tt-next${waiting ? " is-wait" : ""}`} onClick={() => onSelect(next.key)}>
                    <i aria-hidden="true" />
                    <b>{next.kind === "ondemand" ? "on demand" : fmtWhen(next.departsAt, tz, timetable.now)}</b> {next.code}
                    {next.status === "held" ? " · held" : waiting ? " · awaiting approval" : next.status === "boarding" ? " · boarding" : ""}
                  </button>
                )}
                <h3>{env.name}</h3>
                <span className={`tt-mode tt-mode--${i === 0 ? "entry" : env.mode}`}>
                  <ModeIcon mode={i === 0 ? "entry" : env.mode} />
                  {i === 0 ? "Entry" : env.mode}
                </span>
                <small>
                  {env.appCount} applications · {env.workloadCount} workloads
                  {env.progressingCount > 0 && <em> · {env.progressingCount} rolling out</em>}
                </small>
              </div>
            );
          })}
        </div>
      </section>

      <div className="tt-concourse">
        <DeparturesBoard timetable={timetable} overview={overview} selectedKey={selected?.key} onSelect={onSelect} />
        <Tracker overview={overview} timetable={timetable} value={tracked} onChange={onTrack} onOpenApp={onOpenApp} />
      </div>

      {selected ? (
        <ReleasePanel
          key={selected.key}
          dep={selected}
          overview={overview}
          timetable={timetable}
          canDeploy={canDeploy}
          onChanged={onChanged}
          onOpenApp={onOpenApp}
          onToast={onToast}
          onPin={() => selectedKey !== selected.key && onSelect(selected.key)}
        />
      ) : (
        <div className="tt-empty">
          <PrIcon.Train />
          <p>No release yet. Set a schedule in Ladder, or promote on demand.</p>
        </div>
      )}

      <ReleaseGraph timetable={timetable} environments={envs} selectedKey={selected?.key} onSelect={onSelect} />

      <section className="tt-card" aria-label="Release schedule">
        <div className="tt-sec-head">
          <div>
            <p className="tt-eyebrow">Ladder settings</p>
            <h2>Release schedule</h2>
          </div>
          <p>
            Set once per hop; every release follows it. A hop without a schedule is on demand. Times in {tz}.
          </p>
          {canManage && (
            <button type="button" className="btn-outline tt-btn" onClick={onOpenLadder}>
              Edit schedule
            </button>
          )}
        </div>
        <div className="tt-sched-scroll">
          <table className="tt-sched">
            <thead>
              <tr>
                <th>Hop</th>
                {WEEKDAYS.map(([key, label]) => (
                  <th key={key}>{label}</th>
                ))}
                <th>Rules</th>
              </tr>
            </thead>
            <tbody>
              {timetable.hops.map((hop) => {
                const fromEnv = envs.find((e) => e.id === hop.fromEnvironmentId);
                const toEnv = envs.find((e) => e.id === hop.toEnvironmentId);
                const sched = hop.schedule;
                return (
                  <tr key={hop.toEnvironmentId}>
                    <th>
                      {fromEnv?.name} → {toEnv?.name}
                      <small>{hop.requiredApprovals ? `${hop.requiredApprovals} approval${hop.requiredApprovals === 1 ? "" : "s"}` : "no approval"}</small>
                    </th>
                    {sched.enabled ? (
                      WEEKDAYS.map(([key]) => (
                        <td key={key}>
                          {sched.days[key]?.length ? (
                            sched.days[key].map((t) => (
                              <span key={t} className="tt-t">
                                {t}
                              </span>
                            ))
                          ) : (
                            <span className="tt-none">—</span>
                          )}
                        </td>
                      ))
                    ) : (
                      <td colSpan={7} className="tt-rules">
                        On demand — promote from the board whenever it is ready
                      </td>
                    )}
                    <td className="tt-rules">
                      {sched.enabled ? `Cut-off ${sched.cutoffMinutes} min` : "—"}
                      {fromEnv?.minSoakMinutes ? ` · soak ${fromEnv.minSoakMinutes >= 60 ? `${Math.round(fromEnv.minSoakMinutes / 60)} h` : `${fromEnv.minSoakMinutes} min`} in ${fromEnv.name}` : ""}
                      {sched.owner ? <Fragment><br />Runs as {sched.owner}</Fragment> : null}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      </section>
    </div>
  );
}
