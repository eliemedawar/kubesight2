/**
 * Derive admin-granted allowed actions from persisted access rules.
 * Mirrors accessRulesForm.js / backend access_summary.py for nav visibility.
 */

import {
  FULL_CLUSTER_ACTION_IDS,
  NAMESPACE_DEFAULT_ACTION_IDS,
} from "./accessActions";
import {
  accessRulesToGrants,
  effectiveActionIdsForGrant,
} from "./accessRulesForm";

function legacyClusterIds(user) {
  return new Set(user.clusterAccess || []);
}

function legacyNamespacePairs(user) {
  return new Set(
    (user.namespaceAccess || []).map((row) => `${row.clusterId}\0${row.namespace}`)
  );
}

function roleLike(user) {
  return { name: user.role, permissions: user.permissions || [] };
}

function legacyGrantsFromUser(user) {
  const grants = {};
  const clusterIds = [...legacyClusterIds(user)];

  clusterIds.forEach((clusterId) => {
    const namespaces = [...legacyNamespacePairs(user)]
      .filter((pair) => pair.startsWith(`${clusterId}\0`))
      .map((pair) => pair.split("\0")[1]);

    if (namespaces.length) {
      grants[clusterId] = {
        clusterId,
        allowed: true,
        mode: "namespaces",
        namespaces,
        resourceAccess: { allowedActions: [...NAMESPACE_DEFAULT_ACTION_IDS] },
      };
    } else {
      grants[clusterId] = {
        clusterId,
        allowed: true,
        mode: "full",
        namespaces: [],
        resourceAccess: { allowedActions: [...FULL_CLUSTER_ACTION_IDS] },
      };
    }
  });

  return grants;
}

function actionIdsFromGrants(grants, user) {
  const role = roleLike(user);
  const actionIds = new Set();
  Object.values(grants).forEach((grant) => {
    effectiveActionIdsForGrant(grant, role).forEach((id) => actionIds.add(id));
  });
  return [...actionIds];
}

/** Allowed action IDs granted by admin (not just role capabilities). */
export function getGrantedActionIds(user) {
  if (!user) {
    return [];
  }
  if (user.isAdmin === true || user.role === "admin") {
    return [...FULL_CLUSTER_ACTION_IDS];
  }

  const rules = user.accessRules || [];
  if (rules.length) {
    const clusterIds = [...new Set(rules.map((rule) => rule.clusterId).filter(Boolean))];
    const grants = accessRulesToGrants(rules, clusterIds);
    return actionIdsFromGrants(grants, user);
  }

  return actionIdsFromGrants(legacyGrantsFromUser(user), user);
}

export function hasGrantedAction(user, actionId) {
  if (!user) {
    return false;
  }
  if (user.isAdmin === true || user.role === "admin") {
    return true;
  }
  return getGrantedActionIds(user).includes(actionId);
}

/** Nav pages mapped to admin "Allowed Actions" checkboxes. */
export const PAGE_GRANTED_ACTIONS = {
  dashboard: "view_metrics",
  clusterOverview: "view_metrics",
  namespaces: "view_resources",
  inventory: "view_resources",
  applicationDetails: "view_resources",
  resources: "view_resources",
  logs: "view_logs",
  alerts: "view_alerts",
  alertPolicies: "view_alerts",
  upgrade: "upgrade_precheck",
};

export function pageGrantedByAdmin(user, pageKey) {
  const actionId = PAGE_GRANTED_ACTIONS[pageKey];
  if (!actionId) {
    return true;
  }
  return hasGrantedAction(user, actionId);
}
