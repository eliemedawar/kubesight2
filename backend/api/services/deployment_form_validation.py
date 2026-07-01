"""Validate an imported deployment form into a structured ✅/⚠️/❌ result.

The file is never trusted. Validation layers, in order:

  1. **Authorization / context** — results the service computed (template still
     exists, template version unchanged, cluster + namespace accessible).
  2. **Schema / answers** — run :func:`resolve_template`; its first error is a
     blocking ❌ (it already enforces allowed sources, sensitive-source bans,
     serviceType allow-list, ingress-needs-service, name validity, ...).
  3. **Live cluster** — referenced *existing* ConfigMaps/Secrets/TLS secrets/
     storage classes/pull secrets must exist; the image registry must be allowed.
     *Created* resources (in the resolved ``provisioned*`` lists) are skipped.
  4. **Resource / HPA guards** — CPU/memory quantities are well-formed; HPA needs
     CPU+memory requests (the resolver forces it off otherwise — surfaced as ⚠️).

Result shape: ``{"checks": [{"field","label","level","message"}], "blocking": bool}``
where ``level`` is ``"ok" | "warn" | "error"``.
"""

from __future__ import annotations

import copy
import re
from typing import Any, Dict, List, Optional

from .template_resolver import resolve_template

# Kubernetes resource quantity, e.g. 250m, 1, 512Mi, 2Gi.
_QUANTITY_RE = re.compile(r"^\d+(\.\d+)?(m|k|Ki|Mi|Gi|Ti|Pi|Ei|K|M|G|T|P|E)?$")


def _registry_of(image: str) -> str:
    """The registry host of an image reference (empty for docker.io shorthand)."""
    image = str(image or "").strip()
    if not image:
        return ""
    first = image.split("/", 1)[0]
    # A registry host has a dot or colon (port), or is 'localhost'.
    if "." in first or ":" in first or first == "localhost":
        return first
    return ""


class _Checks:
    def __init__(self) -> None:
        self.items: List[Dict[str, Any]] = []

    def add(self, field: str, label: str, level: str, message: str) -> None:
        self.items.append({"field": field, "label": label, "level": level, "message": message})

    def ok(self, field: str, label: str, message: str) -> None:
        self.add(field, label, "ok", message)

    def warn(self, field: str, label: str, message: str) -> None:
        self.add(field, label, "warn", message)

    def error(self, field: str, label: str, message: str) -> None:
        self.add(field, label, "error", message)


def validate_import(
    template: Optional[Dict[str, Any]],
    answers: Dict[str, Any],
    *,
    dropdown_data: Optional[Dict[str, List[str]]] = None,
    context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    checks = _Checks()
    ctx = context or {}
    dd = dropdown_data or {}
    namespace = str(ctx.get("namespace") or (answers.get("basics") or {}).get("namespace") or "").strip()

    # ---- 1. Authorization / context ----
    if not ctx.get("templateExists", True):
        checks.error("template", "Template", "The source template no longer exists.")
        return {"checks": checks.items, "blocking": True}
    if ctx.get("versionMatches") is False:
        checks.warn(
            "template", "Template version",
            "The template changed since this form was generated — defaults were refreshed. Review the values.",
        )
    if ctx.get("clusterAccessible") is False:
        checks.error("basics.clusterId", "Cluster access", "You do not have access to the selected cluster.")
    elif ctx.get("clusterAccessible") is True:
        checks.ok("basics.clusterId", "Cluster access", "Cluster is accessible.")
    if ctx.get("namespaceAccessible") is False:
        checks.error("basics.namespace", "Namespace access", "You do not have access to the selected namespace.")
    elif ctx.get("namespaceAccessible") is True:
        checks.ok("basics.namespace", "Namespace access", "Namespace is accessible.")

    # ---- 2. Schema / answers via the resolver ----
    # Volume mounts are not part of the Excel form — they're completed in the Deploy
    # Wizard after import. So validate against a template copy without them (and warn),
    # instead of hard-failing on their required names.
    resolve_input = template or {}
    if (resolve_input.get("schema") or {}).get("volumeMounts"):
        checks.warn(
            "volumes", "Volume mounts",
            "This template has volume mounts — configure them in the Deploy Wizard after import.",
        )
        resolve_input = copy.deepcopy(resolve_input)
        resolve_input.get("schema", {}).pop("volumeMounts", None)

    payload, err = resolve_template(resolve_input, answers or {})
    if err:
        checks.error("values", "Deployment values", err)
        # Can't do live checks without a resolved payload.
        return {"checks": checks.items, "blocking": True}
    checks.ok("values", "Deployment values", "All values are consistent with the template.")

    # ---- 3. Live cluster reference checks ----
    # Skip entirely when namespace access was denied — the references can't be
    # trusted or listed, and a spurious "not found" would only add noise on top of
    # the access error already reported above.
    if ctx.get("namespaceAccessible") is False:
        return {"checks": checks.items, "blocking": True}

    cluster_known = "namespaces" in dd
    if cluster_known and namespace:
        if namespace in (dd.get("namespaces") or []):
            checks.ok("basics.namespace", "Namespace exists", f"Namespace '{namespace}' exists.")
        else:
            checks.error("basics.namespace", "Namespace exists", f"Namespace '{namespace}' was not found in the cluster.")

    env = payload.get("environment") or {}
    provisioned_cms = {c.get("name") for c in env.get("provisionedConfigMaps") or []}
    provisioned_secrets = {s.get("name") for s in env.get("provisionedSecrets") or []}
    provisioned_tls = {t.get("name") for t in env.get("provisionedTlsSecrets") or []}
    provisioned_docker = {d.get("name") for d in env.get("provisionedDockerSecrets") or []}

    def _check_ref(name: str, kind: str, dd_key: str, provisioned: set, field: str) -> None:
        if not name or name in provisioned:
            return  # created by this deploy — nothing to verify
        if dd_key not in dd:
            return  # couldn't list; skip silently (covered by cluster-known gate)
        if name in (dd.get(dd_key) or []):
            checks.ok(field, f"{kind} reference", f"{kind} '{name}' exists.")
        else:
            checks.error(field, f"{kind} reference", f"{kind} '{name}' was not found in namespace '{namespace}'.")

    for var in env.get("envVars") or []:
        vf = var.get("valueFrom") if isinstance(var, dict) else None
        if not isinstance(vf, dict):
            continue
        if vf.get("kind") == "configMap":
            _check_ref(vf.get("name"), "ConfigMap", "configMaps", provisioned_cms, f"env.{var.get('name')}")
        elif vf.get("kind") == "secret":
            _check_ref(vf.get("name"), "Secret", "secrets", provisioned_secrets, f"env.{var.get('name')}")

    for ref in env.get("configMapRefs") or []:
        name = ref.get("name") if isinstance(ref, dict) else ref
        _check_ref(name, "ConfigMap", "configMaps", provisioned_cms, "configMapRefs")
    for ref in env.get("secretRefs") or []:
        name = ref.get("name") if isinstance(ref, dict) else ref
        _check_ref(name, "Secret", "secrets", provisioned_secrets, "secretRefs")

    networking = payload.get("networking") or {}
    ingress = networking.get("ingress") or {}
    if ingress.get("tlsEnabled") and ingress.get("tlsSecret"):
        _check_ref(ingress.get("tlsSecret"), "TLS secret", "tlsSecrets", provisioned_tls, "ingress.tls.secret")

    for container in payload.get("containers") or []:
        ips = container.get("imagePullSecret")
        if ips:
            _check_ref(ips, "Image pull secret", "imagePullSecrets", provisioned_docker, "imagePullSecret.name")

    storage = payload.get("storage") or {}
    sc = (storage.get("newPvc") or {}).get("storageClass")
    if sc and "storageClasses" in dd:
        if sc in (dd.get("storageClasses") or []):
            checks.ok("overrides.storageSize", "Storage class", f"Storage class '{sc}' exists.")
        else:
            checks.warn("overrides.storageSize", "Storage class", f"Storage class '{sc}' was not found; the cluster default may be used.")

    # Image registry allow-list.
    allowed_registries = dd.get("allowedRegistries")
    if allowed_registries:
        for container in payload.get("containers") or []:
            reg = _registry_of(container.get("image"))
            if reg and reg not in allowed_registries:
                checks.error("overrides.image", "Image registry", f"Registry '{reg}' is not in the allowed list.")
            elif reg:
                checks.ok("overrides.image", "Image registry", f"Registry '{reg}' is allowed.")

    # ---- 4. Resource / HPA guards ----
    resources = payload.get("resources") or {}
    for f_key, label in (("cpuRequest", "CPU request"), ("cpuLimit", "CPU limit"),
                         ("memoryRequest", "Memory request"), ("memoryLimit", "Memory limit")):
        raw = str(resources.get(f_key) or "").strip()
        if raw and not _QUANTITY_RE.match(raw):
            checks.error(f"overrides.resources.{f_key}", label, f"'{raw}' is not a valid Kubernetes quantity.")

    scaling = payload.get("scaling") or {}
    hpa = scaling.get("hpa") or {}
    wanted_hpa = bool(((answers.get("overrides") or {}).get("hpaEnabled")) or hpa.get("enabled"))
    has_requests = bool(str(resources.get("cpuRequest") or "").strip() and str(resources.get("memoryRequest") or "").strip())
    if wanted_hpa and not has_requests:
        checks.warn("overrides.hpaEnabled", "Autoscaling (HPA)",
                    "HPA needs CPU and memory requests — it will be disabled until both are set.")
    elif hpa.get("enabled"):
        checks.ok("overrides.hpaEnabled", "Autoscaling (HPA)", "Autoscaling is configured.")

    blocking = any(c["level"] == "error" for c in checks.items)
    return {"checks": checks.items, "blocking": blocking}
