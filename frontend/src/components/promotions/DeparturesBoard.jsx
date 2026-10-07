import { useEffect, useMemo, useRef, useState } from "react";

import { NEEDS_ATTENTION, boardStatus, countStates, dayKey, departureApps, fmtDay, fmtTime, fmtWhen } from "./timetableModel.js";

const FLAP = "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789:-";

function Tiles({ text, tone = "" }) {
  return (
    <span className={`tt-tiles tt-tiles--${tone}`} aria-label={text}>
      {[...text].map((c, i) =>
        c === " " ? (
          <span key={i} className="tt-ch tt-ch--sp" />
        ) : (
          // The character is drawn from data-show by CSS, so the flap
          // animation never touches React-owned text nodes.
          <span key={i} className="tt-ch" data-c={c} data-show={c} aria-hidden="true" />
        )
      )}
    </span>
  );
}

/** Split-flap settle whenever the board's text changes. */
function useFlap(ref, signature) {
  useEffect(() => {
    const root = ref.current;
    if (!root || window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) return undefined;
    const chars = [...root.querySelectorAll(".tt-ch[data-c]")];
    const start = performance.now();
    chars.forEach((el, i) => {
      el._end = 140 + (i % 9) * 40 + Math.random() * 140;
    });
    let frame;
    const step = (now) => {
      let live = 0;
      for (const el of chars) {
        if (now - start < el._end) {
          el.dataset.show = FLAP[Math.floor(Math.random() * FLAP.length)];
          live += 1;
        } else if (el.dataset.show !== el.dataset.c) {
          el.dataset.show = el.dataset.c;
        }
      }
      if (live) frame = requestAnimationFrame(step);
    };
    frame = requestAnimationFrame(step);
    return () => {
      cancelAnimationFrame(frame);
      chars.forEach((el) => (el.dataset.show = el.dataset.c));
    };
  }, [ref, signature]);
}

function detailFor(dep, rows, tz, now, envName) {
  const c = countStates(rows);
  const approval = dep.approval || {};
  const needs = approval.required > 0;
  switch (dep.kind === "ondemand" ? "ondemand" : dep.status) {
    case "ondemand":
      return { main: `${c.eligible || 0} eligible · promote any time`, sub: needs ? `${approval.required} approval required` : "No approval needed" };
    case "boarding":
      return {
        main: `Boarding · cut-off ${fmtTime(dep.cutoffAt, tz)}`,
        sub: needs ? `${approval.required} approval required · requested at cut-off` : "No approval needed",
      };
    case "scheduled":
      return { main: `Scheduled · cut-off ${fmtTime(dep.cutoffAt, tz)}`, sub: `Into ${envName}` };
    case "held":
      return { main: "Held", sub: "Nothing closes or deploys until it is released" };
    case "skipped":
      return { main: "Skipped", sub: "Its applications wait for the next release" };
    case "empty":
      return { main: "Nothing was eligible at the cut-off", sub: dep.note || "" };
    case "approval":
      return {
        main: "Awaiting approval",
        sub: `Signal held · ${approval.obtained || 0}/${approval.required || 1} approval${(approval.required || 1) === 1 ? "" : "s"} · bundle #${dep.release?.bundleIds?.[0] ?? "?"}`,
      };
    case "exception":
      return { main: "Awaiting approval · exception", sub: dep.release?.exceptionReason || "" };
    case "ready":
      return { main: `Approved · deploys ${fmtWhen(dep.departsAt, tz, now)}`, sub: approval.approver ? `Approved by ${approval.approver}` : "Approval obtained" };
    case "promoting":
      return { main: `Promoting · ${dep.release?.applications ?? rows.length} applications rolling out`, sub: "Rollout watched" };
    case "promoted":
      return { main: "Promoted · all new pods healthy", sub: dep.release?.actor ? `by ${dep.release.actor}` : "" };
    case "failed": {
      const bad = (dep.release?.counts?.refused || 0) + (dep.release?.counts?.failed || 0);
      return { main: "Failed", sub: bad ? `${bad} workload${bad === 1 ? "" : "s"} refused or rolled back` : "A rollout failed and was rolled back" };
    }
    case "refused":
      return { main: "Refused", sub: "Nothing was deployed" };
    case "rejected":
      return { main: "Rejected by approvers", sub: "" };
    case "expired":
      return { main: "Expired before approval", sub: "" };
    default:
      return { main: "", sub: "" };
  }
}

/**
 * The departures board: every release leaving every environment, past few
 * hours to the next few days. Click a row to open that release below.
 */
export default function DeparturesBoard({ timetable, overview, selectedKey, onSelect }) {
  const [filter, setFilter] = useState("soon");
  const tz = timetable.timezone;
  const now = timetable.now;
  const envName = (id) => overview.environments.find((e) => e.id === id)?.name || "?";
  const rows = useMemo(() => {
    const nowMs = new Date(now).getTime();
    return timetable.departures
      .filter((d) => {
        // Next 24 h: what is about to leave, plus anything still open — a
        // release awaiting approval stays on the board until it is settled.
        if (filter === "soon") {
          if (d.kind === "ondemand" || d.key === selectedKey || NEEDS_ATTENTION.has(d.status)) return true;
          const at = new Date(d.departsAt).getTime() - nowMs;
          return at > -12 * 3600000 && at < 24 * 3600000;
        }
        if (filter === "attention") return NEEDS_ATTENTION.has(d.status);
        return true;
      })
      .map((d) => ({ dep: d, apps: departureApps(d, overview) }));
  }, [timetable, overview, filter, now, selectedKey]);
  const ref = useRef(null);
  const signature = rows.map((r) => `${r.dep.key}:${r.dep.status}`).join("|") + filter;
  useFlap(ref, signature);

  return (
    <section className="tt-board" aria-label="Departures">
      <div className="tt-board-head">
        <h2>Departures</h2>
        <span className="tt-board-sub">Scheduled releases out of each environment · times in {tz}</span>
        <div className="tt-board-filter" role="group" aria-label="Show">
          {[
            ["soon", "Next 24 h"],
            ["attention", "Needs attention"],
            ["all", "All week"],
          ].map(([key, label]) => (
            <button key={key} type="button" className={`btn-ghost${filter === key ? " is-on" : ""}`} aria-pressed={filter === key} onClick={() => setFilter(key)}>
              {label}
            </button>
          ))}
        </div>
      </div>
      <div className="tt-board-scroll">
        <div className="tt-board-table" ref={ref} role="list">
          <div className="tt-brow tt-brow--head" role="presentation">
            <span>TIME</span>
            <span>RELEASE</span>
            <span>ROUTE</span>
            <span className="tt-num">APPS</span>
            <span>STATUS</span>
            <span>DETAIL</span>
          </div>
          {rows.length === 0 && <p className="tt-board-empty">Nothing here.</p>}
          {rows.map(({ dep, apps }) => {
            const st = boardStatus(dep);
            const c = countStates(apps);
            const count = dep.kind === "release" ? dep.release?.applications : dep.status === "boarding" || dep.kind === "ondemand" ? c.eligible || 0 : null;
            const detail = detailFor(dep, apps, tz, now, envName(dep.toEnvironmentId));
            const otherDay = dep.departsAt && dayKey(dep.departsAt, tz) !== dayKey(now, tz);
            const skips = dep.release?.kind === "exception" ? ["skip"] : [];
            return (
              <button
                key={dep.key}
                type="button"
                role="listitem"
                className={`btn-ghost tt-brow${dep.key === selectedKey ? " is-sel" : ""}${["promoted", "empty", "expired"].includes(dep.status) ? " is-past" : ""}`}
                onClick={() => onSelect(dep.key)}
                aria-label={`${dep.code}, ${envName(dep.fromEnvironmentId)} to ${envName(dep.toEnvironmentId)}, ${st.label}`}
              >
                <span className="tt-tcell">
                  <Tiles text={dep.departsAt ? fmtTime(dep.departsAt, tz) : "--:--"} />
                  {otherDay && <em>{fmtDay(dep.departsAt, tz).toUpperCase()}</em>}
                  {dep.kind === "ondemand" && <em>ANY TIME</em>}
                </span>
                <Tiles text={dep.code} tone={skips.length ? "wait" : ""} />
                <span className="tt-route">
                  {envName(dep.fromEnvironmentId).toUpperCase()} <em>→</em> {envName(dep.toEnvironmentId).toUpperCase()}
                </span>
                <span className="tt-num tt-apps">{count ?? "—"}</span>
                <Tiles text={st.text} tone={st.tone} />
                <span className="tt-remark">
                  <b>{detail.main}</b>
                  {detail.sub && <span>{detail.sub}</span>}
                </span>
              </button>
            );
          })}
        </div>
      </div>
      <div className="tt-board-foot">
        <span>
          <i className="tt-dot--go" />
          Boarding / in progress
        </span>
        <span>
          <i className="tt-dot--wait" />
          Awaiting approval / cut-off risk
        </span>
        <span>
          <i className="tt-dot--ok" />
          Promoted, all pods healthy
        </span>
        <span>
          <i className="tt-dot--bad" />
          Failed / blocked
        </span>
      </div>
    </section>
  );
}
