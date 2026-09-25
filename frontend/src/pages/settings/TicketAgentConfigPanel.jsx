import { useCallback, useEffect, useMemo, useState } from "react";
import ErrorBanner from "../../components/common/ErrorBanner.jsx";
import LoadingState from "../../components/common/LoadingState.jsx";
import {
  getTicketAgentSettings,
  testTicketAgentTelegram,
  updateTicketAgentSettings,
} from "../../api/ticketAgentApi.js";

/**
 * Hermes ticket agent — the Configuration tab of its integrations card.
 *
 * Hermes handles each inbound ticket itself, through KubeSight's MCP tools,
 * and writes the comments. What is configured here is KubeSight's side: the
 * switch, the confidence bar the execute tool enforces, comment visibility, and
 * the Telegram bot that carries approval requests. Which Hermes it talks to is
 * an environment variable on the backend (TICKET_AGENT_HERMES_URL/TOKEN), shown
 * here read-only.
 */

const EMPTY = {
  enabled: false,
  minConfidence: "High",
  publicComments: true,
  approvalTimeoutHours: 24,
  telegramEnabled: false,
  telegramBotToken: "",
  telegramChatId: "",
  telegramApprovers: "",
};

function formFrom(settings) {
  return {
    enabled: Boolean(settings?.enabled),
    minConfidence: settings?.minConfidence || "High",
    publicComments: settings?.publicComments !== false,
    approvalTimeoutHours: settings?.approvalTimeoutHours ?? 24,
    telegramEnabled: Boolean(settings?.telegramEnabled),
    telegramBotToken: "",
    telegramChatId: settings?.telegramChatId || "",
    telegramApprovers: settings?.telegramApprovers || "",
  };
}

export default function TicketAgentConfigPanel({ canManage = false, onChanged }) {
  const [settings, setSettings] = useState(null);
  const [form, setForm] = useState(EMPTY);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  const load = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const data = await getTicketAgentSettings();
      setSettings(data);
      setForm(formFrom(data));
    } catch (err) {
      setError(err.message || "Failed to load the ticket agent settings.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const dirty = useMemo(
    () => settings && JSON.stringify(form) !== JSON.stringify(formFrom(settings)),
    [form, settings]
  );
  const set = (key, value) => setForm((prev) => ({ ...prev, [key]: value }));
  const ro = !canManage;

  const save = async (event) => {
    event?.preventDefault();
    setSaving(true);
    setError("");
    setNotice("");
    try {
      const payload = { ...form, approvalTimeoutHours: Number(form.approvalTimeoutHours) };
      if (!payload.telegramBotToken) delete payload.telegramBotToken;
      const data = await updateTicketAgentSettings(payload);
      setSettings(data);
      setForm(formFrom(data));
      setNotice("Ticket agent settings saved.");
      onChanged?.();
    } catch (err) {
      setError(err.message || "Failed to save the ticket agent settings.");
    } finally {
      setSaving(false);
    }
  };

  const test = async () => {
    setTesting(true);
    setError("");
    setNotice("");
    try {
      const result = await testTicketAgentTelegram();
      setSettings(result);
      if (result.status === "ok") setNotice(result.message);
      else setError(result.message || "The Telegram test failed.");
      onChanged?.();
    } catch (err) {
      setError(err.message || "The Telegram test failed.");
    } finally {
      setTesting(false);
    }
  };

  if (loading) return <LoadingState label="Loading ticket agent settings..." />;

  const hermesOk = settings?.hermesConfigured && settings?.hermesDedicated;

  return (
    <div className="zoho-page">
      {error ? <ErrorBanner message={error} onDismiss={() => setError("")} /> : null}
      {notice ? <p className="settings-panel-notice">{notice}</p> : null}

      <form onSubmit={save}>
        <section className="card sg-zh-setsec">
          <div className="card-header-row">
            <h3>Hermes</h3>
            <span className={`status-pill ${settings?.active ? "ok" : hermesOk ? "muted" : "warn"}`}>
              {settings?.active ? "Handling tickets" : hermesOk ? "Off" : "Hermes not connected"}
            </span>
          </div>
          <p className="muted">
            When this is on, every ticket Zoho or Jira sends is handed to your Hermes (the one with
            the <span className="mono">kubesight</span> skill). Hermes reads it and acts through
            KubeSight's MCP tools: it deploys, changes a variable or restarts, moves the ticket
            (In Progress → Done, or Impediment) and writes every comment. The deploy itself goes
            through the normal deploy automation, so cluster approvals, rollback and the pod-health
            check still apply.
          </p>
          {!hermesOk ? (
            <p className="sg-zh-inline-error">
              {settings?.hermesHint ||
                "Set TICKET_AGENT_HERMES_URL and TICKET_AGENT_HERMES_TOKEN on the backend."}
            </p>
          ) : null}
          <div className="settings-form sg-zh-setform">
            <label className="checkbox-label">
              <input
                type="checkbox"
                checked={form.enabled}
                onChange={(e) => set("enabled", e.target.checked)}
                disabled={ro}
              />
              Hand every inbound ticket to Hermes
            </label>
            <div className="sg-zh-jrow4">
              <label title="The execute tool refuses anything under this — Hermes has to ask for approval instead">
                Runs on its own at
                <select
                  value={form.minConfidence}
                  onChange={(e) => set("minConfidence", e.target.value)}
                  disabled={ro}
                >
                  <option value="High">High confidence only</option>
                  <option value="Medium">Medium or High</option>
                </select>
                <span className="field-hint">
                  Below this, or when Hermes disagrees with the ticket's dropdowns, it asks on Telegram.
                </span>
              </label>
              <label>
                Approval expires after (hours)
                <input
                  type="number"
                  min={1}
                  max={168}
                  value={form.approvalTimeoutHours}
                  onChange={(e) => set("approvalTimeoutHours", e.target.value)}
                  disabled={ro}
                />
              </label>
            </div>
            <label className="checkbox-label">
              <input
                type="checkbox"
                checked={form.publicComments}
                onChange={(e) => set("publicComments", e.target.checked)}
                disabled={ro}
              />
              Post Hermes' comments as public (the requester can see them)
            </label>
          </div>
        </section>

        <section className="card sg-zh-setsec">
          <div className="card-header-row">
            <h3>Telegram approvals</h3>
            <span className={`status-pill ${settings?.telegramReady ? "ok" : "muted"}`}>
              {settings?.telegramReady ? "Ready" : "Not set up"}
            </span>
          </div>
          <p className="muted">
            When Hermes isn't confident, KubeSight posts the request to this chat with Approve and
            Reject buttons. Approving runs exactly what Hermes proposed. Use a bot made for
            KubeSight: a bot can have only one reader, so a token that also drives a Hermes
            Telegram gateway won't receive the button presses.
          </p>
          <div className="settings-form sg-zh-setform">
            <label className="checkbox-label">
              <input
                type="checkbox"
                checked={form.telegramEnabled}
                onChange={(e) => set("telegramEnabled", e.target.checked)}
                disabled={ro}
              />
              Send approval requests to Telegram
            </label>
            <div className="sg-zh-jrow4">
              <label>
                Bot token
                <input
                  type="password"
                  autoComplete="new-password"
                  value={form.telegramBotToken}
                  onChange={(e) => set("telegramBotToken", e.target.value)}
                  placeholder={settings?.telegramBotTokenConfigured ? "•••• (leave blank to keep)" : "123456:ABC…"}
                  disabled={ro}
                />
              </label>
              <label>
                Chat id
                <input
                  value={form.telegramChatId}
                  onChange={(e) => set("telegramChatId", e.target.value)}
                  placeholder="-1001234567890"
                  disabled={ro}
                />
                <span className="field-hint">The group (or person) approval requests go to.</span>
              </label>
            </div>
            <label>
              Who may approve
              <input
                value={form.telegramApprovers}
                onChange={(e) => set("telegramApprovers", e.target.value)}
                placeholder="@devops_lead, 123456789"
                disabled={ro}
              />
              <span className="field-hint">
                Telegram @usernames or user ids, comma-separated. Empty means anyone in the chat.
              </span>
            </label>
          </div>
          {settings?.lastTestMessage ? (
            <p className={settings.lastTestStatus === "ok" ? "muted" : "sg-zh-inline-error"}>
              Last test: {settings.lastTestMessage}
            </p>
          ) : null}
          {canManage ? (
            <div className="sg-zh-savebar-actions">
              <button
                type="button"
                className="secondary"
                onClick={test}
                disabled={testing || dirty || !settings?.telegramReady}
                title={dirty ? "Save first" : settings?.telegramReady ? "" : "Enable Telegram and set a token + chat id"}
              >
                {testing ? "Sending…" : "Send a test message"}
              </button>
            </div>
          ) : null}
        </section>

        {dirty && canManage ? (
          <div className="sg-zh-savebar">
            <span>Unsaved ticket agent changes</span>
            <div className="sg-zh-savebar-actions">
              <button type="button" className="secondary" onClick={() => setForm(formFrom(settings))}>
                Discard
              </button>
              <button type="submit" className="primary" disabled={saving}>
                {saving ? "Saving…" : "Save"}
              </button>
            </div>
          </div>
        ) : null}
      </form>
    </div>
  );
}
