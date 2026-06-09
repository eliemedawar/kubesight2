import LoadingState from "./LoadingState.jsx";
import { SCOPE_LOADING_HINT } from "../../utils/accessViewState.js";

const PAGE_LABELS = {
  dashboard: "Loading dashboard...",
  clusters: "Loading clusters...",
  clusterManagement: "Loading cluster management...",
  clusterOverview: "Loading cluster overview...",
  inventory: "Loading applications...",
  applicationDetails: "Loading application details...",
  namespaces: "Loading namespaces...",
  resources: "Loading resources...",
  logs: "Loading logs...",
  alerts: "Loading alerts...",
  alertPolicies: "Loading alert policies...",
  alertRouting: "Loading alert routing...",
  upgrade: "Loading upgrade center...",
  userManagement: "Loading user management...",
  auditLogs: "Loading audit logs...",
  settings: "Loading settings...",
};

export default function RouteLoadingFallback({ pageKey, label }) {
  return (
    <LoadingState
      label={label || PAGE_LABELS[pageKey] || "Loading page..."}
      hint={SCOPE_LOADING_HINT}
    />
  );
}
