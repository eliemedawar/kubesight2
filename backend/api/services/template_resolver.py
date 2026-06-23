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

ENV_SOURCES = {
    "value",
    "existingConfigMap",
    "createConfigMap",
    "existingSecret",
    "createSecret",
}
# Sources that expose the value in plaintext — forbidden for sensitive variables.
PLAINTEXT_SOURCES = {"value", "existingConfigMap", "createConfigMap"}

ResolveResult = Tuple[Optional[Dict[str, Any]], Optional[str]]


def _spec_copy(template: Dict[str, Any], key: str, default: Any) -> Any:
    value = template.get(key)
    return copy.deepcopy(value) if value is not None else copy.deepcopy(default)


class _Provisioned:
    """Accumulates Create ConfigMap/Secret data, grouped by resource name."""

    def __init__(self) -> None:
        self._config_maps: Dict[str, Dict[str, str]] = {}
        self._secrets: Dict[str, Dict[str, str]] = {}

    def add_config_map(self, name: str, key: str, value: str) -> None:
        self._config_maps.setdefault(name, {})[key] = value

    def add_secret(self, name: str, key: str, value: str) -> None:
        self._secrets.setdefault(name, {})[key] = value

    def config_maps(self) -> List[Dict[str, Any]]:
        return [{"name": n, "data": d} for n, d in self._config_maps.items()]

    def secrets(self) -> List[Dict[str, Any]]:
        return [{"name": n, "stringData": d} for n, d in self._secrets.items()]


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
        if not value and required:
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
        "configMapRefs": base_environment.get("configMapRefs") or [],
        "secretRefs": base_environment.get("secretRefs") or [],
        "provisionedConfigMaps": provisioned.config_maps(),
        "provisionedSecrets": provisioned.secrets(),
        "provisionedDockerSecrets": [docker_secret] if docker_secret else [],
        "provisionedTlsSecrets": tls_secrets,
    }

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
