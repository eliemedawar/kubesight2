import { useEffect, useRef, useState } from "react";
import { useTicketingApi } from "../ticketing/TicketingContext.jsx";

/**
 * Pick where a dropdown's options come from: a source kind, an optional cascade
 * parent, and a live preview of exactly what the next sync would publish.
 *
 * Deliberately NOT wrapped in `.settings-form` — that class is a `grid auto-fit`
 * and would stretch these stacked blocks into columns.
 */
export default function ZohoOptionSourceForm({
  fieldId,
  fieldLabel,
  sources,
  parentsFor,
  value,
  onChange,
}) {
  const api = useTicketingApi();
  const { sourceKind, parentFieldId } = value;
  const kind = sources.find((s) => s.key === sourceKind);
  const parents = parentsFor(sourceKind);
  const [preview, setPreview] = useState(null);
  const [previewing, setPreviewing] = useState(false);
  const [previewError, setPreviewError] = useState("");

  // A parent only makes sense for a source that groups its values; drop a stale
  // one (e.g. loaded from a binding whose parent was since rebound) rather than
  // sending a combination the backend will reject. Keyed on the ids alone —
  // `parents` is a fresh array every render.
  const parentIds = parents.map((p) => p.fieldId).join(",");
  const changeRef = useRef(onChange);
  changeRef.current = onChange;
  useEffect(() => {
    if (parentFieldId && !parentIds.split(",").includes(parentFieldId)) {
      changeRef.current({ sourceKind, parentFieldId: "" });
    }
  }, [parentFieldId, parentIds, sourceKind]);

  useEffect(() => {
    if (!sourceKind) {
      setPreview(null);
      return undefined;
    }
    let cancelled = false;
    setPreviewing(true);
    setPreviewError("");
    api
      .previewFieldBinding(fieldId, { sourceKind, parentFieldId: parentFieldId || undefined })
      .then((data) => {
        if (cancelled) return;
        setPreview(data);
        setPreviewError(data?.error || "");
      })
      .catch((err) => {
        if (!cancelled) setPreviewError(err.message || "Could not resolve the source.");
      })
      .finally(() => {
        if (!cancelled) setPreviewing(false);
      });
    return () => {
      cancelled = true;
    };
  }, [fieldId, sourceKind, parentFieldId]);

  const values = preview?.values?.filter((v) => v !== "-None-") || [];
  const byParent = preview?.byParent || {};

  return (
    <div className="sg-zh-srcbox">
      <label className="sg-zh-form-full">
        Options come from
        <select
          value={sourceKind}
          onChange={(e) => onChange({ ...value, sourceKind: e.target.value, parentFieldId: "" })}
        >
          {sources.map((s) => (
            <option key={s.key} value={s.key}>
              {s.label}
            </option>
          ))}
        </select>
        {kind ? <span className="field-hint">{kind.description}</span> : null}
      </label>

      {kind?.parentKind ? (
        <label className="sg-zh-form-full">
          Filtered by (cascade parent)
          <select
            value={parentFieldId || ""}
            onChange={(e) => onChange({ ...value, parentFieldId: e.target.value })}
            disabled={!parents.length}
          >
            <option value="">No cascade — show every value</option>
            {parents.map((p) => (
              <option key={p.fieldId} value={p.fieldId}>
                {p.label}
              </option>
            ))}
          </select>
          <span className="field-hint">
            {parents.length
              ? `Picking a value there narrows “${fieldLabel}” on the ticket to the matching options.`
              : "No field on this layout is bound to the source these options group by, so there is nothing to cascade from yet."}
          </span>
        </label>
      ) : null}

      <div className="sg-zh-srcprev">
        <div className="sg-zh-srcprev-head">
          <span>Preview</span>
          <span className="muted">
            {previewing ? "resolving…" : `${values.length} option${values.length === 1 ? "" : "s"}`}
          </span>
        </div>
        {previewError ? (
          <p className="muted">{previewError}</p>
        ) : (
          <>
            <div className="sg-zh-preview-vals">
              {values.slice(0, 12).map((v) => (
                <span key={v} className="sg-tag">
                  {v}
                </span>
              ))}
              {!values.length && !previewing ? (
                <span className="muted">nothing to publish yet</span>
              ) : null}
              {values.length > 12 ? (
                <span className="sg-zh-more">+{values.length - 12} more</span>
              ) : null}
            </div>
            {parentFieldId && Object.keys(byParent).length ? (
              <p className="field-hint">
                Grouped into {Object.keys(byParent).length} parent value(s) — e.g.{" "}
                <code>
                  {Object.keys(byParent)[0]} → {(Object.values(byParent)[0] || []).join(", ")}
                </code>
              </p>
            ) : null}
          </>
        )}
      </div>
    </div>
  );
}
