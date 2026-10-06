import { useCallback, useEffect, useId, useRef, useState } from "react";
import "../../../styles/signal/schedules.css";
import "../../../styles/signal/webhooks.css";
import {
  createCiWebhook,
  deleteCiWebhook,
  getCiWebhookSourceStatus,
  listCiWebhookDeliveries,
  listCiWebhooks,
  previewCiWebhook,
  revealCiWebhookSecret,
  rotateCiWebhookSecret,
  setupCiWebhookInSource,
  testCiWebhook,
  updateCiWebhook,
} from "../../../api/ciApi.js";
import { ChipsInput, Field, Segmented, Switch } from "../pipeline/controls.jsx";
import { PlIcon } from "../pipeline/icons.jsx";
import { InputControl } from "./SchedulesSection.jsx";
import { conditionsFor, effectiveValues, pipelineFor } from "./scheduleModel.js";
import {
  KINDS,
  blankWebhook,
  buildsWhat,
  curlExample,
  deliveryLabel,
  deliveryTone,
  formProblems,
  formatAgo,
  outcomeOf,
  rejectedRecently,
  repositoryName,
  requestChoosesRef,
  sampleBody,
  samplePush,
  targetLabel,
  targetOptions,
  toForm,
  toPayload,
} from "./webhookModel.js";

// The last delivery moves on its own (somebody pushes, a release tool calls),
// so the list re-reads itself while it is on screen.
const REFRESH_MS = 20000;

const copyText = async (text) => {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
};

/**
 * Webhooks: builds that start when something calls a URL — Bitbucket on a
 * push, or any tool that can POST.
 *
 * Records, not settings, like schedules: each saves on its own Save. What a
 * body would build is asked of the server (Preview) rather than worked out
 * here, so the promise on screen is the one a delivery keeps.
 */
export default function WebhooksSection({ service, canEdit, canRun, onError, onNotice, onSummary, onOpenBuild }) {
  const [data, setData] = useState(null);
  const [editing, setEditing] = useState(null);
  const [busy, setBusy] = useState(false);
  const [openId, setOpenId] = useState(null);
  // The secret of a webhook created a moment ago, shown once without a reveal.
  const [fresh, setFresh] = useState(null);
  const summaryRef = useRef(onSummary);
  summaryRef.current = onSummary;

  const load = useCallback(
    () =>
      listCiWebhooks(service.id)
        .then((next) => {
          setData(next);
          summaryRef.current?.(next.items || []);
        })
        .catch((err) => {
          setData((prev) => prev || { items: [], pipelines: [], limits: {} });
          onError(err.message || "Could not load webhooks.");
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
  const defaultBranch = data?.defaultBranch || service.defaultBranch || "main";
  const repository = repositoryName(service.repositoryUrl);
  const isBitbucket = (data?.repositoryProvider || service.repositoryProvider || "bitbucket") === "bitbucket";
  const canPush = Boolean(data?.repositoryConnected ?? service.repositoryUrl) && isBitbucket;
  const atLimit = limits.maxWebhooks ? items.length >= limits.maxWebhooks : false;

  const save = async (form, parameters) => {
    setBusy(true);
    try {
      const payload = toPayload(form, parameters);
      const saved = form.id
        ? await updateCiWebhook(service.id, form.id, payload)
        : await createCiWebhook(service.id, payload);
      setEditing(null);
      if (!form.id) {
        setOpenId(saved.id);
        setFresh({ id: saved.id, secret: saved.secret });
        onNotice(
          saved.kind === "bitbucket_push"
            ? `Webhook “${saved.name}” created. Press Set up in Bitbucket to register it on the repository.`
            : `Webhook “${saved.name}” created. Copy its URL and secret into the tool that will call it.`
        );
      } else {
        onNotice(`Webhook “${saved.name}” saved.`);
      }
      await load();
      return null;
    } catch (err) {
      return err.message || "Could not save the webhook.";
    } finally {
      setBusy(false);
    }
  };

  const toggle = async (hook, enabled) => {
    try {
      await updateCiWebhook(service.id, hook.id, { enabled });
      onNotice(enabled ? `“${hook.name}” is on — calls to it start builds again.` : `“${hook.name}” is off. Calls are answered and logged, and build nothing.`);
      load();
    } catch (err) {
      onError(err.message || "Could not change the webhook.");
    }
  };

  const remove = async (hook) => {
    const extra = hook.kind === "bitbucket_push" ? " Remove it from the repository's webhooks in Bitbucket too, or Bitbucket keeps calling a URL that answers 404." : "";
    if (!window.confirm(`Delete the webhook “${hook.name}”? Its URL stops working at once; builds it started are kept.${extra}`)) return;
    try {
      await deleteCiWebhook(service.id, hook.id);
      onNotice(`Webhook “${hook.name}” deleted.`);
      if (editing?.id === hook.id) setEditing(null);
      load();
    } catch (err) {
      onError(err.message || "Could not delete the webhook.");
    }
  };

  if (data === null) return <p className="pl-field-hint">Loading webhooks…</p>;

  const startNew = (kind) => setEditing(blankWebhook(kind, defaultBranch));
  const editorProps = {
    pipelines,
    defaultBranch,
    busy,
    canPush,
    onCancel: () => setEditing(null),
    onSave: save,
  };

  return (
    <div className="sc-root wh-root">
      {editing && !editing.id && <WebhookEditor initial={editing} {...editorProps} />}

      {items.length === 0 && !editing ? (
        <div className="pl-empty sc-empty">
          <span className="pl-empty-glyph" aria-hidden="true">
            <PlIcon name="link" />
          </span>
          <strong>No webhooks</strong>
          <p>
            A webhook starts a build when something calls it: Bitbucket when somebody pushes to{" "}
            <code>{defaultBranch}</code>, or a release tool, a script or another CI that POSTs to a URL — optionally
            naming the branch and build inputs, within what you allow.
          </p>
          {canEdit ? (
            <div className="wh-empty-actions">
              <button
                type="button"
                className="primary btn-compact"
                onClick={() => startNew("bitbucket_push")}
                disabled={!canPush}
                title={canPush ? undefined : "Connect a Bitbucket repository on the Source tab first."}
              >
                <PlIcon name="branch" /> Build on push
              </button>
              <button type="button" className="btn-outline btn-compact" onClick={() => startNew("generic")}>
                <PlIcon name="link" /> Generic webhook URL
              </button>
            </div>
          ) : (
            <p className="sc-readonly">Adding one needs permission to edit pipelines and run builds.</p>
          )}
        </div>
      ) : (
        items.length > 0 && (
          <ul className="sc-list">
            {items.map((hook) =>
              editing?.id === hook.id ? (
                <li key={hook.id} className="sc-item is-editing">
                  <WebhookEditor initial={editing} {...editorProps} />
                </li>
              ) : (
                <WebhookRow
                  key={hook.id}
                  hook={hook}
                  service={service}
                  repository={repository}
                  pipelines={pipelines}
                  defaultBranch={defaultBranch}
                  canEdit={canEdit}
                  canRun={canRun}
                  open={openId === hook.id}
                  freshSecret={fresh?.id === hook.id ? fresh.secret : ""}
                  onOpen={() => setOpenId(openId === hook.id ? null : hook.id)}
                  onToggle={(enabled) => toggle(hook, enabled)}
                  onEdit={() => setEditing(toForm(hook))}
                  onDelete={() => remove(hook)}
                  onChanged={load}
                  onError={onError}
                  onNotice={onNotice}
                  onOpenBuild={onOpenBuild}
                />
              )
            )}
          </ul>
        )
      )}

      {canEdit && items.length > 0 && !editing && (
        <div className="wh-add-row">
          <button
            type="button"
            className="btn-ghost pl-rows-add"
            onClick={() => startNew("bitbucket_push")}
            disabled={atLimit || !canPush}
            title={
              atLimit
                ? `A service may have at most ${limits.maxWebhooks} webhooks.`
                : canPush
                  ? undefined
                  : "Connect a Bitbucket repository on the Source tab first."
            }
          >
            <PlIcon name="plus" /> Build on push
          </button>
          <button
            type="button"
            className="btn-ghost pl-rows-add"
            onClick={() => startNew("generic")}
            disabled={atLimit}
            title={atLimit ? `A service may have at most ${limits.maxWebhooks} webhooks.` : undefined}
          >
            <PlIcon name="plus" /> Generic webhook
          </button>
        </div>
      )}
    </div>
  );
}

function WebhookRow({
  hook,
  service,
  repository,
  pipelines,
  defaultBranch,
  canEdit,
  canRun,
  open,
  freshSecret,
  onOpen,
  onToggle,
  onEdit,
  onDelete,
  onChanged,
  onError,
  onNotice,
  onOpenBuild,
}) {
  const outcome = outcomeOf(hook);
  const problem = hook.pipelineProblem;
  const rejected = rejectedRecently(hook);
  const inputs = Object.entries(hook.variables || {});
  const isPush = hook.kind === "bitbucket_push";
  return (
    <li className={`sc-item wh-item${hook.enabled ? "" : " is-off"}${problem || rejected ? " has-problem" : ""}${open ? " is-open" : ""}`}>
      <span className="sc-icon" aria-hidden="true">
        <PlIcon name={isPush ? "branch" : "link"} />
      </span>
      <div className="sc-copy">
        <strong className="sc-name">
          {hook.name}
          <span className="pl-tag">{isPush ? "Bitbucket push" : "Generic"}</span>
          {!hook.enabled && <span className="pl-tag">Off</span>}
        </strong>
        <p className="sc-when wh-builds">{buildsWhat(hook, defaultBranch)}</p>
        <p className="sc-meta">
          <span>{hook.pipelineName ? `Pipeline ${hook.pipelineName}` : "Default build pipeline"}</span>
          {!isPush && (hook.allowedInputs || []).length > 0 && <span>Request may set {(hook.allowedInputs || []).join(", ")}</span>}
          {!isPush && (hook.mappings || []).length > 0 && (
            <span>
              Maps {(hook.mappings || []).map((item) => targetLabel(item.target)).join(", ")}
            </span>
          )}
          {hook.runsAs && <span>Runs as {hook.runsAs}</span>}
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
          {hook.lastBuild && hook.lastOutcome === "triggered" ? (
            <button type="button" className="btn-ghost sc-buildlink" onClick={() => onOpenBuild?.(hook.lastBuild.id)}>
              {outcome.label}
            </button>
          ) : (
            <span className="sc-last-label">{outcome.label}</span>
          )}
          {hook.lastDeliveryAt && <span className="sc-last-when">{formatAgo(hook.lastDeliveryAt)}</span>}
          {outcome.detail && <span className="sc-last-detail">{outcome.detail}</span>}
        </div>
        {rejected && (
          <p className="sc-problem" role="alert">
            <PlIcon name="lock" /> A call with a wrong or missing secret arrived {formatAgo(hook.lastRejectedAt)}. If
            that was your sender, its secret does not match this webhook's.
          </p>
        )}
        {problem && (
          <p className="sc-problem" role="alert">
            <PlIcon name="alert" /> {problem}
          </p>
        )}
      </div>
      <div className="sc-actions">
        {canEdit && <Switch checked={hook.enabled} onChange={onToggle} label={hook.enabled ? "On" : "Off"} />}
        <button type="button" className="btn-ghost st-action" onClick={onOpen} aria-expanded={open}>
          <PlIcon name={open ? "up" : "down"} /> {open ? "Hide" : "Connect & test"}
        </button>
        {canEdit && (
          <>
            <button type="button" className="btn-ghost st-action" onClick={onEdit}>
              <PlIcon name="variable" /> Edit
            </button>
            <button
              type="button"
              className="btn-ghost pl-tool is-danger"
              aria-label={`Delete ${hook.name}`}
              title="Delete"
              onClick={onDelete}
            >
              <PlIcon name="trash" />
            </button>
          </>
        )}
      </div>
      {open && (
        <WebhookDetails
          hook={hook}
          service={service}
          repository={repository}
          pipelines={pipelines}
          defaultBranch={defaultBranch}
          canEdit={canEdit}
          canRun={canRun}
          freshSecret={freshSecret}
          onChanged={onChanged}
          onError={onError}
          onNotice={onNotice}
          onOpenBuild={onOpenBuild}
        />
      )}
    </li>
  );
}

function WebhookDetails({ hook, service, repository, pipelines, defaultBranch, canEdit, canRun, freshSecret, onChanged, onError, onNotice, onOpenBuild }) {
  const [secret, setSecret] = useState(freshSecret || "");
  const [secretBusy, setSecretBusy] = useState(false);
  const [deliveries, setDeliveries] = useState(null);
  const isPush = hook.kind === "bitbucket_push";
  const parameters = pipelineFor(pipelines, hook.pipelineId ? String(hook.pipelineId) : "")?.parameters || [];

  const loadDeliveries = useCallback(
    () =>
      listCiWebhookDeliveries(service.id, hook.id)
        .then((next) => setDeliveries(next.items || []))
        .catch(() => setDeliveries((prev) => prev || [])),
    [service.id, hook.id]
  );
  useEffect(() => {
    loadDeliveries();
  }, [loadDeliveries, hook.lastDeliveryAt]);

  const copy = async (text, what) => {
    if (await copyText(text)) onNotice(`${what} copied.`);
    else onError(`Could not copy the ${what.toLowerCase()} — select it and copy it by hand.`);
  };

  const reveal = async () => {
    setSecretBusy(true);
    try {
      const result = await revealCiWebhookSecret(service.id, hook.id);
      setSecret(result.secret || "");
    } catch (err) {
      onError(err.message || "Could not read the secret.");
    } finally {
      setSecretBusy(false);
    }
  };

  const rotate = async () => {
    const where = isPush ? "Press Resync in Bitbucket afterwards" : "Every sender has to be given the new one";
    if (!window.confirm(`Make a new secret for “${hook.name}”? The current one stops working immediately. ${where}.`)) return;
    setSecretBusy(true);
    try {
      const result = await rotateCiWebhookSecret(service.id, hook.id);
      setSecret(result.secret || "");
      onNotice(result.message || "Secret rotated.");
    } catch (err) {
      onError(err.message || "Could not rotate the secret.");
    } finally {
      setSecretBusy(false);
    }
  };

  return (
    <div className="wh-details">
      <section className="wh-block" aria-label="Connection">
        <h5 className="pl-block-title">Connection</h5>
        <div className="wh-kv">
          <span className="wh-k">URL</span>
          <code className="wh-v">{hook.url}</code>
          <button type="button" className="btn-ghost st-action" onClick={() => copy(hook.url, "URL")}>
            <PlIcon name="copy" /> Copy
          </button>
        </div>
        {hook.urlProblem && (
          <p className="pl-note">
            <PlIcon name="alert" />
            <span>{hook.urlProblem}</span>
          </p>
        )}
        <div className="wh-kv">
          <span className="wh-k">Secret</span>
          <code className="wh-v">{secret || "•".repeat(24)}</code>
          {secret ? (
            <button type="button" className="btn-ghost st-action" onClick={() => copy(secret, "Secret")}>
              <PlIcon name="copy" /> Copy
            </button>
          ) : (
            canEdit && (
              <button type="button" className="btn-ghost st-action" onClick={reveal} disabled={secretBusy}>
                <PlIcon name="eye" /> Reveal
              </button>
            )
          )}
          {canEdit && (
            <button type="button" className="btn-ghost st-action" onClick={rotate} disabled={secretBusy}>
              <PlIcon name="refresh" /> Rotate
            </button>
          )}
        </div>
        {freshSecret && secret === freshSecret && (
          <p className="wh-fresh">
            <PlIcon name="key" /> Copy it now if the sender needs it — later it is behind Reveal, which is recorded in the audit
            trail.
          </p>
        )}
        {isPush ? (
          <BitbucketSetup hook={hook} service={service} canEdit={canEdit} onChanged={onChanged} onError={onError} onNotice={onNotice} />
        ) : (
          <GenericHowTo hook={hook} secret={secret} parameters={parameters} onCopy={copy} />
        )}
      </section>

      <TryIt
        hook={hook}
        service={service}
        repository={repository}
        parameters={parameters}
        defaultBranch={defaultBranch}
        canRun={canRun}
        onError={onError}
        onNotice={onNotice}
        onOpenBuild={onOpenBuild}
        onRan={() => {
          onChanged();
          loadDeliveries();
        }}
      />

      <section className="wh-block" aria-label="Recent deliveries">
        <div className="wh-block-head">
          <h5 className="pl-block-title">Recent deliveries</h5>
          <button type="button" className="btn-ghost st-action" onClick={loadDeliveries}>
            <PlIcon name="refresh" /> Refresh
          </button>
        </div>
        {deliveries === null ? (
          <p className="pl-field-hint">Loading…</p>
        ) : deliveries.length === 0 ? (
          <p className="pl-field-hint">
            Nothing has called this webhook with the right secret yet.{" "}
            {isPush ? "Push to a matching branch, or start a test build above." : "Run the curl command above, or start a test build."}
          </p>
        ) : (
          <ul className="wh-deliveries">
            {deliveries.map((delivery) => (
              <li key={delivery.id} className={`wh-delivery is-${deliveryTone(delivery.outcome)}`}>
                <span className="sc-dot" aria-hidden="true" />
                <span className="wh-delivery-outcome">{deliveryLabel(delivery.outcome)}</span>
                <span className="wh-delivery-when" title={delivery.receivedAt}>
                  {formatAgo(delivery.receivedAt)}
                </span>
                <span className="wh-delivery-event">
                  {delivery.event === "test" ? `test by ${delivery.testedBy || "?"}` : delivery.event}
                </span>
                <span className="wh-delivery-refs">
                  {(delivery.refs || []).slice(0, 3).map((ref) => (
                    <code key={`${ref.type}-${ref.name}-${ref.commit}`}>
                      {ref.type === "tag" ? "tag " : ""}
                      {ref.name}
                      {ref.commit ? `@${String(ref.commit).slice(0, 7)}` : ""}
                    </code>
                  ))}
                </span>
                {(delivery.builds || []).map((build) => (
                  <button key={build.id} type="button" className="btn-ghost sc-buildlink" onClick={() => onOpenBuild?.(build.id)}>
                    #{build.number} {build.status}
                  </button>
                ))}
                {delivery.message && delivery.outcome !== "triggered" && <span className="wh-delivery-message">{delivery.message}</span>}
              </li>
            ))}
          </ul>
        )}
      </section>
    </div>
  );
}

function BitbucketSetup({ hook, service, canEdit, onChanged, onError, onNotice }) {
  const [status, setStatus] = useState(null);
  const [busy, setBusy] = useState(false);

  const check = useCallback(() => {
    setStatus(null);
    getCiWebhookSourceStatus(service.id, hook.id)
      .then(setStatus)
      .catch((err) => setStatus({ known: false, reason: err.message }));
  }, [service.id, hook.id]);
  useEffect(() => {
    check();
  }, [check]);

  const setup = async () => {
    setBusy(true);
    try {
      const result = await setupCiWebhookInSource(service.id, hook.id);
      const action = result.webhook?.action === "updated" ? "updated" : "created";
      onNotice(`Webhook ${action} on ${result.webhook?.repository || "the repository"}. Pushes now start builds.`);
      check();
      onChanged();
    } catch (err) {
      onError(err.message || "Could not set the webhook up in Bitbucket.");
    } finally {
      setBusy(false);
    }
  };

  const state =
    status === null
      ? { tone: "unknown", text: "Checking Bitbucket…" }
      : !status.known
        ? { tone: "warn", text: status.reason || "Could not check Bitbucket." }
        : !status.exists
          ? { tone: "warn", text: `Not registered on ${status.repository} yet — Bitbucket will not call it.` }
          : status.inSync
            ? { tone: "ok", text: `Registered on ${status.repository} for repo:push, signed with this secret.` }
            : {
                tone: "warn",
                text: `Registered on ${status.repository} but out of date${
                  status.missingEvents?.length ? ` (missing ${status.missingEvents.join(", ")})` : status.active ? " (no secret)" : " (inactive)"
                }. Resync it.`,
              };

  return (
    <div className={`wh-source sc-last is-${state.tone}`}>
      <span className="sc-dot" aria-hidden="true" />
      <span className="wh-source-text">{state.text}</span>
      {canEdit && status !== null && (
        <button type="button" className="btn-outline btn-compact" onClick={setup} disabled={busy}>
          <PlIcon name="link" /> {busy ? "Working…" : status.exists ? "Resync in Bitbucket" : "Set up in Bitbucket"}
        </button>
      )}
    </div>
  );
}

function GenericHowTo({ hook, secret, parameters, onCopy }) {
  const body = sampleBody(hook, parameters);
  const command = curlExample(hook, { secret, body });
  return (
    <div className="wh-howto">
      <div className="wh-block-head">
        <span className="wh-k">Call it</span>
        <button type="button" className="btn-ghost st-action" onClick={() => onCopy(command, "Command")}>
          <PlIcon name="copy" /> Copy
        </button>
      </div>
      <pre className="wh-code">{command}</pre>
      <p className="pl-field-hint">
        The secret can also go in <code>Authorization: Bearer …</code>, GitLab's <code>X-Gitlab-Token</code>, or be used to
        sign the body (<code>X-Hub-Signature-256: sha256=…</code>, as GitHub and Bitbucket do). A refused request answers
        422 and a build that could not start 409, so <code>curl -f</code> fails when nothing was built.
        {hook.allowRefOverride ? " The body may name branch, tag and commit." : ""}
      </p>
    </div>
  );
}

function TryIt({ hook, service, repository, parameters, defaultBranch, canRun, onError, onNotice, onOpenBuild, onRan }) {
  const ids = useId();
  const isPush = hook.kind === "bitbucket_push";
  const initial = () =>
    JSON.stringify(
      isPush
        ? samplePush({ repository, branch: (hook.branchFilters || []).find((item) => !/[*?[]/.test(item)) || defaultBranch })
        : sampleBody(hook, parameters),
      null,
      2
    );
  const [text, setText] = useState(initial);
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState("");

  let parsed = null;
  let parseError = "";
  try {
    parsed = text.trim() ? JSON.parse(text) : {};
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) parseError = "The body must be a JSON object.";
  } catch (err) {
    parseError = `Not valid JSON: ${err.message}`;
  }

  const preview = async () => {
    setBusy("preview");
    try {
      setResult({ mode: "preview", ...(await previewCiWebhook(service.id, hook.id, parsed, isPush ? "repo:push" : "")) });
    } catch (err) {
      onError(err.message || "Could not preview.");
    } finally {
      setBusy("");
    }
  };

  const test = async () => {
    setBusy("test");
    try {
      const outcome = await testCiWebhook(service.id, hook.id, parsed);
      setResult({ mode: "test", ...outcome });
      if (outcome.triggered) onNotice(`${outcome.message} Started as you, as a test.`);
      onRan();
    } catch (err) {
      onError(err.message || "Could not start the test build.");
    } finally {
      setBusy("");
    }
  };

  return (
    <section className="wh-block" aria-labelledby={`${ids}-title`}>
      <div className="wh-block-head">
        <h5 className="pl-block-title" id={`${ids}-title`}>
          Try a body
        </h5>
        <button type="button" className="btn-ghost st-action" onClick={() => setText(initial())}>
          <PlIcon name="reset" /> Sample
        </button>
      </div>
      <p className="pl-field-hint">
        {isPush
          ? "A Bitbucket push as it arrives. Preview shows what it would build; a test build starts it for real, as you."
          : "Paste what your tool sends. Preview runs it through the webhook's rules without building; the paths it finds are what a mapping can read."}
      </p>
      <textarea
        className="is-mono wh-body"
        rows={isPush ? 10 : 6}
        spellCheck={false}
        value={text}
        aria-label="Request body"
        aria-invalid={Boolean(parseError)}
        onChange={(event) => setText(event.target.value)}
      />
      {parseError && (
        <p className="pl-field-error" role="alert">
          <PlIcon name="alert" /> {parseError}
        </p>
      )}
      <div className="st-add-actions wh-try-actions">
        <button type="button" className="btn-outline btn-compact" onClick={preview} disabled={Boolean(parseError) || Boolean(busy)}>
          <PlIcon name="eye" /> {busy === "preview" ? "Checking…" : "Preview"}
        </button>
        {canRun && (
          <button type="button" className="primary btn-compact" onClick={test} disabled={Boolean(parseError) || Boolean(busy)}>
            <PlIcon name="forward" /> {busy === "test" ? "Starting…" : "Start a test build"}
          </button>
        )}
      </div>
      {result && <PlanResult result={result} onOpenBuild={onOpenBuild} />}
    </section>
  );
}

function PlanResult({ result, onOpenBuild }) {
  const outcome = result.mode === "preview" ? (result.outcome === "build" ? "triggered" : result.outcome) : result.outcome;
  const tone = deliveryTone(outcome);
  const heading =
    result.mode === "preview"
      ? result.outcome === "build"
        ? `Would build ${result.builds.length === 1 ? "1 ref" : `${result.builds.length} refs`}`
        : deliveryLabel(result.outcome)
      : result.message || deliveryLabel(result.outcome);
  return (
    <div className={`wh-result is-${tone}`} aria-live="polite">
      <p className="wh-result-head">
        <span className="sc-dot" aria-hidden="true" />
        <strong>{heading}</strong>
        {result.mode === "preview" && result.message && <span>{result.message}</span>}
      </p>
      {result.mode === "preview" &&
        (result.builds || []).map((build) => (
          <div key={`${build.refType}-${build.ref}-${build.commit}`} className="wh-plan">
            <code>
              {build.refType === "tag" ? "tag " : ""}
              {build.ref}
              {build.commit ? ` @ ${String(build.commit).slice(0, 12)}` : ""}
            </code>
            {Object.entries(build.variables || {}).map(([name, value]) => (
              <code key={name} className="wh-plan-var">
                {name}={String(value).length > 24 ? `${String(value).slice(0, 24)}…` : String(value)}
              </code>
            ))}
          </div>
        ))}
      {result.mode === "test" &&
        (result.builds || []).map((build) => (
          <button key={build.id} type="button" className="btn-ghost sc-buildlink" onClick={() => onOpenBuild?.(build.id)}>
            Open build #{build.number}
          </button>
        ))}
      {(result.notes || []).length > 0 && (
        <ul className="wh-notes">
          {result.notes.map((note) => (
            <li key={note}>{note}</li>
          ))}
        </ul>
      )}
      {result.mode === "preview" && (result.paths || []).length > 0 && (
        <details className="wh-paths">
          <summary>{result.paths.length} paths in this body a mapping can read</summary>
          <p>
            {result.paths.map((path) => (
              <code key={path}>{path}</code>
            ))}
          </p>
        </details>
      )}
    </div>
  );
}

function WebhookEditor({ initial, pipelines, defaultBranch, busy, canPush, onCancel, onSave }) {
  const [form, setForm] = useState(initial);
  const [serverError, setServerError] = useState("");
  const [touched, setTouched] = useState(false);
  const ids = useId();
  const pipeline = pipelineFor(pipelines, form.pipelineId);
  const parameters = pipeline?.parameters || [];
  const values = effectiveValues(parameters, form.variables);
  const problems = formProblems(form);
  const isPush = form.kind === "bitbucket_push";
  const allowed = new Set(form.allowedInputs || []);

  const set = (key, value) => setForm((prev) => ({ ...prev, [key]: value }));
  const setValue = (name, value) => setForm((prev) => ({ ...prev, variables: { ...prev.variables, [name]: value } }));
  const setAllowed = (name, on) =>
    setForm((prev) => {
      const next = (prev.allowedInputs || []).filter((item) => item !== name);
      return { ...prev, allowedInputs: on ? [...next, name] : next };
    });
  const setMapping = (index, patch) =>
    setForm((prev) => ({ ...prev, mappings: prev.mappings.map((item, at) => (at === index ? { ...item, ...patch } : item)) }));

  const submit = async () => {
    setTouched(true);
    if (Object.keys(problems).length || busy) return;
    setServerError("");
    const error = await onSave({ ...form, variables: values }, parameters);
    if (error) setServerError(error);
  };

  const targets = targetOptions(parameters);

  return (
    <div className="st-add sc-editor wh-editor" role="group" aria-label={form.id ? `Edit ${initial.name}` : "New webhook"}>
      {!form.id && (
        <Field label="Kind" wide>
          <Segmented
            label="Webhook kind"
            value={form.kind}
            options={KINDS.map((kind) => ({
              ...kind,
              disabled: kind.value === "bitbucket_push" && !canPush,
            }))}
            onChange={(value) => setForm(blankWebhook(value, defaultBranch))}
          />
        </Field>
      )}
      <div className="pl-grid">
        <Field label="Name" htmlFor={`${ids}-name`} error={touched ? problems.name : ""}>
          <input
            id={`${ids}-name`}
            value={form.name}
            maxLength={120}
            placeholder={isPush ? "Build on push" : "Release tool"}
            onChange={(event) => set("name", event.target.value)}
            autoFocus={!form.id}
          />
        </Field>
        {pipelines.length > 1 && (
          <Field label="Pipeline" htmlFor={`${ids}-pipeline`} hint="Default follows whichever build pipeline is the default when the call arrives.">
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
      </div>

      {isPush ? (
        <div className="pl-grid">
          <Field
            label="Branches that build"
            hint={
              (form.branchFilters || []).length
                ? "A push to any of these builds the pushed commit. Wildcards like release/* work."
                : "Empty: a push to ANY branch builds — every feature branch too."
            }
          >
            <ChipsInput
              value={form.branchFilters}
              onChange={(value) => set("branchFilters", value)}
              lowercase={false}
              label="Branch filters"
              placeholder={`${defaultBranch}, release/*`}
            />
          </Field>
          <Field label="Tags" hint={form.buildTags ? "Empty: every pushed tag builds." : "Pushed tags are ignored."}>
            <Switch checked={form.buildTags} onChange={(value) => set("buildTags", value)} label="Also build pushed tags" />
            {form.buildTags && (
              <ChipsInput
                value={form.tagFilters}
                onChange={(value) => set("tagFilters", value)}
                lowercase={false}
                label="Tag filters"
                placeholder="v*"
              />
            )}
          </Field>
        </div>
      ) : (
        <>
          <div className="pl-grid">
            <Field
              label="What to build"
              htmlFor={`${ids}-ref`}
              error={touched ? problems.branch : ""}
              hint={
                form.refType === "tag"
                  ? "Builds this tag unless the request names another ref."
                  : form.branch
                    ? `Builds the head of ${form.branch} unless the request names another ref.`
                    : `Empty builds the default branch, ${defaultBranch}.`
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
                  placeholder={form.refType === "tag" ? "v1.4.0" : `${defaultBranch} (default)`}
                  onChange={(event) => set("branch", event.target.value)}
                />
              </div>
            </Field>
            <Field label="Request may choose the ref" hint="Lets the body send branch, tag or commit — within the filters below.">
              <Switch
                checked={form.allowRefOverride}
                onChange={(value) => set("allowRefOverride", value)}
                label={form.allowRefOverride ? "Yes — branch / tag / commit from the body" : "No — always the ref above"}
              />
            </Field>
          </div>
          {requestChoosesRef(form) && (
            <div className="pl-grid">
              <Field label="Branches it may build" hint="Empty: any branch.">
                <ChipsInput
                  value={form.branchFilters}
                  onChange={(value) => set("branchFilters", value)}
                  lowercase={false}
                  label="Branch filters"
                  placeholder="main, release/*"
                />
              </Field>
              <Field label="Tags it may build" hint="Empty: any tag.">
                <ChipsInput
                  value={form.tagFilters}
                  onChange={(value) => set("tagFilters", value)}
                  lowercase={false}
                  label="Tag filters"
                  placeholder="v*"
                />
              </Field>
            </div>
          )}
        </>
      )}

      {parameters.length > 0 ? (
        <div className="sc-inputs" role="group" aria-labelledby={`${ids}-inputs`}>
          <h5 className="pl-block-title" id={`${ids}-inputs`}>
            Build inputs
          </h5>
          <p className="sc-inputs-note">
            {isPush
              ? "What every build from this webhook gets."
              : "The value each build gets — and, where you allow it, the request may send its own under “variables”. It is still checked against the input's type and choices."}
          </p>
          {parameters.map((param) => (
            <div key={param.name} className={`wh-input${!isPush && allowed.has(param.name) ? " is-open" : ""}`}>
              <InputControl
                id={`${ids}-in-${param.name}`}
                param={param}
                value={values[param.name]}
                conditions={conditionsFor(pipeline, param.name)}
                onChange={(next) => setValue(param.name, next)}
              />
              {!isPush && (
                <Switch
                  checked={allowed.has(param.name)}
                  onChange={(on) => setAllowed(param.name, on)}
                  label="Request may set"
                />
              )}
            </div>
          ))}
        </div>
      ) : (
        !isPush && (
          <Field
            label="Inputs the request may set"
            hint="This pipeline declares no build inputs, so any names you list here reach the stages as environment variables."
          >
            <ChipsInput
              value={form.allowedInputs}
              onChange={(value) => set("allowedInputs", value)}
              lowercase={false}
              label="Allowed inputs"
              placeholder="VERSION, RELEASE_NOTES"
            />
          </Field>
        )
      )}

      {!isPush && (
        <div className="wh-mappings" role="group" aria-labelledby={`${ids}-map`}>
          <h5 className="pl-block-title" id={`${ids}-map`}>
            Read values from the body
          </h5>
          <p className="sc-inputs-note">
            For a sender whose body you do not control — Nexus, GitHub, a release tool. Each row reads one path, like{" "}
            <code>release.tag_name</code> or <code>commits.0.id</code>; <code>refs/heads/…</code> and{" "}
            <code>refs/tags/…</code> are understood. A missing path keeps the value above.
          </p>
          {(form.mappings || []).map((mapping, index) => (
            <div key={index} className="wh-mapping">
              <select
                aria-label="Target"
                value={mapping.target}
                onChange={(event) => setMapping(index, { target: event.target.value })}
              >
                <option value="">Choose…</option>
                {targets.map((item) => (
                  <option key={item.value} value={item.value}>
                    {item.label}
                  </option>
                ))}
                {mapping.target && !targets.some((item) => item.value === mapping.target) && (
                  <option value={mapping.target}>{mapping.target}</option>
                )}
              </select>
              <span aria-hidden="true">←</span>
              <input
                className="is-mono"
                aria-label="Path in the body"
                value={mapping.path}
                spellCheck={false}
                placeholder="release.tag_name"
                onChange={(event) => setMapping(index, { path: event.target.value })}
              />
              <button
                type="button"
                className="btn-ghost pl-tool is-danger"
                aria-label="Remove mapping"
                onClick={() => set("mappings", form.mappings.filter((_, at) => at !== index))}
              >
                <PlIcon name="x" />
              </button>
            </div>
          ))}
          {touched && problems.mappings && (
            <p className="pl-field-error" role="alert">
              <PlIcon name="alert" /> {problems.mappings}
            </p>
          )}
          <button
            type="button"
            className="btn-ghost pl-rows-add"
            onClick={() => set("mappings", [...(form.mappings || []), { target: "", path: "" }])}
          >
            <PlIcon name="plus" /> Add a mapping
          </button>
        </div>
      )}

      <div className="sc-switches">
        <Switch checked={form.enabled} onChange={(value) => set("enabled", value)} label="On — calls start builds" />
        <Switch
          checked={form.skipIfRunning}
          onChange={(value) => set("skipIfRunning", value)}
          label="Skip a call while this webhook's previous build is still queued or running"
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
        <button type="button" className="primary btn-compact" onClick={submit} disabled={busy}>
          <PlIcon name="check" /> {busy ? "Saving…" : form.id ? "Save webhook" : "Create webhook"}
        </button>
      </div>
    </div>
  );
}
