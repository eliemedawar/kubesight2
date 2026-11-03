import { useCallback, useEffect, useState } from "react";
import { IconAlert, IconCheck } from "../zoho/icons.jsx";
import { useTicketing } from "./TicketingContext.jsx";

// One-click webhook setup, inside Settings → Inbound webhook. KubeSight registers
// its two webhooks (tickets, comments) in the ticketing system with its own
// credential — the counterpart of "Set up in Bitbucket" on a CI service.
//
// The status is a LIVE read of the provider's webhooks, so it is loaded here
// rather than with the config, and "could not look" is shown as a reason, not
// as an error banner. Setup always uses the SAVED settings, which is why it is
// disabled while the form has unsaved changes.

const EVENT_LABELS = {
  Ticket_Add: "ticket created",
  Ticket_Update: "ticket updated",
  Ticket_Comment_Add: "comment added",
  Ticket_Thread_Add: "requester reply",
  "jira:issue_created": "issue created",
  "jira:issue_updated": "issue updated",
  comment_created: "comment created",
};

const ACTION_LABELS = {
  created: "Created",
  updated: "Updated",
  unchanged: "Already correct",
  failed: "Failed",
};

const eventList = (events) => (events || []).map((e) => EVENT_LABELS[e] || e).join(", ");

function driftReasons(hook, providerName) {
  const out = [];
  if (hook.enabled === false) out.push(`switched off in ${providerName}`);
  if (hook.urlMatches === false) out.push("calls an older address or secret");
  if (hook.missingEvents?.length) out.push(`missing ${eventList(hook.missingEvents)}`);
  if (hook.extraEvents?.length) out.push(`also sends ${eventList(hook.extraEvents)}`);
  return out;
}

function HookRow({ hook, providerName }) {
  const reasons = hook.exists ? driftReasons(hook, providerName) : [];
  let pill;
  if (!hook.exists) pill = <span className="status-pill muted">Not set up</span>;
  else if (hook.inSync) pill = <span className="status-pill ok">Registered</span>;
  else pill = <span className="status-pill warn">Out of date</span>;
  return (
    <li className="sg-zh-hook">
      <div className="sg-zh-hook-head">
        <strong>{hook.label}</strong>
        {pill}
      </div>
      <p className="sg-zh-hook-meta muted">
        {hook.exists ? eventList(hook.events) : `Will send: ${eventList(hook.wantedEvents)}`}
        {reasons.length ? ` — ${reasons.join("; ")}` : ""}
      </p>
      <code className="sg-zh-hook-url">{hook.exists ? hook.registeredUrl : hook.url}</code>
    </li>
  );
}

export default function WebhookSetupPanel({ canManage, config, dirty, onConfigChanged }) {
  const { key: providerKey, name: providerName, api } = useTicketing();
  const [status, setStatus] = useState(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState(null);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    try {
      setStatus(await api.getWebhookStatus());
    } catch (err) {
      setStatus({ known: false, reason: err.message || "Could not read the webhooks." });
    } finally {
      setLoading(false);
    }
  }, [api]);

  // Re-read whenever a saved setting that shapes the webhooks changes.
  useEffect(() => {
    load();
  }, [load, config?.inboundSecretConfigured, config?.webhookBaseUrl, config?.webhookSendUpdates]);

  const runSetup = async () => {
    setBusy(true);
    setError("");
    setResult(null);
    try {
      const response = await api.setupWebhooks();
      setResult(response);
      if (response.status) setStatus(response.status);
      if (response.config) onConfigChanged?.(response.config);
    } catch (err) {
      setError(err.message || "Setting up the webhooks failed.");
    } finally {
      setBusy(false);
    }
  };

  if (status && status.supported === false) return null;

  const webhooks = status?.webhooks || [];
  const anyExists = webhooks.some((w) => w.exists);
  let headPill = <span className="status-pill muted">Checking…</span>;
  if (!loading && status) {
    if (!status.known) headPill = <span className="status-pill muted">Unknown</span>;
    else if (status.inSync) headPill = <span className="status-pill ok">In sync</span>;
    else headPill = <span className="status-pill warn">Needs setup</span>;
  }
  const buttonLabel = busy
    ? "Setting up…"
    : anyExists
      ? "Resync"
      : `Set up in ${providerName}`;
  const blockedReason = !canManage
    ? "Needs the ticketing:manage permission."
    : dirty
      ? "Save your changes first — setup uses the saved settings."
      : "";

  return (
    <div className="sg-zh-hooks">
      <div className="sg-zh-hooks-head">
        <h4>Webhooks in {providerName}</h4>
        {headPill}
        <div className="sg-zh-hooks-actions">
          <button type="button" className="secondary" onClick={load} disabled={loading || busy}>
            {loading ? "Checking…" : "Check again"}
          </button>
          <button
            type="button"
            className="primary"
            onClick={runSetup}
            disabled={busy || Boolean(blockedReason)}
            title={blockedReason || undefined}
          >
            {buttonLabel}
          </button>
        </div>
      </div>

      {!loading && status && !status.known ? (
        <p className="sg-zh-webhook-closed" role="status">
          <IconAlert /> {status.reason}
        </p>
      ) : null}

      {webhooks.length ? (
        <ul className="sg-zh-hook-list">
          {webhooks.map((hook) => (
            <HookRow key={hook.kind} hook={hook} providerName={providerName} />
          ))}
        </ul>
      ) : null}

      {error ? (
        <p className="sg-zh-webhook-closed" role="alert">
          <IconAlert /> {error}
        </p>
      ) : null}

      {result ? (
        <div className={`sg-zh-hooks-result ${result.ok ? "ok" : "warn"}`} role="status">
          {result.ok ? <IconCheck /> : <IconAlert />}
          <div>
            {result.webhooks.map((hook) => (
              <p key={hook.kind}>
                <strong>{hook.label}:</strong> {ACTION_LABELS[hook.action] || hook.action}
                {hook.error ? ` — ${hook.error}` : ""}
              </p>
            ))}
            {result.secretGenerated ? (
              <p>KubeSight generated the shared secret and put it in the webhook URLs.</p>
            ) : null}
          </div>
        </div>
      ) : null}

      {status?.others?.length ? (
        <div className="sg-zh-hooks-others">
          <p className="muted">
            These {providerName} webhooks also call KubeSight. If they deliver the same tickets,
            delete them in {providerName}:
          </p>
          <ul>
            {status.others.map((hook) => (
              <li key={hook.id}>
                {hook.name || `#${hook.id}`} — <code>{hook.url}</code>
                {hook.enabled === false ? " (off)" : ""}
              </li>
            ))}
          </ul>
        </div>
      ) : null}

      <p className="muted sg-zh-hooks-note">
        {providerKey === "zoho"
          ? "KubeSight cannot see Zoho workflow rules or Deluge functions. If one already posts tickets or comments here, switch it off in Zoho after setup; until then a ticket delivered twice is still handled once. The Zoho token needs the Desk webhook scopes."
          : "KubeSight cannot see Jira Automation rules. If one already posts issues here, switch it off after setup; until then an issue delivered twice is still handled once."}
        {status?.gateway ? " Registered through the gateway base URL." : ""}
      </p>
    </div>
  );
}
