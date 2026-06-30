"""Parse raw Kubernetes YAML into inventory *template drafts*.

A draft has the exact *template detail* shape that
:func:`api.services.user_template_service._detail` returns and that the frontend
``CreateTemplateModal``'s ``formFromTemplate`` consumes — so an imported workload
can be reviewed in the existing create-template modal and saved as a
``UserTemplate`` with no extra mapping.

Each workload doc (Deployment/StatefulSet/DaemonSet/Job/CronJob) becomes one
draft. A ``Service`` in the same file is folded into the matching workload's
``networking.service`` (selector ⊆ pod labels); a ``PersistentVolumeClaim`` is
used to fill in storage size. Everything imported is surfaced; lossy mappings are
reported per-draft in ``warnings`` rather than silently dropped.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

import yaml

# Mirror of the kinds the wizard manifest generator can emit.
WORKLOAD_KINDS = {"Deployment", "StatefulSet", "DaemonSet", "Job", "CronJob"}
# Env var names that almost always hold a credential. A plain inline value with a
# matching name is auto-routed through a Secret (sensitive) rather than baked into
# the template in plaintext — the deployer supplies its value at deploy time.
_SENSITIVE_KEY_RE = re.compile(
    r"(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
    r"private[_-]?key|credential|cred|auth)",
    re.IGNORECASE,
)
# Workloads that carry a replica count (the others scale differently or not at all).
REPLICA_KINDS = {"Deployment", "StatefulSet"}

_PROBE_FIELDS = (
    ("readiness", "readinessProbe"),
    ("liveness", "livenessProbe"),
    ("startup", "startupProbe"),
)

# Deploy-time sources offered per volume-mount kind (mirrors the template schema).
_VOLUME_KIND_SOURCES = {
    "configMap": ["existingConfigMap", "createConfigMap"],
    "secret": ["existingSecret", "createSecret"],
}

DraftResult = Tuple[Optional[List[Dict[str, Any]]], Optional[str]]


def _split_image(ref: str) -> Tuple[str, str]:
    """Split a container image reference into ``(image, tag)``.

    Mirrors the re-join logic in ``wizard_manifest_generator._container_spec``: a
    tag is only the part after a ``:`` in the *final* path segment. Digest-pinned
    references (``@sha256:...``) are kept whole with no separate tag.
    """
    ref = (ref or "").strip()
    if not ref:
        return "", "latest"
    last_segment = ref.rsplit("/", 1)[-1]
    if "@" in last_segment:
        return ref, ""
    if ":" in last_segment:
        repo, tag = ref.rsplit(":", 1)
        return repo, tag
    return ref, "latest"


def _pod_template(doc: Dict[str, Any], kind: str) -> Dict[str, Any]:
    """Return the pod template ({metadata, spec}) for a workload kind."""
    spec = doc.get("spec") or {}
    if kind == "CronJob":
        return ((spec.get("jobTemplate") or {}).get("spec") or {}).get("template") or {}
    return spec.get("template") or {}


def _selector_labels(doc: Dict[str, Any], pod_labels: Dict[str, Any]) -> Dict[str, str]:
    """Labels usable for Service matching: pod-template labels + selector matchLabels."""
    combined: Dict[str, str] = {}
    match_labels = ((doc.get("spec") or {}).get("selector") or {}).get("matchLabels") or {}
    for source in (match_labels, pod_labels):
        for key, value in (source or {}).items():
            if key is not None and value is not None:
                combined[str(key)] = str(value)
    return combined


def _resources(container: Dict[str, Any]) -> Dict[str, str]:
    res = container.get("resources") or {}
    requests = res.get("requests") or {}
    limits = res.get("limits") or {}
    return {
        "cpuRequest": str(requests.get("cpu") or ""),
        "cpuLimit": str(limits.get("cpu") or ""),
        "memoryRequest": str(requests.get("memory") or ""),
        "memoryLimit": str(limits.get("memory") or ""),
    }


def _ports(container: Dict[str, Any]) -> List[int]:
    ports: List[int] = []
    for entry in container.get("ports") or []:
        raw = (entry or {}).get("containerPort")
        try:
            ports.append(int(raw))
        except (TypeError, ValueError):
            continue
    return ports


def _env_schema(container: Dict[str, Any], warnings: List[str]) -> List[Dict[str, Any]]:
    """Each container env var becomes a configurable env-schema row.

    Plain values keep their value as the row default; configMap/secret refs map to
    the matching kind (secret refs are flagged sensitive). Field/resource refs
    can't be modelled as deploy-time inputs, so they're skipped with a warning.
    """
    rows: List[Dict[str, Any]] = []
    for item in container.get("env") or []:
        name = str((item or {}).get("name") or "").strip()
        if not name:
            continue
        value_from = item.get("valueFrom") or {}
        if "value" in item and not value_from:
            # A sensitive-looking name forces a Secret; the plaintext value is
            # dropped so credentials never get baked into a reusable template.
            if _SENSITIVE_KEY_RE.search(name):
                rows.append({"key": name, "required": True, "sensitive": True,
                             "kind": "secret", "default": ""})
                warnings.append(
                    f"Environment variable '{name}' looks sensitive and was set to Secret; "
                    "the deployer supplies its value at deploy time."
                )
            else:
                rows.append({"key": name, "required": False, "sensitive": False,
                             "kind": "value", "default": str(item.get("value") or "")})
        elif value_from.get("configMapKeyRef"):
            rows.append({"key": name, "required": False, "sensitive": False,
                         "kind": "configMap", "default": ""})
        elif value_from.get("secretKeyRef"):
            rows.append({"key": name, "required": False, "sensitive": True,
                         "kind": "secret", "default": ""})
        else:
            warnings.append(f"Environment variable '{name}' uses an unsupported source and was skipped.")
    return rows


def _probe(raw: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    built: Dict[str, Any] = {}
    if raw.get("initialDelaySeconds") is not None:
        built["initialDelaySeconds"] = raw["initialDelaySeconds"]
    if raw.get("periodSeconds") is not None:
        built["periodSeconds"] = raw["periodSeconds"]

    http = raw.get("httpGet")
    tcp = raw.get("tcpSocket")
    exec_action = raw.get("exec")
    if http:
        built["type"] = "http"
        built["path"] = str(http.get("path") or "/")
        if http.get("port") is not None:
            built["port"] = http["port"]
    elif tcp:
        built["type"] = "tcp"
        if tcp.get("port") is not None:
            built["port"] = tcp["port"]
    elif exec_action:
        built["type"] = "command"
        command = exec_action.get("command") or []
        built["command"] = " ".join(str(c) for c in command) if isinstance(command, list) else str(command)
    else:
        return None
    return built


def _health_checks(container: Dict[str, Any]) -> Dict[str, Any]:
    checks: Dict[str, Any] = {}
    for key, field in _PROBE_FIELDS:
        built = _probe(container.get(field) or {})
        if built:
            checks[key] = built
    return checks


def _storage(
    container: Dict[str, Any],
    pod_spec: Dict[str, Any],
    pvc_sizes: Dict[str, str],
    warnings: List[str],
) -> Optional[Dict[str, Any]]:
    """Build a storage block from the first PVC-backed volume mount."""
    volumes = pod_spec.get("volumes") or []
    pvc_volumes = {
        str((v or {}).get("name")): str((v.get("persistentVolumeClaim") or {}).get("claimName") or "")
        for v in volumes
        if (v or {}).get("persistentVolumeClaim")
    }
    if not pvc_volumes:
        return None
    for mount in container.get("volumeMounts") or []:
        vol_name = str((mount or {}).get("name") or "")
        if vol_name not in pvc_volumes:
            continue
        mount_path = str(mount.get("mountPath") or "/data")
        claim = pvc_volumes[vol_name]
        size = pvc_sizes.get(claim, "")
        if not size:
            warnings.append(
                f"Storage size for claim '{claim}' wasn't found in the file; defaulting to 1Gi."
            )
        return {
            "pvcMode": "new",
            "newPvc": {"name": claim or f"{vol_name}-pvc", "size": size or "1Gi",
                       "accessMode": "ReadWriteOnce"},
            "volumeMounts": [{"name": "data", "mountPath": mount_path,
                              "readOnly": bool(mount.get("readOnly"))}],
        }
    return None


def _config_secret_volume_mounts(
    container: Dict[str, Any],
    pod_spec: Dict[str, Any],
    warnings: List[str],
) -> List[Dict[str, Any]]:
    """Map ConfigMap/Secret-backed volume mounts into template volume-mount schema.

    The deployer later picks an existing ConfigMap/Secret or creates one. A
    ``subPath`` (mounting a single key as a file rather than the whole resource as a
    directory) is preserved so the mount behaves like the source manifest. PVC
    mounts are handled separately by :func:`_storage`.
    """
    by_name: Dict[str, str] = {}
    for vol in pod_spec.get("volumes") or []:
        name = str((vol or {}).get("name") or "")
        if not name:
            continue
        if vol.get("configMap"):
            by_name[name] = "configMap"
        elif vol.get("secret"):
            by_name[name] = "secret"

    mounts: List[Dict[str, Any]] = []
    seen_paths = set()
    for mount in container.get("volumeMounts") or []:
        kind = by_name.get(str((mount or {}).get("name") or ""))
        if not kind:
            continue
        mount_path = str(mount.get("mountPath") or "").strip()
        if not mount_path or mount_path in seen_paths:
            continue
        seen_paths.add(mount_path)
        row = {
            "mountPath": mount_path,
            "kind": kind,
            "allowedSources": _VOLUME_KIND_SOURCES[kind],
            "readOnly": bool(mount.get("readOnly")),
        }
        sub_path = str(mount.get("subPath") or "").strip()
        if sub_path:
            row["subPath"] = sub_path
        mounts.append(row)
    return mounts


def _service_block(
    services: List[Dict[str, Any]],
    match_labels: Dict[str, str],
) -> Optional[Dict[str, Any]]:
    """First Service whose selector is a non-empty subset of the workload's labels."""
    for svc in services:
        selector = (svc.get("spec") or {}).get("selector") or {}
        if not selector:
            continue
        if all(str(match_labels.get(str(k))) == str(v) for k, v in selector.items()):
            spec = svc.get("spec") or {}
            ports = spec.get("ports") or []
            first = ports[0] if ports else {}
            block: Dict[str, Any] = {
                "enabled": True,
                "type": spec.get("type") or "ClusterIP",
                "protocol": first.get("protocol") or "TCP",
            }
            if first.get("port") is not None:
                block["port"] = first["port"]
            target = first.get("targetPort")
            if target is not None:
                block["targetPort"] = target
            return block
    return None


def _draft_from_workload(
    doc: Dict[str, Any],
    services: List[Dict[str, Any]],
    pvc_sizes: Dict[str, str],
) -> Optional[Dict[str, Any]]:
    kind = str(doc.get("kind") or "")
    if kind not in WORKLOAD_KINDS:
        return None
    name = str((doc.get("metadata") or {}).get("name") or "").strip() or kind.lower()
    warnings: List[str] = []

    pod_template = _pod_template(doc, kind)
    pod_spec = pod_template.get("spec") or {}
    containers = pod_spec.get("containers") or []
    if not containers:
        warnings.append("Workload has no containers; nothing to import.")
        return {"name": name, "workloadType": kind, "warnings": warnings, "containers": []}
    if len(containers) > 1:
        warnings.append(
            f"Workload has {len(containers)} containers; only the first ('"
            f"{containers[0].get('name') or 'main'}') was imported."
        )
    container = containers[0] or {}
    image, tag = _split_image(container.get("image") or "")

    draft: Dict[str, Any] = {
        "name": name,
        "description": f"Imported from YAML ({kind})",
        "category": "Imported",
        "workloadType": kind,
        "containers": [{
            "name": str(container.get("name") or "main"),
            "image": image,
            "tag": tag,
            "pullPolicy": container.get("imagePullPolicy") or "IfNotPresent",
            "ports": _ports(container),
        }],
        "resources": _resources(container),
        # Sensible default surface for "what can change"; the user adjusts the rest.
        "schema": {"overrides": {"tag": True, "replicas": True}},
    }

    if kind in REPLICA_KINDS:
        replicas = (doc.get("spec") or {}).get("replicas")
        try:
            draft["scaling"] = {"replicas": int(replicas)}
        except (TypeError, ValueError):
            draft["scaling"] = {"replicas": 1}

    env_rows = _env_schema(container, warnings)
    if env_rows:
        draft["schema"]["env"] = env_rows

    health = _health_checks(container)
    if health:
        draft["healthChecks"] = health

    storage = _storage(container, pod_spec, pvc_sizes, warnings)
    if storage:
        draft["storage"] = storage

    volume_mounts = _config_secret_volume_mounts(container, pod_spec, warnings)
    if volume_mounts:
        draft["schema"]["volumeMounts"] = volume_mounts

    match_labels = _selector_labels(doc, (pod_template.get("metadata") or {}).get("labels") or {})
    service = _service_block(services, match_labels)
    if service:
        draft["networking"] = {"service": service}

    draft["warnings"] = warnings
    return draft


def parse_yaml_to_template_drafts(yaml_text: str) -> DraftResult:
    """Parse raw YAML into a list of template drafts (one per workload).

    Returns ``(drafts, None)`` on success or ``(None, error)`` when the YAML is
    invalid or contains no importable workloads.
    """
    if not (yaml_text or "").strip():
        return None, "No YAML content provided."

    try:
        docs = [doc for doc in yaml.safe_load_all(yaml_text) if isinstance(doc, dict)]
    except yaml.YAMLError as exc:
        return None, f"Invalid YAML: {exc}"

    if not docs:
        return None, "No Kubernetes resources found in the YAML."

    services = [d for d in docs if d.get("kind") == "Service"]
    pvc_sizes: Dict[str, str] = {}
    for d in docs:
        if d.get("kind") == "PersistentVolumeClaim":
            pvc_name = str((d.get("metadata") or {}).get("name") or "")
            size = str((((d.get("spec") or {}).get("resources") or {}).get("requests") or {}).get("storage") or "")
            if pvc_name and size:
                pvc_sizes[pvc_name] = size

    drafts: List[Dict[str, Any]] = []
    for doc in docs:
        draft = _draft_from_workload(doc, services, pvc_sizes)
        if draft is not None:
            drafts.append(draft)

    if not drafts:
        return None, (
            "No Deployment, StatefulSet, DaemonSet, Job, or CronJob found in the YAML."
        )
    return drafts, None
