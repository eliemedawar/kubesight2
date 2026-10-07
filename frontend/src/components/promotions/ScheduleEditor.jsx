import { useEffect, useState } from "react";

import { savePromotionSchedule } from "../../api/promotionsApi.js";
import { PrIcon } from "./icons.jsx";

const DAYS = [
  ["mon", "Mon"],
  ["tue", "Tue"],
  ["wed", "Wed"],
  ["thu", "Thu"],
  ["fri", "Fri"],
  ["sat", "Sat"],
  ["sun", "Sun"],
];
const CUTOFFS = [0, 5, 10, 15, 30, 60, 120];
const ZONES = ["Asia/Beirut", "Asia/Dubai", "Europe/London", "Europe/Paris", "UTC", "America/New_York"];
const WEEKDAYS = ["mon", "tue", "wed", "thu", "fri"];

const PRESETS = [
  { label: "Every 2 h, weekdays", days: Object.fromEntries(WEEKDAYS.map((d) => [d, ["09:30", "11:30", "13:30", "15:30"]])) },
  { label: "Tue & Thu 14:00", days: { tue: ["14:00"], thu: ["14:00"] } },
  { label: "Weekdays 10:00", days: Object.fromEntries(WEEKDAYS.map((d) => [d, ["10:00"]])) },
  { label: "Fridays 10:00", days: { fri: ["10:00"] } },
];

const browserZone = () => {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
  } catch {
    return "UTC";
  }
};

function toDraft(schedule) {
  return {
    enabled: Boolean(schedule?.enabled),
    days: Object.fromEntries(DAYS.map(([d]) => [d, [...(schedule?.days?.[d] || [])]])),
    cutoffMinutes: schedule?.cutoffMinutes ?? 15,
    timezone: schedule?.timezone && schedule.timezone !== "UTC" ? schedule.timezone : schedule?.owner ? schedule.timezone : browserZone(),
  };
}

/**
 * One hop's release schedule: departure times per weekday, the cut-off and
 * the timezone. Off = on demand. Saving switches it on as you: scheduled
 * releases deploy as the person who saved the schedule.
 */
export default function ScheduleEditor({ env, prev, canManage, onSaved }) {
  const [draft, setDraft] = useState(() => toDraft(env.schedule));
  const [adding, setAdding] = useState({});
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [saved, setSaved] = useState(false);
  useEffect(() => setDraft(toDraft(env.schedule)), [env.schedule]);

  const total = DAYS.reduce((n, [d]) => n + draft.days[d].length, 0);
  const dirty = JSON.stringify(draft) !== JSON.stringify(toDraft(env.schedule));

  const addTime = (day) => {
    const value = (adding[day] || "").trim();
    if (!/^([01]\d|2[0-3]):[0-5]\d$/.test(value)) return;
    setDraft({ ...draft, days: { ...draft.days, [day]: [...new Set([...draft.days[day], value])].sort() } });
    setAdding({ ...adding, [day]: "" });
  };
  const removeTime = (day, value) => setDraft({ ...draft, days: { ...draft.days, [day]: draft.days[day].filter((t) => t !== value) } });

  const save = async () => {
    setBusy(true);
    setError("");
    try {
      await savePromotionSchedule(env.id, { ...draft, enabled: draft.enabled && total > 0 });
      setSaved(true);
      setTimeout(() => setSaved(false), 2500);
      await onSaved();
    } catch (err) {
      setError(err.message || "The schedule did not save.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <article className={`tt-sched-card${draft.enabled ? " is-on" : ""}`}>
      <header>
        <div>
          <h4>
            {prev.name} <PrIcon.Arrow /> {env.name}
          </h4>
          <p>
            {draft.enabled && total
              ? `${total} departure${total === 1 ? "" : "s"} a week · cut-off ${draft.cutoffMinutes} min before`
              : "On demand — someone promotes it from the board"}
            {env.schedule?.owner && draft.enabled ? ` · runs as ${env.schedule.owner}` : ""}
          </p>
        </div>
        <label className="tt-switch">
          <input
            type="checkbox"
            role="switch"
            checked={draft.enabled}
            disabled={!canManage || busy}
            onChange={(event) => setDraft({ ...draft, enabled: event.target.checked })}
          />
          <span>{draft.enabled ? "Scheduled" : "On demand"}</span>
        </label>
      </header>

      {draft.enabled && (
        <>
          <div className="tt-presets" role="group" aria-label="Start from">
            {PRESETS.map((p) => (
              <button
                key={p.label}
                type="button"
                className="btn-ghost tt-preset"
                disabled={!canManage}
                onClick={() => setDraft({ ...draft, days: Object.fromEntries(DAYS.map(([d]) => [d, [...(p.days[d] || [])]])) })}
              >
                {p.label}
              </button>
            ))}
          </div>
          <div className="tt-week">
            {DAYS.map(([day, label]) => (
              <div key={day} className="tt-day">
                <span className="tt-day-name">{label}</span>
                {draft.days[day].map((t) => (
                  <span key={t} className="tt-time">
                    {t}
                    {canManage && (
                      <button type="button" className="icon-button tt-time-x" onClick={() => removeTime(day, t)} aria-label={`Remove ${label} ${t}`}>
                        <PrIcon.X />
                      </button>
                    )}
                  </span>
                ))}
                {canManage && (
                  <form
                    className="tt-add-time"
                    onSubmit={(event) => {
                      event.preventDefault();
                      addTime(day);
                    }}
                  >
                    <input
                      type="time"
                      value={adding[day] || ""}
                      onChange={(event) => setAdding({ ...adding, [day]: event.target.value })}
                      aria-label={`Add a ${label} departure`}
                    />
                    <button type="submit" className="btn-ghost tt-add-btn" aria-label={`Add ${label} time`} disabled={!adding[day]}>
                      <PrIcon.Plus />
                    </button>
                  </form>
                )}
              </div>
            ))}
          </div>
          <div className="tt-sched-row">
            <label>
              <span>Cut-off</span>
              <select value={draft.cutoffMinutes} disabled={!canManage} onChange={(event) => setDraft({ ...draft, cutoffMinutes: Number(event.target.value) })}>
                {CUTOFFS.map((m) => (
                  <option key={m} value={m}>
                    {m === 0 ? "at departure" : `${m} min before`}
                  </option>
                ))}
              </select>
            </label>
            <label>
              <span>Timezone</span>
              <input list={`tt-zones-${env.id}`} value={draft.timezone} disabled={!canManage} onChange={(event) => setDraft({ ...draft, timezone: event.target.value })} />
              <datalist id={`tt-zones-${env.id}`}>
                {ZONES.map((z) => (
                  <option key={z} value={z} />
                ))}
              </datalist>
            </label>
          </div>
          <p className="tt-sched-note">
            At each cut-off, every application eligible in {prev.name} becomes one release into {env.name}, sent as one change bundle that deploys at
            the departure time — once the cluster&apos;s approval rule is met.
          </p>
        </>
      )}

      {error && (
        <p className="tt-sched-error" role="alert">
          {error}
        </p>
      )}
      {canManage && (dirty || saved) && (
        <div className="tt-sched-save">
          {saved ? <span className="tt-saved">Saved</span> : <span className="tt-unsaved">Unsaved changes</span>}
          {dirty && (
            <>
              <button type="button" className="btn-outline" disabled={busy} onClick={() => setDraft(toDraft(env.schedule))}>
                Discard
              </button>
              <button type="button" className="primary" disabled={busy} onClick={save}>
                {busy ? "Saving…" : "Save schedule"}
              </button>
            </>
          )}
        </div>
      )}
    </article>
  );
}
