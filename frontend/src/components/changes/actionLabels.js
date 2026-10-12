/** What each change-bundle action is called, for the drawer and the Change Bundles page. */

export const ACTION_OPTIONS = [
  { value: "edit_deployment", label: "Edit deployment (YAML)", needs: ["yaml"] },
  { value: "change_image", label: "Change image / tag", needs: ["image"] },
  { value: "scale_replicas", label: "Scale replicas", needs: ["replicas"] },
  { value: "update_env", label: "Update env variables (YAML)", needs: ["yaml"] },
  { value: "update_resources", label: "Update resource requests/limits (YAML)", needs: ["yaml"] },
  { value: "update_hpa", label: "Enable/disable or update HPA (YAML)", needs: ["yaml"] },
  { value: "delete_deployment", label: "Delete deployment", needs: [] },
];

export const ACTION_LABEL = {
  ...Object.fromEntries(ACTION_OPTIONS.map((o) => [o.value, o.label])),
  // Not offered here: KubeSight stages these itself when a direct change on an
  // approval-gated cluster is sent for approval.
  create_from_template: "Create from template",
  apply_yaml: "Apply YAML",
  restart_workload: "Restart",
  rollback_deployment: "Roll back deployment",
  edit_configmap: "Edit config map (YAML)",
  edit_secret: "Edit secret (YAML)",
  edit_ingress: "Edit ingress (YAML)",
  edit_resource: "Edit resource (YAML)",
  helm_install: "Helm install",
  helm_upgrade: "Helm upgrade",
  helm_rollback: "Helm rollback",
  helm_uninstall: "Helm uninstall",
};
