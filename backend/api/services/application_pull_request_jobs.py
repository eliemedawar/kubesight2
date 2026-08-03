"""Locked-down Kubernetes Job launcher for guarded Bitbucket pull requests."""

from __future__ import annotations

import base64
import json
import os
import subprocess

from .application_analysis_jobs import _controlled_egress_proxy


def build_job_resources(
    *,
    pull_request_id: int,
    bundle: dict,
    write_token: str,
    credential_type: str,
    principal: str,
    callback_token: str,
    subdirectory: str,
) -> list[dict]:
    namespace = os.getenv("APPLICATION_ANALYSIS_NAMESPACE", "kubesight-analysis")
    image = os.getenv("APPLICATION_ANALYSIS_WORKER_IMAGE", "kubesight-backend:latest")
    name = f"ks-app-pr-{pull_request_id}"
    secret_name = f"{name}-credentials"
    labels = {
        "app.kubernetes.io/name": "kubesight-application-pull-request",
        "kubesight.io/pull-request-id": str(pull_request_id),
    }
    controlled_proxy = _controlled_egress_proxy("Build Verified")
    proxy_env = [
        {"name": "HTTPS_PROXY", "value": controlled_proxy[0]},
        {"name": "HTTP_PROXY", "value": controlled_proxy[0]},
        {
            "name": "NO_PROXY",
            "value": ".svc,.svc.cluster.local,localhost,127.0.0.1",
        },
    ]
    secret_values = {
        "write-token": write_token,
        "callback-token": callback_token,
        "credential-type": credential_type,
        "principal": principal,
        "request.json": json.dumps(bundle, separators=(",", ":")),
    }
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": secret_name, "namespace": namespace, "labels": labels},
        "type": "Opaque",
        "data": {
            key: base64.b64encode(value.encode()).decode()
            for key, value in secret_values.items()
        },
    }
    security = {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "runAsNonRoot": True,
        "runAsUser": 65532,
        "runAsGroup": 65532,
        "capabilities": {"drop": ["ALL"]},
    }
    volume_mounts = [
        {"name": "workspace", "mountPath": "/workspace"},
        {"name": "tmp", "mountPath": "/tmp"},
    ]
    credential_mount = {
        "name": "pr-request",
        "mountPath": "/run/kubesight-pr",
        "readOnly": True,
    }
    common_env = [
        {"name": "HOME", "value": "/tmp"},
        {"name": "TMPDIR", "value": "/tmp"},
        {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"},
        {"name": "PULL_REQUEST_ID", "value": str(pull_request_id)},
        {
            "name": "KUBESIGHT_PR_REQUEST_FILE",
            "value": "/run/kubesight-pr/request.json",
        },
        {
            "name": "BITBUCKET_WRITE_TOKEN_FILE",
            "value": "/run/kubesight-pr/write-token",
        },
        {
            "name": "KUBESIGHT_PR_CALLBACK_TOKEN_FILE",
            "value": "/run/kubesight-pr/callback-token",
        },
        {
            "name": "BITBUCKET_CREDENTIAL_TYPE",
            "valueFrom": {
                "secretKeyRef": {"name": secret_name, "key": "credential-type"}
            },
        },
        {
            "name": "BITBUCKET_PRINCIPAL",
            "valueFrom": {"secretKeyRef": {"name": secret_name, "key": "principal"}},
        },
        {
            "name": "KUBESIGHT_PR_CALLBACK_URL",
            "value": os.getenv(
                "APPLICATION_PULL_REQUEST_CALLBACK_URL",
                "http://kubesight-backend.kubesight.svc.cluster.local:5000/api/application-pull-request-worker",
            ),
        },
        *proxy_env,
    ]
    resources = {
        "requests": {"cpu": "250m", "memory": "512Mi"},
        "limits": {
            "cpu": os.getenv("APPLICATION_BUILD_CPU_LIMIT", "2"),
            "memory": os.getenv("APPLICATION_BUILD_MEMORY_LIMIT", "4Gi"),
            "ephemeral-storage": os.getenv(
                "APPLICATION_ANALYSIS_EPHEMERAL_LIMIT", "2Gi"
            ),
        },
    }

    def pr_container(action: str) -> dict:
        return {
            "name": f"pr-{action}",
            "image": image,
            "imagePullPolicy": os.getenv(
                "APPLICATION_ANALYSIS_IMAGE_PULL_POLICY", "IfNotPresent"
            ),
            "command": ["python", "-m", "api.application_pull_request_worker"],
            "env": [*common_env, {"name": "KUBESIGHT_PR_ACTION", "value": action}],
            "resources": resources,
            "securityContext": security,
            "volumeMounts": [*volume_mounts, credential_mount],
        }

    build_container = {
        "name": "credential-free-build-verifier",
        "image": image,
        "imagePullPolicy": os.getenv(
            "APPLICATION_ANALYSIS_IMAGE_PULL_POLICY", "IfNotPresent"
        ),
        "command": ["python", "-m", "api.application_build_verifier"],
        "env": [
            {"name": "HOME", "value": "/tmp"},
            {"name": "TMPDIR", "value": "/tmp"},
            {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"},
            {"name": "ANALYSIS_MODE", "value": "Build Verified"},
            {"name": "ANALYSIS_SUBDIRECTORY", "value": subdirectory},
            {
                "name": "APPLICATION_BUILD_NETWORK_POLICY",
                "value": "Controlled proxy unavailable to build process",
            },
        ],
        "resources": resources,
        "securityContext": security,
        "volumeMounts": volume_mounts,
    }
    job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": namespace, "labels": labels},
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": int(
                os.getenv("APPLICATION_ANALYSIS_DEADLINE_SECONDS", "1800")
            ),
            "ttlSecondsAfterFinished": int(
                os.getenv("APPLICATION_ANALYSIS_JOB_TTL_SECONDS", "900")
            ),
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "restartPolicy": "Never",
                    "serviceAccountName": os.getenv(
                        "APPLICATION_ANALYSIS_SERVICE_ACCOUNT",
                        "kubesight-analysis-worker",
                    ),
                    "automountServiceAccountToken": False,
                    "securityContext": {
                        "runAsNonRoot": True,
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "volumes": [
                        {
                            "name": "workspace",
                            "emptyDir": {
                                "sizeLimit": os.getenv(
                                    "APPLICATION_ANALYSIS_EPHEMERAL_LIMIT", "2Gi"
                                )
                            },
                        },
                        {"name": "tmp", "emptyDir": {"sizeLimit": "256Mi"}},
                        {
                            "name": "pr-request",
                            "secret": {"secretName": secret_name},
                        },
                    ],
                    "initContainers": [pr_container("prepare"), build_container],
                    "containers": [pr_container("publish")],
                },
            },
        },
    }
    policy = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": name, "namespace": namespace},
        "spec": {
            "podSelector": {"matchLabels": labels},
            "policyTypes": ["Ingress", "Egress"],
            "ingress": [],
            "egress": [
                {
                    "to": [
                        {
                            "namespaceSelector": {
                                "matchLabels": {
                                    "kubernetes.io/metadata.name": "kube-system"
                                }
                            }
                        }
                    ],
                    "ports": [
                        {"protocol": "UDP", "port": 53},
                        {"protocol": "TCP", "port": 53},
                    ],
                },
                {
                    "to": [
                        {
                            "namespaceSelector": {
                                "matchLabels": {
                                    "kubernetes.io/metadata.name": "kubesight"
                                }
                            }
                        }
                    ],
                    "ports": [{"protocol": "TCP", "port": 5000}],
                },
                {
                    "to": [{"ipBlock": {"cidr": controlled_proxy[1]}}],
                    "ports": [
                        {"protocol": "TCP", "port": controlled_proxy[2]}
                    ],
                },
            ],
        },
    }
    return [secret, policy, job]


def launch(resources: list[dict]) -> str:
    job = next(item for item in resources if item.get("kind") == "Job")
    command = ["kubectl"]
    kubeconfig = os.getenv("K8S_KUBECONFIG", "").strip()
    if kubeconfig:
        command.extend(["--kubeconfig", kubeconfig])
    command.extend(["apply", "-f", "-"])
    completed = subprocess.run(
        command,
        input=json.dumps(
            {"apiVersion": "v1", "kind": "List", "items": resources}
        ),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("The isolated pull-request job could not be scheduled.")
    return job["metadata"]["name"]
