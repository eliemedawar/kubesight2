import { useCallback, useEffect, useMemo, useState } from "react";
import { getResourceListByType } from "../../api";
import {
  ALLOWED_ACTIONS,
  BROWSER_RESOURCE_TYPES,
  selectableActionsForRole,
} from "../../lib/accessActions";
import { countSelectedResources } from "../../lib/accessRulesForm";
import { emptyNamespaceResourceBucket } from "../../lib/resourceTypes";

export default function ResourceBrowser({
  clusterId,
  clusterLabel,
  resourceAccess,
  onResourceAccessChange,
  namespaces,
  namespacesLoading,
  namespacesError,
  selectedRole,
  disabled,
}) {
  const selectableActions = useMemo(
    () => selectableActionsForRole(selectedRole),
    [selectedRole]
  );
  const [search, setSearch] = useState("");
  const [resourcesCache, setResourcesCache] = useState({});
  const [loadingResources, setLoadingResources] = useState(false);
  const [resourceError, setResourceError] = useState("");

  const pickerNs = resourceAccess.pickerNamespace || "";
  const pickerType = resourceAccess.pickerResourceType || "pod";
  const listKey = BROWSER_RESOURCE_TYPES.find((t) => t.value === pickerType)?.listKey || "pods";

  const cacheKey = `${clusterId}:${pickerNs}`;
  const loadedResources = resourcesCache[cacheKey];

  const loadResources = useCallback(async () => {
    if (!clusterId || !pickerNs) return;
    setLoadingResources(true);
    setResourceError("");
    try {
      const data = await getResourceListByType(clusterId, pickerNs, listKey);
      setResourcesCache((prev) => ({
        ...prev,
        [cacheKey]: { ...(prev[cacheKey] || emptyNamespaceResourceBucket()), ...data },
      }));
    } catch (err) {
      setResourceError(err.message);
    } finally {
      setLoadingResources(false);
    }
  }, [clusterId, pickerNs, cacheKey, listKey]);

  useEffect(() => {
    if (pickerNs && clusterId) {
      loadResources();
    }
  }, [pickerNs, clusterId, listKey, loadResources]);

  const bucket = resourceAccess.namespaces[pickerNs] || emptyNamespaceResourceBucket();
  const selectedList = bucket[listKey] || [];

  const availableItems = useMemo(() => {
    if (!loadedResources) return [];
    const items = loadedResources[listKey] || [];
    return items.map((item) => (typeof item === "string" ? item : item.name)).filter(Boolean);
  }, [loadedResources, listKey]);

  const filteredItems = useMemo(() => {
    const q = search.trim().toLowerCase();
    if (!q) return availableItems;
    return availableItems.filter((name) => name.toLowerCase().includes(q));
  }, [availableItems, search]);

  const toggleResource = (name) => {
    const ns = pickerNs;
    const next = { ...resourceAccess, namespaces: { ...resourceAccess.namespaces } };
    const b = { ...emptyNamespaceResourceBucket(), ...(next.namespaces[ns] || {}) };
    const list = [...(b[listKey] || [])];
    const idx = list.indexOf(name);
    if (idx >= 0) list.splice(idx, 1);
    else list.push(name);
    b[listKey] = list;
    next.namespaces[ns] = b;
    onResourceAccessChange(next);
  };

  const selectAll = () => {
    const ns = pickerNs;
    const next = { ...resourceAccess, namespaces: { ...resourceAccess.namespaces } };
    const b = { ...emptyNamespaceResourceBucket(), ...(next.namespaces[ns] || {}) };
    b[listKey] = [...new Set([...(b[listKey] || []), ...filteredItems])];
    next.namespaces[ns] = b;
    onResourceAccessChange(next);
  };

  const clearAll = () => {
    const ns = pickerNs;
    const next = { ...resourceAccess, namespaces: { ...resourceAccess.namespaces } };
    const b = { ...emptyNamespaceResourceBucket(), ...(next.namespaces[ns] || {}) };
    b[listKey] = [];
    next.namespaces[ns] = b;
    onResourceAccessChange(next);
  };

  const toggleAction = (actionId) => {
    const current = new Set(resourceAccess.allowedActions || []);
    if (current.has(actionId)) current.delete(actionId);
    else current.add(actionId);
    onResourceAccessChange({
      ...resourceAccess,
      allowedActions: [...current],
    });
  };

  const counts = countSelectedResources(resourceAccess);

  return (
    <div className="resource-browser">
      <p className="eyebrow">Browse resources — {clusterLabel}</p>
      <div className="resource-browser__hierarchy form-grid">
        <label>
          Namespace
          <select
            value={pickerNs}
            disabled={disabled || namespacesLoading}
            onChange={(e) =>
              onResourceAccessChange({
                ...resourceAccess,
                pickerNamespace: e.target.value,
              })
            }
          >
            <option value="">Select namespace</option>
            {namespaces.map((ns) => {
              const name = ns.name || ns;
              return (
                <option key={name} value={name}>
                  {name}
                </option>
              );
            })}
          </select>
        </label>
        <label>
          Resource type
          <select
            value={pickerType}
            disabled={disabled || !pickerNs}
            onChange={(e) =>
              onResourceAccessChange({
                ...resourceAccess,
                pickerResourceType: e.target.value,
              })
            }
          >
            {BROWSER_RESOURCE_TYPES.map((t) => (
              <option key={t.value} value={t.value}>
                {t.label}
              </option>
            ))}
          </select>
        </label>
      </div>

      {namespacesError ? <p className="banner-message error">{namespacesError}</p> : null}
      {!pickerNs ? (
        <p className="muted">Choose a namespace to load resources from this cluster.</p>
      ) : (
        <>
          <div className="resource-browser__toolbar">
            <input
              type="search"
              placeholder="Search resources…"
              value={search}
              onChange={(e) => setSearch(e.target.value)}
              disabled={disabled || loadingResources}
            />
            <button type="button" className="btn-outline" disabled={disabled} onClick={selectAll}>
              Select all
            </button>
            <button type="button" className="btn-outline" disabled={disabled} onClick={clearAll}>
              Clear all
            </button>
          </div>

          {loadingResources ? <p className="muted">Loading {pickerType}s…</p> : null}
          {resourceError ? <p className="banner-message error">{resourceError}</p> : null}

          {!loadingResources && !resourceError ? (
            <div className="resource-browser__list">
              {filteredItems.length ? (
                filteredItems.map((name) => (
                  <label key={name} className="resource-browser__item">
                    <input
                      type="checkbox"
                      checked={selectedList.includes(name)}
                      disabled={disabled}
                      onChange={() => toggleResource(name)}
                    />
                    {name}
                  </label>
                ))
              ) : (
                <p className="muted">No {pickerType}s found in this namespace.</p>
              )}
            </div>
          ) : null}

          <div className="resource-counts">
            <span>Pods selected: {counts.pods}</span>
            <span>Deployments selected: {counts.deployments}</span>
            <span>Services selected: {counts.services}</span>
          </div>

          <fieldset className="allowed-actions-fieldset">
            <legend>Allowed actions</legend>
            <p className="muted">What this user can do with the selected resources.</p>
            <div className="allowed-actions-grid">
              {selectableActions.map((action) => (
                <label key={action.id} className="allowed-action-card">
                  <input
                    type="checkbox"
                    checked={(resourceAccess.allowedActions || []).includes(action.id)}
                    disabled={disabled}
                    onChange={() => toggleAction(action.id)}
                  />
                  <span>
                    <strong>{action.label}</strong>
                    <span className="muted">{action.description}</span>
                  </span>
                </label>
              ))}
            </div>
          </fieldset>
        </>
      )}
    </div>
  );
}
