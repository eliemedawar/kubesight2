import { useEffect, useRef, useState } from "react";

/**
 * An identifier the operator will need to paste somewhere else.
 *
 * Cluster ids, job ids, request ids and image digests all end up in a ticket, a
 * kubectl command, or a message to a colleague. Selecting them by hand from a
 * table cell is error-prone in the specific way that matters: a truncated
 * digest looks like a digest.
 *
 * Falls back to a plain element when the clipboard API is unavailable — it
 * requires a secure context, and self-hosted installations on plain HTTP are
 * exactly this product's deployment story.
 */
export default function CopyableId({ value, label, truncate = 0, className = "" }) {
  const [copied, setCopied] = useState(false);
  const timerRef = useRef(null);

  useEffect(() => () => window.clearTimeout(timerRef.current), []);

  if (!value) {
    return <span className="muted">—</span>;
  }

  const text = String(value);
  const shown = truncate > 0 && text.length > truncate ? `${text.slice(0, truncate)}…` : text;
  const canCopy = typeof navigator !== "undefined" && Boolean(navigator.clipboard?.writeText);

  if (!canCopy) {
    return (
      <code className={`copyable-id ${className}`.trim()} title={text}>
        {shown}
      </code>
    );
  }

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.clearTimeout(timerRef.current);
      timerRef.current = window.setTimeout(() => setCopied(false), 1600);
    } catch {
      // Permission denied or a non-secure context that still exposed the API.
      // Leaving the value visible and selectable is the useful fallback.
      setCopied(false);
    }
  };

  return (
    <button
      type="button"
      className={`copyable-id copyable-id--button ${className}`.trim()}
      onClick={copy}
      title={`Copy ${label || "value"}: ${text}`}
      aria-label={`Copy ${label || "value"}`}
    >
      <code>{shown}</code>
      <span className="copyable-id-state" aria-live="polite">
        {copied ? "Copied" : ""}
      </span>
    </button>
  );
}
