import { useEffect, useMemo, useState } from "react";
import { createPortal } from "react-dom";
import { downloadCiCodeScanReport, getCiCodeScan, sendCiCodeScanReport } from "../../api/ciApi.js";

const EMAIL_RE = /^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$/;

const COUNT_LABEL = {
  info: "every severity",
  warning: "WARNING and ERROR",
  error: "ERROR only",
};

/**
 * The quality gate's verdict for one stage, above that stage's log: how many
 * blocking findings, how many were allowed, and the PDF — to download, or to
 * send to people picked here. The stage's saved list only pre-fills the
 * picker; nothing is sent until somebody presses Send.
 */
export default function CodeScanReportPanel({ buildId, stage }) {
  const [data, setData] = useState(null);
  const [error, setError] = useState("");
  const [downloading, setDownloading] = useState(false);
  const [sending, setSending] = useState(false);
  const [sentTo, setSentTo] = useState(null);
  const finished = !["pending", "running"].includes(stage.status);

  useEffect(() => {
    let cancelled = false;
    setData(null);
    setError("");
    setSentTo(null);
    if (!finished) return undefined;
    getCiCodeScan(buildId, stage.id)
      .then((payload) => !cancelled && setData(payload))
      .catch((err) => !cancelled && setError(err.message || "Could not load the scan report."));
    return () => {
      cancelled = true;
    };
  }, [buildId, stage.id, finished]);

  const gate = stage.codeScan;
  const download = async () => {
    setDownloading(true);
    setError("");
    try {
      await downloadCiCodeScanReport(buildId, stage.id);
    } catch (err) {
      setError(err.message || "Could not download the report.");
    } finally {
      setDownloading(false);
    }
  };

  if (!finished) {
    return (
      <div className="sg-ci-gate is-pending">
        <GateIcon />
        <div className="sg-ci-gate-text">
          <strong>Quality gate</strong>
          <span>
            Fails above {gate.maxBlocking} blocking finding{gate.maxBlocking === 1 ? "" : "s"}, counting{" "}
            {COUNT_LABEL[gate.countFrom] || "every severity"}. The report is ready when the stage ends.
          </span>
        </div>
      </div>
    );
  }

  if (!data) {
    return (
      <div className={`sg-ci-gate${error ? " is-error" : ""}`}>
        <GateIcon />
        <div className="sg-ci-gate-text">
          <strong>Quality gate</strong>
          <span>{error || "Reading the scan results…"}</span>
        </div>
      </div>
    );
  }

  const failed = data.verdict === "failed";
  const tone = !data.reportAvailable ? "is-muted" : failed ? "is-failed" : "is-passed";
  const bySeverity = data.blockingBySeverity || {};

  return (
    <>
      <div className={`sg-ci-gate ${tone}`}>
        <GateIcon />
        <div className="sg-ci-gate-text">
          <strong>
            {!data.reportAvailable
              ? "Quality gate — no report"
              : failed
                ? "Quality gate failed"
                : "Quality gate passed"}
          </strong>
          {data.reportAvailable ? (
            <span>
              <b className="sg-ci-gate-num">{data.blocking}</b> blocking finding{data.blocking === 1 ? "" : "s"},{" "}
              {data.maxBlocking} allowed
              <span className="sg-ci-gate-sev">
                ERROR {bySeverity.error ?? 0} · WARNING {bySeverity.warning ?? 0} · INFO {bySeverity.info ?? 0}
              </span>
            </span>
          ) : (
            <span>{data.reason}</span>
          )}
          {error && <span className="sg-ci-gate-error">{error}</span>}
          {sentTo && (
            <span className="sg-ci-gate-sent" role="status">
              Sent to {sentTo.join(", ")}.
            </span>
          )}
        </div>
        {data.reportAvailable && (
          <div className="sg-ci-gate-actions">
            <button type="button" className="btn-outline btn-compact" onClick={download} disabled={downloading}>
              {downloading ? "Preparing…" : "Download PDF"}
            </button>
            <button
              type="button"
              className={`${failed ? "primary" : "btn-outline"} btn-compact`}
              onClick={() => setSending(true)}
            >
              Send report…
            </button>
          </div>
        )}
      </div>
      {sending && (
        <SendReportDialog
          buildId={buildId}
          stage={stage}
          data={data}
          onDownload={download}
          onClose={() => setSending(false)}
          onSent={(addresses) => {
            setSending(false);
            setSentTo(addresses);
          }}
        />
      )}
    </>
  );
}

function GateIcon() {
  return (
    <span className="sg-ci-gate-icon" aria-hidden="true">
      <svg viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round">
        <path d="M8 1.75 2.75 3.9v3.6c0 3.1 2.2 5.6 5.25 6.75 3.05-1.15 5.25-3.65 5.25-6.75V3.9L8 1.75Z" />
        <path d="m5.75 8 1.6 1.6 2.9-3.1" />
      </svg>
    </span>
  );
}

function SendReportDialog({ buildId, stage, data, onClose, onSent, onDownload }) {
  const [recipients, setRecipients] = useState(() => data.recipients || []);
  const [text, setText] = useState("");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    const onKey = (event) => event.key === "Escape" && !busy && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [busy, onClose]);

  const chosen = new Set(recipients.map((item) => item.toLowerCase()));
  const query = text.trim().toLowerCase();
  const suggestions = useMemo(
    () =>
      (data.suggestions || [])
        .filter((person) => !chosen.has(person.email.toLowerCase()))
        .filter(
          (person) =>
            !query ||
            person.email.toLowerCase().includes(query) ||
            (person.name || "").toLowerCase().includes(query) ||
            (person.username || "").toLowerCase().includes(query)
        )
        .slice(0, 6),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [data.suggestions, query, recipients]
  );
  const typedText = text.trim();
  const typedIsEmail = EMAIL_RE.test(typedText);

  const add = (raw) => {
    const next = String(raw)
      .split(/[,;\s]+/)
      .map((item) => item.trim())
      .filter((item) => EMAIL_RE.test(item) && !chosen.has(item.toLowerCase()));
    if (next.length) setRecipients((prev) => [...prev, ...next]);
    const rest = String(raw)
      .split(/[,;\s]+/)
      .map((item) => item.trim())
      .filter((item) => item && !EMAIL_RE.test(item));
    setText(rest.join(" "));
  };

  const send = async () => {
    // An address still in the box counts — people press Send without Enter.
    // Anything else there is a search nobody finished, and is said so.
    if (typedText && !typedIsEmail) {
      setError(`“${typedText}” is not an email address. Pick a person from the list or clear it.`);
      return;
    }
    const all = typedIsEmail && !chosen.has(typedText.toLowerCase()) ? [...recipients, typedText] : recipients;
    setBusy(true);
    setError("");
    try {
      const result = await sendCiCodeScanReport(buildId, stage.id, { recipients: all, note });
      onSent(result.sentTo || all);
    } catch (err) {
      setError(err.message || "The report could not be sent.");
      setBusy(false);
    }
  };

  const failed = data.verdict === "failed";
  const pendingCount = recipients.length + (typedIsEmail && !chosen.has(typedText.toLowerCase()) ? 1 : 0);

  // Portalled: the build drawer is itself a fixed layer, and a dialog nested
  // inside it would be clipped by its scroll box.
  return createPortal(
    <div
      className="modal-backdrop sg-ci-send-backdrop"
      role="presentation"
      onClick={(event) => {
        event.stopPropagation();
        if (!busy) onClose();
      }}
    >
      <div
        className="modal-card sg-ci-send"
        role="dialog"
        aria-modal="true"
        aria-labelledby="sg-ci-send-title"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="modal-card__header">
          <h3 id="sg-ci-send-title">Send the scan report</h3>
          <p className="muted">
            A PDF of {failed ? "why the quality gate failed" : "the scan's findings"} — {data.blocking} blocking
            finding{data.blocking === 1 ? "" : "s"}, {data.maxBlocking} allowed — with a link back to this build.
          </p>
        </div>

        {!data.emailConfigured && (
          <p className="banner-message warning">
            Email is not set up on this KubeSight, so the report cannot be sent from here.{" "}
            <button type="button" className="btn-ghost sg-ci-linkbtn" onClick={onDownload}>
              Download the PDF
            </button>{" "}
            and send it yourself, or configure SMTP in Settings.
          </p>
        )}

        <label className="form-label" htmlFor="sg-ci-send-to">
          Send to
        </label>
        <div className="sg-ci-send-chips" onClick={() => document.getElementById("sg-ci-send-to")?.focus()}>
          {recipients.map((address) => (
            <span className="chip sg-ci-send-chip" key={address}>
              {address}
              <button
                type="button"
                className="btn-ghost"
                aria-label={`Remove ${address}`}
                onClick={() => setRecipients((prev) => prev.filter((item) => item !== address))}
              >
                ×
              </button>
            </span>
          ))}
          <input
            id="sg-ci-send-to"
            value={text}
            autoComplete="off"
            placeholder={recipients.length ? "Add another…" : "Name or email address"}
            onChange={(event) => {
              const next = event.target.value;
              if (/[,;]$/.test(next)) add(next);
              else setText(next);
            }}
            onKeyDown={(event) => {
              if (event.key === "Enter") {
                event.preventDefault();
                if (typedIsEmail) add(text);
                else if (suggestions[0]) {
                  setRecipients((prev) => [...prev, suggestions[0].email]);
                  setText("");
                }
              } else if (event.key === "Backspace" && !text && recipients.length) {
                setRecipients((prev) => prev.slice(0, -1));
              }
            }}
          />
        </div>
        {typedText && !typedIsEmail && !suggestions.length && (
          <p className="field-hint">No KubeSight user matches — type their full email address.</p>
        )}
        {suggestions.length > 0 && (
          <ul className="sg-ci-send-suggest" aria-label="KubeSight users">
            {suggestions.map((person) => (
              <li key={person.email}>
                <button
                  type="button"
                  className="btn-ghost"
                  onClick={() => {
                    setRecipients((prev) => [...prev, person.email]);
                    setText("");
                  }}
                >
                  <span>{person.name}</span>
                  <code>{person.email}</code>
                </button>
              </li>
            ))}
          </ul>
        )}
        {(data.recipients || []).length > 0 && (
          <p className="field-hint">Pre-filled from this stage&apos;s quality gate. Change it freely — this send only.</p>
        )}

        <label className="form-label" htmlFor="sg-ci-send-note">
          Message <span className="muted">(optional)</span>
        </label>
        <textarea
          id="sg-ci-send-note"
          className="sg-ci-send-note"
          rows={3}
          maxLength={2000}
          value={note}
          placeholder="Please fix the ERROR findings before the next release."
          onChange={(event) => setNote(event.target.value)}
        />

        {error && <p className="banner-message error">{error}</p>}

        <div className="sg-ci-send-actions">
          <button type="button" className="btn-outline btn-compact" onClick={onDownload}>
            Download PDF
          </button>
          <span className="sg-ci-send-spacer" />
          <button type="button" className="btn-outline btn-compact" onClick={onClose} disabled={busy}>
            Cancel
          </button>
          <button
            type="button"
            className="primary btn-compact"
            onClick={send}
            disabled={busy || !data.emailConfigured || !pendingCount}
          >
            {busy ? "Sending…" : pendingCount > 1 ? `Send to ${pendingCount} people` : "Send"}
          </button>
        </div>
      </div>
    </div>,
    document.body
  );
}
