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
    # 0.7.x covers Kubernetes 1.27+ and so every minor the builder offers.
    # Kubernetes 1.34 and newer need Metrics Server 0.9.x — pin that release
    # before listing those minors here.
    versions=("0.7.2",),
    supported_k8s_minors=("1.29", "1.30", "1.31", "1.32"),
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
