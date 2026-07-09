import { useState } from "react";
import { IconCheck, IconCopy } from "./icons.jsx";

// Generic ok/failed/never pill for backend status strings ("ok" | "error" | null).
export function StatusPill({ status, okLabel = "OK", failLabel = "Failed", neverLabel = "Never" }) {
  if (!status) return <span className="status-pill muted">{neverLabel}</span>;
  const ok = status === "ok";
  return (
    <span className={`status-pill ${ok ? "ok" : "danger"}`}>{ok ? okLabel : failLabel}</span>
  );
}

// Copy-to-clipboard button with a 2s "Copied" confirmation.
export function CopyButton({ text, label = "Copy URL", className = "" }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
    } catch {
      /* clipboard unavailable — nothing sensible to fall back to */
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  };
  return (
    <button type="button" className={`secondary sg-zh-copy ${className}`} onClick={copy}>
      {copied ? <IconCheck /> : <IconCopy />}
      {copied ? "Copied" : label}
    </button>
  );
}

// "4 min ago" style relative timestamps for the command bar / flow strip / feed.
export function timeAgo(iso) {
  if (!iso) return "";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "";
  const mins = Math.round((Date.now() - then) / 60000);
  if (mins < 1) return "just now";
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours} h ago`;
  const days = Math.round(hours / 24);
  return `${days} d ago`;
}

// Minutes until the next scheduled sync, or null when unknown / overdue.
export function minutesToNextSync(config) {
  if (!config?.enabled || !config?.lastSyncAt) return null;
  const interval = config.syncIntervalMinutes || 30;
  const last = new Date(config.lastSyncAt).getTime();
  if (Number.isNaN(last)) return null;
  const mins = Math.round((last + interval * 60000 - Date.now()) / 60000);
  return mins > 0 ? mins : null;
}
