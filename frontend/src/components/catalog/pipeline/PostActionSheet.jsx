import { useId } from "react";
import { ChipsInput, CommandEditor, DraftTextarea, EnvRows, Field, Segmented, SettingRow } from "./controls.jsx";
import { PlIcon } from "./icons.jsx";
import {
  DEFAULT_CLEANUP_TIMEOUT,
  POST_ACTION_TYPES,
  WEBHOOK_FORMATS,
  postActionSummary,
  postActionTitle,
  postActionType,
  whenOptions,
} from "./postActionModel.js";
import { SecretPicker } from "./StageSheet.jsx";

const CLEANUP_TIMEOUTS = [
  [60, "1 min"],
  [300, "5 min"],
  [600, "10 min"],
  [1800, "30 min"],
];

const FORMAT_NOTES = {
  slack: "Posts a message with the result, branch, commit, failed stage, tests and a link — to the channel the incoming webhook belongs to.",
  teams: "Posts an Adaptive Card (the format Teams workflow webhooks take) with the same facts and an “Open the build” button.",
  json: "POSTs one JSON document — event, service, build (number, status, branch, commit, url), failed stage, tests — for any receiver to use.",
};

/** Choosing what a new post action is, before it exists. */
export function PostActionPicker({ onPick, onCancel }) {
  return (
    <section className="pl-picker" aria-labelledby="pl-post-picker-title">
      <header className="pl-picker-head">
        <span className="pl-kicker">New post action · when the build ends</span>
        <h3 id="pl-post-picker-title">What should happen when a build ends?</h3>
        <p>
          Notifications go out once the whole build is over. Cleanup runs in the workspace as soon as
          the stages on the runner are done. Pick one — you choose when it fires next.
        </p>
      </header>
      <div className="pl-picker-grid">
        {POST_ACTION_TYPES.map((kind) => (
          <button
            key={kind.value}
            type="button"
            className={`btn-ghost pl-picker-card pl-post-pick is-${kind.value}`}
            onClick={() => onPick(kind.value)}
          >
            <span className="pl-picker-glyph" aria-hidden="true">
              <PlIcon name={kind.icon} />
            </span>
            <strong>{kind.verb}</strong>
            <small>{kind.description}</small>
            <span className="pl-picker-cta" aria-hidden="true">
              Add <PlIcon name="chevron" />
            </span>
          </button>
        ))}
      </div>
      {onCancel && (
        <div className="pl-picker-foot">
          <button type="button" className="btn-outline btn-compact" onClick={onCancel}>
            Cancel
          </button>
        </div>
      )}
    </section>
  );
}

/**
 * One post action: what it does, when it fires, and its fields.
 *
 * The "when" block carries the semantics in words, because the two kinds fire
 * at different moments and that is the thing people get wrong: notifications
 * at the true end of the build, cleanup at the end of the runner stages.
 */
export default function PostActionSheet({
  action,
  index,
  total,
  actions,
  secretKeys,
  problems,
  change,
  editable,
  onChange,
  onChangeType,
  onMove,
  onRemove,
  onGoToTab,
}) {
  const ids = useId();
  const kind = postActionType(action.type);
  const problemFor = (field) => problems.find((item) => item.field === field)?.message;
  const notify = action.type !== "commands";

  return (
    <article className={`pl-sheet pl-post-sheet is-${action.type}`} aria-labelledby={`${ids}-title`}>
      <header className="pl-sheet-head">
        <span className="pl-sheet-glyph" aria-hidden="true">
          <PlIcon name={kind?.icon || "alert"} />
        </span>
        <div className="pl-sheet-title">
          <span className="pl-kicker">
            Post action {index + 1} of {total}
            <span aria-hidden="true"> · </span>
            When the build ends
            {change && (
              <span className={`pl-flow-change is-${change}`}>{change === "new" ? "New — not saved yet" : "Edited"}</span>
            )}
          </span>
          {action.type === "commands" && editable ? (
            <input
              id={`${ids}-title`}
              className={`pl-sheet-name${problemFor("name") ? " is-invalid" : ""}`}
              value={action.name || ""}
              maxLength={80}
              placeholder="Name this cleanup"
              aria-label="Cleanup name"
              onChange={(event) => onChange({ name: event.target.value })}
            />
          ) : (
            <h3 id={`${ids}-title`} className="pl-sheet-name is-static">
              {postActionTitle(action)}
            </h3>
          )}
          <p className={`pl-sheet-summary${action.type === "commands" ? " is-mono" : ""}`}>{postActionSummary(action)}</p>
        </div>
        {editable && (
          <div className="pl-sheet-tools">
            <div className="pl-toolgroup" role="group" aria-label="Post action actions">
              <button
                type="button"
                className="btn-ghost pl-tool"
                onClick={() => onMove(-1)}
                disabled={index === 0}
                title="Move up"
                aria-label="Move post action up"
              >
                <PlIcon name="up" />
              </button>
              <button
                type="button"
                className="btn-ghost pl-tool"
                onClick={() => onMove(1)}
                disabled={index === total - 1}
                title="Move down"
                aria-label="Move post action down"
              >
                <PlIcon name="down" />
              </button>
              <button
                type="button"
                className="btn-ghost pl-tool is-danger"
                onClick={onRemove}
                title="Remove post action"
                aria-label="Remove post action"
              >
                <PlIcon name="trash" />
              </button>
            </div>
          </div>
        )}
      </header>

      {problems.length > 0 && (
        <div className="pl-problems" role="group" aria-label="Problems with this post action">
          {problems.map((problem, position) => (
            <div key={position} className="pl-problem is-error">
              <PlIcon name="alert" />
              <div>
                <p>{problem.message}</p>
              </div>
            </div>
          ))}
        </div>
      )}

      <section className="pl-block" aria-labelledby={`${ids}-what`}>
        <h4 id={`${ids}-what`} className="pl-block-title">
          What it does
        </h4>
        <Segmented
          label="Post action kind"
          size="lg"
          value={action.type}
          disabled={!editable}
          options={POST_ACTION_TYPES.map((item) => ({
            value: item.value,
            label: item.verb,
            hint: item.label,
            icon: item.icon,
          }))}
          onChange={onChangeType}
        />
      </section>

      <section className="pl-block" aria-labelledby={`${ids}-when`}>
        <h4 id={`${ids}-when`} className="pl-block-title">
          When it {notify ? "is sent" : "runs"}
        </h4>
        <Segmented
          label={notify ? "When it is sent" : "When it runs"}
          value={action.when || "always"}
          disabled={!editable}
          options={whenOptions(action.type)}
          onChange={(when) => onChange({ when })}
        />
        <div className="pl-note is-muted pl-post-semantics">
          <PlIcon name={notify ? "message" : "terminal"} />
          {notify ? (
            <p>
              Sent <strong>once, when the whole build is over</strong> — after any approval, deploy or
              app store stage — so “succeeded” includes the deploy. A <strong>timed-out</strong> build
              counts as failed. A <strong>cancelled</strong> build only sends “Always” notifications,
              and one cancelled before it started sends nothing. A failed delivery is retried a few
              times and never sent twice.
            </p>
          ) : (
            <p>
              Runs <strong>in the build workspace as soon as the last stage on the runner ends</strong>,
              before any approval, deploy or app store stage — the workspace is gone by then. The
              stages' outcome is in <code>$KUBESIGHT_STAGES_RESULT</code> (<code>success</code> or{" "}
              <code>failure</code>). It <strong>never changes the build's result</strong>: a failed
              cleanup is red on its own row, and the build keeps its colour. A cancelled build stops
              where it is, so its cleanup does not run.
            </p>
          )}
        </div>
      </section>

      {action.type === "email" && (
        <section className="pl-block" aria-labelledby={`${ids}-email`}>
          <h4 id={`${ids}-email`} className="pl-block-title">
            The email
          </h4>
          <Field
            label="Recipients"
            wide
            error={problemFor("recipients")}
            hint="Up to 25 addresses. Press Enter, a comma or a space after each one."
          >
            <ChipsInput
              value={action.recipients || []}
              lowercase={false}
              disabled={!editable}
              label="Recipients"
              placeholder="team@example.com"
              onChange={(recipients) => onChange({ recipients })}
            />
          </Field>
          <Field
            label="Subject"
            htmlFor={`${ids}-subject`}
            optional
            wide
            hint={
              <>
                Empty sends “[KubeSight] Payments #12 failed (main)”. <code>{"{service}"}</code>,{" "}
                <code>{"{build}"}</code>, <code>{"{result}"}</code> and <code>{"{branch}"}</code> are
                filled in.
              </>
            }
          >
            <input
              id={`${ids}-subject`}
              value={action.subject || ""}
              maxLength={200}
              disabled={!editable}
              placeholder="[KubeSight] {service} {build} {result}"
              onChange={(event) => onChange({ subject: event.target.value })}
            />
          </Field>
          <Field
            label="Message"
            htmlFor={`${ids}-message`}
            optional
            wide
            hint="Shown above the build's facts: result, branch, commit, trigger, duration, the failed stage and why, test counts and a link to the build."
          >
            <DraftTextarea
              id={`${ids}-message`}
              rows={3}
              maxLength={4000}
              value={action.message || ""}
              disabled={!editable}
              placeholder="The release train is blocked until this is green."
              onChangeText={(message) => onChange({ message })}
            />
          </Field>
          <p className="pl-field-hint">Sent through the SMTP relay configured in Settings.</p>
        </section>
      )}

      {action.type === "webhook" && (
        <section className="pl-block" aria-labelledby={`${ids}-hook`}>
          <h4 id={`${ids}-hook`} className="pl-block-title">
            The webhook
          </h4>
          <Field label="Format" wide hint={FORMAT_NOTES[action.format || "json"]}>
            <Segmented
              label="Webhook format"
              value={action.format || "json"}
              disabled={!editable}
              options={WEBHOOK_FORMATS}
              onChange={(format) => onChange({ format })}
            />
          </Field>
          <Field
            label="URL secret"
            htmlFor={`${ids}-secret`}
            wide
            error={problemFor("urlSecret")}
            hint="A webhook URL is a credential — anyone holding it can post — so it lives in a CI secret, never in the pipeline, the build or its log."
          >
            {secretKeys.length ? (
              <div className="pl-post-secret">
                <select
                  id={`${ids}-secret`}
                  className="is-mono"
                  value={action.urlSecret || ""}
                  disabled={!editable}
                  onChange={(event) => onChange({ urlSecret: event.target.value })}
                >
                  <option value="">Choose a secret…</option>
                  {action.urlSecret && !secretKeys.some((item) => item.key === action.urlSecret) && (
                    <option value={action.urlSecret}>{action.urlSecret} (deleted)</option>
                  )}
                  {secretKeys.map((item) => (
                    <option key={item.key} value={item.key}>
                      {item.key}
                      {item.scope === "global" ? " (global)" : ""}
                    </option>
                  ))}
                </select>
                <button type="button" className="btn-outline btn-compact" onClick={() => onGoToTab?.("settings")}>
                  <PlIcon name="lock" /> Manage secrets
                </button>
              </div>
            ) : (
              <div className="pl-empty-inline">
                <p>
                  No secrets yet. Add one in Settings whose value is the webhook URL (for Slack,
                  <code> https://hooks.slack.com/services/…</code>), then choose it here.
                </p>
                <button type="button" className="btn-outline btn-compact" onClick={() => onGoToTab?.("settings")}>
                  <PlIcon name="lock" /> Manage secrets
                </button>
              </div>
            )}
          </Field>
        </section>
      )}

      {action.type === "commands" && (
        <section className="pl-block" aria-labelledby={`${ids}-cmds`}>
          <h4 id={`${ids}-cmds`} className="pl-block-title">
            The cleanup
          </h4>
          <Field
            label="Commands"
            htmlFor={`${ids}-commands`}
            wide
            error={problemFor("commands")}
            hint={
              <>
                Runs in the checkout, like a command stage, with the variables earlier stages exported.
                Branch on <code>$KUBESIGHT_STAGES_RESULT</code> to do something only on failure.
              </>
            }
          >
            <CommandEditor
              id={`${ids}-commands`}
              lines={action.commands}
              disabled={!editable}
              invalid={Boolean(problemFor("commands"))}
              caption="sh · cleanup · stops at the first failing line"
              placeholder={'kubectl delete namespace "preview-$KUBESIGHT_BUILD_NUMBER" --ignore-not-found\nrm -rf build/tmp'}
              onChange={(commands) => onChange({ commands })}
            />
          </Field>
          <div className="pl-grid">
            <Field
              label="Container image"
              htmlFor={`${ids}-image`}
              optional
              hint="Empty uses the runner's default image (or the machine itself on an agent)."
            >
              <input
                id={`${ids}-image`}
                className="is-mono"
                value={action.image || ""}
                placeholder="alpine:3.20"
                disabled={!editable}
                spellCheck={false}
                onChange={(event) => onChange({ image: event.target.value })}
              />
            </Field>
            <Field
              label="Working directory"
              htmlFor={`${ids}-workdir`}
              optional
              hint="Relative to the repository."
            >
              <input
                id={`${ids}-workdir`}
                className="is-mono"
                value={action.workingDirectory || ""}
                placeholder="."
                disabled={!editable}
                spellCheck={false}
                onChange={(event) => onChange({ workingDirectory: event.target.value })}
              />
            </Field>
          </div>
          <CleanupTimeout ids={ids} action={action} editable={editable} onChange={onChange} error={problemFor("timeoutSeconds")} />
          <div className="pl-settings">
            <SettingRow
              icon="key"
              title="Secrets"
              hint="Credentials the cleanup needs, masked in its log"
              value={(action.secretRefs || []).length ? `${action.secretRefs.length} attached` : "None"}
              isSet={(action.secretRefs || []).length > 0}
              tone={problemFor("secretRefs") ? "error" : undefined}
              defaultOpen={Boolean(problemFor("secretRefs"))}
            >
              <SecretPicker
                stage={action}
                secretKeys={secretKeys}
                editable={editable}
                onChange={onChange}
                onGoToTab={onGoToTab}
              />
            </SettingRow>
            <SettingRow
              icon="variable"
              title="Variables"
              hint="Extra environment for the cleanup"
              value={Object.keys(action.env || {}).length ? `${Object.keys(action.env).length} set` : "None"}
              isSet={Object.keys(action.env || {}).length > 0}
            >
              <EnvRows value={action.env || {}} disabled={!editable} onChange={(env) => onChange({ env })} />
            </SettingRow>
          </div>
        </section>
      )}
    </article>
  );
}

function CleanupTimeout({ ids, action, editable, onChange, error }) {
  const seconds = Number(action.timeoutSeconds) || DEFAULT_CLEANUP_TIMEOUT;
  return (
    <Field
      label="Time limit"
      htmlFor={`${ids}-timeout`}
      error={error}
      hint="The cleanup is stopped after this. Between 30 seconds and 30 minutes."
    >
      <div className="pl-timeout">
        <div className="pl-timeout-input">
          <input
            id={`${ids}-timeout`}
            type="number"
            min={0.5}
            max={30}
            step={0.5}
            value={action.timeoutSeconds === "" ? "" : Math.round((seconds / 60) * 100) / 100}
            disabled={!editable}
            onChange={(event) =>
              onChange({
                timeoutSeconds: event.target.value === "" ? "" : Math.round(Number(event.target.value) * 60),
              })
            }
          />
          <span>minutes</span>
        </div>
        {editable && (
          <div className="pl-presets" role="group" aria-label="Common time limits">
            {CLEANUP_TIMEOUTS.map(([value, label]) => (
              <button
                key={value}
                type="button"
                className={`btn-ghost${seconds === value ? " is-on" : ""}`}
                aria-pressed={seconds === value}
                onClick={() => onChange({ timeoutSeconds: value })}
              >
                {label}
              </button>
            ))}
          </div>
        )}
      </div>
    </Field>
  );
}
