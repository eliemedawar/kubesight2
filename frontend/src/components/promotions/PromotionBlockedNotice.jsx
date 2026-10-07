import { useState } from "react";

import "../../styles/signal/promotions.css";
import { requestPromotionException } from "../../api/promotionsApi.js";
import { PrIcon } from "./icons.jsx";

const MIN_REASON = 10;

/**
 * What a deploy form shows when the promotion ladder refused the deploy (409
 * with `data.promotion`): why, where to go instead, and — when the form still
 * holds the manifest — a way to ask approvers for an exception right here.
 */
export default function PromotionBlockedNotice({ verdict, changes, onRequested }) {
  const [asking, setAsking] = useState(false);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [sent, setSent] = useState(null);

  const previous = verdict?.previous?.name || "the previous environment";
  const canAsk = Array.isArray(changes) && changes.length > 0 && changes.every((c) => c.yaml);

  const submit = async () => {
    setBusy(true);
    setError("");
    try {
      const data = await requestPromotionException({ reason: reason.trim(), changes });
      setSent(data);
      onRequested?.(data);
    } catch (err) {
      setError(err.message || "Could not send it for approval.");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="pr-root pr-blocked" role="alert">
      <div className="pr-rule pr-rule--danger">
        <PrIcon.Stop />
        <div className="pr-blocked-copy">
          <strong>Refused by the promotion ladder</strong>
          <span>{verdict?.message}</span>
          <span className="pr-blocked-links">
            <a className="pr-link" href="#/promotions/board">
              Open Promotions
            </a>
            {canAsk && !asking && !sent && (
              <button type="button" className="btn-ghost pr-link" onClick={() => setAsking(true)}>
                Ask for an exception
              </button>
            )}
          </span>
        </div>
      </div>
      {sent ? (
        <p className="pr-callout pr-callout--info" role="status">
          <PrIcon.Hand />
          <span>{sent.message}</span>
        </p>
      ) : (
        asking && (
          <div className="pr-exception">
            <label className="pr-field">
              <span>Why does this have to skip {previous}?</span>
              <textarea
                rows={3}
                value={reason}
                onChange={(event) => setReason(event.target.value)}
                placeholder="Approvers read this — e.g. the incident it fixes and why it cannot wait."
              />
              <small>Someone other than you approves it; it deploys by itself once they do.</small>
            </label>
            {error && <p className="pr-text-danger">{error}</p>}
            <div className="pr-picker-foot">
              <button type="button" className="btn-outline" onClick={() => setAsking(false)} disabled={busy}>
                Cancel
              </button>
              <button
                type="button"
                className="primary"
                onClick={submit}
                disabled={busy || reason.trim().length < MIN_REASON}
              >
                {busy ? "Sending…" : "Request exception"}
              </button>
            </div>
          </div>
        )
      )}
    </div>
  );
}

/** The ladder's verdict on a failed deploy request, or null. */
export function promotionRefusal(err) {
  return err?.status === 409 && err?.data?.promotion ? err.data.promotion : null;
}
