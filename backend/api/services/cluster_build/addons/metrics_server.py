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
    versions=("0.9.0", "0.7.2"),
    manifest_files=("components.yaml",),
    manifest_urls=(
        "https://github.com/kubernetes-sigs/metrics-server/releases/download/"
        "v{version}/components.yaml",
    ),
    manifest_sha256={
        "0.9.0": (
            "1cec29a5267809306a2c6ec74a3e449abbb705b4a8beed0c8a1963910f72c79b",
        ),
        "0.7.2": (
            "f103539a54ed72efe66616afc74a8bfaed651703cb3918797599046af5617441",
        ),
    },
    # Upstream's compatibility matrix: 0.9.x is the release documented for
    # Kubernetes 1.34+, while 0.7.x covers 1.27+ and is what the older minors
    # in this catalog were validated on.
    k8s_minors_by_version={
        "0.9.0": ("1.34", "1.35", "1.36"),
        "0.7.2": ("1.29", "1.30", "1.31", "1.32"),
    },
    readiness_commands=(
        "-n kube-system rollout status deployment/metrics-server --timeout=600s",
        "wait --for=condition=Available "
        "apiservice/v1beta1.metrics.k8s.io --timeout=600s",
    ),
)
