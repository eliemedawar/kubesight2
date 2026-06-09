/** Normalize cluster dropdown entries to { id, name }. */
export function normalizeClusterOptions(options = []) {
  return options.map((option) => {
    if (option && typeof option === "object") {
      const id = option.id || option.clusterId || option.cluster || "";
      return { id, name: option.name || id };
    }
    const id = String(option || "");
    return { id, name: id };
  });
}

export function clusterOptionLabel(cluster) {
  if (!cluster) {
    return "";
  }
  if (cluster.name && cluster.name !== cluster.id) {
    return cluster.name;
  }
  return cluster.id || cluster.name || "";
}
