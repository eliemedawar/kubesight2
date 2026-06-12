/**
 * Access grant state ↔ backend accessRules (allow-only in UI).
 */

import {
  actionPermissions,
  ALLOWED_ACTIONS,
  DEFAULT_ALLOWED_ACTIONS,
  filterActionIdsForRole,
  FULL_CLUSTER_ACTION_IDS,
  NAMESPACE_DEFAULT_ACTION_IDS,
  selectableActionsForRole,
} from "./accessActions";
import { roleDescription } from "./rolePresets";
import { BROWSER_RESOURCE_TYPES } from "./accessActions";
import { emptyNamespaceResourceBucket } from "./resourceTypes";

export function emptyResourceAccess() {
  return {
    namespaces: {},
    allowedActions: [...DEFAULT_ALLOWED_ACTIONS],
    pickerNamespace: "",
    pickerResourceType: "pod",
  };
}

export function emptyClusterGrant(clusterId) {
  return {
    clusterId,
    allowed: false,
    mode: "full",
    namespaces: [],
    resourceAccess: emptyResourceAccess(),
  };
}

function ensureNsBucket(resourceAccess, namespace) {
  if (!resourceAccess.namespaces[namespace]) {
    resourceAccess.namespaces[namespace] = emptyNamespaceResourceBucket();
  }
  return resourceAccess.namespaces[namespace];
}

function rulesForCluster(allowRules, clusterId) {
  return allowRules.filter((rule) => rule.clusterId === clusterId);
}

function syncAllowedActionsFromRules(grant, clusterRules) {
  const actionIds = new Set();
  clusterRules.forEach((rule) => {
    ALLOWED_ACTIONS.forEach((action) => {
      if (action.permissions.includes(rule.permissionKey)) {
        actionIds.add(action.id);
      }
    });
  });
  if (!actionIds.size) {
    return;
  }
  grant.resourceAccess = {
    ...grant.resourceAccess,
    allowedActions: [...actionIds],
  };
}

function parseClusterGrant(clusterId, allowRules) {
  const grant = emptyClusterGrant(clusterId);
  const clusterRules = rulesForCluster(allowRules, clusterId);
  if (!clusterRules.length) {
    return grant;
  }

  grant.allowed = true;

  const namedResourceRules = clusterRules.filter(
    (rule) =>
      ["pod", "deployment", "service"].includes(rule.resourceType) &&
      rule.resourceName &&
      rule.namespace
  );
  if (namedResourceRules.length) {
    grant.mode = "resources";
    namedResourceRules.forEach((rule) => {
      const bucket = ensureNsBucket(grant.resourceAccess, rule.namespace);
      const listKey =
        rule.resourceType === "pod"
          ? "pods"
          : rule.resourceType === "deployment"
            ? "deployments"
            : "services";
      if (!bucket[listKey].includes(rule.resourceName)) {
        bucket[listKey].push(rule.resourceName);
      }
      grant.resourceAccess.pickerNamespace = grant.resourceAccess.pickerNamespace || rule.namespace;
      grant.resourceAccess.pickerResourceType = rule.resourceType;
    });
    syncAllowedActionsFromRules(grant, clusterRules);
    return grant;
  }

  const namespaceRules = clusterRules.filter((rule) => rule.namespace);
  if (namespaceRules.length) {
    const namespaces = [...new Set(namespaceRules.map((rule) => rule.namespace).filter(Boolean))];
    const namespaceScopedOnly = namespaceRules.every(
      (rule) => rule.resourceType === "namespace" || !rule.resourceName
    );
    if (namespaceScopedOnly && namespaces.length) {
      grant.mode = "namespaces";
      grant.namespaces = namespaces;
      syncAllowedActionsFromRules(grant, clusterRules);
      grant.resourceAccess = {
        ...grant.resourceAccess,
        allowedActions: ensureNamespaceResourceActions(
          grant.resourceAccess?.allowedActions || NAMESPACE_DEFAULT_ACTION_IDS
        ),
      };
      return grant;
    }
  }

  // Full cluster: cluster-level allow rules without namespace scoping
  if (clusterRules.some((rule) => !rule.namespace && rule.permissionKey === "clusters:view")) {
    grant.mode = "full";
    syncAllowedActionsFromRules(grant, clusterRules);
  }

  return grant;
}

/** Parse backend allow rules into editor grants (allow-only; deny rules ignored in UI). */
export function accessRulesToGrants(accessRules, clusterIds) {
  const grants = {};
  clusterIds.forEach((id) => {
    grants[id] = emptyClusterGrant(id);
  });

  const allowRules = (accessRules || []).filter((rule) => rule.effect !== "deny");
  const touchedClusterIds = new Set(
    allowRules.map((rule) => rule.clusterId).filter(Boolean)
  );

  touchedClusterIds.forEach((clusterId) => {
    grants[clusterId] = parseClusterGrant(clusterId, allowRules);
  });

  return grants;
}

function pushAllow(rules, row) {
  rules.push({ ...row, effect: "allow" });
}

function roleCanEmit(permissionKey, rolePermissions) {
  if (!rolePermissions?.length) {
    return true;
  }
  return rolePermissions.includes(permissionKey);
}

function ensureNamespaceResourceActions(actionIds) {
  const ids = [...actionIds];
  const onlyLogs = ids.length === 1 && ids[0] === "view_logs";
  if (!onlyLogs && ids.length > 0 && !ids.includes("view_resources")) {
    ids.unshift("view_resources");
  }
  return ids;
}

function ensureResourceViewActions(actionIds) {
  const ids = [...actionIds];
  if (!ids.includes("view_resources")) {
    ids.unshift("view_resources");
  }
  return ids;
}

function pushNamespaceViewResourceRules(rules, clusterId, namespace, rolePermissions) {
  actionPermissions(["view_resources"]).forEach((permissionKey) => {
    if (permissionKey === "namespaces:view" || !roleCanEmit(permissionKey, rolePermissions)) {
      return;
    }
    pushAllow(rules, {
      clusterId,
      namespace,
      resourceType: "namespace",
      permissionKey,
    });
  });
}

/** Strip action checkboxes that the role cannot use. */
export function sanitizeClusterGrantsForRole(clusterGrants, role) {
  const selectableIds = new Set(selectableActionsForRole(role).map((action) => action.id));
  const next = { ...clusterGrants };
  Object.entries(next).forEach(([clusterId, grant]) => {
    if (!grant?.resourceAccess?.allowedActions) {
      return;
    }
    next[clusterId] = {
      ...grant,
      resourceAccess: {
        ...grant.resourceAccess,
        allowedActions: grant.resourceAccess.allowedActions.filter((id) => selectableIds.has(id)),
      },
    };
  });
  return next;
}

/** Convert editor grants to backend accessRules (allow-only). */
export function grantsToAccessRules(clusterGrants, rolePermissions = null) {
  const rules = [];

  Object.values(clusterGrants).forEach((grant) => {
    if (grant?.allowed !== true) return;
    const cid = grant.clusterId;

    if (grant.mode === "full") {
      const actionIds = filterActionIdsForRole(FULL_CLUSTER_ACTION_IDS, rolePermissions);
      const permSet = new Set(actionPermissions(actionIds));
      (rolePermissions || []).forEach((perm) => permSet.add(perm));
      permSet.forEach((perm) => {
        if (!roleCanEmit(perm, rolePermissions)) {
          return;
        }
        pushAllow(rules, {
          clusterId: cid,
          resourceType: "cluster",
          permissionKey: perm,
        });
      });
      if (roleCanEmit("clusters:view", rolePermissions)) {
        pushAllow(rules, { clusterId: cid, resourceType: "cluster", permissionKey: "clusters:view" });
      }
      return;
    }

    if (roleCanEmit("clusters:view", rolePermissions)) {
      pushAllow(rules, {
        clusterId: cid,
        resourceType: "cluster",
        permissionKey: "clusters:view",
      });
    }

    if (grant.mode === "namespaces") {
      const nsActions = ensureNamespaceResourceActions(
        filterActionIdsForRole(
          grant.resourceAccess?.allowedActions || NAMESPACE_DEFAULT_ACTION_IDS,
          rolePermissions
        )
      );
      (grant.namespaces || []).forEach((ns) => {
        pushAllow(rules, {
          clusterId: cid,
          namespace: ns,
          resourceType: "namespace",
          permissionKey: "namespaces:view",
        });
        if (nsActions.includes("view_resources")) {
          pushNamespaceViewResourceRules(rules, cid, ns, rolePermissions);
        }
        if (nsActions.includes("view_logs") && roleCanEmit("logs:view", rolePermissions)) {
          pushAllow(rules, {
            clusterId: cid,
            namespace: ns,
            resourceType: "namespace",
            permissionKey: "logs:view",
          });
        }
        if (nsActions.includes("view_metrics") && roleCanEmit("overview:view", rolePermissions)) {
          pushAllow(rules, {
            clusterId: cid,
            namespace: ns,
            resourceType: "namespace",
            permissionKey: "overview:view",
          });
        }
        if (nsActions.includes("view_alerts") && roleCanEmit("alerts:view", rolePermissions)) {
          pushAllow(rules, {
            clusterId: cid,
            namespace: ns,
            resourceType: "namespace",
            permissionKey: "alerts:view",
          });
        }
        if (
          nsActions.includes("view_service_ports") &&
          roleCanEmit("services:ports:view", rolePermissions)
        ) {
          pushAllow(rules, {
            clusterId: cid,
            namespace: ns,
            resourceType: "namespace",
            permissionKey: "services:ports:view",
          });
        }
      });
      if (
        nsActions.includes("upgrade_precheck") &&
        roleCanEmit("upgrades:precheck", rolePermissions)
      ) {
        pushAllow(rules, {
          clusterId: cid,
          resourceType: "cluster",
          permissionKey: "upgrades:precheck",
        });
      }
      return;
    }

    if (grant.mode === "resources") {
      const nsMap = grant.resourceAccess?.namespaces || {};
      const hasSelectedResources = Object.values(nsMap).some((bucket) =>
        BROWSER_RESOURCE_TYPES.some((type) => (bucket[type.listKey] || []).length > 0)
      );
      let actions = filterActionIdsForRole(
        grant.resourceAccess?.allowedActions || DEFAULT_ALLOWED_ACTIONS,
        rolePermissions
      );
      if (hasSelectedResources) {
        actions = ensureResourceViewActions(actions);
      }
      Object.entries(nsMap).forEach(([ns, bucket]) => {
        const hasAny = BROWSER_RESOURCE_TYPES.some((type) => (bucket[type.listKey] || []).length > 0);
        if (!hasAny) return;

        if (roleCanEmit("namespaces:view", rolePermissions)) {
          pushAllow(rules, {
            clusterId: cid,
            namespace: ns,
            resourceType: "namespace",
            permissionKey: "namespaces:view",
          });
        }

        if (actions.includes("view_metrics") && roleCanEmit("overview:view", rolePermissions)) {
          pushAllow(rules, {
            clusterId: cid,
            namespace: ns,
            resourceType: "namespace",
            permissionKey: "overview:view",
          });
        }
        if (actions.includes("view_alerts") && roleCanEmit("alerts:view", rolePermissions)) {
          pushAllow(rules, {
            clusterId: cid,
            namespace: ns,
            resourceType: "namespace",
            permissionKey: "alerts:view",
          });
        }
        if (
          actions.includes("view_service_ports") &&
          roleCanEmit("services:ports:view", rolePermissions)
        ) {
          pushAllow(rules, {
            clusterId: cid,
            namespace: ns,
            resourceType: "namespace",
            permissionKey: "services:ports:view",
          });
        }

        (bucket.pods || []).forEach((name) => {
          if (actions.includes("view_resources") && roleCanEmit("pods:view", rolePermissions)) {
            pushAllow(rules, {
              clusterId: cid,
              namespace: ns,
              resourceType: "pod",
              resourceName: name,
              permissionKey: "pods:view",
            });
          }
          if (actions.includes("view_logs") && roleCanEmit("logs:view", rolePermissions)) {
            pushAllow(rules, {
              clusterId: cid,
              namespace: ns,
              resourceType: "pod",
              resourceName: name,
              permissionKey: "logs:view",
            });
          }
        });

        BROWSER_RESOURCE_TYPES.filter((type) => type.value !== "pod").forEach((type) => {
          (bucket[type.listKey] || []).forEach((name) => {
            if (
              actions.includes("view_resources") &&
              roleCanEmit(type.permissionKey, rolePermissions)
            ) {
              pushAllow(rules, {
                clusterId: cid,
                namespace: ns,
                resourceType: type.value,
                resourceName: name,
                permissionKey: type.permissionKey,
              });
            }
          });
        });
      });

      if (
        actions.includes("upgrade_precheck") &&
        roleCanEmit("upgrades:precheck", rolePermissions)
      ) {
        pushAllow(rules, {
          clusterId: cid,
          resourceType: "cluster",
          permissionKey: "upgrades:precheck",
        });
      }
    }
  });

  return dedupeRules(rules);
}

function dedupeRules(rules) {
  const seen = new Set();
  return rules.filter((r) => {
    const key = JSON.stringify({
      c: r.clusterId,
      n: r.namespace || "",
      t: r.resourceType,
      rn: r.resourceName || "",
      p: r.permissionKey,
    });
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

export function countSelectedResources(resourceAccess) {
  const nsMap = resourceAccess?.namespaces || {};
  const counts = Object.fromEntries(BROWSER_RESOURCE_TYPES.map((type) => [type.listKey, 0]));
  Object.values(nsMap).forEach((bucket) => {
    BROWSER_RESOURCE_TYPES.forEach((type) => {
      counts[type.listKey] += (bucket[type.listKey] || []).length;
    });
  });
  const total = Object.values(counts).reduce((sum, value) => sum + value, 0);
  return { ...counts, total };
}

/** Action IDs implied by a grant — mirrors grantsToAccessRules(). */
export function effectiveActionIdsForGrant(grant, role) {
  if (grant?.allowed !== true) {
    return [];
  }
  const rolePerms = role?.permissions ?? null;

  if (grant.mode === "full") {
    return filterActionIdsForRole(FULL_CLUSTER_ACTION_IDS, rolePerms);
  }

  if (grant.mode === "namespaces") {
    const stored = grant.resourceAccess?.allowedActions;
    const ids =
      stored?.length > 0 ? stored : NAMESPACE_DEFAULT_ACTION_IDS;
    return filterActionIdsForRole(ids, rolePerms);
  }

  if (grant.mode === "resources") {
    const counts = countSelectedResources(grant.resourceAccess);
    if (!counts.total) {
      return [];
    }
    return ensureResourceViewActions(
      filterActionIdsForRole(
        grant.resourceAccess?.allowedActions || DEFAULT_ALLOWED_ACTIONS,
        rolePerms
      )
    );
  }

  return [];
}

function namespaceBucketHasResources(bucket) {
  return BROWSER_RESOURCE_TYPES.some((type) => (bucket?.[type.listKey] || []).length > 0);
}

/** Merge persisted grants with the cluster list so preview matches the editor UI. */
export function mergeClusterGrantsForPreview(clusterGrants, clusters = []) {
  const merged = {};
  (clusters || []).forEach((cluster) => {
    const id = cluster.id;
    const raw = clusterGrants?.[id];
    merged[id] = {
      ...emptyClusterGrant(id),
      ...(raw || {}),
      clusterId: id,
      allowed: raw?.allowed === true,
    };
  });
  return merged;
}

/** Stable signature of which clusters are toggled on (for reactive preview). */
export function allowedClusterSnapshot(clusterGrants, clusters = []) {
  return (clusters || [])
    .map((c) => `${c.id}:${clusterGrants?.[c.id]?.allowed === true ? 1 : 0}`)
    .join("|");
}

export function buildEffectiveAccessPreview(
  clusterGrants,
  clustersById,
  selectedRole,
  clusters = []
) {
  const grantsMap = mergeClusterGrantsForPreview(clusterGrants, clusters);
  const clusterLines = [];
  const namespaces = [];
  const resources = [];
  const actionIdSet = new Set();

  Object.values(grantsMap).forEach((grant) => {
    if (grant.allowed !== true) return;

    effectiveActionIdsForGrant(grant, selectedRole).forEach((id) => actionIdSet.add(id));

    const label = clustersById[grant.clusterId]?.name || grant.clusterId;
    if (grant.mode === "full") {
      clusterLines.push(`${label} (all namespaces)`);
      return;
    }
    clusterLines.push(label);
    if (grant.mode === "namespaces") {
      (grant.namespaces || []).forEach((ns) => namespaces.push(ns));
    }
    if (grant.mode === "resources") {
      Object.entries(grant.resourceAccess?.namespaces || {}).forEach(([ns, bucket]) => {
        if (!namespaceBucketHasResources(bucket)) {
          return;
        }
        namespaces.push(ns);
        (bucket.pods || []).forEach((n) => resources.push(n));
        BROWSER_RESOURCE_TYPES.filter((type) => type.value !== "pod").forEach((type) => {
          (bucket[type.listKey] || []).forEach((n) => resources.push(`${n} (${type.label})`));
        });
      });
    }
  });

  const actionLabels = ALLOWED_ACTIONS.filter((a) => actionIdSet.has(a.id)).map((a) => a.label);

  let counts = { ...emptyNamespaceResourceBucket(), total: 0 };
  Object.values(grantsMap).forEach((g) => {
    if (g?.mode === "resources") {
      const c = countSelectedResources(g.resourceAccess);
      BROWSER_RESOURCE_TYPES.forEach((type) => {
        counts[type.listKey] += c[type.listKey] || 0;
      });
      counts.total += c.total;
    }
  });

  return {
    roleName: selectedRole?.name || "—",
    roleDescription: roleDescription(selectedRole?.name) || selectedRole?.description || "",
    clusters: [...new Set(clusterLines)],
    namespaces: [...new Set(namespaces)],
    resources: [...new Set(resources)],
    allowedActions: actionLabels,
    counts,
  };
}
