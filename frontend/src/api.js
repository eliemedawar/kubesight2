/** @deprecated Import from ./api/* modules; kept for backward compatibility. */
export { setUnauthorizedHandler, request, getBaseUrl } from "./api/client";
export { login, logout, fetchCurrentUser } from "./api/authApi";
export {
  listUsers,
  getUser,
  createUser,
  updateUser,
  disableUser,
  deleteUser,
  listUserAccessRules,
  replaceUserAccessRules,
  listRoles,
  getRole,
  createRole,
  updateRole,
  deleteRole,
  listPermissions,
  updateRolePermissions,
  listAuditLogs,
} from "./api/usersApi";
export {
  listClusters,
  listCustomClusters,
  createCustomCluster,
  updateCustomCluster,
  deleteCustomCluster,
  testCustomCluster,
  getClusterOverview,
  listNamespacesByCluster,
  getResourcesByClusterNamespace,
  getResourceListByType,
  getLogs,
  listNamespacePodsForLogs,
  listPodContainers,
  getContainerLogs,
} from "./api/clustersApi";
export { listAlerts, testAlertEmail } from "./api/alertsApi";
export {
  getAlertPolicyCatalog,
  listAlertPolicies,
  createAlertPolicy,
  updateAlertPolicy,
  deleteAlertPolicy,
  setAlertPolicyEnabled,
  listAlertHistory,
  getAlertPolicyStats,
} from "./api/alertPoliciesApi";
export { getSettings, updateSettings } from "./api/settingsApi";
export { getDashboardSummary } from "./api/dashboardApi";
export { listInventory, getInventoryDetail } from "./api/inventoryApi";
export { getUpgradeInfo, getUpgradeJob, runUpgradePrecheck, startUpgrade } from "./api/upgradesApi";
export {
  listApplicationServices,
  getApplicationService,
  createApplicationService,
  updateApplicationService,
  deleteApplicationService,
  listPickerDeployments,
  listPickerPods,
} from "./api/applicationServicesApi";
export {
  listClients,
  getClient,
  createClient,
  updateClient,
  deleteClient,
} from "./api/clientsApi";
export {
  createDeploymentRequest,
  listDeploymentRequests,
  approveDeploymentRequest,
  declineDeploymentRequest,
  getDeploymentRequestRecipients,
  updateDeploymentRequestRecipients,
} from "./api/deploymentRequestsApi";
