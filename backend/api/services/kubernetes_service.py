"""Kubernetes subprocess operations — delegates to k8s_provider."""

from .. import k8s_provider

__all__ = [
    "is_real_mode_enabled",
    "should_use_real_k8s",
    "resolve_cluster_access",
    "run_upgrade_precheck_k8s",
    "run_upgrade_start_k8s",
    "pod_logs_from_k8s",
    "namespace_events_from_k8s",
]

is_real_mode_enabled = k8s_provider.is_real_mode_enabled
should_use_real_k8s = k8s_provider.should_use_real_k8s
resolve_cluster_access = k8s_provider.resolve_cluster_access
run_upgrade_precheck_k8s = k8s_provider.run_upgrade_precheck_k8s
run_upgrade_start_k8s = k8s_provider.run_upgrade_start_k8s
pod_logs_from_k8s = k8s_provider.pod_logs_from_k8s
namespace_events_from_k8s = k8s_provider.namespace_events_from_k8s
