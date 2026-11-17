"""Platform rules for a rendered Helm chart, checked before a Deploy stage
installs or upgrades it.

``ai-cluster`` is the Areeba AI cluster's contract (OCIDUAI-K8S), the same
rules as the platform team's ``areeba-check`` (areeba-governance 0.1.0): the
front door, secrets in OpenBao, the database through the pooler, images from
Nexus, pod security, resources, no card data. Each finding is FAIL (the deploy
is refused, nothing changes) or WARN (written to the build log).

When the platform publishes a new version of its rules, change them here too:
a rule KubeSight does not know is not checked.
"""

from __future__ import annotations

import json
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml

POLICIES = {"ai-cluster": "the AI cluster rules (areeba-governance 0.1.0)"}

# The AI cluster's contract (areeba-governance 0.1.0, contract.json).
_REGISTRY = "registry.areeba.com"
_PULL_SECRET = "regcred"
_GATEWAY = "areeba-fabric"
_GATEWAY_NAMESPACE = "fabric"
_LISTENERS = ("https-areeba", "https-kubesight")
_SECRET_STORE = "openbao"
_SECRET_STORE_KIND = "SecretStore"
_DB_SECRETS = ("postgres", "postgres-app")
_DB_FORBIDDEN_HOSTS = ("pg-ai-rw", "pg-ai-ro", "pg-ai-r", "pg-ai-1", "pg-ai-2", "pg-ai-3")
_DB_SSLMODE = "verify-full"
_FORBIDDEN_EGRESS = ("10.43.17.16",)
_MAX_POD_CPU = 4.0
_PROMETHEUS_NAMESPACE = "observability"

_CARD = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
_SECRETY = re.compile(r"(passw|secret|token|api[_-]?key|private[_-]?key|access[_-]?key)", re.I)

Finding = Dict[str, str]


def label(policy: str) -> str:
    return POLICIES.get(policy, policy)


def check(policy: str, rendered: str) -> List[Finding]:
    """Every rule the rendered manifests break, FAILs first."""
    if policy not in POLICIES:
        return [_finding("FAIL", "policy", "-", f"unknown rule set '{policy}'", "pick a rule set KubeSight knows")]
    findings: List[Finding] = []
    for doc in _documents(rendered):
        _check_object(doc, findings)
    return sorted(findings, key=lambda item: item["level"] != "FAIL")


def summary(findings: List[Finding]) -> Tuple[int, int]:
    fails = sum(1 for item in findings if item["level"] == "FAIL")
    return fails, len(findings) - fails


def describe(finding: Finding) -> str:
    return f"{finding['level']} [{finding['rule']}] {finding['where']}: {finding['problem']} — fix: {finding['fix']}"


def _finding(level: str, rule: str, where: str, problem: str, fix: str) -> Finding:
    return {"level": level, "rule": rule, "where": where, "problem": problem, "fix": fix}


def _documents(rendered: str) -> Iterable[Dict[str, Any]]:
    try:
        for doc in yaml.safe_load_all(rendered or ""):
            if isinstance(doc, dict) and doc.get("kind"):
                yield doc
    except yaml.YAMLError:
        return


def _name(obj: Dict[str, Any]) -> str:
    meta = obj.get("metadata") or {}
    return f"{obj.get('kind', '?')}/{meta.get('name', '?')}"


def _pod_spec(obj: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    kind, spec = obj.get("kind"), obj.get("spec") or {}
    if kind == "Pod":
        return spec
    if kind in ("Deployment", "StatefulSet", "DaemonSet", "ReplicaSet", "Job"):
        return (spec.get("template") or {}).get("spec") or {}
    if kind == "CronJob":
        return (((spec.get("jobTemplate") or {}).get("spec") or {}).get("template") or {}).get("spec") or {}
    return None


def _cpu(value: Any) -> float:
    text = str(value or "0")
    return float(text[:-1]) / 1000 if text.endswith("m") else float(text)


def _luhn(digits: str) -> bool:
    total, alternate = 0, False
    for char in reversed(digits):
        number = int(char)
        if alternate:
            number *= 2
            if number > 9:
                number -= 9
        total += number
        alternate = not alternate
    return total % 10 == 0


def _strings(node: Any) -> Iterable[str]:
    if isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for value in node:
            yield from _strings(value)
    elif isinstance(node, str):
        yield node


def _check_object(obj: Dict[str, Any], out: List[Finding]) -> None:  # noqa: C901 — one rule list
    add = lambda *args: out.append(_finding(*args))  # noqa: E731
    kind, where, spec = obj.get("kind", ""), _name(obj), obj.get("spec") or {}

    for text in _strings(obj):
        match = next((m for m in _CARD.finditer(text)
                      if (d := re.sub(r"\D", "", m.group(0))) and 13 <= len(d) <= 19
                      and _luhn(d) and len(set(d)) > 2), None)
        if match:
            add("FAIL", "R13 no card data", where, "a value looks like a card number (passes the Luhn check)",
                "remove it: no card data in any value, manifest, log or test fixture (PCI DSS 3)")
            break

    if kind == "Namespace":
        add("FAIL", "namespaces", where, "the chart creates a namespace",
            "remove it: the platform creates and owns namespaces")
    if kind in ("ClusterRole", "ClusterRoleBinding"):
        add("FAIL", "rights", where, "cluster-wide rights",
            "use a Role and RoleBinding in your own namespace")
    if kind == "Service" and spec.get("type") in ("LoadBalancer", "NodePort"):
        add("FAIL", "R7 one front door", where, f"Service of type {spec.get('type')}",
            f"make it ClusterIP and publish it with an HTTPRoute on {_GATEWAY} in {_GATEWAY_NAMESPACE}")
    if kind == "Service" and spec.get("externalIPs"):
        add("FAIL", "R7 one front door", where, "Service with externalIPs", "remove externalIPs")
    if kind == "Gateway":
        add("FAIL", "R7 one front door", where, "a Gateway of its own",
            f"attach an HTTPRoute to {_GATEWAY} in {_GATEWAY_NAMESPACE} instead")
    if kind == "Ingress":
        add("FAIL", "R7 one front door", where, "an Ingress (nothing serves Ingress on this cluster)",
            f"replace it with an HTTPRoute to {_GATEWAY_NAMESPACE}/{_GATEWAY}, sectionName https-areeba")
    if kind == "HTTPRoute":
        for parent in spec.get("parentRefs") or []:
            if not (parent.get("name") == _GATEWAY and parent.get("namespace") == _GATEWAY_NAMESPACE
                    and parent.get("sectionName") in _LISTENERS):
                add("FAIL", "R7 one front door", where, f"parentRef {json.dumps(parent, sort_keys=True)}",
                    f"use {{name: {_GATEWAY}, namespace: {_GATEWAY_NAMESPACE}, sectionName: https-areeba}}")
    if kind in ("SecurityPolicy", "BackendTrafficPolicy", "ClientTrafficPolicy", "HTTPRouteFilter", "EnvoyPatchPolicy"):
        targets = spec.get("targetRefs") or ([spec["targetRef"]] if spec.get("targetRef") else [])
        if any((target or {}).get("kind") == "Gateway" for target in targets):
            add("FAIL", "R7 one front door", where, "targets the shared Gateway",
                "attach Envoy policies only to your own HTTPRoutes")
    if kind == "Secret" and (obj.get("data") or obj.get("stringData")) and obj.get("type") != "helm.sh/release.v1":
        add("FAIL", "R4 secrets", where, "a Secret with values in the chart",
            f"put the value in OpenBao (kv/<namespace>/<name>) and read it with an ExternalSecret ({_SECRET_STORE})")
    if kind == "ExternalSecret":
        ref = spec.get("secretStoreRef") or {}
        if ref.get("name") != _SECRET_STORE or ref.get("kind", _SECRET_STORE_KIND) != _SECRET_STORE_KIND:
            add("FAIL", "R4 secrets", where, f"secretStoreRef {json.dumps(ref, sort_keys=True)}",
                f"use {{name: {_SECRET_STORE}, kind: {_SECRET_STORE_KIND}}}")
        target = (spec.get("target") or {}).get("name") or (obj.get("metadata") or {}).get("name")
        if target in _DB_SECRETS:
            add("FAIL", "R5 database", where, f"defines the platform's database Secret '{target}'",
                "remove it: the platform creates the database, its logins and both Secrets")
    if kind == "ConfigMap":
        for key, value in (obj.get("data") or {}).items():
            if _SECRETY.search(str(key)) and isinstance(value, str) and len(value.strip()) >= 8:
                add("WARN", "R4 secrets", where, f"key '{key}' in a ConfigMap looks like a secret",
                    "if it is one, move it to OpenBao and an ExternalSecret")
    if kind in ("CiliumNetworkPolicy", "NetworkPolicy"):
        text = json.dumps(spec)
        for address in _FORBIDDEN_EGRESS:
            if address in text:
                add("FAIL", "images: no app egress to Nexus", where, f"opens traffic to {address} (Nexus)",
                    "remove it: only the nodes pull images")
    if kind == "CiliumNetworkPolicy" and spec.get("egress") and "kube-dns" not in json.dumps(spec.get("egress")):
        add("WARN", "network", where, "has egress rules but no DNS rule",
            "add egress to k8s-app: kube-dns in kube-system, port 53")
    if kind in ("ServiceMonitor", "PodMonitor", "PrometheusRule") and (obj.get("metadata") or {}).get("namespace") == "monitoring":
        add("WARN", "observability", where, "placed in namespace 'monitoring'",
            f"keep it in your namespace; the platform's Prometheus ({_PROMETHEUS_NAMESPACE}) selects it")

    pod = _pod_spec(obj)
    if pod is None:
        return
    for flag in ("hostNetwork", "hostPID", "hostIPC"):
        if pod.get(flag):
            add("FAIL", "R6 pod security", where, f"{flag}: true", "remove it")
    for volume in pod.get("volumes") or []:
        if "hostPath" in (volume or {}):
            add("FAIL", "R6 pod security", where, f"hostPath volume '{volume.get('name')}'", "use a PVC or an emptyDir")
    if not pod.get("imagePullSecrets"):
        add("WARN", "images", where, "no imagePullSecrets", f"reference '{_PULL_SECRET}'")
    pod_security = pod.get("securityContext") or {}
    for container in (pod.get("initContainers") or []) + (pod.get("containers") or []):
        cwhere = f"{where} container {container.get('name')}"
        image = str(container.get("image") or "")
        if not image.startswith(_REGISTRY + "/"):
            add("FAIL", "R3 images", cwhere, f"image '{image}' is not from {_REGISTRY}",
                f"push it to {_REGISTRY} (vendor images through its proxy repositories)")
        elif "@sha256:" not in image:
            add("WARN", "R3 images", cwhere, "image pinned by tag only", "pin it by digest")
        security = container.get("securityContext") or {}
        if security.get("privileged"):
            add("FAIL", "R6 pod security", cwhere, "privileged: true", "remove it")
        if security.get("allowPrivilegeEscalation") is not False:
            add("WARN", "R6 pod security", cwhere, "allowPrivilegeEscalation not false", "set it to false")
        if not (security.get("runAsNonRoot") or pod_security.get("runAsNonRoot")):
            add("WARN", "R6 pod security", cwhere, "runAsNonRoot not set", "run as a non-root user")
        resources = container.get("resources") or {}
        requests, limits = resources.get("requests") or {}, resources.get("limits") or {}
        if not requests.get("cpu") or not requests.get("memory") or not limits.get("memory"):
            add("WARN", "R2 resources", cwhere, "requests (cpu, memory) or memory limit missing",
                "set them; the namespace's quota counts them")
        try:
            if _cpu(requests.get("cpu")) > _MAX_POD_CPU:
                add("FAIL", "R2 resources", cwhere, f"asks for {requests.get('cpu')} CPU",
                    f"no node fits more than about {_MAX_POD_CPU:g} CPU: split it into replicas")
        except ValueError:
            pass
        for env in container.get("env") or []:
            value = env.get("value")
            if not isinstance(value, str):
                continue
            low, name = value.lower(), str(env.get("name") or "")
            for host in _DB_FORBIDDEN_HOSTS:
                if re.search(rf"(^|[^a-z0-9-]){re.escape(host)}([^a-z0-9-]|$)", low):
                    add("FAIL", "R5 database", cwhere, f"env {name} points at '{host}'",
                        "connect through your pooler pooler-<app>.databases.svc:5432")
            if "sslmode=" in low and f"sslmode={_DB_SSLMODE}" not in low:
                add("FAIL", "R5 database", cwhere, f"env {name}: sslmode is not {_DB_SSLMODE}",
                    f"use sslmode={_DB_SSLMODE} with the CA from ConfigMap postgres-ca")
            is_ref = name.upper().endswith(("_FILE", "_PATH", "_DIR", "_NAME", "_REF")) or value.startswith(("/", "$(", "http"))
            if _SECRETY.search(name) and len(value) >= 8 and not is_ref:
                add("FAIL", "R4 secrets", cwhere, f"env {name} holds a literal value",
                    "read it from a Secret made by an ExternalSecret (valueFrom.secretKeyRef)")
