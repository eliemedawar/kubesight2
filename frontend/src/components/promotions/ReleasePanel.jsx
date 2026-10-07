import { useEffect, useMemo, useRef, useState } from "react";

import { createPromotionRelease, setDepartureExcluded, setDepartureState } from "../../api/promotionsApi.js";
import { PrIcon } from "./icons.jsx";
import ReviewDialog from "./ReviewDialog.jsx";
import VersionMenu from "./VersionMenu.jsx";
import { gateQueue, planRelease, plural, versionChoices } from "./promotionModel.js";
import {
  APP_ORDER,
  APP_STATE,
  boardStatus,
  bySystem,
  countStates,
  departureApps,
  fmtTime,
  fmtWhen,
  minutesUntil,
  promotableItems,
} from "./timetableModel.js";

/**
 * The selected release: what it is (code, route, version, change), the four
 * facts an operator needs before acting (approval, health, cut-off, rollback),
 * the actions, then its applications by system.
 */
export default function ReleasePanel({ dep, overview, timetable, canDeploy, onChanged, onOpenApp, onToast, onPin }) {
  const envs = overview.environments;
  const from = envs.find((e) => e.id === dep.fromEnvironmentId);
  const to = envs.find((e) => e.id === dep.toEnvironmentId);
  const tz = timetable.timezone;
  const rows = useMemo(() => departureApps(dep, overview), [dep, overview]);
  const counts = countStates(rows);
  const [filter, setFilter] = useState("all");
  const [open, setOpen] = useState(() => new Set());
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [exception, setException] = useState(null);
  const [menuFor, setMenuFor] = useState(null);

  useEffect(() => {
    setFilter("all");
    setOpen(new Set());
  }, [dep.key]);

  const isOpen = dep.kind === "ondemand" || ["boarding", "scheduled", "held", "skipped"].includes(dep.status);
  const scheduled = dep.kind === "scheduled";
  const items = promotableItems(rows);
  const workloads = items.reduce((n, i) => n + i.targets.length, 0);
  const shown = rows.filter((r) => filter === "all" || r.state === filter);
  const groups = bySystem(shown);
  const openSet = open.size ? open : new Set(groups.slice(0, 1).map((g) => g.name));
  const clusterNames = [...new Set(rows.flatMap((r) => r.targets.map((t) => overview.clusters?.[t.clusterId]?.name || t.clusterId)))];
  const st = boardStatus(dep);
  const approval = dep.approval || { required: 0, obtained: 0 };
  const release = dep.release;

  const ready = (counts.eligible || 0) + (counts.soaking || 0) + (counts.scheduled || 0) + (counts.approval || 0) + (counts.promoted || 0);
  const waiting = counts.late || 0;
  const blocked = (counts.blocked || 0) + (counts.refused || 0) + (counts.failed || 0);
  const cutoffIn = minutesUntil(dep.cutoffAt);

  const slot = { environmentId: dep.toEnvironmentId, departsAt: dep.departsAt };
  const act = async (fn, message) => {
    // Keep this release on screen: acting on the default one can change
    // which departure is the default (hold the boarding one → the next boards).
    onPin?.();
    setBusy(true);
    try {
      await fn();
      onToast(message);
      await onChanged();
    } catch (err) {
      onToast(err.message || "That did not work.");
    } finally {
      setBusy(false);
    }
  };

  const promote = () =>
    act(async () => {
      const result = await createPromotionRelease({
        environmentId: dep.toEnvironmentId,
        items,
        name: `${to.name} · ${dep.code}`,
        departsAt: scheduled ? dep.departsAt : undefined,
      });
      setConfirming(false);
      return result;
    }, `${dep.code}: ${plural(items.length, "application")} sent to ${to.name}`);

  // Exceptions: a late or blocked application, promoted with another version
  // and a reason, through the release review.
  const exceptionPlan = useMemo(() => {
    if (!exception) return null;
    const hop = envs.findIndex((e) => e.id === dep.toEnvironmentId) - 1;
    const rowsByKey = new Map(gateQueue(overview.apps, hop).map((r) => [r.app.key, r]));
    return planRelease(new Map([[exception.app.key, { image: exception.choice.image, exception: true, skips: exception.choice.skips.length ? exception.choice.skips : [from.name] }]]), rowsByKey, overview.clusters);
  }, [exception, envs, dep.toEnvironmentId, overview, from]);

  const approvalFact = (() => {
    if (!approval.required) return { tone: "ok", val: "Not required", sub: `${to.name} deploys without approval`, icon: <PrIcon.Check /> };
    if (release && approval.obtained >= approval.required) {
      return { tone: "ok", val: `${approval.obtained}/${approval.required} obtained`, sub: approval.approver ? `Approved by ${approval.approver}` : "Approval obtained", icon: <PrIcon.Check /> };
    }
    if (release) {
      return { tone: "wait", val: `${approval.obtained}/${approval.required} · awaiting`, sub: `Signal held · ${plural(approval.required, "approval")} required`, icon: <PrIcon.Clock /> };
    }
    return {
      tone: "",
      val: `${approval.required} required`,
      sub: scheduled ? "Requested from approvers at the cut-off" : "Requested when you promote",
      icon: <PrIcon.Hand />,
    };
  })();

  const timeFact = (() => {
    if (dep.kind === "ondemand") return { lbl: "Departs", val: "On demand", sub: "Promote whenever it is ready" };
    if (release) {
      return {
        lbl: dep.status === "promoted" ? "Promoted" : "Departs",
        val: fmtWhen(dep.departsAt, tz, timetable.now),
        sub: release.departsAt ? `Closed ${fmtWhen(release.createdAt, tz, timetable.now)}` : `Promoted by hand${release.actor ? ` · ${release.actor}` : ""}`,
      };
    }
    return {
      lbl: "Cut-off",
      val: fmtTime(dep.cutoffAt, tz),
      sub: cutoffIn > 0 ? `in ${cutoffIn < 120 ? `${cutoffIn} min` : `${Math.round(cutoffIn / 60)} h`} · departs ${fmtWhen(dep.departsAt, tz, timetable.now)}` : `departs ${fmtWhen(dep.departsAt, tz, timetable.now)}`,
      tone: cutoffIn > 0 && cutoffIn <= 60 ? "go" : "",
    };
  })();

  const rollback = timetable.rollback || { automatic: true, timeoutMinutes: 15 };
  const left = (counts.soaking || 0) + (counts.late || 0) + (counts.blocked || 0) + (counts.moved || 0);

  return (
    <section className="tt-release" aria-label={`Release ${dep.code}`}>
      <div className="tt-r-top">
        <div className="tt-r-title">
          <h2>
            <code>{dep.code}</code> · {from?.name} <span className="tt-arrow">→</span> {to?.name}
          </h2>
          <span className={`tt-pill tt-pill--${st.tone}`}>{st.label}</span>
        </div>
        <p className="tt-r-meta">
          <span>
            <b>{release ? release.applications : rows.length}</b> applications
          </span>
          <span>
            Change{" "}
            {release?.bundleIds?.length ? (
              <b>{release.bundleIds.map((id) => `bundle #${id}`).join(", ")}</b>
            ) : approval.required ? (
              "one bundle, raised at the cut-off"
            ) : (
              "applied directly, no approval"
            )}
          </span>
          <span>
            Release <b>{dep.version || release?.version || "assigned on promotion"}</b>
          </span>
          {clusterNames.length > 0 && <span>{clusterNames.join(", ")}</span>}
        </p>

        <div className="tt-facts">
          <div className={`tt-fact tt-fact--${approvalFact.tone}`}>
            <span className="tt-lbl">Approval</span>
            <span className="tt-val">
              {approvalFact.icon}
              {approvalFact.val}
            </span>
            <span className="tt-sub">{approvalFact.sub}</span>
          </div>
          <div className={`tt-fact${blocked ? "" : " tt-fact--ok"}`}>
            <span className="tt-lbl">Health</span>
            <span className="tt-val tt-health">
              <span className="tt-h-ok">
                <b>{ready}</b> ready
              </span>
              {waiting > 0 && (
                <span className="tt-h-wait">
                  <b>{waiting}</b> waiting
                </span>
              )}
              {blocked > 0 && (
                <span className="tt-h-bad">
                  <b>{blocked}</b> {counts.failed ? "failed" : "blocked"}
                </span>
              )}
            </span>
            <span className="tt-sub">{release ? "Read from the release's change bundle" : `Eligible = healthy in ${from?.name} for its soak time`}</span>
          </div>
          <div className={`tt-fact${timeFact.tone ? ` tt-fact--${timeFact.tone}` : ""}`}>
            <span className="tt-lbl">{timeFact.lbl}</span>
            <span className="tt-val">
              <PrIcon.Clock />
              {timeFact.val}
            </span>
            <span className="tt-sub">{timeFact.sub}</span>
          </div>
          <div className="tt-fact">
            <span className="tt-lbl">Rollback</span>
            <span className="tt-val">
              <PrIcon.Refresh />
              {rollback.automatic ? "Automatic" : "Manual"}
            </span>
            <span className="tt-sub">
              {rollback.automatic ? `A rollout not healthy within ${rollback.timeoutMinutes} min is rolled back` : "Roll back from the workload if a rollout fails"}
            </span>
          </div>
        </div>

        <div className="tt-r-actions">
          <span className="tt-hint">
            {isOpen && dep.status !== "skipped"
              ? `${items.length} eligible now${counts.soaking ? ` · ${counts.soaking} join if their soak ends before the cut-off` : ""}`
              : dep.status === "approval"
                ? "Deploys automatically once approved."
                : dep.status === "ready"
                  ? `Approved — deploys at ${fmtWhen(dep.departsAt, tz, timetable.now)}.`
                  : ""}
          </span>
          {canDeploy && scheduled && dep.status !== "held" && dep.status !== "skipped" && (
            <>
              <button type="button" className="btn-outline tt-btn" disabled={busy} onClick={() => act(() => setDepartureState({ ...slot, state: "held" }), `${dep.code} held — nothing closes until you release it`)}>
                <PrIcon.Pause />
                Hold
              </button>
              <button type="button" className="btn-outline tt-btn" disabled={busy} onClick={() => act(() => setDepartureState({ ...slot, state: "skipped" }), `${dep.code} skipped — its applications ride the next release`)}>
                <PrIcon.Skip />
                Skip to next
              </button>
            </>
          )}
          {canDeploy && scheduled && (dep.status === "held" || dep.status === "skipped") && (
            <button type="button" className="btn-outline tt-btn" disabled={busy} onClick={() => act(() => setDepartureState({ ...slot, state: "open" }), `${dep.code} is back on the timetable`)}>
              {dep.status === "held" ? "Release hold" : "Run it after all"}
            </button>
          )}
          {canDeploy && isOpen && dep.status !== "skipped" && (
            <button type="button" className="primary tt-btn" disabled={busy || !items.length} onClick={() => setConfirming(true)}>
              Promote {plural(items.length, "app")} to {to?.name}
              <PrIcon.Arrow />
            </button>
          )}
          {release?.bundleIds?.length > 0 && (
            <a className="btn-outline tt-btn" href="#/change-bundles/all">
              Open change bundle #{release.bundleIds[0]}
            </a>
          )}
        </div>
      </div>

      <div className="tt-capacity">
        <div className="tt-cap-bar" aria-hidden="true">
          {APP_ORDER.filter((k) => counts[k]).map((k) => (
            <i key={k} className={`tt-seat--${k}`} style={{ width: `${(counts[k] / Math.max(1, rows.length)) * 100}%` }} />
          ))}
        </div>
        <div className="tt-chips" role="group" aria-label="Filter applications">
          <button type="button" className={`btn-ghost tt-chip${filter === "all" ? " is-on" : ""}`} onClick={() => setFilter("all")}>
            <b>{rows.length}</b> applications
          </button>
          {APP_ORDER.filter((k) => counts[k]).map((k) => (
            <button key={k} type="button" className={`btn-ghost tt-chip${filter === k ? " is-on" : ""}`} onClick={() => { setFilter(k); setOpen(new Set()); }}>
              <i className={`tt-seat--${k}`} />
              {APP_STATE[k].label} <b>{counts[k]}</b>
            </button>
          ))}
        </div>
        {isOpen && scheduled && dep.status !== "skipped" && (
          <p className="tt-rule-line">
            <b>Joins automatically:</b> every application eligible in {from?.name} by {fmtTime(dep.cutoffAt, tz)}. Anything not eligible by then rides the next release — nobody picks applications by hand.
          </p>
        )}
      </div>

      {rows.length === 0 ? (
        <div className="tt-empty">
          <PrIcon.Check />
          <p>{isOpen ? `Nothing is waiting for ${to?.name} — it is caught up with ${from?.name}.` : "This release has no applications."}</p>
        </div>
      ) : (
        <div className="tt-systems">
          {groups.map((group) => {
            const isOpenGroup = openSet.has(group.name);
            const good = group.items.filter((r) => ["eligible", "promoted", "scheduled"].includes(r.state)).length;
            return (
              <div key={group.name} className="tt-sys" data-open={isOpenGroup}>
                <button
                  type="button"
                  className="btn-ghost tt-sys-head"
                  aria-expanded={isOpenGroup}
                  onClick={() => {
                    const next = new Set(openSet);
                    if (next.has(group.name)) next.delete(group.name);
                    else next.add(group.name);
                    setOpen(next);
                  }}
                >
                  <PrIcon.ChevronDown />
                  <span className="tt-sys-name">{group.name}</span>
                  <span className="tt-seats" aria-hidden="true">
                    {group.items.map((r) => (
                      <i key={r.key} className={`tt-seat tt-seat--${r.state}`} title={`${r.app.name} · ${APP_STATE[r.state].label}`} />
                    ))}
                  </span>
                  <span className="tt-sys-count">
                    <b>{good}</b> / {group.items.length} {release ? "promoted" : "eligible"}
                  </span>
                </button>
                {isOpenGroup &&
                  group.items.map((row) => (
                    <div key={row.key} className="tt-app">
                      <i className={`tt-seat tt-seat--${row.state}`} aria-hidden="true" />
                      <button type="button" className="btn-ghost tt-who" onClick={() => onOpenApp(row.app.key)}>
                        <b>{row.app.name}</b>
                        <span>{row.app.repository}</span>
                      </button>
                      <span className="tt-ver">
                        <span className="tt-ver-from">{row.fromTag}</span>
                        <span aria-hidden="true">→</span>
                        <span className="tt-ver-to">{row.toTag}</span>
                      </span>
                      <span className="tt-why">{whyText(row, from, to, dep, tz)}</span>
                      <span className={`tt-status tt-status--${APP_STATE[row.state].tone}`}>{APP_STATE[row.state].label}</span>
                      <span className="tt-row-act">
                        {canDeploy && scheduled && isOpen && row.state !== "moved" && row.state !== "sent" && (
                          <button
                            type="button"
                            className="icon-button tt-x"
                            title="Move to the next release"
                            aria-label={`Move ${row.app.name} to the next release`}
                            disabled={busy}
                            onClick={() => act(() => setDepartureExcluded({ ...slot, repository: row.app.repository, excluded: true }), `${row.app.name} moved to the next release`)}
                          >
                            <PrIcon.X />
                          </button>
                        )}
                        {canDeploy && row.state === "moved" && (
                          <button
                            type="button"
                            className="btn-ghost tt-link"
                            disabled={busy}
                            onClick={() => act(() => setDepartureExcluded({ ...slot, repository: row.app.repository, excluded: false }), `${row.app.name} is back in ${dep.code}`)}
                          >
                            Bring back
                          </button>
                        )}
                        {canDeploy && isOpen && ["late", "blocked"].includes(row.state) && (
                          <span className="tt-menu-anchor">
                            <button type="button" className="btn-ghost tt-link" onClick={() => setMenuFor(menuFor === row.key ? null : row.key)}>
                              Exception…
                            </button>
                            {menuFor === row.key && (
                              <VersionMenu
                                choices={versionChoices(row.app, envs, envs.indexOf(to))}
                                current={null}
                                onClose={() => setMenuFor(null)}
                                onPick={(choice) => {
                                  setMenuFor(null);
                                  setException({ app: row.app, choice });
                                }}
                              />
                            )}
                          </span>
                        )}
                      </span>
                    </div>
                  ))}
              </div>
            );
          })}
        </div>
      )}

      <div className="tt-r-foot">
        <span>
          <b>{groups.length}</b> systems · open one to see its applications
        </span>
        <span>
          Approvers see <b>one change</b> for the whole release
        </span>
        {counts.blocked > 0 && (
          <span>
            <b>{counts.blocked}</b> not eligible stay behind — a versioned tag makes them eligible
          </span>
        )}
      </div>

      {confirming && (
        <ConfirmPromote
          dep={dep}
          from={from}
          to={to}
          items={items}
          workloads={workloads}
          clusters={clusterNames}
          approval={approval}
          rollback={rollback}
          counts={counts}
          left={left}
          busy={busy}
          onCancel={() => setConfirming(false)}
          onConfirm={promote}
        />
      )}
      {exception && exceptionPlan && (
        <ReviewDialog
          plan={exceptionPlan}
          from={from}
          to={to}
          clusters={overview.clusters}
          onRemove={() => setException(null)}
          onClose={() => setException(null)}
          onDone={async () => {
            setException(null);
            await onChanged();
          }}
        />
      )}
    </section>
  );
}

function whyText(row, from, to, dep, tz) {
  switch (row.state) {
    case "eligible":
      return `Healthy in ${from?.name}${row.passedAt ? ` since ${new Date(row.passedAt).toLocaleString(undefined, { weekday: "short", hour: "2-digit", minute: "2-digit" })}` : ""}`;
    case "soaking":
      return `Soak ends in ${row.soakMinutesLeft} min · joins before the cut-off`;
    case "late":
      return row.soakMinutesLeft ? `Soak ends in ${row.soakMinutesLeft} min — after the cut-off · rides the next release` : `${row.detail} · rides the next release`;
    case "moved":
      return "Moved off this release by hand · rides the next one";
    case "sent":
      return row.detail;
    case "scheduled":
      return `Deploys at ${fmtTime(dep.departsAt, tz)} with the release`;
    default:
      return row.detail || "";
  }
}

function ConfirmPromote({ dep, from, to, items, workloads, clusters, approval, rollback, counts, left, busy, onCancel, onConfirm }) {
  const goRef = useRef(null);
  useEffect(() => {
    goRef.current?.focus();
    const onKey = (event) => event.key === "Escape" && !busy && onCancel();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy, onCancel]);
  return (
    <div className="tt-modal-root" role="presentation">
      <div className="tt-scrim" onClick={() => !busy && onCancel()} />
      <div className="tt-modal" role="dialog" aria-modal="true" aria-labelledby="tt-confirm-title">
        <header>
          <p className="tt-eyebrow">Confirm promotion</p>
          <h3 id="tt-confirm-title">Promote {dep.code}?</h3>
        </header>
        <dl>
          <dt>Applications</dt>
          <dd>
            {plural(items.length, "application")} · {plural(workloads, "workload")}
          </dd>
          <dt>Route</dt>
          <dd>
            {from?.name} → {to?.name}
            {clusters.length ? ` · ${clusters.join(", ")}` : ""}
          </dd>
          <dt>Change</dt>
          <dd>{approval.required ? "One change bundle for the whole release" : "Applied directly — no approval on this cluster"}</dd>
          <dt>Release</dt>
          <dd>
            <code>{dep.version || "assigned now"}</code>
          </dd>
          <dt>Approval</dt>
          <dd>
            {approval.required ? (
              <>
                <span className="tt-icon-wait">
                  <PrIcon.Clock />
                </span>
                <span>
                  {plural(approval.required, "approval")} required — it deploys once approved; you cannot approve it yourself
                </span>
              </>
            ) : (
              <>
                <PrIcon.Check />
                <span>Not required</span>
              </>
            )}
          </dd>
          <dt>Rollback</dt>
          <dd>
            <PrIcon.Check />
            <span>{rollback.automatic ? `Available · automatic if a rollout is not healthy within ${rollback.timeoutMinutes} min` : "Available · manual"}</span>
          </dd>
        </dl>
        <p className="tt-left-out">
          {left
            ? `Not included: ${[
                counts.soaking && `${counts.soaking} still soaking`,
                counts.late && `${counts.late} not eligible yet`,
                counts.blocked && `${counts.blocked} not eligible (mutable tag)`,
                counts.moved && `${counts.moved} moved to the next release`,
              ]
                .filter(Boolean)
                .join(", ")}.`
            : "Every application in the release is included."}
        </p>
        <footer>
          <button type="button" className="btn-outline" onClick={onCancel} disabled={busy}>
            Cancel
          </button>
          <button ref={goRef} type="button" className="primary" onClick={onConfirm} disabled={busy}>
            {busy ? "Promoting…" : `Promote ${plural(items.length, "app")} to ${to?.name}`}
          </button>
        </footer>
      </div>
    </div>
  );
}
