import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { listNamespacesByCluster } from "../../api";
import {
  accessRulesToGrants,
  countSelectedResources,
  emptyClusterGrant,
  emptyResourceAccess,
  grantsToAccessRules,
} from "../../lib/accessRulesForm";
import {
  filterActionIdsForRole,
  FULL_CLUSTER_ACTION_IDS,
  NAMESPACE_DEFAULT_ACTION_IDS,
} from "../../lib/accessActions";
import { getClusterEnvironment, clusterStatusTone } from "../../lib/userAccessForm";
import ResourceBrowser from "./ResourceBrowser";

function ClusterStatusBadge({ status }) {
  if (!status) return <span className="status-pill info">Unknown</span>;
  return <span className={`status-pill ${clusterStatusTone(status)}`}>{status}</span>;
}

function AccessWarning({ children }) {
  return <p className="access-warning banner-message">{children}</p>;
}

export default function AccessRulesEditor({
  clusters,
  clusterGrants,
  onClusterGrantsChange,
  selectedRole,
  disabled,
}) {
  const [namespaceCache, setNamespaceCache] = useState({});
  const loadedRef = useRef(new Set());

  const loadNamespaces = useCallback(async (clusterId) => {
    if (loadedRef.current.has(clusterId)) return;
    loadedRef.current.add(clusterId);
    setNamespaceCache((p) => ({
      ...p,
      [clusterId]: { loading: true, items: [], error: "" },
    }));
    try {
      const data = await listNamespacesByCluster(clusterId);
      setNamespaceCache((p) => ({
        ...p,
        [clusterId]: { loading: false, items: data.items || [], error: "" },
      }));
    } catch (err) {
      setNamespaceCache((p) => ({
        ...p,
        [clusterId]: { loading: false, items: [], error: err.message },
      }));
    }
  }, []);

  const updateGrant = (clusterId, patch) => {
    onClusterGrantsChange((prev) => {
      const current = prev[clusterId] || emptyClusterGrant(clusterId);
      return {
        ...prev,
        [clusterId]: { ...current, ...patch, clusterId },
      };
    });
  };

  const resourceAccessForMode = (grant, mode) => {
    const rolePerms = selectedRole?.permissions ?? null;
    if (mode === "resources") {
      return grant.resourceAccess?.namespaces ? grant.resourceAccess : emptyResourceAccess();
    }
    if (mode === "full") {
      return {
        ...emptyResourceAccess(),
        allowedActions: filterActionIdsForRole(FULL_CLUSTER_ACTION_IDS, rolePerms),
      };
    }
    return {
      ...emptyResourceAccess(),
      allowedActions: filterActionIdsForRole(NAMESPACE_DEFAULT_ACTION_IDS, rolePerms),
    };
  };

  const setAllowed = (clusterId, allowed) => {
    onClusterGrantsChange((prev) => {
      const next = { ...prev };
      if (!allowed) {
        delete next[clusterId];
        return next;
      }
      next[clusterId] = {
        ...emptyClusterGrant(clusterId),
        allowed: true,
        mode: "full",
        namespaces: [],
        resourceAccess: resourceAccessForMode({}, "full"),
        clusterId,
      };
      return next;
    });
  };

  const setMode = (clusterId, mode) => {
    onClusterGrantsChange((prev) => {
      const grant = prev[clusterId] || emptyClusterGrant(clusterId);
      return {
        ...prev,
        [clusterId]: {
          ...grant,
          mode,
          namespaces: mode === "namespaces" ? grant.namespaces || [] : [],
          resourceAccess: resourceAccessForMode(grant, mode),
          clusterId,
        },
      };
    });
    if (mode === "namespaces" || mode === "resources") {
      loadNamespaces(clusterId);
    }
  };

  const toggleNamespace = (clusterId, ns) => {
    onClusterGrantsChange((prev) => {
      const grant = prev[clusterId] || emptyClusterGrant(clusterId);
      const list = grant.namespaces || [];
      const adding = !list.includes(ns);
      const next = adding ? [...list, ns] : list.filter((n) => n !== ns);
      const allowedActions = new Set(grant.resourceAccess?.allowedActions || []);
      if (adding && grant.mode === "namespaces" && !allowedActions.has("view_resources")) {
        allowedActions.add("view_resources");
      }
      return {
        ...prev,
        [clusterId]: {
          ...grant,
          namespaces: next,
          clusterId,
          resourceAccess: {
            ...grant.resourceAccess,
            allowedActions: [...allowedActions],
          },
        },
      };
    });
  };

  useEffect(() => {
    Object.values(clusterGrants).forEach((g) => {
      if (g?.allowed && (g.mode === "namespaces" || g.mode === "resources")) {
        loadNamespaces(g.clusterId);
      }
    });
  }, [clusterGrants, loadNamespaces]);

  return (
    <div className="access-rules-editor">
      <section className="form-section">
        <h4>Cluster access</h4>
        <p className="muted">
          Choose how much of each cluster this user can see. You do not need to know technical permission names.
        </p>
        <div className="cluster-access-list">
          {clusters.map((cluster) => {
            const grant = clusterGrants[cluster.id] || emptyClusterGrant(cluster.id);
            const nsState = namespaceCache[cluster.id] || {
              loading: false,
              items: [],
              error: "",
            };
            return (
              <article
                key={cluster.id}
                className={`cluster-access-card ${grant.allowed ? "is-selected" : ""}`}
              >
                <div className="cluster-access-card__header">
                  <div className="cluster-access-card__title">
                    <strong>{cluster.name || cluster.id}</strong>
                    <span className="muted cluster-access-card__id">{cluster.id}</span>
                    <span className="muted cluster-access-card__env">
                      {getClusterEnvironment(cluster)}
                    </span>
                  </div>
                  <ClusterStatusBadge status={cluster.status} />
                </div>

                <label className="access-toggle-row">
                  <input
                    type="checkbox"
                    checked={grant.allowed}
                    disabled={disabled}
                    onChange={(e) => setAllowed(cluster.id, e.target.checked)}
                  />
                  Allow access to this cluster
                </label>

                {grant.allowed ? (
                  <div className="access-mode-options">
                    <label className="access-mode-option">
                      <input
                        type="radio"
                        name={`mode-${cluster.id}`}
                        checked={grant.mode === "full"}
                        disabled={disabled}
                        onChange={() => setMode(cluster.id, "full")}
                      />
                      Full cluster access
                    </label>
                    {grant.mode === "full" ? (
                      <AccessWarning>
                        This user will have access to all namespaces and resources in this cluster.
                      </AccessWarning>
                    ) : null}

                    <label className="access-mode-option">
                      <input
                        type="radio"
                        name={`mode-${cluster.id}`}
                        checked={grant.mode === "namespaces"}
                        disabled={disabled}
                        onChange={() => setMode(cluster.id, "namespaces")}
                      />
                      Namespace access
                    </label>
                    {grant.mode === "namespaces" ? (
                      <>
                        <AccessWarning>
                          This user will have access to all resources inside the selected namespaces.
                        </AccessWarning>
                        <div className="namespace-picker">
                          <p className="eyebrow">Select namespaces</p>
                          {nsState.loading ? (
                            <p className="muted">Loading namespaces…</p>
                          ) : null}
                          {nsState.error ? (
                            <p className="banner-message error">{nsState.error}</p>
                          ) : null}
                          <div className="namespace-picker__list">
                            {nsState.items.map((ns) => {
                              const name = ns.name || ns;
                              return (
                                <label key={name} className="namespace-picker__item">
                                  <input
                                    type="checkbox"
                                    checked={(grant.namespaces || []).includes(name)}
                                    disabled={disabled}
                                    onChange={() => toggleNamespace(cluster.id, name)}
                                  />
                                  {name}
                                </label>
                              );
                            })}
                          </div>
                        </div>
                      </>
                    ) : null}

                    <label className="access-mode-option">
                      <input
                        type="radio"
                        name={`mode-${cluster.id}`}
                        checked={grant.mode === "resources"}
                        disabled={disabled}
                        onChange={() => setMode(cluster.id, "resources")}
                      />
                      Specific resource access
                    </label>
                    {grant.mode === "resources" ? (
                      <ResourceBrowser
                        clusterId={cluster.id}
                        clusterLabel={cluster.name || cluster.id}
                        resourceAccess={grant.resourceAccess || emptyResourceAccess()}
                        onResourceAccessChange={(ra) =>
                          updateGrant(cluster.id, { resourceAccess: ra })
                        }
                        namespaces={nsState.items}
                        namespacesLoading={nsState.loading}
                        namespacesError={nsState.error}
                        selectedRole={selectedRole}
                        disabled={disabled}
                      />
                    ) : null}
                  </div>
                ) : null}
              </article>
            );
          })}
        </div>
      </section>
    </div>
  );
}

export { grantsToAccessRules, accessRulesToGrants, countSelectedResources };
