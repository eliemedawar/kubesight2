export const HEALTH_LABELS = {
  healthy: "Healthy",
  warning: "Warning",
  critical: "Critical",
  unknown: "Unknown",
};

export const HEALTH_ICONS = {
  healthy: "",
  warning: "",
  critical: "",
  unknown: "",
};

export const statusTone = (status) => {
  const value = String(status || "").toLowerCase();
  if (value === "healthy" || value === "passed" || value === "pass") {
    return "pass";
  }
  if (value === "warning") {
    return "warning";
  }
  if (value === "critical" || value === "failed" || value === "fail") {
    return "fail";
  }
  return "unknown";
};

export const formatDashboardTime = (isoValue) => {
  if (!isoValue) {
    return "—";
  }
  try {
    const date = new Date(isoValue);
    if (Number.isNaN(date.getTime())) {
      return "—";
    }
    return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  } catch {
    return "—";
  }
};

export const formatLatestVersion = (version) => {
  if (!version || version === "unknown") {
    return "Unknown";
  }
  return version;
};

export const utilizationTone = (metric) => {
  if (!metric?.available) {
    return "unknown";
  }
  return statusTone(metric.status);
};

export const utilizationIcon = (metric) => {
  if (!metric?.available) return null;
  return null;
};

export const formatUtilizationValue = (metric) => {
  if (!metric?.available || metric.percent == null) {
    return "Metrics unavailable";
  }
  return `${metric.percent}%`;
};

export const versionStatusTone = (status) => {
  if (status === "up_to_date") {
    return "pass";
  }
  if (status === "unknown") {
    return "unknown";
  }
  return "warning";
};

export const versionStatusDisplay = (version) => {
  if (!version) {
    return { icon: "", message: "Unknown" };
  }
  return {
    icon: "",
    message: version.statusMessage || version.statusLabel || "Unknown",
  };
};
