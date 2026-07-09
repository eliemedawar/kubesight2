/** @deprecated Import from ./api/* modules; kept for backward compatibility. */
export { setUnauthorizedHandler, request, getBaseUrl } from "./api/client";
export {
  login,
  logout,
  fetchCurrentUser,
  changeTemporaryPassword,
  setupTotp,
  verifyFirstLoginTotp,
  verifyLoginMfa,
} from "./api/authApi";
export {
  listUsers,
  getUser,
  createUser,
  updateUser,
  disableUser,
  deleteUser,
  resendTemporaryPassword,
  resetUserMfa,
  unlockUser,
  resetFailedAttempts,
  forcePasswordReset,
  lockUser,
  enableUser,
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
  exportAuditLogs,
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
  listNamespaceMetricsByCluster,
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
  listPickerWorkloads,
} from "./api/applicationServicesApi";
export {
  listClients,
  getClient,
  createClient,
  updateClient,
  deleteClient,
  listTransportTypes,
  listClientServices,
  saveClientServiceConnection,
  getClientServiceTopology,
  deleteClientServiceConnection,
} from "./api/clientsApi";
export {
  listComponents,
  getComponent,
  createComponent,
  updateComponent,
  deleteComponent,
  checkComponentHealth,
} from "./api/topologyComponentsApi";
export {
  createDeploymentRequest,
  listDeploymentRequests,
  listMyDeploymentRequests,
  approveDeploymentRequest,
  declineDeploymentRequest,
  getClusterDeployEligibility,
  getDeploymentRequestRecipients,
  updateDeploymentRequestRecipients,
} from "./api/deploymentRequestsApi";
