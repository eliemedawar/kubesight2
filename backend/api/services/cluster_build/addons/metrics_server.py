"""Metrics Server add-on."""

from __future__ import annotations

from .base import AddonDescriptor


METRICS_SERVER = AddonDescriptor(
    id="metrics-server",
    display_name="Metrics Server",
    description=(
        "Resource metrics for KubeSight dashboards, kubectl top, and "
        "Horizontal Pod Autoscaling."
    ),
    support_tier="supported",
    # 0.7.x supports every Kubernetes version offered by the builder (1.29–1.32).
    versions=("0.7.2",),
    manifest_files=("components.yaml",),
    manifest_urls=(
        "https://github.com/kubernetes-sigs/metrics-server/releases/download/"
        "v{version}/components.yaml",
    ),
    manifest_sha256=(
        "f103539a54ed72efe66616afc74a8bfaed651703cb3918797599046af5617441",
    ),
    readiness_commands=(
        "-n kube-system rollout status deployment/metrics-server --timeout=600s",
        "wait --for=condition=Available "
        "apiservice/v1beta1.metrics.k8s.io --timeout=600s",
    ),
)
