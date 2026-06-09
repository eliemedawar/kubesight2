import { evaluateAccess, hasPermission } from "./authz.js";

/** Compare encoded or decoded inventory ids from list/detail responses. */
export function inventoryIdsMatch(left, right) {
  if (!left || !right) {
    return !left && !right;
  }
  const normalize = (value) => {
    try {
      return decodeURIComponent(String(value));
    } catch {
      return String(value);
    }
  };
  return normalize(left) === normalize(right);
}

/** Matches backend make_inventory_id(cluster, namespace, name). */
export function resolveInventoryId(item) {
  if (!item) return "";
  if (item.id) return item.id;
  const clusterId = item.cluster || item.clusterId;
  const namespace = item.namespace;
  const name = item.name;
  if (clusterId && namespace && name) {
    return encodeURIComponent(`${clusterId}|${namespace}|${name}`);
  }
  return "";
}

export function getPrimaryWorkloadName(item) {
  if (!item) return "";
  if (item.workloadName) return item.workloadName;
  const names = item.workloadNames || [];
  return names[0] || "";
}

export function isDeploymentWorkload(item) {
  const type = String(item?.workloadType || "").toLowerCase();
  return type === "deployment";
}

export function hasHelmRelease(item) {
  return Boolean(item?.releaseName || item?.source === "Helm");
}

function clusterIdFromItem(item) {
  return item?.cluster || item?.clusterId || "";
}

export function canViewAppLogs(user, item) {
  if (!user || !item) return false;
  if (!hasPermission(user, "logs:view")) return false;
  const clusterId = clusterIdFromItem(item);
  const namespace = item.namespace;
  if (!clusterId || !namespace) return false;
  return evaluateAccess(user, {
    clusterId,
    namespace,
    permissionKey: "logs:view",
    resourceType: "namespace",
  });
}

export function canOperateDeployment(user, item) {
  if (!user || !item) return false;
  if (!hasPermission(user, "apps:deploy")) return false;
  if (!isDeploymentWorkload(item)) return false;
  const clusterId = clusterIdFromItem(item);
  const namespace = item.namespace;
  const workloadName = getPrimaryWorkloadName(item);
  if (!clusterId || !namespace || !workloadName) return false;
  return evaluateAccess(user, {
    clusterId,
    namespace,
    permissionKey: "apps:deploy",
    resourceType: "deployment",
    resourceName: workloadName,
  });
}

export function buildWorkloadActionPayload(item) {
  return {
    clusterId: clusterIdFromItem(item),
    namespace: item.namespace,
    workloadType: "deployment",
    workloadName: getPrimaryWorkloadName(item),
  };
}
