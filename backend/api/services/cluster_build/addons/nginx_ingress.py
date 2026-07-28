"""Maintained F5/NGINX Ingress Controller add-on."""

from __future__ import annotations

from .base import AddonDescriptor


NGINX_INGRESS = AddonDescriptor(
    id="nginx-ingress",
    display_name="NGINX Ingress Controller",
    description=(
        "Maintained NGINX Ingress Controller with a NodePort service for "
        "VM and bare-metal clusters."
    ),
    support_tier="supported",
    versions=("5.5.4",),
    manifest_files=(
        "crds.yaml",
        "ns-and-sa.yaml",
        "rbac.yaml",
        "nginx-config.yaml",
        "ingress-class.yaml",
        "nginx-ingress.yaml",
        "nodeport.yaml",
    ),
    manifest_urls=(
        "https://raw.githubusercontent.com/nginx/kubernetes-ingress/"
        "v{version}/deploy/crds.yaml",
        "https://raw.githubusercontent.com/nginx/kubernetes-ingress/"
        "v{version}/deployments/common/ns-and-sa.yaml",
        "https://raw.githubusercontent.com/nginx/kubernetes-ingress/"
        "v{version}/deployments/rbac/rbac.yaml",
        "https://raw.githubusercontent.com/nginx/kubernetes-ingress/"
        "v{version}/deployments/common/nginx-config.yaml",
        "https://raw.githubusercontent.com/nginx/kubernetes-ingress/"
        "v{version}/deployments/common/ingress-class.yaml",
        "https://raw.githubusercontent.com/nginx/kubernetes-ingress/"
        "v{version}/deployments/deployment/nginx-ingress.yaml",
        "https://raw.githubusercontent.com/nginx/kubernetes-ingress/"
        "v{version}/deployments/service/nodeport.yaml",
    ),
    manifest_sha256={
        "5.5.4": (
            "9a79a8233162a61fc16820c17c62ec6c33f2c1666b69814f30603ef4e0b93098",
            "699c7e4fa9992baa0d30c979d59ac44f23d87dcaacc32cd5748e81bbc0d70f4f",
            "1f40827042a0f26f7d79f7fecce70b0a43e80cb38e260650df1c4fdad9605920",
            "c232ef3d5bd84abe4d40c9a4b86a3f390ea2ec71434b0028d91d6c5bb28e163e",
            "b720fb1fe9b5beac1e347a5bb0859eafa1b2fba492d4698789fe13043a3dfa6e",
            "e76d4d5369e57d27e21f37aaa3760028910e91c89e4e19b050a3802d460565fd",
            "c5d28abea9be0631e1bfe33ab6b2f7b7cff808f943225ae7b187fbb390fc829c",
        ),
    },
    readiness_commands=(
        "-n nginx-ingress rollout status deployment/nginx-ingress --timeout=900s",
    ),
    # NGINX publishes an explicit matrix: NIC 5.5.4 supports Kubernetes
    # 1.29-1.36, spanning every minor this builder offers.
    k8s_minors_by_version={
        "5.5.4": ("1.29", "1.30", "1.31", "1.32", "1.34", "1.35", "1.36"),
    },
)
