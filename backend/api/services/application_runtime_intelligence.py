"""Permission-scoped Phase 3 runtime intelligence for mapped applications.

This module only performs Kubernetes GET operations. It deliberately produces a
small redacted evidence envelope rather than persisting raw workload manifests:
literal environment values, ConfigMap data, Secret data, tokens, and cluster
credentials are never retained or sent to Hermes.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Iterable

import yaml

from ..access_engine import (
    can_access_cluster,
    can_access_namespace,
    can_access_resource,
)
from ..audit import log_audit
from ..db import db
from ..k8s_provider import (
    K8sCommandError,
    list_namespaced_resources_json,
    read_namespaced_resource_json,
    resolve_cluster_access,
    should_use_real_k8s,
)
from ..models import (
    ApplicationAnalysis,
    ApplicationRuntimeSnapshot,
    User,
)

_KIND_RESOURCE = {
    "deployment": "deployments",
    "statefulset": "statefulsets",
    "daemonset": "daemonsets",
    "pod": "pods",
}
_SAFE_REVISION_KEYS = {
    "app.kubernetes.io/version",
    "app.kubernetes.io/revision",
    "vcs-ref",
    "vcs-revision",
    "git-commit",
    "git-sha",
    "commit",
    "commit-sha",
}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def _selector_matches(selector: dict, labels: dict) -> bool:
    return bool(selector) and all(labels.get(key) == value for key, value in selector.items())


def _pod_template(workload: dict, kind: str) -> tuple[dict, dict]:
    if kind == "pod":
        return workload.get("metadata") or {}, workload.get("spec") or {}
    template = (workload.get("spec") or {}).get("template") or {}
    return template.get("metadata") or {}, template.get("spec") or {}


def _safe_probe(value: dict | None) -> dict | None:
    if not isinstance(value, dict):
        return None
    result = {
        key: value.get(key)
        for key in ("initialDelaySeconds", "periodSeconds", "timeoutSeconds", "failureThreshold")
        if value.get(key) is not None
    }
    if isinstance(value.get("httpGet"), dict):
        result["httpGet"] = {
            key: value["httpGet"].get(key)
            for key in ("path", "port", "scheme")
            if value["httpGet"].get(key) is not None
        }
    elif isinstance(value.get("tcpSocket"), dict):
        result["tcpSocket"] = {"port": value["tcpSocket"].get("port")}
    elif isinstance(value.get("grpc"), dict):
        result["grpc"] = {
            key: value["grpc"].get(key)
            for key in ("port", "service")
            if value["grpc"].get(key) is not None
        }
    elif value.get("exec") is not None:
        # Commands may contain credentials; record presence, never the command.
        result["execConfigured"] = True
    return result


def _safe_env(container: dict) -> tuple[list[dict], list[dict]]:
    env = []
    for item in container.get("env") or []:
        if not isinstance(item, dict) or not item.get("name"):
            continue
        safe = {"name": str(item["name"])}
        value_from = item.get("valueFrom") or {}
        if value_from.get("secretKeyRef"):
            ref = value_from["secretKeyRef"]
            safe["source"] = "Secret"
            safe["reference"] = ref.get("name")
            safe["key"] = ref.get("key")
        elif value_from.get("configMapKeyRef"):
            ref = value_from["configMapKeyRef"]
            safe["source"] = "ConfigMap"
            safe["reference"] = ref.get("name")
            safe["key"] = ref.get("key")
        elif value_from.get("fieldRef"):
            safe["source"] = "FieldRef"
            safe["fieldPath"] = value_from["fieldRef"].get("fieldPath")
        elif value_from.get("resourceFieldRef"):
            safe["source"] = "ResourceFieldRef"
        else:
            safe["source"] = "Literal"
            safe["configured"] = "value" in item
        env.append(safe)

    env_from = []
    for item in container.get("envFrom") or []:
        if (item.get("secretRef") or {}).get("name"):
            env_from.append(
                {"source": "Secret", "reference": item["secretRef"]["name"]}
            )
        if (item.get("configMapRef") or {}).get("name"):
            env_from.append(
                {"source": "ConfigMap", "reference": item["configMapRef"]["name"]}
            )
    return env, env_from


def _safe_container(container: dict) -> dict:
    env, env_from = _safe_env(container)
    security = container.get("securityContext") or {}
    capabilities = security.get("capabilities") or {}
    return {
        "name": container.get("name"),
        "image": container.get("image"),
        "ports": [
            {
                "name": port.get("name"),
                "containerPort": port.get("containerPort"),
                "protocol": port.get("protocol", "TCP"),
            }
            for port in container.get("ports") or []
            if isinstance(port, dict)
        ],
        "environment": env,
        "environmentFrom": env_from,
        "resources": container.get("resources") or {},
        "securityContext": {
            "runAsNonRoot": security.get("runAsNonRoot"),
            "readOnlyRootFilesystem": security.get("readOnlyRootFilesystem"),
            "allowPrivilegeEscalation": security.get("allowPrivilegeEscalation"),
            "privileged": security.get("privileged"),
            "capabilitiesAdd": capabilities.get("add") or [],
            "capabilitiesDrop": capabilities.get("drop") or [],
        },
        "livenessProbe": _safe_probe(container.get("livenessProbe")),
        "readinessProbe": _safe_probe(container.get("readinessProbe")),
        "startupProbe": _safe_probe(container.get("startupProbe")),
    }


def _revision_metadata(workload: dict) -> dict:
    metadata = workload.get("metadata") or {}
    values = {}
    for source in (metadata.get("labels") or {}, metadata.get("annotations") or {}):
        for key, value in source.items():
            normalized = str(key).lower().rsplit("/", 1)[-1]
            if normalized in _SAFE_REVISION_KEYS and value is not None:
                values[str(key)] = str(value)[:255]
    return values


def _workload_summary(workload: dict, kind: str) -> tuple[dict, dict]:
    metadata = workload.get("metadata") or {}
    template_metadata, pod_spec = _pod_template(workload, kind)
    spec = workload.get("spec") or {}
    status = workload.get("status") or {}
    if kind == "daemonset":
        desired = status.get("desiredNumberScheduled", 0)
        ready = status.get("numberReady", 0)
        available = status.get("numberAvailable", ready)
    elif kind == "pod":
        desired = 1
        ready = int(
            any(
                condition.get("type") == "Ready" and condition.get("status") == "True"
                for condition in status.get("conditions") or []
            )
        )
        available = ready
    else:
        desired = spec.get("replicas", 0)
        ready = status.get("readyReplicas", 0)
        available = status.get("availableReplicas", ready)
    labels = template_metadata.get("labels") or metadata.get("labels") or {}
    summary = {
        "kind": kind.title().replace("Statefulset", "StatefulSet").replace("Daemonset", "DaemonSet"),
        "name": metadata.get("name"),
        "namespace": metadata.get("namespace"),
        "labels": labels,
        "desiredReplicas": desired,
        "readyReplicas": ready,
        "availableReplicas": available,
        "serviceAccountName": pod_spec.get("serviceAccountName") or "default",
        "automountServiceAccountToken": pod_spec.get("automountServiceAccountToken"),
        "securityContext": {
            key: (pod_spec.get("securityContext") or {}).get(key)
            for key in ("runAsNonRoot", "runAsUser", "runAsGroup", "fsGroup", "seccompProfile")
            if (pod_spec.get("securityContext") or {}).get(key) is not None
        },
        "containers": [
            _safe_container(item)
            for item in pod_spec.get("containers") or []
            if isinstance(item, dict)
        ],
        "revisionMetadata": _revision_metadata(workload),
    }
    return summary, labels


def _pod_owned_by(pod: dict, kind: str, name: str, labels: dict) -> bool:
    if kind == "pod":
        return (pod.get("metadata") or {}).get("name") == name
    refs = (pod.get("metadata") or {}).get("ownerReferences") or []
    if kind == "deployment":
        return any(
            ref.get("kind") == "ReplicaSet"
            and str(ref.get("name") or "").startswith(f"{name}-")
            for ref in refs
        )
    owner_kind = {"statefulset": "StatefulSet", "daemonset": "DaemonSet"}[kind]
    if any(ref.get("kind") == owner_kind and ref.get("name") == name for ref in refs):
        return True
    return _selector_matches(labels, (pod.get("metadata") or {}).get("labels") or {})


def _pod_summary(pod: dict) -> dict:
    metadata = pod.get("metadata") or {}
    status = pod.get("status") or {}
    statuses = status.get("containerStatuses") or []
    return {
        "name": metadata.get("name"),
        "phase": status.get("phase"),
        "ready": any(
            item.get("type") == "Ready" and item.get("status") == "True"
            for item in status.get("conditions") or []
        ),
        "restarts": sum(int(item.get("restartCount") or 0) for item in statuses),
        "images": [item.get("image") for item in statuses if item.get("image")],
    }


def _service_summary(service: dict) -> dict:
    metadata = service.get("metadata") or {}
    spec = service.get("spec") or {}
    return {
        "name": metadata.get("name"),
        "type": spec.get("type", "ClusterIP"),
        "selector": spec.get("selector") or {},
        "ports": [
            {
                "name": item.get("name"),
                "port": item.get("port"),
                "targetPort": item.get("targetPort"),
                "protocol": item.get("protocol", "TCP"),
            }
            for item in spec.get("ports") or []
            if isinstance(item, dict)
        ],
    }


def _ingress_summary(ingress: dict, service_names: set[str]) -> dict | None:
    metadata = ingress.get("metadata") or {}
    rules = []
    matched = False
    for rule in (ingress.get("spec") or {}).get("rules") or []:
        paths = []
        for item in (rule.get("http") or {}).get("paths") or []:
            backend = item.get("backend") or {}
            service = (backend.get("service") or {}).get("name")
            if service in service_names:
                matched = True
                paths.append(
                    {
                        "path": item.get("path") or "/",
                        "pathType": item.get("pathType"),
                        "service": service,
                        "port": (backend.get("service") or {}).get("port"),
                    }
                )
        if paths:
            rules.append({"host": rule.get("host"), "paths": paths})
    if not matched:
        return None
    return {
        "name": metadata.get("name"),
        "rules": rules,
        "tlsConfigured": bool((ingress.get("spec") or {}).get("tls")),
    }


def _network_policy_summary(policy: dict, labels: dict) -> dict | None:
    spec = policy.get("spec") or {}
    selector = (spec.get("podSelector") or {}).get("matchLabels") or {}
    if selector and not _selector_matches(selector, labels):
        return None
    return {
        "name": (policy.get("metadata") or {}).get("name"),
        "podSelector": spec.get("podSelector") or {},
        "policyTypes": spec.get("policyTypes") or [],
        "ingressRuleCount": len(spec.get("ingress") or []),
        "egressRuleCount": len(spec.get("egress") or []),
    }


def _collect_evidence(analysis: ApplicationAnalysis) -> dict:
    application = analysis.application
    cluster_id = application.mapped_cluster_id
    namespace = application.mapped_namespace
    kind = str(application.mapped_workload_kind or "").strip().lower()
    name = application.mapped_workload_name
    if kind not in _KIND_RESOURCE:
        raise ValueError("Map the application to a supported Kubernetes workload first.")
    access = resolve_cluster_access(cluster_id)
    if not access:
        raise LookupError("The mapped cluster is not available.")
    if not should_use_real_k8s(cluster_id):
        raise ValueError("Runtime evidence requires a configured live Kubernetes cluster.")

    workload = read_namespaced_resource_json(
        access, _KIND_RESOURCE[kind], namespace, name
    )
    workload_summary, labels = _workload_summary(workload, kind)
    resource_kinds = (
        "pods",
        "services",
        "ingress",
        "networkpolicies.networking.k8s.io",
    )
    with ThreadPoolExecutor(max_workers=len(resource_kinds)) as pool:
        futures = {
            resource_kind: pool.submit(
                list_namespaced_resources_json,
                access,
                resource_kind,
                namespace,
            )
            for resource_kind in resource_kinds
        }
        resource_items = {
            resource_kind: future.result()
            for resource_kind, future in futures.items()
        }
    pods = [
        _pod_summary(item)
        for item in resource_items["pods"]
        if _pod_owned_by(item, kind, name, labels)
    ]
    services = [
        _service_summary(item)
        for item in resource_items["services"]
        if _selector_matches((item.get("spec") or {}).get("selector") or {}, labels)
    ]
    service_names = {item["name"] for item in services if item.get("name")}
    ingresses = [
        summary
        for summary in (
            _ingress_summary(item, service_names)
            for item in resource_items["ingress"]
        )
        if summary
    ]
    policies = [
        summary
        for summary in (
            _network_policy_summary(item, labels)
            for item in resource_items["networkpolicies.networking.k8s.io"]
        )
        if summary
    ]
    return {
        "evidenceState": "Runtime Observed",
        "observedAt": _iso(_now()),
        "clusterId": cluster_id,
        "namespace": namespace,
        "workload": workload_summary,
        "pods": pods,
        "services": services,
        "ingresses": ingresses,
        "networkPolicies": policies,
        "redaction": {
            "literalEnvironmentValuesRetained": False,
            "configMapValuesRetained": False,
            "secretValuesRetained": False,
            "clusterCredentialsRetained": False,
        },
    }


def _walk_values(value: Any, keys: set[str]) -> Iterable[Any]:
    if isinstance(value, dict):
        for key, child in value.items():
            if str(key).lower() in keys:
                yield child
            yield from _walk_values(child, keys)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_values(child, keys)


def _source_ports(result: dict) -> set[int]:
    values = set()
    for value in _walk_values(result.get("api_inventory") or [], {"port", "container_port"}):
        try:
            port = int(value)
            if 1 <= port <= 65535:
                values.add(port)
        except (TypeError, ValueError):
            pass
    return values


def _source_names(items: Any) -> set[str]:
    names = set()
    for value in _walk_values(items, {"name", "key", "variable", "environment_variable"}):
        if isinstance(value, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]*", value):
            names.add(value)
    return names


def _comparison_item(
    category: str,
    source: Any,
    runtime: Any,
    status: str,
    detail: str,
) -> dict:
    return {
        "category": category,
        "source": source,
        "runtime": runtime,
        "status": status,
        "detail": detail,
        "sourceEvidenceState": "Source Inferred",
        "runtimeEvidenceState": "Runtime Observed",
    }


def _compare(analysis: ApplicationAnalysis, evidence: dict) -> list[dict]:
    result = analysis.result_summary or {}
    containers = (evidence.get("workload") or {}).get("containers") or []
    runtime_ports = {
        int(item["containerPort"])
        for container in containers
        for item in container.get("ports") or []
        if isinstance(item.get("containerPort"), int)
    }
    source_ports = _source_ports(result)
    comparisons = []
    if source_ports:
        missing = sorted(source_ports - runtime_ports)
        comparisons.append(
            _comparison_item(
                "Ports",
                sorted(source_ports),
                sorted(runtime_ports),
                "Matched" if not missing else "Missing",
                "All source-derived ports are declared by the workload."
                if not missing
                else f"Container port declarations are missing: {missing}.",
            )
        )
    else:
        comparisons.append(
            _comparison_item(
                "Ports", [], sorted(runtime_ports), "Cannot Verify",
                "No numeric source port evidence was available.",
            )
        )

    runtime_env = {
        item.get("name")
        for container in containers
        for item in container.get("environment") or []
        if item.get("name")
    }
    runtime_secret_names = {
        item.get("name")
        for container in containers
        for item in container.get("environment") or []
        if item.get("source") == "Secret" and item.get("name")
    }
    runtime_secret_names.update(
        item.get("reference")
        for container in containers
        for item in container.get("environmentFrom") or []
        if item.get("source") == "Secret" and item.get("reference")
    )
    source_config = _source_names(result.get("configuration_inventory") or [])
    missing_config = sorted(source_config - runtime_env)
    comparisons.append(
        _comparison_item(
            "Configuration",
            sorted(source_config),
            sorted(runtime_env),
            ("Cannot Verify" if not source_config else "Matched" if not missing_config else "Missing"),
            "No source configuration inventory was available."
            if not source_config
            else "All source configuration names are present."
            if not missing_config
            else f"Runtime environment names are missing: {missing_config}.",
        )
    )
    source_secrets = _source_names(result.get("secret_requirements") or [])
    missing_secrets = sorted(source_secrets - runtime_env - runtime_secret_names)
    comparisons.append(
        _comparison_item(
            "Secret references",
            sorted(source_secrets),
            sorted(runtime_secret_names),
            ("Cannot Verify" if not source_secrets else "Matched" if not missing_secrets else "Missing"),
            "Secret values were never read. Comparison uses names and references only."
            if source_secrets
            else "No source secret requirements were available.",
        )
    )

    dependencies = {
        str(item.destination_component).lower()
        for item in analysis.communications.all()
        if item.destination_component
    }
    services = {str(item.get("name")).lower() for item in evidence.get("services") or []}
    matched_services = sorted(
        dep for dep in dependencies if any(dep in service or service in dep for service in services)
    )
    comparisons.append(
        _comparison_item(
            "Dependencies and Services",
            sorted(dependencies),
            sorted(services),
            "Cannot Verify" if not dependencies else "Matched" if matched_services else "Missing",
            "Service-name matching is deterministic and may not verify external dependencies.",
        )
    )

    routes = {
        str(value)
        for value in _walk_values(result.get("api_inventory") or [], {"path", "route"})
        if isinstance(value, str) and value.startswith("/")
    }
    ingress_paths = {
        str(path.get("path") or "/")
        for ingress in evidence.get("ingresses") or []
        for rule in ingress.get("rules") or []
        for path in rule.get("paths") or []
    }
    unmatched_routes = sorted(
        route
        for route in routes
        if not any(route == path or route.startswith(path.rstrip("/") + "/") for path in ingress_paths)
    )
    comparisons.append(
        _comparison_item(
            "Routes and Ingress",
            sorted(routes),
            sorted(ingress_paths),
            "Cannot Verify" if not routes else "Matched" if not unmatched_routes else "Missing",
            "Ingress coverage is evaluated by path prefix."
            if routes
            else "No source routes were available.",
        )
    )

    health_routes = sorted(
        route for route in routes if re.search(r"(health|ready|live|startup)", route, re.I)
    )
    probe_paths = sorted(
        {
            str((container.get(probe) or {}).get("httpGet", {}).get("path"))
            for container in containers
            for probe in ("livenessProbe", "readinessProbe", "startupProbe")
            if (container.get(probe) or {}).get("httpGet", {}).get("path")
        }
    )
    comparisons.append(
        _comparison_item(
            "Health endpoints and probes",
            health_routes,
            probe_paths,
            "Cannot Verify" if not health_routes else "Matched"
            if any(path in probe_paths for path in health_routes) else "Missing",
            "Probe commands are not retained; HTTP paths are compared when available.",
        )
    )

    policies = evidence.get("networkPolicies") or []
    comparisons.append(
        _comparison_item(
            "Expected communication and NetworkPolicies",
            sorted(dependencies),
            [item.get("name") for item in policies],
            "Cannot Verify" if not dependencies else "Matched" if policies else "Missing",
            "Policy presence is runtime-observed; allowed peers cannot always be mapped to source names.",
        )
    )

    images = [item.get("image") for item in containers if item.get("image")]
    docker = result.get("docker_analysis") or {}
    declared_runtime = (
        docker.get("runtime")
        or docker.get("base_image")
        or docker.get("baseImage")
        or docker.get("declared_runtime")
    )
    comparisons.append(
        _comparison_item(
            "Declared runtime and deployed image",
            declared_runtime,
            images,
            "Cannot Verify" if not declared_runtime else "Matched"
            if any(str(declared_runtime).lower() in image.lower() for image in images)
            else "Unexpected",
            "Image names are compared without pulling image content.",
        )
    )
    revisions = (evidence.get("workload") or {}).get("revisionMetadata") or {}
    commit = analysis.commit_sha
    comparisons.append(
        _comparison_item(
            "Source commit and deployed image metadata",
            commit,
            revisions,
            "Cannot Verify" if not commit or not revisions else "Matched"
            if any(str(value).startswith(commit) or commit.startswith(str(value)) for value in revisions.values())
            else "Unexpected",
            "Only explicit, non-secret revision labels and annotations are used.",
        )
    )
    return comparisons


def _topology(analysis: ApplicationAnalysis, evidence: dict) -> dict:
    app_id = f"application:{analysis.application.name}"
    workload = evidence.get("workload") or {}
    workload_id = f"workload:{workload.get('kind')}/{workload.get('name')}"
    nodes = [
        {"id": app_id, "label": analysis.application.name, "type": "Application", "evidenceState": "Source Inferred"},
        {"id": workload_id, "label": workload.get("name"), "type": workload.get("kind"), "evidenceState": "Runtime Observed"},
    ]
    edges = [
        {
            "id": "mapping",
            "source": app_id,
            "destination": workload_id,
            "relation": "mapped to",
            "evidenceState": "Runtime Observed",
            "confidence": "Confirmed",
        }
    ]
    for pod in evidence.get("pods") or []:
        pod_id = f"pod:{pod.get('name')}"
        nodes.append(
            {
                "id": pod_id,
                "label": pod.get("name"),
                "type": "Pod",
                "status": pod.get("phase"),
                "ready": pod.get("ready"),
                "evidenceState": "Runtime Observed",
            }
        )
        edges.append(
            {
                "id": f"{workload_id}->{pod_id}",
                "source": workload_id,
                "destination": pod_id,
                "relation": "owns",
                "evidenceState": "Runtime Observed",
                "confidence": "Confirmed",
            }
        )
    for service in evidence.get("services") or []:
        node_id = f"service:{service.get('name')}"
        nodes.append({"id": node_id, "label": service.get("name"), "type": "Service", "evidenceState": "Runtime Observed"})
        edges.append({
            "id": f"{node_id}->{workload_id}",
            "source": node_id,
            "destination": workload_id,
            "relation": "selects",
            "ports": service.get("ports") or [],
            "evidenceState": "Runtime Observed",
            "confidence": "Confirmed",
        })
    for ingress in evidence.get("ingresses") or []:
        ingress_id = f"ingress:{ingress.get('name')}"
        nodes.append({"id": ingress_id, "label": ingress.get("name"), "type": "Ingress", "evidenceState": "Runtime Observed"})
        for rule in ingress.get("rules") or []:
            for path in rule.get("paths") or []:
                edges.append({
                    "id": f"{ingress_id}->{path.get('service')}:{path.get('path')}",
                    "source": ingress_id,
                    "destination": f"service:{path.get('service')}",
                    "relation": "routes",
                    "host": rule.get("host"),
                    "path": path.get("path"),
                    "evidenceState": "Runtime Observed",
                    "confidence": "Confirmed",
                })
    return {"nodes": nodes, "edges": edges, "observedAt": evidence.get("observedAt")}


def _policy_recommendation(analysis: ApplicationAnalysis, evidence: dict) -> dict:
    workload = evidence.get("workload") or {}
    labels = workload.get("labels") or {}
    selector = {}
    preferred = ("app.kubernetes.io/name", "app", "k8s-app", "component")
    for key in preferred:
        if labels.get(key):
            selector[key] = labels[key]
            break
    if not selector and labels:
        key = sorted(labels)[0]
        selector[key] = labels[key]
    if not selector:
        return {
            "status": "Cannot Generate",
            "reason": "The mapped pod template has no stable labels.",
            "reviewOnly": True,
        }

    service_ports = sorted(
        {
            int(port["targetPort"] if isinstance(port.get("targetPort"), int) else port["port"])
            for service in evidence.get("services") or []
            for port in service.get("ports") or []
            if isinstance(port.get("port"), int)
        }
    )
    communications = analysis.communications.all()
    egress_ports = sorted(
        {
            (str(item.protocol or "TCP").upper(), int(item.port))
            for item in communications
            if item.port and 1 <= int(item.port) <= 65535
        }
    )
    ingress_rules = []
    if service_ports:
        ingress_rules.append(
            {
                "from": [{"podSelector": {}}],
                "ports": [{"protocol": "TCP", "port": port} for port in service_ports],
            }
        )
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
        }
    ]
    if egress_ports:
        # Destination identity is not confirmed from source names, so this is a
        # port-only review proposal rather than an automatically applicable rule.
        egress_rules.append(
            {
                "to": [{"podSelector": {}}],
                "ports": [
                    {"protocol": protocol if protocol in {"TCP", "UDP", "SCTP"} else "TCP", "port": port}
                    for protocol, port in egress_ports
                ],
            }
        )
    policy = {
        "apiVersion": "networking.k8s.io/v1",
        "kind": "NetworkPolicy",
        "metadata": {
            "name": f"{workload.get('name')}-recommended",
            "namespace": evidence.get("namespace"),
        },
        "spec": {
            "podSelector": {"matchLabels": selector},
            "policyTypes": ["Ingress", "Egress"],
            "ingress": ingress_rules,
            "egress": egress_rules,
        },
    }
    return {
        "status": "Generated",
        "reviewOnly": True,
        "autoApply": False,
        "evidenceState": "Runtime Observed",
        "limitations": [
            "Review same-namespace peers before applying.",
            "Add an explicit ingress-controller namespace peer when Ingress is used.",
            "External destination identities cannot be confirmed from source evidence alone.",
            "No Kubernetes resource was created or modified.",
        ],
        "manifest": policy,
        "yaml": yaml.safe_dump(policy, sort_keys=False),
    }


def _gate(gate_id: str, title: str, status: str, evidence: str, recommendation: str = "") -> dict:
    return {
        "id": gate_id,
        "title": title,
        "status": status,
        "evidence": evidence,
        "recommendation": recommendation,
    }


def _readiness(analysis: ApplicationAnalysis, evidence: dict, comparisons: list[dict]) -> list[dict]:
    workload = evidence.get("workload") or {}
    containers = workload.get("containers") or []
    desired = int(workload.get("desiredReplicas") or 0)
    available = int(workload.get("availableReplicas") or 0)
    gates = [
        _gate(
            "runtime-available", "Workload availability",
            "Pass" if desired > 0 and available >= desired else "Fail",
            f"{available}/{desired} desired replicas are available.",
            "Restore all desired replicas before promotion.",
        ),
        _gate(
            "image-pinned", "Immutable image reference",
            "Pass" if containers and all(
                item.get("image") and not str(item["image"]).endswith(":latest")
                and (":" in str(item["image"]) or "@sha256:" in str(item["image"]))
                for item in containers
            ) else "Warning",
            ", ".join(str(item.get("image")) for item in containers) or "No image observed.",
            "Use a version tag or digest; avoid latest.",
        ),
        _gate(
            "probes", "Health probes",
            "Pass" if containers and all(item.get("livenessProbe") and item.get("readinessProbe") for item in containers) else "Fail",
            "Liveness and readiness probes are required for every application container.",
            "Configure source-aligned liveness and readiness probes.",
        ),
        _gate(
            "resources", "CPU and memory requests/limits",
            "Pass" if containers and all(
                (item.get("resources") or {}).get("requests", {}).get("cpu")
                and (item.get("resources") or {}).get("requests", {}).get("memory")
                and (item.get("resources") or {}).get("limits", {}).get("cpu")
                and (item.get("resources") or {}).get("limits", {}).get("memory")
                for item in containers
            ) else "Warning",
            "Resource settings were evaluated without reading metrics history.",
            "Set CPU/memory requests and limits for each container.",
        ),
        _gate(
            "container-security", "Restricted container security context",
            "Pass" if containers and all(
                (item.get("securityContext") or {}).get("runAsNonRoot") is True
                and (item.get("securityContext") or {}).get("readOnlyRootFilesystem") is True
                and (item.get("securityContext") or {}).get("allowPrivilegeEscalation") is False
                and "ALL" in ((item.get("securityContext") or {}).get("capabilitiesDrop") or [])
                for item in containers
            ) else "Fail",
            "runAsNonRoot, readOnlyRootFilesystem, privilege escalation, and dropped capabilities were checked.",
            "Apply the Kubernetes restricted security posture.",
        ),
        _gate(
            "network-policy", "Network isolation",
            "Pass" if evidence.get("networkPolicies") else "Warning",
            f"{len(evidence.get('networkPolicies') or [])} selecting NetworkPolicy resource(s) observed.",
            "Review the generated NetworkPolicy recommendation.",
        ),
    ]
    open_high = [
        finding
        for finding in analysis.findings.all()
        if finding.status == "Open" and finding.severity in {"Critical", "High"}
    ]
    gates.append(
        _gate(
            "security-findings", "Critical and high findings",
            "Pass" if not open_high else "Fail",
            f"{len(open_high)} open Critical/High finding(s).",
            "Resolve or explicitly accept the risk before promotion.",
        )
    )
    mismatches = [
        item for item in comparisons if item.get("status") in {"Missing", "Unexpected"}
    ]
    gates.append(
        _gate(
            "source-runtime-drift", "Source-to-runtime drift",
            "Pass" if not mismatches else "Warning",
            f"{len(mismatches)} missing or unexpected comparison result(s).",
            "Review drift before deployment promotion.",
        )
    )
    build = ((analysis.result_summary or {}).get("application_profile") or {}).get("build_verification") or {}
    gates.append(
        _gate(
            "build-verified", "Build verification",
            "Pass" if build.get("status") in {"Completed", "Passed"} else "Warning",
            build.get("status") or "This analysis was not Build Verified.",
            "Run Build Verified mode with the required toolchain before production.",
        )
    )
    return gates


def _snapshot_to_dict(row: ApplicationRuntimeSnapshot) -> dict:
    return {
        "id": row.id,
        "analysisId": row.analysis_id,
        "clusterId": row.cluster_id,
        "namespace": row.namespace,
        "workloadKind": row.workload_kind,
        "workloadName": row.workload_name,
        "status": row.status,
        "safeErrorMessage": row.safe_error_message,
        "evidence": row.evidence or {},
        "comparison": row.comparison or [],
        "topology": row.topology or {},
        "networkPolicy": row.network_policy or {},
        "readinessGates": row.readiness_gates or [],
        "collectedBy": row.collected_by.username if row.collected_by else None,
        "createdAt": _iso(row.created_at),
    }


def _authorize_mapping(analysis: ApplicationAnalysis, user: User) -> None:
    application = analysis.application
    mapping = (
        application.mapped_cluster_id,
        application.mapped_namespace,
        application.mapped_workload_kind,
        application.mapped_workload_name,
    )
    if not all(mapping):
        return
    cluster_id, namespace, kind_value, name = mapping
    kind = str(kind_value).strip().lower()
    if (
        not can_access_cluster(user, cluster_id)
        or not can_access_namespace(user, cluster_id, namespace)
        or kind not in _KIND_RESOURCE
        or not can_access_resource(user, cluster_id, namespace, kind, name)
    ):
        raise PermissionError("You cannot access the mapped Kubernetes workload.")


def latest_snapshot(analysis_id: int, user: User) -> dict:
    analysis = db.session.get(ApplicationAnalysis, analysis_id)
    if analysis is None:
        raise LookupError("Analysis not found.")
    _authorize_mapping(analysis, user)
    application = analysis.application
    row = (
        ApplicationRuntimeSnapshot.query.filter_by(
            analysis_id=analysis_id,
            cluster_id=application.mapped_cluster_id,
            namespace=application.mapped_namespace,
            workload_kind=application.mapped_workload_kind,
            workload_name=application.mapped_workload_name,
        )
        .order_by(ApplicationRuntimeSnapshot.created_at.desc())
        .first()
    )
    if row is None:
        return {
            "status": "Not Collected",
            "mapping": {
                "clusterId": analysis.application.mapped_cluster_id,
                "namespace": analysis.application.mapped_namespace,
                "workloadKind": analysis.application.mapped_workload_kind,
                "workloadName": analysis.application.mapped_workload_name,
            },
        }
    return _snapshot_to_dict(row)


def collect_snapshot(analysis_id: int, user: User) -> dict:
    analysis = db.session.get(ApplicationAnalysis, analysis_id)
    if analysis is None:
        raise LookupError("Analysis not found.")
    application = analysis.application
    mapping = (
        application.mapped_cluster_id,
        application.mapped_namespace,
        application.mapped_workload_kind,
        application.mapped_workload_name,
    )
    if not all(mapping):
        raise ValueError("Map the application to a cluster, namespace, and workload first.")
    cluster_id, namespace, kind_value, name = mapping
    kind = str(kind_value).strip().lower()
    if kind not in _KIND_RESOURCE:
        raise ValueError("The mapped workload kind is not supported.")
    _authorize_mapping(analysis, user)

    try:
        evidence = _collect_evidence(analysis)
    except K8sCommandError as exc:
        raise RuntimeError("Kubernetes runtime evidence could not be collected.") from exc
    comparisons = _compare(analysis, evidence)
    topology = _topology(analysis, evidence)
    policy = _policy_recommendation(analysis, evidence)
    readiness = _readiness(analysis, evidence, comparisons)
    row = ApplicationRuntimeSnapshot(
        analysis_id=analysis.id,
        cluster_id=cluster_id,
        namespace=namespace,
        workload_kind=str(kind_value),
        workload_name=name,
        collected_by_user_id=user.id,
        status="Completed",
        evidence=evidence,
        comparison=comparisons,
        topology=topology,
        network_policy=policy,
        readiness_gates=readiness,
    )
    db.session.add(row)
    db.session.commit()
    log_audit(
        "application.runtime_snapshot.collected",
        actor=user,
        target_type="application_analysis",
        target_id=str(analysis.id),
        details={
            "cluster_id": cluster_id,
            "namespace": namespace,
            "workload_kind": kind_value,
            "workload_name": name,
            "evidence_state": "Runtime Observed",
            "hermes_received_runtime_credentials": False,
        },
    )
    return _snapshot_to_dict(row)
