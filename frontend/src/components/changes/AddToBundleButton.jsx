import { useState } from "react";
import { useChangeBundle } from "../../context/ChangeBundleContext";

/**
 * Stages a change into the user's Change Bundle (the "cart") instead of applying
 * it immediately. Drop this next to an existing Apply/Deploy button and pass a
 * `descriptor` (or a `buildDescriptor()` that returns one) describing the change:
 *
 *   <AddToBundleButton descriptor={{ actionType: "edit_deployment", clusterId,
 *      clusterName, namespace, resourceName, yaml }} />
 */
export default function AddToBundleButton({
  descriptor,
  buildDescriptor,
  label = "Add to Bundle",
  className = "btn-secondary",
  disabled = false,
  onAdded,
}) {
  const { enabled, addAndOpen } = useChangeBundle();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  if (!enabled) return null;

  const handleClick = async () => {
    setBusy(true);
    setError("");
    try {
      const payload = buildDescriptor ? await buildDescriptor() : descriptor;
      if (!payload) {
        throw new Error("Nothing to add");
      }
      await addAndOpen(payload);
      if (onAdded) onAdded();
    } catch (err) {
      setError(err.message || "Failed to add to bundle");
    } finally {
      setBusy(false);
    }
  };

  return (
    <span style={{ display: "inline-flex", flexDirection: "column", gap: 4 }}>
      <button type="button" className={className} onClick={handleClick} disabled={busy || disabled}>
        {busy ? "Adding…" : label}
      </button>
      {error ? (
        <span className="muted" style={{ color: "#dc2626", fontSize: "0.72rem" }}>
          {error}
        </span>
      ) : null}
    </span>
  );
}
