import { useEffect, useState } from "react";
import { listNamespacesByCluster } from "../../api/clustersApi.js";
import SearchableSelect from "../common/SearchableSelect.jsx";

const CREATE_OPTION = "__create__";

export default function NamespaceSelect({
  clusterId,
  value,
  onChange,
  required = true,
  disabled = false,
  allowCreate = true,
}) {
  const [namespaces, setNamespaces] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const [creating, setCreating] = useState(false);

  useEffect(() => {
    if (!clusterId) {
      setNamespaces([]);
      setLoading(false);
      setError("");
      return undefined;
    }

    let cancelled = false;
    setLoading(true);
    setError("");

    listNamespacesByCluster(clusterId)
      .then((data) => {
        if (cancelled) {
          return;
        }
        const names = (data.items || [])
          .map((ns) => (typeof ns === "string" ? ns : ns?.name))
          .filter(Boolean)
          .sort();
        setNamespaces(names);
      })
      .catch((err) => {
        if (!cancelled) {
          setNamespaces([]);
          setError(err.message || "Failed to load namespaces");
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoading(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [clusterId]);

  // Reset create mode when the cluster changes.
  useEffect(() => {
    setCreating(false);
  }, [clusterId]);

  const placeholder = !clusterId
    ? "Select a cluster first"
    : loading
      ? "Loading namespaces..."
      : namespaces.length
        ? "Select namespace"
        : "No namespaces available";

  const emitValue = (next) => onChange({ target: { value: next } });

  const handleSelectChange = (event) => {
    if (event.target.value === CREATE_OPTION) {
      setCreating(true);
      emitValue("");
      return;
    }
    onChange(event);
  };

  if (creating) {
    return (
      <>
        <input
          type="text"
          value={value || ""}
          placeholder="new-namespace"
          required={required}
          disabled={disabled || !clusterId}
          autoFocus
          onChange={onChange}
          pattern="[a-z0-9]([-a-z0-9]*[a-z0-9])?"
          title="Lowercase letters, numbers and hyphens (RFC 1123)"
        />
        <button
          type="button"
          className="link-button namespace-select-hint"
          onClick={() => { setCreating(false); emitValue(""); }}
          style={{ background: "none", border: "none", padding: 0, cursor: "pointer" }}
        >
          Choose existing namespace instead
        </button>
        {namespaces.includes((value || "").trim()) ? (
          <p className="muted namespace-select-hint">This namespace already exists — it will be reused.</p>
        ) : null}
      </>
    );
  }

  return (
    <>
      <SearchableSelect
        required={required}
        value={value}
        onChange={handleSelectChange}
        disabled={disabled || loading || !clusterId}
      >
        <option value="">{placeholder}</option>
        {allowCreate && clusterId ? (
          <option value={CREATE_OPTION}>+ Create new namespace…</option>
        ) : null}
        {namespaces.map((name) => (
          <option key={name} value={name}>
            {name}
          </option>
        ))}
      </SearchableSelect>
      {error ? <p className="muted namespace-select-hint">{error}</p> : null}
    </>
  );
}
