"""Resolve a template's *schema* + a deployment's *answers* into a flat payload.

This is the keystone of the template/deployment split. A template no longer stores
deployment values directly; it stores:

  * default values for every field (the existing ``containers``/``resources``/...
    spec sub-objects), and
  * an optional ``schema`` block describing what a deployer may change
    (``overrides``), which environment variables are required/sensitive and where
    their values may come from (``env``), and which backing services the app needs
    (``dependencies``).

``resolve_template(template, answers)`` merges the two into the *exact* flat payload
shape that :func:`api.services.wizard_manifest_generator.generate_wizard_manifests`
already consumes. The generator is therefore unchanged except for emitting the
ConfigMaps/Secrets this resolver asks it to create (``provisionedConfigMaps`` /
``provisionedSecrets`` on the environment).

Schema reference (all keys optional)::

    spec.schema = {
      "overrides": {
        "image": bool, "tag": bool, "replicas": bool, "resources": bool,
        "storageSize": bool,
        "serviceType": ["ClusterIP", "NodePort", "LoadBalancer"],  # allowed set
      },
      "env": [
        {"key", "required": bool, "sensitive": bool, "default": str|None,
         "description": str, "allowedSources": [<source>...]},
      ],
      "dependencies": [
        {"kind", "name", "required": bool, "provisioning": ["create","existing"],
         "wiring": [{"from": "host"|"port"|"password"|..., "into": "<ENV>",
                     "as": "value"|"secret"}]},
      ],
      "imagePullSecret": {"mode": "none"|"existing"|"create", "name": str,
                          "overridable": bool},
    }

Answer reference::

    answers = {
      "basics": {...},                       # appName, namespace, clusterId, ...
      "overrides": {"tag", "replicas", "resources", "storageSize", "serviceType"},
      "env": {"<KEY>": {"source", "value", "configMapName"|"secretName", "key"}},
      "dependencies": {"<name>": {"mode": "create"|"existing", ...}},
      "ingress": {"host", "tls": {"mode": "create"|"existing", "secret"}},
      "changeSummary": str,
    }
"""

from __future__ import annotations

import copy
from typing import Any, Dict, List, Optional, Tuple

from .wizard_manifest_generator import validate_k8s_name

ENV_SOURCES = {
    "value",
    "existingConfigMap",
    "createConfigMap",
    "existingSecret",
    "createSecret",
}
# Sources that expose the value in plaintext — forbidden for sensitive variables.
PLAINTEXT_SOURCES = {"value", "existingConfigMap", "createConfigMap"}

# Volume mounts source a whole ConfigMap/Secret as files. Each kind allows an
# existing resource or a freshly-created one.
VOLUME_KIND_SOURCES = {
    "configMap": ["existingConfigMap", "createConfigMap"],
    "secret": ["existingSecret", "createSecret"],
}

SERVICE_TYPES = {"ClusterIP", "NodePort", "LoadBalancer"}
SERVICE_PROTOCOLS = {"TCP", "UDP"}
# Kubernetes restricts NodePort allocation to this range by default.
NODE_PORT_MIN, NODE_PORT_MAX = 30000, 32767


def _slug(value: str, fallback: str) -> str:
    cleaned = "".join(c if c.isalnum() else "-" for c in str(value).lower())
    cleaned = "-".join(part for part in cleaned.split("-") if part)
    return cleaned or fallback

ResolveResult = Tuple[Optional[Dict[str, Any]], Optional[str]]


def _spec_copy(template: Dict[str, Any], key: str, default: Any) -> Any:
    value = template.get(key)
    return copy.deepcopy(value) if value is not None else copy.deepcopy(default)


class _Provisioned:
    """Accumulates Create ConfigMap/Secret data, grouped by resource name.

    Text values land in ``data``/``stringData``; binary values (already base64
    from the client) land in a ConfigMap's ``binaryData`` / a Secret's ``data``,
    which the kubelet decodes back to raw bytes on mount.
    """

    def __init__(self) -> None:
        self._config_maps: Dict[str, Dict[str, str]] = {}
        self._config_maps_binary: Dict[str, Dict[str, str]] = {}
        self._secrets: Dict[str, Dict[str, str]] = {}
        self._secrets_binary: Dict[str, Dict[str, str]] = {}

    def add_config_map(self, name: str, key: str, value: str, binary: bool = False) -> None:
        bucket = self._config_maps_binary if binary else self._config_maps
        bucket.setdefault(name, {})[key] = value

    def add_secret(self, name: str, key: str, value: str, binary: bool = False) -> None:
        bucket = self._secrets_binary if binary else self._secrets
        bucket.setdefault(name, {})[key] = value

    @staticmethod
    def _merge(
        text: Dict[str, Dict[str, str]],
        binary: Dict[str, Dict[str, str]],
        text_key: str,
        binary_key: str,
    ) -> List[Dict[str, Any]]:
        names: List[str] = list(text)
        names += [n for n in binary if n not in text]
        out: List[Dict[str, Any]] = []
        for name in names:
            doc: Dict[str, Any] = {"name": name}
            if text.get(name):
                doc[text_key] = text[name]
            if binary.get(name):
                doc[binary_key] = binary[name]
            out.append(doc)
        return out

    def config_maps(self) -> List[Dict[str, Any]]:
        return self._merge(self._config_maps, self._config_maps_binary, "data", "binaryData")

    def secrets(self) -> List[Dict[str, Any]]:
        return self._merge(self._secrets, self._secrets_binary, "stringData", "data")


def _resolve_storage(answer: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Build a storage payload from the deployer's PVC/PV choices.

    Returns ``None`` when the deployer left storage disabled, signalling the caller
    to keep the template's storage defaults.
    """
    if not answer or not answer.get("enabled"):
        return None
    mount_path = str(answer.get("mountPath") or "/data").strip() or "/data"
    storage: Dict[str, Any] = {
        "volumeMounts": [{"name": "data", "mountPath": mount_path, "readOnly": False}],
    }
    if (answer.get("mode") or "new") == "existing":
        storage["pvcMode"] = "existing"
        storage["existingPvc"] = str(answer.get("existingPvc") or "").strip()
        return storage

    new_pvc = answer.get("newPvc") or {}
    storage["pvcMode"] = "new"
    storage["newPvc"] = {
        "name": str(new_pvc.get("name") or "data-pvc").strip(),
        "size": str(new_pvc.get("size") or "1Gi").strip(),
        "accessMode": new_pvc.get("accessMode") or "ReadWriteOnce",
        "storageClass": str(new_pvc.get("storageClass") or "").strip(),
    }
    pv = answer.get("pv") or {}
    if pv.get("enabled"):
        storage["advanced"] = {
            "createManualPv": True,
            "pvName": str(pv.get("name") or "").strip(),
            "capacity": str(pv.get("capacity") or new_pvc.get("size") or "1Gi").strip(),
            "storageType": pv.get("storageType") or "hostPath",
            "reclaimPolicy": pv.get("reclaimPolicy") or "Retain",
            "hostPath": str(pv.get("hostPath") or "").strip(),
            "nfsServer": str(pv.get("nfsServer") or "").strip(),
            "nfsPath": str(pv.get("nfsPath") or "").strip(),
            "localPath": str(pv.get("localPath") or "").strip(),
            "nodeName": str(pv.get("nodeName") or "").strip(),
        }
        # A manually-bound PV uses an empty storage class on the PVC.
        storage["newPvc"]["storageClass"] = ""
    return storage


def _resolve_image_pull_secret(
    schema_cfg: Dict[str, Any],
    answer: Dict[str, Any],
    app_name: str,
    containers: List[Dict[str, Any]],
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Decide the pod's imagePullSecret and any registry secret to provision.

    Returns ``(docker_secret_or_None, error)`` and stamps ``imagePullSecret`` on the
    first container so the generator emits a pod-level reference.
    """
    schema_cfg = schema_cfg or {}
    template_mode = (schema_cfg.get("mode") or "").strip()
    overridable = bool(schema_cfg.get("overridable"))

    # Image pull secrets are a deploy-time concern: the deployer's choice wins unless
    # the template explicitly pins a mode and disallows overriding it.
    locked = bool(template_mode) and not overridable
    mode = template_mode if locked else (str(answer.get("mode") or template_mode or "none").strip())

    if mode == "none":
        return None, None
    if mode not in ("existing", "create"):
        return None, f"Unknown image pull secret mode '{mode}'."

    if mode == "existing":
        name = str((schema_cfg.get("name") if locked else (answer.get("name") or schema_cfg.get("name"))) or "").strip()
        if not name:
            return None, "An image pull secret name is required."
        if containers:
            containers[0]["imagePullSecret"] = name
        return None, None

    # mode == "create": the deployer supplies registry credentials at deploy time.
    name = str(answer.get("name") or schema_cfg.get("name") or f"{app_name}-registry").strip()
    username = str(answer.get("username") or "").strip()
    password = str(answer.get("password") or "")
    if not username or not password:
        return None, "Registry username and password are required to create a pull secret."
    if containers:
        containers[0]["imagePullSecret"] = name
    docker_secret = {
        "name": name,
        "registry": str(answer.get("registry") or "https://index.docker.io/v1/").strip(),
        "username": username,
        "password": password,
    }
    if answer.get("email"):
        docker_secret["email"] = str(answer["email"]).strip()
    return docker_secret, None


def _resolve_env_entry(
    field: Dict[str, Any],
    answer: Dict[str, Any],
    provisioned: _Provisioned,
) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Resolve one env schema field into a generator env entry (or an error)."""
    key = str(field.get("key") or "").strip()
    if not key:
        return None, None
    required = bool(field.get("required"))
    sensitive = bool(field.get("sensitive"))
    allowed = field.get("allowedSources") or list(ENV_SOURCES)

    source = (answer.get("source") or "").strip()
    if not source:
        # Fall back to the template default when the deployer left it blank.
        default = field.get("default")
        if default is not None and str(default) != "":
            return {"name": key, "value": str(default)}, None
        if required:
            return None, f"Environment variable '{key}' is required."
        return None, None

    if source not in ENV_SOURCES:
        return None, f"Unknown source '{source}' for '{key}'."
    if source not in allowed:
        return None, f"Source '{source}' is not allowed for '{key}'."
    if sensitive and source in PLAINTEXT_SOURCES:
        return None, f"'{key}' is sensitive and cannot use source '{source}'."

    if source == "value":
        value = str(answer.get("value") or "")
        if not value:
            # Deployer left it blank — fall back to the template default so a
            # defaulted variable deploys as-is. A typed value overrides the default.
            default = field.get("default")
            if default is not None and str(default) != "":
                return {"name": key, "value": str(default)}, None
            if required:
                return None, f"Environment variable '{key}' is required."
        return {"name": key, "value": value}, None

    ref_key = str(answer.get("key") or key).strip() or key

    if source in ("existingConfigMap", "createConfigMap"):
        cm_name = str(answer.get("configMapName") or "").strip()
        if not cm_name:
            return None, f"A ConfigMap name is required for '{key}'."
        if source == "createConfigMap":
            provisioned.add_config_map(cm_name, ref_key, str(answer.get("value") or ""))
        return {"name": key, "valueFrom": {"kind": "configMap", "name": cm_name, "key": ref_key}}, None

    secret_name = str(answer.get("secretName") or "").strip()
    if not secret_name:
        return None, f"A Secret name is required for '{key}'."
    if source == "createSecret":
        if not answer.get("value") and required:
            return None, f"A value is required to create the secret for '{key}'."
        provisioned.add_secret(secret_name, ref_key, str(answer.get("value") or ""))
    return {"name": key, "valueFrom": {"kind": "secret", "name": secret_name, "key": ref_key}}, None


def _resolve_volume_mounts(
    volume_schema: List[Dict[str, Any]],
    volume_answers: Dict[str, Any],
    provisioned: _Provisioned,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Resolve each declared volume mount into a generator ``mountedFiles`` entry.

    The deployer chooses an existing ConfigMap/Secret or asks to create one; in the
    latter case the supplied file entries are accumulated for provisioning.
    """
    mounts: List[Dict[str, Any]] = []
    for index, vm in enumerate(volume_schema):
        mount_path = str(vm.get("mountPath") or "").strip()
        if not mount_path:
            continue
        kind = "secret" if vm.get("kind") == "secret" else "configMap"
        allowed = vm.get("allowedSources") or VOLUME_KIND_SOURCES[kind]
        answer = volume_answers.get(mount_path) or {}
        source = (answer.get("source") or allowed[0]).strip()
        if source not in allowed:
            return [], f"Source '{source}' is not allowed for the volume mounted at '{mount_path}'."

        vol_name = f"vol-{_slug(mount_path, str(index))}"
        read_only = bool(vm.get("readOnly"))
        items = answer.get("items") or []
        # A subPath mounts a single key as a file at the path (preserved from an
        # imported manifest), instead of mounting the whole resource as a directory.
        sub_path = str(vm.get("subPath") or "").strip()
        extra = {"subPath": sub_path} if sub_path else {}

        if kind == "configMap":
            name = str(answer.get("configMapName") or "").strip()
            if not name:
                return [], f"A ConfigMap name is required for the volume mounted at '{mount_path}'."
            if source == "createConfigMap":
                for item in items:
                    item_key = str(item.get("key") or "").strip()
                    if item_key:
                        provisioned.add_config_map(name, item_key, str(item.get("value") or ""), binary=bool(item.get("binary")))
            mounts.append({"volumeName": vol_name, "mountPath": mount_path, "configMap": name, "readOnly": read_only, **extra})
        else:
            name = str(answer.get("secretName") or "").strip()
            if not name:
                return [], f"A Secret name is required for the volume mounted at '{mount_path}'."
            if source == "createSecret":
                for item in items:
                    item_key = str(item.get("key") or "").strip()
                    if item_key:
                        provisioned.add_secret(name, item_key, str(item.get("value") or ""), binary=bool(item.get("binary")))
            mounts.append({"volumeName": vol_name, "mountPath": mount_path, "secret": name, "readOnly": read_only, **extra})
    return mounts, None


def _apply_overrides(
    template: Dict[str, Any],
    schema_overrides: Dict[str, Any],
    overrides: Dict[str, Any],
    containers: List[Dict[str, Any]],
    resources: Dict[str, Any],
    storage: Dict[str, Any],
    networking: Dict[str, Any],
    scaling: Dict[str, Any],
) -> Optional[str]:
    """Mutate the spec sub-objects with deployer overrides the schema permits."""
    if overrides.get("tag") and schema_overrides.get("tag") and containers:
        containers[0]["tag"] = str(overrides["tag"]).strip()
    if overrides.get("image") and schema_overrides.get("image") and containers:
        containers[0]["image"] = str(overrides["image"]).strip()

    if overrides.get("replicas") is not None and schema_overrides.get("replicas"):
        try:
            scaling["replicas"] = max(0, int(overrides["replicas"]))
        except (TypeError, ValueError):
            return "Replicas override must be a whole number."

    # Enabling/disabling autoscaling is always a safe deploy-time choice; the
    # resource-request guard below protects correctness regardless of the value.
    if overrides.get("hpaEnabled") is not None:
        scaling.setdefault("hpa", {})["enabled"] = bool(overrides["hpaEnabled"])

    # HPA value overrides (min/max replicas, CPU/memory thresholds). A blank
    # field inherits the template default; a provided value overwrites it.
    hpa_override = overrides.get("hpa")
    if isinstance(hpa_override, dict):
        hpa = scaling.setdefault("hpa", {})
        for field in ("minReplicas", "maxReplicas", "cpuThreshold", "memoryThreshold"):
            raw = hpa_override.get(field)
            if raw in (None, ""):
                continue
            try:
                hpa[field] = max(0, int(raw))
            except (TypeError, ValueError):
                return f"HPA {field} override must be a whole number."

    if overrides.get("resources") and schema_overrides.get("resources"):
        for field in ("cpuRequest", "cpuLimit", "memoryRequest", "memoryLimit"):
            if overrides["resources"].get(field):
                resources[field] = str(overrides["resources"][field]).strip()

    if overrides.get("storageSize") and schema_overrides.get("storageSize"):
        new_pvc = storage.setdefault("newPvc", {})
        new_pvc["size"] = str(overrides["storageSize"]).strip()

    allowed_types = schema_overrides.get("serviceType")
    if overrides.get("serviceType") and isinstance(allowed_types, list):
        chosen = str(overrides["serviceType"]).strip()
        if chosen not in allowed_types:
            return f"Service type '{chosen}' is not permitted by this template."
        networking.setdefault("service", {})["type"] = chosen
    return None


def _apply_dependencies(
    deps_schema: List[Dict[str, Any]],
    deps_answers: Dict[str, Any],
    app_name: str,
    env_entries: List[Dict[str, Any]],
    provisioned: _Provisioned,
) -> Optional[str]:
    """Wire each dependency's outputs into env vars per its ``wiring`` block."""
    existing_keys = {e["name"] for e in env_entries}
    for dep in deps_schema:
        name = str(dep.get("name") or dep.get("kind") or "").strip()
        if not name:
            continue
        # Informational dependencies (no wiring) are documentation only — they are
        # surfaced to the deployer but resolve to nothing.
        if not (dep.get("wiring") or []):
            continue
        required = bool(dep.get("required"))
        answer = deps_answers.get(name) or {}
        mode = (answer.get("mode") or "").strip()
        if not mode:
            if required:
                return f"Dependency '{name}' is required — choose create or existing."
            continue
        if mode not in ("create", "existing"):
            return f"Dependency '{name}' has an invalid mode '{mode}'."

        # In "create" mode the in-cluster service DNS name defaults to the dep name.
        default_host = name if mode == "create" else str(answer.get("host") or "").strip()
        for wire in dep.get("wiring") or []:
            into = str(wire.get("into") or "").strip()
            if not into or into in existing_keys:
                continue
            source_field = str(wire.get("from") or "").strip()
            as_kind = (wire.get("as") or "value").strip()

            if as_kind == "secret":
                secret_name = str(answer.get("secretName") or f"{app_name}-{name}").strip()
                ref_key = str(answer.get("passwordKey") or source_field or into.lower()).strip()
                if mode == "create":
                    provisioned.add_secret(secret_name, ref_key, str(answer.get(source_field) or ""))
                env_entries.append({"name": into, "valueFrom": {"kind": "secret", "name": secret_name, "key": ref_key}})
            else:
                value = answer.get(source_field)
                if value is None and source_field == "host":
                    value = default_host
                env_entries.append({"name": into, "value": str(value or "")})
            existing_keys.add(into)
    return None


def detect_container_ports(containers: List[Dict[str, Any]]) -> List[int]:
    """Collect the distinct container ports declared across all containers.

    Kubesight's flat template stores ports as a list of numbers
    (``container["ports"] = [8080, ...]``), but be lenient and also accept the
    rendered-manifest shape (``[{"containerPort": 8080}]``).
    """
    ports: List[int] = []
    for container in containers or []:
        for raw in container.get("ports") or []:
            value = raw.get("containerPort") if isinstance(raw, dict) else raw
            try:
                num = int(value)
            except (TypeError, ValueError):
                continue
            if 1 <= num <= 65535 and num not in ports:
                ports.append(num)
    return ports


def _resolve_service_exposure(
    answer: Optional[Dict[str, Any]],
    app_name: str,
    networking: Dict[str, Any],
    containers: List[Dict[str, Any]],
) -> Optional[str]:
    """Apply the deployer's Service Exposure choices to ``networking['service']``.

    When ``answer`` is ``None`` the deployer never reached the step (e.g. an older
    client or an Add-to-Bundle), so the template's own service defaults are left
    untouched. Otherwise the deployer's choice is authoritative: it either disables
    the Service or rebuilds it from the chosen name/type/ports.

    Returns an error string, or ``None`` on success.
    """
    if answer is None:
        return None

    service = networking.setdefault("service", {})

    if not answer.get("createService"):
        # The deployer explicitly opted out — never silently create a Service.
        service["enabled"] = False
        return None

    svc_type = str(answer.get("serviceType") or service.get("type") or "ClusterIP").strip()
    if svc_type not in SERVICE_TYPES:
        return f"Service type '{svc_type}' is not valid (use ClusterIP, NodePort, or LoadBalancer)."

    svc_name = str(answer.get("serviceName") or f"{app_name}-service").strip()
    name_err = validate_k8s_name(svc_name)
    if name_err:
        return f"Service name {name_err[0].lower()}{name_err[1:]}"

    detected = detect_container_ports(containers)
    raw_ports = answer.get("ports")
    if not isinstance(raw_ports, list) or not raw_ports:
        # Fall back to the detected container ports when the client sent none.
        raw_ports = [{"port": p, "targetPort": p} for p in detected]

    resolved_ports: List[Dict[str, Any]] = []
    seen_ports: set = set()
    for entry in raw_ports:
        if not isinstance(entry, dict):
            continue
        # A port can be deselected in the UI; skip anything not included.
        if "include" in entry and not entry.get("include"):
            continue
        try:
            port = int(entry.get("port"))
        except (TypeError, ValueError):
            return "Each service port must be a whole number."
        if not (1 <= port <= 65535):
            return f"Service port {port} is out of range (1–65535)."
        if port in seen_ports:
            return f"Service port {port} is listed more than once."
        seen_ports.add(port)

        target_raw = entry.get("targetPort")
        if target_raw in (None, ""):
            target_port = port
        else:
            try:
                target_port = int(target_raw)
            except (TypeError, ValueError):
                return f"Target port for service port {port} must be a whole number."
            if not (1 <= target_port <= 65535):
                return f"Target port {target_port} is out of range (1–65535)."

        protocol = str(entry.get("protocol") or "TCP").upper()
        if protocol not in SERVICE_PROTOCOLS:
            return f"Protocol '{protocol}' is not valid (use TCP or UDP)."

        name = str(entry.get("name") or "").strip() or f"{protocol.lower()}-{port}"
        spec: Dict[str, Any] = {
            "name": name,
            "protocol": protocol,
            "port": port,
            "targetPort": target_port,
        }

        node_port_raw = entry.get("nodePort")
        if node_port_raw not in (None, ""):
            if svc_type != "NodePort":
                return "A nodePort can only be set when the service type is NodePort."
            try:
                node_port = int(node_port_raw)
            except (TypeError, ValueError):
                return f"nodePort for service port {port} must be a whole number."
            if not (NODE_PORT_MIN <= node_port <= NODE_PORT_MAX):
                return f"nodePort {node_port} is out of range ({NODE_PORT_MIN}–{NODE_PORT_MAX})."
            spec["nodePort"] = node_port

        resolved_ports.append(spec)

    if not resolved_ports:
        return "A Service requires at least one port — include a port or disable the Service."

    # The deployer's choice fully replaces the template's service defaults. Drop the
    # legacy single-port keys so only the resolved ``ports`` list is authoritative.
    service.pop("port", None)
    service.pop("targetPort", None)
    service.pop("protocol", None)
    service.update({
        "enabled": True,
        "name": svc_name,
        "type": svc_type,
        "ports": resolved_ports,
    })
    return None


def resolve_template(template: Dict[str, Any], answers: Dict[str, Any]) -> ResolveResult:
    """Merge a template + deployment answers into a flat wizard payload.

    Returns ``(payload, None)`` on success or ``(None, error)`` on the first
    validation failure.
    """
    template = template or {}
    answers = answers or {}
    schema = template.get("schema") or {}

    basics = dict(answers.get("basics") or {})
    app_name = str(basics.get("appName") or template.get("name") or template.get("id") or "app").strip()

    containers = _spec_copy(template, "containers", [])
    resources = _spec_copy(template, "resources", {})
    storage = _spec_copy(template, "storage", {})
    networking = _spec_copy(template, "networking", {})
    scaling = _spec_copy(template, "scaling", {})
    health_checks = _spec_copy(template, "healthChecks", {})
    base_environment = _spec_copy(template, "environment", {})

    override_error = _apply_overrides(
        template,
        schema.get("overrides") or {},
        answers.get("overrides") or {},
        containers,
        resources,
        storage,
        networking,
        scaling,
    )
    if override_error:
        return None, override_error

    # Service Exposure: the deployer decides whether the deployment also creates a
    # Service. Their choice (when present) is authoritative over the template default.
    service_error = _resolve_service_exposure(
        answers.get("serviceExposure"),
        app_name,
        networking,
        containers,
    )
    if service_error:
        return None, service_error

    # A deployer's storage choices replace the template's storage defaults entirely.
    resolved_storage = _resolve_storage(answers.get("storage") or {})
    if resolved_storage is not None:
        storage = resolved_storage

    provisioned = _Provisioned()
    env_entries: List[Dict[str, Any]] = []
    env_answers = answers.get("env") or {}
    env_schema = schema.get("env") or []

    if env_schema:
        for field in env_schema:
            entry, err = _resolve_env_entry(field, env_answers.get(field.get("key")) or {}, provisioned)
            if err:
                return None, err
            if entry:
                env_entries.append(entry)
    else:
        # No schema: carry the template's default env vars through untouched.
        env_entries = base_environment.get("envVars") or []

    # Ad-hoc variables the deployer added (not declared in the template schema).
    # They allow every source and are treated as optional, non-sensitive.
    existing_env_names = {e.get("name") for e in env_entries}
    for extra in answers.get("extraEnv") or []:
        key = str(extra.get("name") or "").strip()
        if not key or key in existing_env_names:
            continue
        field = {"key": key, "required": False, "sensitive": False, "allowedSources": list(ENV_SOURCES)}
        entry, err = _resolve_env_entry(field, extra, provisioned)
        if err:
            return None, err
        if entry:
            env_entries.append(entry)
            existing_env_names.add(key)

    dep_error = _apply_dependencies(
        schema.get("dependencies") or [],
        answers.get("dependencies") or {},
        app_name,
        env_entries,
        provisioned,
    )
    if dep_error:
        return None, dep_error

    mounted_files, vm_error = _resolve_volume_mounts(
        schema.get("volumeMounts") or [],
        answers.get("volumes") or {},
        provisioned,
    )
    if vm_error:
        return None, vm_error

    docker_secret, ips_error = _resolve_image_pull_secret(
        schema.get("imagePullSecret") or {},
        answers.get("imagePullSecret") or {},
        app_name,
        containers,
    )
    if ips_error:
        return None, ips_error

    # Ingress host/path/TLS come from the deployer, never the template.
    tls_secrets: List[Dict[str, Any]] = []
    ingress_answer = answers.get("ingress") or {}
    if ingress_answer:
        # An Ingress routes through a Service, so one must exist for this deployment.
        if not (networking.get("service") or {}).get("enabled"):
            return None, "Ingress requires a Service. Please create or select a Service."
        ingress = networking.setdefault("ingress", {})
        ingress["enabled"] = True
        if ingress_answer.get("host"):
            ingress["host"] = str(ingress_answer["host"]).strip()
        if ingress_answer.get("path"):
            ingress["path"] = str(ingress_answer["path"]).strip()
        tls = ingress_answer.get("tls") or {}
        tls_mode = (tls.get("mode") or "").strip()
        if tls_mode == "existing":
            if not tls.get("secret"):
                return None, "Select the existing TLS certificate secret."
            ingress["tlsEnabled"] = True
            ingress["tlsSecret"] = str(tls["secret"]).strip()
        elif tls_mode == "create":
            secret_name = str(tls.get("secret") or f"{app_name}-tls").strip()
            cert = tls.get("cert") or ""
            key = tls.get("key") or ""
            if not cert or not key:
                return None, "A certificate and private key are required to create a TLS secret."
            tls_secrets.append({"name": secret_name, "cert": cert, "key": key})
            ingress["tlsEnabled"] = True
            ingress["tlsSecret"] = secret_name

    environment = {
        **base_environment,
        "envVars": env_entries,
        "mountedFiles": (base_environment.get("mountedFiles") or []) + mounted_files,
        "configMapRefs": base_environment.get("configMapRefs") or [],
        "secretRefs": base_environment.get("secretRefs") or [],
        "provisionedConfigMaps": provisioned.config_maps(),
        "provisionedSecrets": provisioned.secrets(),
        "provisionedDockerSecrets": [docker_secret] if docker_secret else [],
        "provisionedTlsSecrets": tls_secrets,
    }

    # HPA needs CPU + memory requests to compute utilization. Force it off in the
    # merged config when either is missing so the payload stays consistent with
    # what the manifest generator will emit.
    hpa_cfg = scaling.get("hpa")
    if isinstance(hpa_cfg, dict) and hpa_cfg.get("enabled"):
        has_requests = bool(
            str(resources.get("cpuRequest") or "").strip()
            and str(resources.get("memoryRequest") or "").strip()
        )
        if not has_requests:
            hpa_cfg["enabled"] = False

    payload = {
        "basics": basics,
        "workloadType": template.get("workloadType") or "Deployment",
        "containers": containers,
        "environment": environment,
        "resources": resources,
        "storage": storage,
        "networking": networking,
        "healthChecks": health_checks,
        "scaling": scaling,
        "changeSummary": answers.get("changeSummary") or "",
    }
    return payload, None
