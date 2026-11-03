import { useState } from "react";
// The URL row is the build webhooks' one (.wh-kv), so it is styled the same.
import "../../../styles/signal/webhooks.css";
import { ChipsInput, Field, SettingRow, Switch } from "../pipeline/controls.jsx";
import { PlIcon } from "../pipeline/icons.jsx";
import { EVENTS } from "./mergeCheckModel.js";

/**
 * The address Bitbucket is given. Normally KubeSight's own; when Bitbucket
 * Cloud cannot resolve it (an internal name), a gateway such as Tyk that
 * forwards to it — the same editor as a build webhook's URL. Saved on its own,
 * not through the save bar, because Set up in Bitbucket uses the saved one.
 */
function CallUrl({ config, webhook, canEdit, onCopy, onSave }) {
  const own = config.webhookUrl || config.webhookPath;
  const callUrl = config.callUrl || own;
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);

  const save = async (value) => {
    setBusy(true);
    const ok = await onSave(value.trim());
    setBusy(false);
    if (ok) setEditing(false);
  };

  if (editing) {
    return (
      <div className="wh-kv wh-kv-edit">
        <span className="wh-k">URL</span>
        <div className="pl-field wh-v-edit">
          <input
            type="url"
            className="is-mono"
            value={draft}
            placeholder={own}
            aria-label="Gateway URL"
            autoFocus
            disabled={busy}
            onChange={(event) => setDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter") save(draft);
              if (event.key === "Escape") {
                event.stopPropagation();
                setEditing(false);
              }
            }}
          />
          <p className="pl-field-hint">
            The address Bitbucket should call when a gateway such as Tyk sits in front of KubeSight. The gateway
            must forward the request, body and headers unchanged, to <code>{own}</code>. Leave it empty to use
            KubeSight's own URL.
          </p>
        </div>
        <button type="button" className="btn-outline btn-compact" onClick={() => save(draft)} disabled={busy}>
          {busy ? "Saving…" : "Save"}
        </button>
        <button type="button" className="btn-ghost st-action" onClick={() => setEditing(false)} disabled={busy}>
          Cancel
        </button>
      </div>
    );
  }

  return (
    <>
      <div className="wh-kv">
        <span className="wh-k">URL</span>
        <code className="wh-v">{callUrl}</code>
        <button type="button" className="btn-ghost st-action" onClick={() => onCopy(callUrl, "URL")}>
          <PlIcon name="copy" /> Copy
        </button>
        {canEdit && onSave && (
          <button
            type="button"
            className="btn-ghost st-action"
            onClick={() => {
              setDraft(config.externalUrl || "");
              setEditing(true);
            }}
          >
            <PlIcon name="variable" /> Edit
          </button>
        )}
      </div>
      {config.externalUrl ? (
        <p className="pl-field-hint wh-kv-note">
          Through a gateway. It forwards to KubeSight at <code>{own}</code>.
        </p>
      ) : null}
      {webhook?.exists && webhook.urlMatches === false ? (
        <p className="pl-field-error" role="alert">
          <PlIcon name="alert" /> Bitbucket still calls <code>{webhook.registeredUrl}</code>. Press Set up in
          Bitbucket above to move it.
        </p>
      ) : null}
    </>
  );
}

/**
 * How Bitbucket and KubeSight talk: what triggers a check, what is reported
 * back, and — for anyone not using the setup button — the URL and secret.
 *
 * The hand-setup details sit behind one row, open on demand: the button in the
 * status card does all of it, and a wall of URL, secret and header names in
 * front of it read as homework.
 */
export default function ConnectionView({
  config,
  form,
  webhook,
  service,
  canEdit,
  // Whether the status card above shows the "Set up in Bitbucket" button —
  // the copy only points at it when it is there.
  setupOffered = false,
  secret,
  secretBusy = false,
  onReveal,
  onRotate,
  onCopy,
  onField,
  onToggleEvent,
  // (url) => Promise<boolean>; saves the gateway URL on its own.
  onSaveGatewayUrl,
}) {
  const url = config.callUrl || config.webhookUrl || config.webhookPath;
  const branches = form.targetBranches || [];
  const missingUpdated = !form.events.includes("pullrequest:updated");

  return (
    <div className="mc-view">
      <header className="pl-view-head">
        <div>
          <span className="pl-kicker">Bitbucket ↔ KubeSight</span>
          <h3>Connection</h3>
          <p>
            What makes a check run, and what KubeSight sends back.{" "}
            {webhook?.exists
              ? `The webhook in ${webhook.repository} delivers here.`
              : setupOffered
                ? "Use Set up in Bitbucket above, or wire it by hand below."
                : !config.sourceReady
                  ? "Connect a repository on the Source tab first; then it can be set up from here, or wired by hand below."
                  : "Wire it by hand below."}
          </p>
        </div>
      </header>

      <section className="mc-card">
        <h4 className="pl-block-title">Run the checks when</h4>
        <div className="mc-events">
          {EVENTS.map((event) => {
            const on = form.events.includes(event.value);
            return (
              <label key={event.value} className={`mc-event${on ? " is-on" : ""}`}>
                <input
                  type="checkbox"
                  checked={on}
                  disabled={!canEdit}
                  onChange={() => onToggleEvent(event.value)}
                />
                <span>
                  <strong>{event.label}</strong>
                  {event.hint && <small>{event.hint}</small>}
                </span>
              </label>
            );
          })}
        </div>
        {missingUpdated && (
          <p className="pl-field-error" role="alert">
            <PlIcon name="alert" /> Without “new commits are pushed” a pull request is judged by its
            first commit only, and fixing it never clears the gate.
          </p>
        )}

        <Field
          label="Only for merges into"
          hint={
            branches.length
              ? "Patterns like release/* work. Pull requests into any other branch are ignored."
              : `Empty checks every branch. The setup button protects ${service.defaultBranch || "the default branch"} when this is empty.`
          }
        >
          <ChipsInput
            label="Target branches"
            lowercase={false}
            value={branches}
            placeholder="main, release/*"
            disabled={!canEdit}
            onChange={(next) => onField("targetBranches", next)}
          />
        </Field>
      </section>

      <section className="mc-card">
        <h4 className="pl-block-title">Bitbucket calls</h4>
        <CallUrl config={config} webhook={webhook} canEdit={canEdit} onCopy={onCopy} onSave={onSaveGatewayUrl} />
      </section>

      <section className="mc-card">
        <h4 className="pl-block-title">Report back to Bitbucket</h4>
        <div className="pl-grid">
          <Field
            label="Build status key"
            htmlFor="mc-status-key"
            hint="The name the verdict is filed under, and the one a branch restriction requires. Re-running replaces the status with this key."
          >
            <input
              id="mc-status-key"
              className="is-mono"
              value={form.statusKey}
              disabled={!canEdit}
              onChange={(event) => onField("statusKey", event.target.value)}
            />
          </Field>
          <Field label="Pull request comment" hint="One comment per verdict, naming which check blocked it and why.">
            <Switch
              checked={form.postComment}
              disabled={!canEdit}
              label={form.postComment ? "Explain the verdict in a comment" : "No comment, status only"}
              onChange={(value) => onField("postComment", value)}
            />
          </Field>
        </div>
      </section>

      <div className="pl-settings">
        <SettingRow
          icon="link"
          title="Set it up by hand"
          hint="The webhook URL, its secret, and the branch restriction"
          value={config.secretConfigured ? "Secret set" : "No secret yet"}
          isSet={config.secretConfigured}
        >
          <Field
            label="Webhook URL"
            hint={
              config.webhookUrl
                ? "Repository settings → Webhooks → Add webhook. Pull request events only."
                : "Only the path is known: set PUBLIC_BASE_URL so the full address shows here and on every build status."
            }
          >
            <div className="mc-copyrow">
              <input readOnly className="is-mono" value={url} onFocus={(event) => event.target.select()} />
              <button type="button" className="btn-outline btn-compact" onClick={() => onCopy(url, "URL")}>
                <PlIcon name="copy" /> Copy
              </button>
            </div>
          </Field>
          <Field
            label="Secret"
            hint="Paste it into the webhook's Secret field — Bitbucket then signs each delivery (X-Hub-Signature) and the secret never travels. Revealing it is audited."
          >
            <div className="mc-copyrow">
              <input
                readOnly
                className="is-mono"
                type={secret ? "text" : "password"}
                value={secret || "••••••••••••••••••••••••"}
                onFocus={(event) => event.target.select()}
              />
              {canEdit && !secret && (
                <button type="button" className="btn-outline btn-compact" onClick={onReveal} disabled={secretBusy}>
                  <PlIcon name="eye" /> Reveal
                </button>
              )}
              {secret && (
                <button type="button" className="btn-outline btn-compact" onClick={() => onCopy(secret, "Secret")}>
                  <PlIcon name="copy" /> Copy
                </button>
              )}
              {canEdit && (
                <button
                  type="button"
                  className="btn-outline btn-compact danger"
                  onClick={onRotate}
                  disabled={secretBusy}
                  aria-busy={secretBusy || undefined}
                >
                  <PlIcon name="refresh" /> {secretBusy ? "Working…" : "Rotate"}
                </button>
              )}
            </div>
          </Field>
          <ol className="mc-manual">
            <li>
              <strong>Webhook:</strong> the URL above, the secret above, and the pull request events
              ticked in “Run the checks when”.
            </li>
            <li>
              <strong>Branch restriction:</strong> Repository settings → Branch restrictions → on{" "}
              {branches.length ? branches.join(", ") : service.defaultBranch || "your protected branches"}, enable{" "}
              <em>Require successful builds before merging</em> and require the <code>{form.statusKey}</code> status.
            </li>
            <li>
              <strong>Credential:</strong> the one this service uses needs write access — a verdict is a
              build status written to the commit.
            </li>
          </ol>
          <p className="pl-field-hint">
            A sender that cannot sign can put the secret in an <code>X-KubeSight-Secret</code> header, or
            append <code>?secret=…</code> to the URL as a last resort.
          </p>
        </SettingRow>
      </div>
    </div>
  );
}
