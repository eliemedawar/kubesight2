import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";
import "../../../styles/signal/schedules.css";
import {
  createCiSchedule,
  deleteCiSchedule,
  listCiSchedules,
  previewCiSchedule,
  runCiScheduleNow,
  updateCiSchedule,
} from "../../../api/ciApi.js";
import { Field, Segmented, Switch } from "../pipeline/controls.jsx";
import { PlIcon } from "../pipeline/icons.jsx";
import {
  PRESETS,
  blankSchedule,
  browserTimeZone,
  conditionSentence,
  conditionsFor,
  effectiveValues,
  formProblems,
  formatRun,
  formatUntil,
  outcomeOf,
  pipelineFor,
  presetFor,
  timeZoneOptions,
  toForm,
  toPayload,
} from "./scheduleModel.js";

// Last-run status moves on its own (a build finishes, a 02:00 run fires), so
// the list re-reads itself while it is on screen. Slow on purpose: nothing
// here is urgent, and the build drawer is where a running build is watched.
const REFRESH_MS = 30000;

/**
 * Schedules: builds that start on their own, at a cron time in a timezone.
 *
 * Records, not settings — each one saves when its own Save is pressed, like a
 * secret, and never waits for the page's save bar. The form shows what the
 * SERVER makes of the expression (its words, its next runs) rather than
 * evaluating cron itself, so the promise on screen is the one the engine keeps.
 */
export default function SchedulesSection({ service, canEdit, canRun, onError, onNotice, onSummary, onOpenBuild }) {
  const [data, setData] = useState(null);
  const [editing, setEditing] = useState(null);
  const [busy, setBusy] = useState(false);
  const [running, setRunning] = useState(null);
  const summaryRef = useRef(onSummary);
  summaryRef.current = onSummary;

  const load = useCallback(
    () =>
      listCiSchedules(service.id)
        .then((next) => {
          setData(next);
          summaryRef.current?.(next.items || []);
        })
        .catch((err) => {
          setData((prev) => prev || { items: [], pipelines: [], limits: {} });
          onError(err.message || "Could not load schedules.");
        }),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [service.id]
  );

  useEffect(() => {
    load();
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") load();
    }, REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [load]);

  const items = data?.items || [];
  const pipelines = data?.pipelines || [];
  const limits = data?.limits || {};
  const atLimit = limits.maxSchedules ? items.length >= limits.maxSchedules : false;

  const save = async (form, parameters) => {
    setBusy(true);
    try {
      const payload = toPayload(form, parameters);
      const saved = form.id
        ? await updateCiSchedule(service.id, form.id, payload)
        : await createCiSchedule(service.id, payload);
      setEditing(null);
      onNotice(
        saved.enabled && saved.nextRunAt
          ? `Schedule “${saved.name}” saved. Next build ${formatRun(saved.nextRunAt, saved.timezone)} (${saved.timezone}).`
          : `Schedule “${saved.name}” saved, switched off.`
      );
      await load();
      return null;
    } catch (err) {
      return err.message || "Could not save the schedule.";
    } finally {
      setBusy(false);
    }
  };

  const toggle = async (schedule, enabled) => {
    try {
      await updateCiSchedule(service.id, schedule.id, { enabled });
      onNotice(enabled ? `“${schedule.name}” is on again — its next run is counted from now.` : `“${schedule.name}” is off. Nothing runs until it is switched back on.`);
      load();
    } catch (err) {
      onError(err.message || "Could not change the schedule.");
    }
  };

  const remove = async (schedule) => {
    if (!window.confirm(`Delete the schedule “${schedule.name}”? Builds it already started are kept.`)) return;
    try {
      await deleteCiSchedule(service.id, schedule.id);
      onNotice(`Schedule “${schedule.name}” deleted.`);
      if (editing?.id === schedule.id) setEditing(null);
      load();
    } catch (err) {
      onError(err.message || "Could not delete the schedule.");
    }
  };

  const runNow = async (schedule) => {
    setRunning(schedule.id);
    try {
      const build = await runCiScheduleNow(service.id, schedule.id);
      onNotice(`Build #${build.number} queued from “${schedule.name}”. Tonight's run is unchanged.`);
      load();
    } catch (err) {
      onError(err.message || "Could not start the build.");
    } finally {
      setRunning(null);
    }
  };

  if (data === null) return <p className="pl-field-hint">Loading schedules…</p>;

  const startNew = () =>
    setEditing(blankSchedule({ name: items.some((item) => item.name.toLowerCase() === "nightly build") ? "" : "Nightly build" }));

  return (
    <div className="sc-root">
      {editing && !editing.id && (
        <ScheduleEditor
          initial={editing}
          pipelines={pipelines}
          defaultBranch={data.defaultBranch || service.defaultBranch}
          busy={busy}
          onCancel={() => setEditing(null)}
          onSave={save}
        />
      )}

      {items.length === 0 && !editing ? (
        <div className="pl-empty sc-empty">
          <span className="pl-empty-glyph" aria-hidden="true">
            <PlIcon name="clock" />
          </span>
          <strong>No schedules</strong>
          <p>
            This service builds when someone presses Run build, or when a merge check or the deploy
            automation asks. A schedule starts one on its own — a nightly build of{" "}
            <code>{service.defaultBranch || "main"}</code>, a weekly release-branch build — at the time you pick,
            in the timezone you pick.
          </p>
          {canEdit ? (
            <button type="button" className="primary btn-compact" onClick={startNew}>
              <PlIcon name="plus" /> Add a nightly build
            </button>
          ) : (
            <p className="sc-readonly">Adding one needs permission to edit pipelines and run builds.</p>
          )}
        </div>
      ) : (
        items.length > 0 && (
          <ul className="sc-list">
            {items.map((schedule) =>
              editing?.id === schedule.id ? (
                <li key={schedule.id} className="sc-item is-editing">
                  <ScheduleEditor
                    initial={editing}
                    pipelines={pipelines}
                    defaultBranch={data.defaultBranch || service.defaultBranch}
                    busy={busy}
                    onCancel={() => setEditing(null)}
                    onSave={save}
                  />
                </li>
              ) : (
                <ScheduleRow
                  key={schedule.id}
                  schedule={schedule}
                  defaultBranch={data.defaultBranch || service.defaultBranch}
                  canEdit={canEdit}
                  canRun={canRun}
                  running={running === schedule.id}
                  onToggle={(enabled) => toggle(schedule, enabled)}
                  onEdit={() => setEditing(toForm(schedule))}
                  onDelete={() => remove(schedule)}
                  onRun={() => runNow(schedule)}
                  onOpenBuild={onOpenBuild}
                />
              )
            )}
          </ul>
        )
      )}

      {canEdit && items.length > 0 && !editing && (
        <button
          type="button"
          className="btn-ghost pl-rows-add"
          onClick={startNew}
          disabled={atLimit}
          title={atLimit ? `A service may have at most ${limits.maxSchedules} schedules.` : undefined}
        >
          <PlIcon name="plus" /> Add a schedule
        </button>
      )}

      <ScanRecipe pipelines={pipelines} />
    </div>
  );
}

function ScheduleRow({ schedule, defaultBranch, canEdit, canRun, running, onToggle, onEdit, onDelete, onRun, onOpenBuild }) {
  const outcome = outcomeOf(schedule);
  const problem = schedule.pipelineProblem || schedule.cronProblem;
  const ref =
    schedule.refType === "tag" ? (
      <>
        tag <code>{schedule.branch}</code>
      </>
    ) : schedule.branch ? (
      <code>{schedule.branch}</code>
    ) : (
      <>
        <code>{defaultBranch || "main"}</code> (default branch)
      </>
    );
  const inputs = Object.entries(schedule.variables || {});
  return (
    <li className={`sc-item${schedule.enabled ? "" : " is-off"}${problem ? " has-problem" : ""}`}>
      <span className="sc-icon" aria-hidden="true">
        <PlIcon name="clock" />
      </span>
      <div className="sc-copy">
        <strong className="sc-name">
          {schedule.name}
          {!schedule.enabled && <span className="pl-tag">Off</span>}
        </strong>
        <p className="sc-when">
          <span>{schedule.description || schedule.cron}</span>
          <span className="sc-zone">{schedule.timezone}</span>
          <code className="sc-cron">{schedule.cron}</code>
        </p>
        <p className="sc-meta">
          {schedule.enabled && schedule.nextRunAt ? (
            <span>
              Next <b>{formatRun(schedule.nextRunAt, schedule.timezone)}</b> · {formatUntil(schedule.nextRunAt)}
            </span>
          ) : (
            <span>No next run while it is off</span>
          )}
          <span>Builds {ref}</span>
          <span>{schedule.pipelineName ? `Pipeline ${schedule.pipelineName}` : "Default build pipeline"}</span>
          {schedule.runsAs && <span>Runs as {schedule.runsAs}</span>}
        </p>
        {inputs.length > 0 && (
          <p className="sc-vars">
            {inputs.map(([name, value]) => (
              <code key={name}>
                {name}={value.length > 24 ? `${value.slice(0, 24)}…` : value}
              </code>
            ))}
          </p>
        )}
        <div className={`sc-last is-${outcome.tone}`}>
          <span className="sc-dot" aria-hidden="true" />
          {schedule.lastBuild && schedule.lastOutcome === "triggered" ? (
            <button type="button" className="btn-ghost sc-buildlink" onClick={() => onOpenBuild?.(schedule.lastBuild.id)}>
              {outcome.label}
            </button>
          ) : (
            <span className="sc-last-label">{outcome.label}</span>
          )}
          {schedule.lastRunAt && <span className="sc-last-when">{formatRun(schedule.lastRunAt, schedule.timezone)}</span>}
          {outcome.detail && <span className="sc-last-detail">{outcome.detail}</span>}
        </div>
        {problem && (
          <p className="sc-problem" role="alert">
            <PlIcon name="alert" /> {problem}
          </p>
        )}
      </div>
      <div className="sc-actions">
        {canEdit && (
          <Switch checked={schedule.enabled} onChange={onToggle} label={schedule.enabled ? "On" : "Off"} />
        )}
        {canRun && (
          <button type="button" className="btn-ghost st-action" onClick={onRun} disabled={running}>
            <PlIcon name="forward" /> {running ? "Starting…" : "Run now"}
          </button>
        )}
        {canEdit && (
          <>
            <button type="button" className="btn-ghost st-action" onClick={onEdit}>
              <PlIcon name="variable" /> Edit
            </button>
            <button
              type="button"
              className="btn-ghost pl-tool is-danger"
              aria-label={`Delete ${schedule.name}`}
              title="Delete"
              onClick={onDelete}
            >
              <PlIcon name="trash" />
            </button>
          </>
        )}
      </div>
    </li>
  );
}

function usePreview(cron, timezone) {
  const [preview, setPreview] = useState({ loading: true });
  useEffect(() => {
    let cancelled = false;
    setPreview((prev) => ({ ...prev, loading: true }));
    const timer = window.setTimeout(() => {
      previewCiSchedule({ cron, timezone, count: 3 })
        .then((result) => !cancelled && setPreview({ loading: false, ...result }))
        .catch((err) => !cancelled && setPreview({ loading: false, valid: false, error: err.message || "Could not check the expression." }));
    }, 300);
    return () => {
      cancelled = true;
      window.clearTimeout(timer);
    };
  }, [cron, timezone]);
  return preview;
}

function ScheduleEditor({ initial, pipelines, defaultBranch, busy, onCancel, onSave }) {
  const [form, setForm] = useState(initial);
  const [serverError, setServerError] = useState("");
  const [touched, setTouched] = useState(false);
  const cronRef = useRef(null);
  const ids = useId();
  const mine = browserTimeZone();
  const zones = useMemo(() => timeZoneOptions(form.timezone, initial.timezone, mine), [form.timezone, initial.timezone, mine]);
  const preview = usePreview(form.cron, form.timezone);
  const pipeline = pipelineFor(pipelines, form.pipelineId);
  const parameters = pipeline?.parameters || [];
  const values = effectiveValues(parameters, form.variables);
  const problems = formProblems(form);
  const preset = presetFor(form.cron);

  const set = (key, value) => setForm((prev) => ({ ...prev, [key]: value }));
  const setValue = (name, value) => setForm((prev) => ({ ...prev, variables: { ...prev.variables, [name]: value } }));

  const submit = async () => {
    setTouched(true);
    if (Object.keys(problems).length || busy) return;
    setServerError("");
    // Every declared input travels with the value shown, so the schedule
    // builds with exactly what the form said — not with a default that is
    // changed on the Pipeline tab next week.
    const error = await onSave({ ...form, variables: values }, parameters);
    if (error) setServerError(error);
  };

  return (
    <div className="st-add sc-editor" role="group" aria-label={form.id ? `Edit ${initial.name}` : "New schedule"}>
      <div className="pl-grid">
        <Field label="Name" htmlFor={`${ids}-name`} error={touched ? problems.name : ""}>
          <input
            id={`${ids}-name`}
            value={form.name}
            maxLength={120}
            placeholder="Nightly build"
            onChange={(event) => set("name", event.target.value)}
            autoFocus={!form.id}
          />
        </Field>
        <Field
          label="Timezone"
          htmlFor={`${ids}-tz`}
          hint={
            form.timezone === mine
              ? "Your browser's timezone. Times below are wall-clock times there, through DST changes."
              : "Times are wall-clock times in this zone, through its DST changes."
          }
        >
          <div className="sc-tz">
            <input
              id={`${ids}-tz`}
              className="is-mono"
              list={`${ids}-zones`}
              value={form.timezone}
              spellCheck={false}
              autoComplete="off"
              placeholder="Asia/Beirut"
              onChange={(event) => set("timezone", event.target.value)}
            />
            <datalist id={`${ids}-zones`}>
              {zones.map((zone) => (
                <option key={zone} value={zone} />
              ))}
            </datalist>
            {form.timezone !== mine && (
              <button type="button" className="btn-ghost st-action" onClick={() => set("timezone", mine)}>
                Use mine · {mine}
              </button>
            )}
          </div>
        </Field>
      </div>

      <Field label="When" wide error={touched ? problems.cron : ""}>
        <Segmented
          label="Schedule preset"
          value={preset}
          options={PRESETS}
          onChange={(value) => {
            if (value === "custom") {
              cronRef.current?.focus();
              cronRef.current?.select();
            } else {
              set("cron", value);
            }
          }}
        />
        <div className="sc-cron-row">
          <input
            ref={cronRef}
            className="is-mono"
            value={form.cron}
            spellCheck={false}
            autoComplete="off"
            aria-label="Cron expression"
            aria-describedby={`${ids}-cron-help`}
            placeholder="0 2 * * *"
            onChange={(event) => set("cron", event.target.value)}
          />
          <small id={`${ids}-cron-help`}>
            minute · hour · day of month · month · day of week — or @nightly, @hourly, @weekly
          </small>
        </div>
        <div
          className={`sc-preview${preview.loading ? " is-loading" : ""}${preview.valid === false ? " is-error" : ""}`}
          aria-live="polite"
        >
          {preview.valid === false ? (
            <p className="sc-preview-error">
              <PlIcon name="alert" /> {preview.error}
            </p>
          ) : preview.valid ? (
            <>
              <p className="sc-preview-words">
                <PlIcon name="clock" />
                <strong>{preview.description}</strong>
                <span>{preview.timezone}</span>
              </p>
              <ol className="sc-preview-runs">
                {(preview.nextRuns || []).map((run) => (
                  <li key={run}>
                    <span>{formatRun(run, preview.timezone)}</span>
                    {preview.timezone !== mine && <small>{formatRun(run, mine)} your time</small>}
                  </li>
                ))}
              </ol>
            </>
          ) : (
            <p className="sc-preview-words is-muted">Reading the expression…</p>
          )}
        </div>
      </Field>

      <div className="pl-grid">
        {pipelines.length > 1 && (
          <Field label="Pipeline" htmlFor={`${ids}-pipeline`} hint="Default follows whichever build pipeline is the service's default when it runs.">
            <select id={`${ids}-pipeline`} value={form.pipelineId} onChange={(event) => set("pipelineId", event.target.value)}>
              <option value="">Default build pipeline ({pipelineFor(pipelines, "")?.name || "default"})</option>
              {pipelines
                .filter((item) => item.id)
                .map((item) => (
                  <option key={item.id} value={String(item.id)}>
                    {item.name}
                    {item.enabled ? "" : " (disabled)"}
                  </option>
                ))}
            </select>
          </Field>
        )}
        <Field
          label="What to build"
          htmlFor={`${ids}-ref`}
          error={touched ? problems.branch : ""}
          hint={
            form.refType === "tag"
              ? "Builds exactly this tag every time — useful for a nightly re-scan of a release."
              : form.branch
                ? `Builds the head of ${form.branch} as it is when the schedule fires.`
                : `Empty builds the default branch, ${defaultBranch || "main"}, whatever it is at the time.`
          }
        >
          <div className="sc-ref">
            <Segmented
              label="Ref kind"
              value={form.refType}
              options={[
                { value: "branch", label: "Branch" },
                { value: "tag", label: "Tag" },
              ]}
              onChange={(value) => set("refType", value)}
            />
            <input
              id={`${ids}-ref`}
              className="is-mono"
              value={form.branch}
              maxLength={255}
              spellCheck={false}
              placeholder={form.refType === "tag" ? "v1.4.0" : `${defaultBranch || "main"} (default)`}
              onChange={(event) => set("branch", event.target.value)}
            />
          </div>
        </Field>
      </div>

      {parameters.length > 0 && (
        <div className="sc-inputs" role="group" aria-labelledby={`${ids}-inputs`}>
          <h5 className="pl-block-title" id={`${ids}-inputs`}>
            Build inputs for these builds
          </h5>
          <p className="sc-inputs-note">
            What the pipeline would ask in Run build, answered once for every build this schedule starts.
          </p>
          {parameters.map((param) => (
            <InputControl
              key={param.name}
              id={`${ids}-in-${param.name}`}
              param={param}
              value={values[param.name]}
              conditions={conditionsFor(pipeline, param.name)}
              onChange={(next) => setValue(param.name, next)}
            />
          ))}
        </div>
      )}

      <div className="sc-switches">
        <Switch checked={form.enabled} onChange={(value) => set("enabled", value)} label="On — runs at the times above" />
        <Switch
          checked={form.skipIfRunning}
          onChange={(value) => set("skipIfRunning", value)}
          label="Skip a run while this schedule's previous build is still queued or running"
        />
      </div>

      {serverError && (
        <p className="pl-field-error" role="alert">
          <PlIcon name="alert" /> {serverError}
        </p>
      )}
      <div className="st-add-actions">
        <button type="button" className="btn-outline btn-compact" onClick={onCancel} disabled={busy}>
          Cancel
        </button>
        <button type="button" className="primary btn-compact" onClick={submit} disabled={busy || preview.valid === false}>
          <PlIcon name="check" /> {busy ? "Saving…" : form.id ? "Save schedule" : "Create schedule"}
        </button>
      </div>
    </div>
  );
}

export function InputControl({ id, param, value, conditions, onChange }) {
  // The variable name is shown beside a friendly label, but not twice when
  // the label IS the name.
  const label = param.label || param.name;
  const named = label !== param.name ? <code>{param.name}</code> : null;
  const notes = [param.description, ...conditions.map(conditionSentence)].filter(Boolean);
  if (param.type === "boolean") {
    return (
      <div className="sc-input is-bool">
        <Switch
          checked={value === "true"}
          onChange={(checked) => onChange(checked ? "true" : "false")}
          label={
            <>
              {label} {named}
            </>
          }
        />
        {notes.length > 0 && <small>{notes.join(" · ")}</small>}
      </div>
    );
  }
  const choices = param.choices || [];
  return (
    <div className="sc-input">
      <label htmlFor={id}>
        {label} {named}
        {param.required && <span className="sc-required">required</span>}
      </label>
      {param.type === "choice" && choices.length ? (
        <select id={id} value={value} onChange={(event) => onChange(event.target.value)}>
          {!param.required && !choices.includes("") && <option value="">(none)</option>}
          {choices.map((choice) => (
            <option key={choice} value={choice}>
              {choice}
            </option>
          ))}
        </select>
      ) : param.type === "multiline" ? (
        <textarea id={id} className="is-mono" rows={5} spellCheck={false} value={value} onChange={(event) => onChange(event.target.value)} />
      ) : (
        <input
          id={id}
          className={param.type === "dynamic_choice" ? "is-mono" : ""}
          value={value}
          maxLength={4000}
          placeholder={param.type === "dynamic_choice" ? "A branch or tag name" : ""}
          onChange={(event) => onChange(event.target.value)}
        />
      )}
      {notes.length > 0 && <small>{notes.join(" · ")}</small>}
    </div>
  );
}

/**
 * How to make a nightly dependency scan out of what already exists: a
 * yes/no input, a run condition on the scan stage, and a schedule that says
 * yes. Run conditions compare one build input to one value (equals / not
 * equals); a boolean input reaches stages as the string "true" or "false".
 */
function ScanRecipe({ pipelines }) {
  const wired = pipelines
    .flatMap((pipeline) =>
      (pipeline.conditions || [])
        .filter((condition) =>
          (pipeline.parameters || []).some((param) => param.name === condition.variable && param.type === "boolean")
        )
        .map((condition) => ({ ...condition, pipeline: pipeline.name }))
    )
    .slice(0, 3);
  return (
    <aside className="sc-recipe" aria-label="Nightly dependency scan">
      <PlIcon name="shield" />
      <div>
        <strong>Nightly dependency scan</strong>
        {wired.length > 0 ? (
          <p>
            Already wired:{" "}
            {wired.map((condition, index) => (
              <span key={`${condition.pipeline}-${condition.stage}`}>
                {index ? "; " : ""}
                {conditionSentence(condition)}
              </span>
            ))}
            . Switch that input on in a schedule and only the scheduled builds run it.
          </p>
        ) : (
          <ol>
            <li>
              On the Pipeline tab, add a <b>Yes / no</b> build input named <code>NIGHTLY_SCAN</code>, off by default.
            </li>
            <li>
              Give the Dependency-Check stage the run condition <code>NIGHTLY_SCAN</code> <b>equals</b>{" "}
              <code>true</code>.
            </li>
            <li>
              Add a schedule here with <code>NIGHTLY_SCAN</code> switched on.
            </li>
          </ol>
        )}
        <p className="sc-recipe-note">
          Builds from Run build keep the input off, so the scan stage is closed as skipped with the reason in its
          log and costs nothing; the scheduled build runs it.
        </p>
      </div>
    </aside>
  );
}
