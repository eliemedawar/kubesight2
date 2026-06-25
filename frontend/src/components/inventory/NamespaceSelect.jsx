import { useEffect, useState } from "react";
import { listNamespacesByCluster } from "../../api/clustersApi.js";
import SearchableSelect from "../common/SearchableSelect.jsx";

export default function NamespaceSelect({
  clusterId,
  value,
  onChange,
  required = true,
  disabled = false,
}) {
  const [namespaces, setNamespaces] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");

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

  const placeholder = !clusterId
    ? "Select a cluster first"
    : loading
      ? "Loading namespaces..."
      : namespaces.length
        ? "Select namespace"
        : "No namespaces available";

  return (
    <>
      <SearchableSelect
        required={required}
        value={value}
        onChange={onChange}
        disabled={disabled || loading || !clusterId || (!loading && !namespaces.length)}
      >
        <option value="">{placeholder}</option>
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
