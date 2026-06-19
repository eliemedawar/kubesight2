import { useEffect, useState } from "react";
import { normalizeAlertRouting } from "../../utils/formatters.js";

export default function AlertRoutingModal({
  open,
  routing,
  onClose,
  onSave,
  onTestEmail,
  saving,
  error,
  testingEmail,
  testMessage,
}) {
  const [draft, setDraft] = useState(normalizeAlertRouting(routing));

  useEffect(() => {
    if (open) {
      setDraft(normalizeAlertRouting(routing));
    }
  }, [open, routing]);

  if (!open) {
    return null;
  }

  const setChannelEnabled = (channel, enabled) => {
    setDraft((prev) => ({
      ...prev,
      [channel]: { ...prev[channel], enabled },
    }));
  };

  const setChannelField = (channel, field, value) => {
    setDraft((prev) => ({
      ...prev,
      [channel]: { ...prev[channel], [field]: value },
    }));
  };

  const handleSave = async () => {
    await onSave(draft);
  };

  return (
    <div className="modal-overlay" role="presentation" onClick={onClose}>
      <section
        className="card modal-panel routing-modal"
        role="dialog"
        aria-labelledby="routing-modal-title"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="modal-header">
          <div>
            <h3 id="routing-modal-title">Alert delivery routing</h3>
            <p className="muted">Choose how KubeSight should notify you when alerts fire.</p>
          </div>
          <button type="button" className="modal-close" onClick={onClose} aria-label="Close">
            <svg width="14" height="14" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true"><path fill-rule="evenodd" d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z" clip-rule="evenodd" /></svg>
          </button>
        </header>

        <div className="routing-options">
          <article className={`routing-option ${draft.email.enabled ? "active" : ""}`}>
            <label className="routing-option-toggle">
              <input
                type="checkbox"
                checked={draft.email.enabled}
                onChange={(event) => setChannelEnabled("email", event.target.checked)}
              />
              <span>
                <strong>Email</strong>
                <span className="muted">Send alerts to an inbox</span>
              </span>
            </label>
            {draft.email.enabled ? (
              <label>
                Email address
                <input
                  type="email"
                  placeholder="oncall@company.com"
                  value={draft.email.address}
                  onChange={(event) => setChannelField("email", "address", event.target.value)}
                />
              </label>
            ) : null}
          </article>

          <article className={`routing-option ${draft.slack.enabled ? "active" : ""}`}>
            <label className="routing-option-toggle">
              <input
                type="checkbox"
                checked={draft.slack.enabled}
                onChange={(event) => setChannelEnabled("slack", event.target.checked)}
              />
              <span>
                <strong>Slack</strong>
                <span className="muted">Post to a channel via incoming webhook</span>
              </span>
            </label>
            {draft.slack.enabled ? (
              <label>
                Slack webhook URL
                <input
                  type="url"
                  placeholder="https://hooks.slack.com/services/..."
                  value={draft.slack.webhookUrl}
                  onChange={(event) => setChannelField("slack", "webhookUrl", event.target.value)}
                />
              </label>
            ) : null}
          </article>

          <article className={`routing-option ${draft.webhook.enabled ? "active" : ""}`}>
            <label className="routing-option-toggle">
              <input
                type="checkbox"
                checked={draft.webhook.enabled}
                onChange={(event) => setChannelEnabled("webhook", event.target.checked)}
              />
              <span>
                <strong>Webhook</strong>
                <span className="muted">POST JSON payloads to your endpoint</span>
              </span>
            </label>
            {draft.webhook.enabled ? (
              <label>
                Webhook URL
                <input
                  type="url"
                  placeholder="https://api.company.com/alerts"
                  value={draft.webhook.url}
                  onChange={(event) => setChannelField("webhook", "url", event.target.value)}
                />
              </label>
            ) : null}
          </article>
        </div>

        {error ? <p className="routing-error">{error}</p> : null}
        {testMessage ? <p className="routing-test-message">{testMessage}</p> : null}
        <p className="muted routing-smtp-hint">
          Server SMTP must be configured in backend <code>.env</code> (SMTP_HOST, SMTP_FROM). Use Mailpit locally on
          port 1025 for testing.
        </p>

        <footer className="modal-actions">
          <button type="button" onClick={onClose} disabled={saving || testingEmail}>
            Cancel
          </button>
          <button
            type="button"
            onClick={() => onTestEmail(draft)}
            disabled={saving || testingEmail || !draft.email.enabled || !draft.email.address}
          >
            {testingEmail ? "Sending test..." : "Send test email"}
          </button>
          <button type="button" onClick={handleSave} disabled={saving || testingEmail}>
            {saving ? "Saving..." : "Save routing"}
          </button>
        </footer>
      </section>
    </div>
  );
}
