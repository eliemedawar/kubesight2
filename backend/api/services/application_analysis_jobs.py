"""Locked-down Kubernetes Job launcher for Application Intelligence."""

from __future__ import annotations

import base64
import ipaddress
import json
import os
import re
import subprocess
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class JobLaunch:
    job_name: str
    callback_token: str


def _hermes_internal_egress_rule() -> dict | None:
    """Allow only the configured in-cluster Hermes namespace and TCP port."""
    endpoint = os.getenv("HERMES_API_URL", "").strip()
    if not endpoint:
        return None
    parsed = urlsplit(endpoint)
    hostname = (parsed.hostname or "").lower()
    internal_http = parsed.scheme == "http" and (
        hostname.endswith(".svc") or hostname.endswith(".svc.cluster.local")
    )
    if not internal_http:
        return None

    namespace = os.getenv("HERMES_SERVICE_NAMESPACE", "").strip()
    if not namespace:
        labels = hostname.split(".")
        try:
            svc_index = labels.index("svc")
        except ValueError:
            svc_index = -1
        if svc_index >= 2:
            namespace = labels[svc_index - 1]
        elif len(labels) >= 2:
            namespace = labels[1]
    if not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", namespace):
        raise ValueError(
            "HERMES_SERVICE_NAMESPACE is required for an in-cluster Hermes endpoint."
        )

    raw_port = os.getenv("HERMES_SERVICE_PORT", "").strip()
    port = int(raw_port) if raw_port else (parsed.port or 80)
    if not 1 <= port <= 65535:
        raise ValueError("HERMES_SERVICE_PORT is invalid.")
    return {
        "to": [
            {
                "namespaceSelector": {
                    "matchLabels": {"kubernetes.io/metadata.name": namespace}
                }
            }
        ],
        "ports": [{"protocol": "TCP", "port": port}],
    }


def _dns_name(value: str) -> str:
    safe = re.sub(r"[^a-z0-9-]+", "-", value.lower()).strip("-")
    return safe[:50].rstrip("-") or "analysis"


def _controlled_egress_proxy(analysis_mode: str) -> tuple[str, str, int] | None:
    proxy_url = os.getenv("APPLICATION_ANALYSIS_EGRESS_PROXY_URL", "").strip()
    proxy_cidr = os.getenv("APPLICATION_ANALYSIS_EGRESS_PROXY_CIDR", "").strip()
    raw_port = os.getenv("APPLICATION_ANALYSIS_EGRESS_PROXY_PORT", "").strip()
    if not proxy_url and not proxy_cidr:
        if analysis_mode == "Build Verified":
            raise ValueError(
                "Build Verified Kubernetes execution requires a controlled "
                "egress proxy URL, CIDR, and port."
            )
        return None
    parsed = urlsplit(proxy_url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("APPLICATION_ANALYSIS_EGRESS_PROXY_URL is invalid.")
    try:
        ipaddress.ip_network(proxy_cidr, strict=False)
        port = int(raw_port or parsed.port or (443 if parsed.scheme == "https" else 80))
    except (ValueError, TypeError):
        raise ValueError("APPLICATION_ANALYSIS_EGRESS_PROXY_CIDR or port is invalid.")
    if not 1 <= port <= 65535:
        raise ValueError("APPLICATION_ANALYSIS_EGRESS_PROXY_PORT is invalid.")
    return proxy_url, proxy_cidr, port


def build_job_resources(
    *,
    analysis_id: int,
    repository_url: str,
    revision: str,
    subdirectory: str,
    repository_token: str,
    callback_token: str,
    analysis_mode: str = "Quick",
    repository_credential_type: str = "repository_access_token",
    repository_principal: str = "",
) -> list[dict]:
    namespace = os.getenv("APPLICATION_ANALYSIS_NAMESPACE", "kubesight-analysis")
    image = os.getenv("APPLICATION_ANALYSIS_WORKER_IMAGE", "kubesight-backend:latest")
    job_name = f"ks-app-analysis-{analysis_id}-{_dns_name(callback_token[:8])}"
    secret_name = f"{job_name}-credentials"
    labels = {
        "app.kubernetes.io/name": "kubesight-application-analysis",
        "kubesight.io/analysis-id": str(analysis_id),
        "kubesight.io/network-phase": "bounded-egress",
    }
    controlled_proxy = _controlled_egress_proxy(analysis_mode)
    proxy_env = (
        [
            {"name": "HTTPS_PROXY", "value": controlled_proxy[0]},
            {"name": "HTTP_PROXY", "value": controlled_proxy[0]},
            {
                "name": "NO_PROXY",
                "value": ".svc,.svc.cluster.local,localhost,127.0.0.1",
            },
        ]
        if controlled_proxy
        else []
    )

    secret_data = {
        "repository-token": repository_token,
        "repository-credential-type": repository_credential_type,
        "repository-principal": repository_principal,
        "callback-token": callback_token,
        "hermes-token": os.getenv("HERMES_API_TOKEN", ""),
    }
    secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {
            "name": secret_name,
            "namespace": namespace,
            "labels": labels,
            "ownerReferences": [],
        },
        "type": "Opaque",
        "data": {
            key: base64.b64encode(value.encode("utf-8")).decode("ascii")
            for key, value in secret_data.items()
        },
    }
    volume_mount = {
        "name": "workspace",
        "mountPath": "/workspace",
    }
    security_context = {
        "allowPrivilegeEscalation": False,
        "readOnlyRootFilesystem": True,
        "runAsNonRoot": True,
        "runAsUser": 65532,
        "runAsGroup": 65532,
        "capabilities": {"drop": ["ALL"]},
    }
    job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": job_name, "namespace": namespace, "labels": labels},
        "spec": {
            "backoffLimit": int(os.getenv("APPLICATION_ANALYSIS_BACKOFF_LIMIT", "1")),
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
                        "APPLICATION_ANALYSIS_SERVICE_ACCOUNT", "kubesight-analysis-worker"
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
                    ],
                    "initContainers": [
                        {
                            "name": "read-only-checkout",
                            "image": image,
                            "imagePullPolicy": os.getenv(
                                "APPLICATION_ANALYSIS_IMAGE_PULL_POLICY", "IfNotPresent"
                            ),
                            "command": ["python", "-m", "api.application_checkout"],
                            "env": [
                                {"name": "HOME", "value": "/tmp"},
                                {"name": "TMPDIR", "value": "/tmp"},
                                {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"},
                                {"name": "ANALYSIS_ID", "value": str(analysis_id)},
                                {"name": "ANALYSIS_MODE", "value": analysis_mode},
                                {"name": "ANALYSIS_REPOSITORY_URL", "value": repository_url},
                                {"name": "ANALYSIS_REVISION", "value": revision},
                                {
                                    "name": "KUBESIGHT_ANALYSIS_CALLBACK_URL",
                                    "value": os.getenv(
                                        "APPLICATION_ANALYSIS_CALLBACK_URL",
                                        "http://kubesight-backend.kubesight.svc.cluster.local:5000/api/application-analysis-worker",
                                    ),
                                },
                                {
                                    "name": "KUBESIGHT_ANALYSIS_CALLBACK_TOKEN",
                                    "valueFrom": {
                                        "secretKeyRef": {
                                            "name": secret_name,
                                            "key": "callback-token",
                                        }
                                    },
                                },
                                {
                                    "name": "ANALYSIS_REPOSITORY_TOKEN",
                                    "valueFrom": {
                                        "secretKeyRef": {
                                            "name": secret_name,
                                            "key": "repository-token",
                                        }
                                    },
                                },
                                {
                                    "name": "ANALYSIS_REPOSITORY_CREDENTIAL_TYPE",
                                    "valueFrom": {
                                        "secretKeyRef": {
                                            "name": secret_name,
                                            "key": "repository-credential-type",
                                        }
                                    },
                                },
                                {
                                    "name": "ANALYSIS_REPOSITORY_PRINCIPAL",
                                    "valueFrom": {
                                        "secretKeyRef": {
                                            "name": secret_name,
                                            "key": "repository-principal",
                                        }
                                    },
                                },
                                *proxy_env,
                            ],
                            "resources": {
                                "requests": {"cpu": "100m", "memory": "128Mi"},
                                "limits": {"cpu": "500m", "memory": "512Mi"},
                            },
                            "securityContext": security_context,
                            "volumeMounts": [volume_mount, {"name": "tmp", "mountPath": "/tmp"}],
                        },
                        *(
                            [
                                {
                                    "name": "credential-free-build-verifier",
                                    "image": image,
                                    "imagePullPolicy": os.getenv(
                                        "APPLICATION_ANALYSIS_IMAGE_PULL_POLICY",
                                        "IfNotPresent",
                                    ),
                                    "command": [
                                        "python",
                                        "-m",
                                        "api.application_build_verifier",
                                    ],
                                    "env": [
                                        {"name": "HOME", "value": "/tmp"},
                                        {"name": "TMPDIR", "value": "/tmp"},
                                        {
                                            "name": "PYTHONDONTWRITEBYTECODE",
                                            "value": "1",
                                        },
                                        {
                                            "name": "ANALYSIS_MODE",
                                            "value": analysis_mode,
                                        },
                                        {
                                            "name": "ANALYSIS_SUBDIRECTORY",
                                            "value": subdirectory,
                                        },
                                        {
                                            "name": "APPLICATION_BUILD_NETWORK_POLICY",
                                            "value": (
                                                "Controlled proxy unavailable to build process"
                                            ),
                                        },
                                    ],
                                    "resources": {
                                        "requests": {
                                            "cpu": "250m",
                                            "memory": "512Mi",
                                        },
                                        "limits": {
                                            "cpu": os.getenv(
                                                "APPLICATION_BUILD_CPU_LIMIT", "2"
                                            ),
                                            "memory": os.getenv(
                                                "APPLICATION_BUILD_MEMORY_LIMIT",
                                                "4Gi",
                                            ),
                                            "ephemeral-storage": os.getenv(
                                                "APPLICATION_ANALYSIS_EPHEMERAL_LIMIT",
                                                "2Gi",
                                            ),
                                        },
                                    },
                                    "securityContext": security_context,
                                    "volumeMounts": [
                                        volume_mount,
                                        {"name": "tmp", "mountPath": "/tmp"},
                                    ],
                                }
                            ]
                            if analysis_mode == "Build Verified"
                            else []
                        ),
                    ],
                    "containers": [
                        {
                            "name": "analyzer",
                            "image": image,
                            "imagePullPolicy": os.getenv(
                                "APPLICATION_ANALYSIS_IMAGE_PULL_POLICY", "IfNotPresent"
                            ),
                            "command": ["python", "-m", "api.application_worker"],
                            "env": [
                                {"name": "HOME", "value": "/tmp"},
                                {"name": "TMPDIR", "value": "/tmp"},
                                {"name": "PYTHONDONTWRITEBYTECODE", "value": "1"},
                                {"name": "ANALYSIS_ID", "value": str(analysis_id)},
                                {"name": "ANALYSIS_MODE", "value": analysis_mode},
                                {"name": "ANALYSIS_SUBDIRECTORY", "value": subdirectory},
                                {
                                    "name": "KUBESIGHT_ANALYSIS_CALLBACK_URL",
                                    "value": os.getenv(
                                        "APPLICATION_ANALYSIS_CALLBACK_URL",
                                        "http://kubesight-backend.kubesight.svc.cluster.local:5000/api/application-analysis-worker",
                                    ),
                                },
                                {
                                    "name": "KUBESIGHT_ANALYSIS_CALLBACK_TOKEN",
                                    "valueFrom": {
                                        "secretKeyRef": {
                                            "name": secret_name,
                                            "key": "callback-token",
                                        }
                                    },
                                },
                                {
                                    "name": "HERMES_API_TOKEN",
                                    "valueFrom": {
                                        "secretKeyRef": {
                                            "name": secret_name,
                                            "key": "hermes-token",
                                        }
                                    },
                                },
                                {
                                    "name": "HERMES_API_URL",
                                    "value": os.getenv("HERMES_API_URL", ""),
                                },
                                {
                                    "name": "HERMES_APPLICATION_MODEL",
                                    "value": os.getenv(
                                        "HERMES_APPLICATION_MODEL", "hermes-analysis"
                                    ),
                                },
                                *proxy_env,
                            ],
                            "resources": {
                                "requests": {"cpu": "250m", "memory": "512Mi"},
                                "limits": {
                                    "cpu": os.getenv("APPLICATION_ANALYSIS_CPU_LIMIT", "2"),
                                    "memory": os.getenv(
                                        "APPLICATION_ANALYSIS_MEMORY_LIMIT", "4Gi"
                                    ),
                                    "ephemeral-storage": os.getenv(
                                        "APPLICATION_ANALYSIS_EPHEMERAL_LIMIT", "2Gi"
                                    ),
                                },
                            },
                            "securityContext": security_context,
                            "volumeMounts": [volume_mount, {"name": "tmp", "mountPath": "/tmp"}],
                        }
                    ],
                },
            },
        },
    }
    egress_rules = [
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
            # Worker callbacks stay inside the KubeSight namespace.
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
        (
            {
                "to": [{"ipBlock": {"cidr": controlled_proxy[1]}}],
                "ports": [{"protocol": "TCP", "port": controlled_proxy[2]}],
            }
            if controlled_proxy
            else {
                # Quick/Deep compatibility. Production should configure the
                # controlled proxy or a CNI FQDN policy.
                "ports": [{"protocol": "TCP", "port": 443}]
            }
        ),
    ]
    hermes_internal_rule = _hermes_internal_egress_rule()
    if hermes_internal_rule is not None:
        egress_rules.append(hermes_internal_rule)

    network_policy = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {"name": job_name, "namespace": namespace},
        "spec": {
            "podSelector": {"matchLabels": labels},
            "policyTypes": ["Ingress", "Egress"],
            "ingress": [],
            "egress": egress_rules,
        },
    }
    return [secret, network_policy, job]


def launch(resources: list[dict]) -> str:
    job = next(item for item in resources if item.get("kind") == "Job")
    secret = next(item for item in resources if item.get("kind") == "Secret")
    network_policy = next(item for item in resources if item.get("kind") == "NetworkPolicy")
    command = ["kubectl"]
    kubeconfig = os.getenv("K8S_KUBECONFIG", "").strip()
    if kubeconfig:
        command.extend(["--kubeconfig", kubeconfig])
    command.extend(["apply", "-f", "-"])
    # A stream that starts with ``{`` is parsed by kubectl as JSON, so joining
    # JSON objects with YAML ``---`` separators applies the first object and
    # then fails at the separator.  Send one valid Kubernetes List document so
    # all resources are parsed and applied together.
    manifest = json.dumps(
        {"apiVersion": "v1", "kind": "List", "items": resources}
    )
    completed = subprocess.run(
        command,
        input=manifest,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )
    if completed.returncode != 0:
        raise RuntimeError("The isolated analysis job could not be scheduled.")
    # Attach auxiliary resources after the Job UID exists. Kubernetes garbage
    # collection then removes credentials and the per-job policy when the Job
    # TTL controller deletes the completed Job.
    namespace = job["metadata"]["namespace"]
    get_uid = [*command[:-3], "get", "job", job["metadata"]["name"], "-n", namespace, "-o", "jsonpath={.metadata.uid}"]
    uid_result = subprocess.run(
        get_uid, capture_output=True, text=True, timeout=15, check=False
    )
    uid = uid_result.stdout.strip()
    if uid:
        owner_patch = json.dumps(
            {
                "metadata": {
                    "ownerReferences": [
                        {
                            "apiVersion": "batch/v1",
                            "kind": "Job",
                            "name": job["metadata"]["name"],
                            "uid": uid,
                            "controller": True,
                            "blockOwnerDeletion": True,
                        }
                    ]
                }
            }
        )
        for kind, name in (
            ("secret", secret["metadata"]["name"]),
            ("networkpolicy", network_policy["metadata"]["name"]),
        ):
            subprocess.run(
                [
                    *command[:-3],
                    "patch",
                    kind,
                    name,
                    "-n",
                    namespace,
                    "--type=merge",
                    "-p",
                    owner_patch,
                ],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
    return job["metadata"]["name"]


def cancel(job_name: str) -> None:
    namespace = os.getenv("APPLICATION_ANALYSIS_NAMESPACE", "kubesight-analysis")
    command = ["kubectl"]
    kubeconfig = os.getenv("K8S_KUBECONFIG", "").strip()
    if kubeconfig:
        command.extend(["--kubeconfig", kubeconfig])
    command.extend(
        [
            "delete",
            "job,networkpolicy,secret",
            "-n",
            namespace,
            "-l",
            f"kubesight.io/analysis-id={job_name.split('-')[3] if job_name else ''}",
            "--ignore-not-found=true",
        ]
    )
    subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)


def cleanup_auxiliary(analysis_id: int) -> None:
    """Delete credentials early; the Job owner deletes its policy after exit.

    The NetworkPolicy must remain while the worker Pod is still terminating,
    otherwise cleanup itself would briefly reopen unrestricted egress.
    """
    namespace = os.getenv("APPLICATION_ANALYSIS_NAMESPACE", "kubesight-analysis")
    command = ["kubectl"]
    kubeconfig = os.getenv("K8S_KUBECONFIG", "").strip()
    if kubeconfig:
        command.extend(["--kubeconfig", kubeconfig])
    command.extend(
        [
            "delete",
            "secret",
            "-n",
            namespace,
            "-l",
            f"kubesight.io/analysis-id={analysis_id}",
            "--ignore-not-found=true",
        ]
    )
    subprocess.run(command, capture_output=True, text=True, timeout=30, check=False)
