/** Quick permission templates for the role editor. */

export const ROLE_TEMPLATE_OPTIONS = [
  { id: "viewer", label: "Read-only Viewer" },
  { id: "operator", label: "Operations Engineer" },
  { id: "cluster_admin", label: "Cluster Admin" },
  { id: "security_audit", label: "Security/Audit Viewer" },
  { id: "custom", label: "Custom" },
];

const VIEWER_PERMISSIONS = [
  "clusters:view",
  "overview:view",
  "namespaces:view",
  "resources:view",
  "inventory:view",
  "pods:view",
  "deployments:view",
  "replicasets:view",
  "statefulsets:view",
  "daemonsets:view",
  "jobs:view",
  "cronjobs:view",
  "logs:view",
  "alerts:view",
  "services:view",
  "services:ports:view",
  "helm:view",
  "app_services:view",
  "clients:view",
];

const OPERATOR_PERMISSIONS = [
  ...VIEWER_PERMISSIONS,
  "inventory:register",
  "apps:dryrun",
  "apps:diff",
  "alerts:manage",
  "upgrades:precheck",
];

const CLUSTER_ADMIN_PERMISSIONS = [
  ...OPERATOR_PERMISSIONS,
  "inventory:update",
  "inventory:remove",
  "apps:deploy",
  "helm:install",
  "helm:upgrade",
  "helm:rollback",
  "helm:values:view",
  "helm:values:update",
];

const SECURITY_AUDIT_PERMISSIONS = [
  "clusters:view",
  "overview:view",
  "namespaces:view",
  "resources:view",
  "pods:view",
  "deployments:view",
  "services:view",
  "logs:view",
  "alerts:view",
  "audit:view",
  "settings:view",
];

export const ROLE_TEMPLATE_PERMISSIONS = {
  viewer: VIEWER_PERMISSIONS,
  operator: OPERATOR_PERMISSIONS,
  cluster_admin: CLUSTER_ADMIN_PERMISSIONS,
  security_audit: SECURITY_AUDIT_PERMISSIONS,
  custom: [],
};

export function permissionsForTemplate(templateId) {
  return [...(ROLE_TEMPLATE_PERMISSIONS[templateId] || [])];
}
