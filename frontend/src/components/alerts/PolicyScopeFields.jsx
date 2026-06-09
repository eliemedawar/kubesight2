import { useEffect, useState } from "react";
import { getResourceListByType } from "../../api/clustersApi.js";
import NamespaceSelect from "../inventory/NamespaceSelect.jsx";

export const ALL_RESOURCES_VALUE = "*";

const TARGET_LIST_KEYS = {
  deployment: "deployments",
  pod: "pods",
};

const ALL_RESOURCES_LABELS = {
  deployment: "All Deployments",
  pod: "All Pods",
};

function normalizeScope(scope) {
  const type = scope?.type === "pod" ? "pod" : "deployment";
  const namespace = scope?.namespace || "";
  let resourceName = scope?.resourceName || ALL_RESOURCES_VALUE;
  if (!resourceName || scope?.type === "cluster" || scope?.type === "namespace") {
    resourceName = ALL_RESOURCES_VALUE;
  }
  return { type, namespace, resourceName };
}

export default function PolicyScopeFields({ clusterId, scope, onChange, disabled = false }) {
  const normalized = normalizeScope(scope);
  const [resources, setResources] = useState([]);
  const [loadingResources, setLoadingResources] = useState(false);
  const [resourceError, setResourceError] = useState("");

  const listKey = TARGET_LIST_KEYS[normalized.type] || "deployments";

  useEffect(() => {
    if (!clusterId || !normalized.namespace) {
      setResources([]);
      setLoadingResources(false);
      setResourceError("");
      return undefined;
    }

    let cancelled = false;
    setLoadingResources(true);
    setResourceError("");

    getResourceListByType(clusterId, normalized.namespace, listKey)
      .then((data) => {
        if (cancelled) {
          return;
        }
        const items = (data[listKey] || [])
          .map((item) => (typeof item === "string" ? item : item?.name))
          .filter(Boolean)
          .sort((a, b) => a.localeCompare(b));
        setResources(items);
      })
      .catch((err) => {
        if (!cancelled) {
          setResources([]);
          setResourceError(err.message || "Failed to load resources");
        }
      })
      .finally(() => {
        if (!cancelled) {
          setLoadingResources(false);
        }
      });

    return () => {
      cancelled = true;
    };
  }, [clusterId, normalized.namespace, listKey]);

  const updateScope = (patch) => {
    onChange({ ...normalized, ...patch });
  };

  const handleTargetChange = (event) => {
    updateScope({
      type: event.target.value,
      resourceName: ALL_RESOURCES_VALUE,
    });
  };

  const handleNamespaceChange = (event) => {
    updateScope({
      namespace: event.target.value,
      resourceName: ALL_RESOURCES_VALUE,
    });
  };

  const handleResourceChange = (event) => {
    updateScope({ resourceName: event.target.value });
  };

  const resourcePlaceholder = !clusterId
    ? "Select a cluster first"
    : !normalized.namespace
      ? "Select a namespace first"
      : loadingResources
        ? "Loading resources..."
        : "Select resource";

  const allLabel = ALL_RESOURCES_LABELS[normalized.type] || "All Resources";

  return (
    <div className="alert-policy-form-grid">
      <label>
        Target
        <select
          value={normalized.type}
          onChange={handleTargetChange}
          disabled={disabled}
          required
        >
          <option value="deployment">Deployment</option>
          <option value="pod">Pod</option>
        </select>
      </label>
      <label>
        Namespace
        <NamespaceSelect
          clusterId={clusterId}
          value={normalized.namespace}
          onChange={handleNamespaceChange}
          disabled={disabled}
          required
        />
      </label>
      <label>
        Resource
        <select
          value={normalized.resourceName || ALL_RESOURCES_VALUE}
          onChange={handleResourceChange}
          disabled={disabled || !clusterId || !normalized.namespace || loadingResources}
          required
        >
          <option value="">{resourcePlaceholder}</option>
          <option value={ALL_RESOURCES_VALUE}>{allLabel}</option>
          {resources.map((name) => (
            <option key={name} value={name}>
              {name}
            </option>
          ))}
        </select>
        {resourceError ? <p className="muted namespace-select-hint">{resourceError}</p> : null}
      </label>
    </div>
  );
}

export { normalizeScope };
