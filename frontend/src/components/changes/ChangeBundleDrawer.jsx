import { useEffect, useMemo, useState } from "react";
import { useChangeBundle } from "../../context/ChangeBundleContext";
import { diffBundleItem } from "../../api/changeBundlesApi";

const ACTION_OPTIONS = [
  { value: "edit_deployment", label: "Edit deployment (YAML)", needs: ["yaml"] },
  { value: "change_image", label: "Change image / tag", needs: ["image"] },
  { value: "scale_replicas", label: "Scale replicas", needs: ["replicas"] },
  { value: "update_env", label: "Update env variables (YAML)", needs: ["yaml"] },
  { value: "update_resources", label: "Update resource requests/limits (YAML)", needs: ["yaml"] },
  { value: "update_hpa", label: "Enable/disable or update HPA (YAML)", needs: ["yaml"] },
  { value: "delete_deployment", label: "Delete deployment", needs: [] },
];

const ACTION_LABEL = Object.fromEntries(ACTION_OPTIONS.map((o) => [o.value, o.label]));

const STATUS_COLORS = {
  valid: "#16a34a",
  invalid: "#dc2626",
  pending: "#94a3b8",
};

function ActionBadge({ type }) {
  return (
    <span
      style={{
        fontSize: "0.7rem",
        fontWeight: 600,
        color: "#38bdf8",
        background: "rgba(56,189,248,0.12)",
        border: "1px solid rgba(56,189,248,0.3)",
        borderRadius: 6,
        padding: "2px 8px",
        whiteSpace: "nowrap",
      }}
    >
      {ACTION_LABEL[type] || type}
    </span>
  );
}

export default function ChangeBundleDrawer() {
  const { enabled, isOpen, closeDrawer, bundle, removeItem, submit } = useChangeBundle();
  const [note, setNote] = useState("");
  const [windowStart, setWindowStart] = useState("");
  const [windowEnd, setWindowEnd] = useState("");
  const [stopOnFailure, setStopOnFailure] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [expanded, setExpanded] = useState(null);
  const [diffOpen, setDiffOpen] = useState(null);
  const [diffCache, setDiffCache] = useState({});

  const toggleDiff = async (item) => {
    if (diffOpen === item.id) {
      setDiffOpen(null);
      return;
    }
    setDiffOpen(item.id);
    if (diffCache[item.id] === undefined) {
      setDiffCache((c) => ({ ...c, [item.id]: "Loading diff…" }));
      try {
        const res = await diffBundleItem(bundle.id, item.id);
        setDiffCache((c) => ({ ...c, [item.id]: res.diff || "No differences." }));
      } catch (err) {
        setDiffCache((c) => ({ ...c, [item.id]: `Diff failed: ${err.message}` }));
      }
    }
  };

  const timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone || "local time";
  const pad = (n) => String(n).padStart(2, "0");
  const nowLocal = useMemo(() => {
    const d = new Date();
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(
      d.getMinutes()
    )}`;
  }, []);

  useEffect(() => {
    if (isOpen) {
      setNote(bundle?.note || "");
      setError("");
    }
  }, [isOpen, bundle?.note]);

  if (!enabled || !isOpen) return null;

  const items = bundle?.items || [];
  const hasInvalid = items.some((i) => i.validationStatus === "invalid");
  const missingWindow = !windowStart || !windowEnd;
  const startInPast = windowStart && new Date(windowStart) <= new Date();
  const endBeforeStart = windowStart && windowEnd && new Date(windowEnd) <= new Date(windowStart);
  const windowError = startInPast
    ? "The start time must be in the future."
    : endBeforeStart
      ? "The end time must be after the start time."
      : "";
  const canSubmit = items.length > 0 && !hasInvalid && !missingWindow && !windowError && !busy;

  const handleSubmit = async () => {
    if (!canSubmit) return;
    setBusy(true);
    setError("");
    try {
      await submit({
        note,
        windowStart: new Date(windowStart).toISOString(),
        windowEnd: new Date(windowEnd).toISOString(),
        windowTimezone: timeZone,
        stopOnFailure,
      });
      setWindowStart("");
      setWindowEnd("");
      setNote("");
    } catch (submitError) {
      setError(submitError.message || "Failed to submit bundle");
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="modal-overlay" role="presentation" onClick={closeDrawer}>
      <aside
        role="dialog"
        aria-modal="true"
        aria-label="Change bundle"
        onClick={(e) => e.stopPropagation()}
        style={{
          position: "fixed",
          top: 0,
          right: 0,
          height: "100vh",
          width: "min(560px, 100vw)",
          background: "var(--surface, #1e293b)",
          borderLeft: "1px solid var(--border, #334155)",
          boxShadow: "-12px 0 30px rgba(0,0,0,.35)",
          display: "flex",
          flexDirection: "column",
          overflow: "hidden",
        }}
      >
        <header
          className="modal-header"
          style={{ padding: "var(--space-3)", borderBottom: "1px solid var(--border,#334155)" }}
        >
          <div>
            <h3 style={{ margin: 0 }}>Change Bundle</h3>
            <p className="muted" style={{ margin: "2px 0 0", fontSize: "0.8rem" }}>
              {items.length} change{items.length === 1 ? "" : "s"} staged — review and submit for approval
            </p>
          </div>
          <button type="button" className="modal-close" onClick={closeDrawer} aria-label="Close">
            <svg width="14" height="14" viewBox="0 0 20 20" fill="currentColor" aria-hidden="true">
              <path
                fillRule="evenodd"
                d="M4.293 4.293a1 1 0 011.414 0L10 8.586l4.293-4.293a1 1 0 111.414 1.414L11.414 10l4.293 4.293a1 1 0 01-1.414 1.414L10 11.414l-4.293 4.293a1 1 0 01-1.414-1.414L8.586 10 4.293 5.707a1 1 0 010-1.414z"
                clipRule="evenodd"
              />
            </svg>
          </button>
        </header>

        <div style={{ flex: 1, overflowY: "auto", padding: "var(--space-3)" }}>
          {items.length === 0 ? (
            <p className="muted">
              No changes staged yet. Use the "Add to Bundle" button from the Resources or Inventory
              screens (Edit YAML, Deploy, Scale, etc.) to stage changes here.
            </p>
          ) : (
            <ul style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: "var(--space-2)" }}>
              {items.map((item) => (
                <li
                  key={item.id}
                  className="card"
                  style={{ padding: "var(--space-2) var(--space-3)" }}
                >
                  <div style={{ display: "flex", alignItems: "center", gap: 8, justifyContent: "space-between" }}>
                    <ActionBadge type={item.actionType} />
                    <span
                      style={{ fontSize: "0.72rem", color: STATUS_COLORS[item.validationStatus] || "#94a3b8" }}
                    >
                      {item.validationStatus === "invalid" ? "✕ invalid" : item.validationStatus === "valid" ? "✓ valid" : "…"}
                    </span>
                  </div>
                  <div style={{ marginTop: 4, fontSize: "0.85rem" }}>
                    <strong>{item.clusterName || item.clusterId}</strong>
                    {item.namespace ? ` / ${item.namespace}` : ""}
                    {item.resourceName ? ` / ${item.resourceKind} ${item.resourceName}` : ""}
                  </div>
                  {item.validationMessage ? (
                    <p className="muted" style={{ margin: "4px 0 0", fontSize: "0.75rem", color: "#dc2626" }}>
                      {item.validationMessage}
                    </p>
                  ) : null}
                  <div style={{ marginTop: 6, display: "flex", gap: 12 }}>
                    {item.yamlPreview ? (
                      <button
                        type="button"
                        className="btn-text"
                        style={{ fontSize: "0.78rem", padding: 0 }}
                        onClick={() => setExpanded(expanded === item.id ? null : item.id)}
                      >
                        {expanded === item.id ? "Hide preview" : "View preview"}
                      </button>
                    ) : null}
                    <button
                      type="button"
                      className="btn-text"
                      style={{ fontSize: "0.78rem", padding: 0 }}
                      onClick={() => toggleDiff(item)}
                    >
                      {diffOpen === item.id ? "Hide diff" : "View diff"}
                    </button>
                    <button
                      type="button"
                      className="btn-text"
                      style={{ fontSize: "0.78rem", padding: 0, color: "#dc2626" }}
                      onClick={() => removeItem(item.id)}
                    >
                      Remove
                    </button>
                  </div>
                  {expanded === item.id && item.yamlPreview ? (
                    <pre
                      style={{
                        marginTop: 8,
                        maxHeight: 240,
                        overflow: "auto",
                        background: "#0f172a",
                        color: "#e2e8f0",
                        border: "1px solid #334155",
                        borderRadius: 8,
                        padding: 10,
                        fontSize: "0.72rem",
                        whiteSpace: "pre-wrap",
                        wordBreak: "break-word",
                      }}
                    >
                      {item.yamlPreview}
                    </pre>
                  ) : null}
                  {diffOpen === item.id ? (
                    <pre
                      style={{
                        marginTop: 8,
                        maxHeight: 240,
                        overflow: "auto",
                        background: "#0f172a",
                        color: "#e2e8f0",
                        border: "1px solid #334155",
                        borderRadius: 8,
                        padding: 10,
                        fontSize: "0.72rem",
                        whiteSpace: "pre-wrap",
                        wordBreak: "break-word",
                      }}
                    >
                      {diffCache[item.id] ?? "Loading diff…"}
                    </pre>
                  ) : null}
                </li>
              ))}
            </ul>
          )}
        </div>

        <footer style={{ borderTop: "1px solid var(--border,#334155)", padding: "var(--space-3)" }}>
          <div className="form-grid">
            <label className="form-grid__full">
              Note / reason
              <textarea
                value={note}
                onChange={(e) => setNote(e.target.value)}
                rows={2}
                placeholder="Why is this change needed?"
              />
            </label>
            <label>
              Window start
              <input
                type="datetime-local"
                value={windowStart}
                min={nowLocal}
                onChange={(e) => setWindowStart(e.target.value)}
              />
            </label>
            <label>
              Window end
              <input
                type="datetime-local"
                value={windowEnd}
                min={windowStart || nowLocal}
                onChange={(e) => setWindowEnd(e.target.value)}
              />
            </label>
            <label
              className="form-grid__full"
              style={{ flexDirection: "row", alignItems: "center", gap: 8, color: "#ef4444", fontWeight: 600 }}
            >
              <input
                type="checkbox"
                checked={stopOnFailure}
                onChange={(e) => setStopOnFailure(e.target.checked)}
                style={{ width: "auto" }}
              />
              Stop executing remaining changes after the first failure (recommended)
            </label>
            <p className="form-grid__full muted" style={{ margin: 0, fontSize: "0.75rem" }}>
              Times are in your timezone ({timeZone}). The bundle must be approved before the window
              starts; approved changes apply automatically when the window opens.
            </p>
          </div>
          {windowError ? <p className="banner-message error">{windowError}</p> : null}
          {hasInvalid ? (
            <p className="banner-message error">Fix or remove invalid changes before submitting.</p>
          ) : null}
          {error ? <p className="banner-message error">{error}</p> : null}
          <div className="modal-actions" style={{ marginTop: "var(--space-2)" }}>
            <button type="button" className="btn-text" onClick={closeDrawer} disabled={busy}>
              Close
            </button>
            <button type="button" className="btn-primary" disabled={!canSubmit} onClick={handleSubmit}>
              {busy ? "Submitting…" : "Submit for Approval"}
            </button>
          </div>
        </footer>
      </aside>
    </div>
  );
}
