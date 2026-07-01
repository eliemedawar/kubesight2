"""Turn a ``UserTemplate`` into an ordered list of deployment-form fields.

This module is the single source of truth shared by the Excel *generator* and
*parser*, so the round-trip is position-independent: every field carries

  * ``key``          — a stable string id written into the workbook's hidden
                       metadata (``field_key -> cell``) map,
  * ``answer_path``  — where the value nests in the wizard ``answers`` object
                       (``None`` for locked/display-only rows), and
  * ``type``         — how to coerce the cell value on import.

The template's ``schema`` block decides which fields are editable/required/
dropdown; everything else is emitted as a *locked* display row. Fields map 1:1
onto the ``answers`` contract that
:func:`api.services.template_resolver.resolve_template` consumes, so import never
re-implements deployment logic — it only rebuilds ``answers`` and re-resolves.

Design choices that keep manual input minimal and secret-safe:
  * Service type is exposed as ``overrides.serviceType`` (resolver applies it
    without rebuilding the template's ports), not a full ports table.
  * Ingress TLS and imagePullSecret only offer *references* (existing secrets) —
    never inline cert/key/password — so no secret material lives in the Excel.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

# Bump when the field layout / metadata contract changes in a breaking way.
FORM_SCHEMA_VERSION = 1

# Simple-Mode groups, in display order. Advanced groups only appear when the
# template's schema actually exposes them.
GROUP_BASIC = "Basic Info"
GROUP_LOCKED = "Template Defaults (locked)"
GROUP_IMAGE = "Image Version"
GROUP_ENV = "Environment Variables"
GROUP_NETWORK = "Network / Ingress"
GROUP_SCALING = "Scaling / HPA"
GROUP_STORAGE = "Storage"
GROUP_VOLUMES = "Volumes"
GROUP_DEPENDENCIES = "Dependencies"
GROUP_PULL_SECRET = "Image Pull Secret"
GROUP_REVIEW = "Review Notes"

GROUP_ORDER = [
    GROUP_BASIC,
    GROUP_LOCKED,
    GROUP_IMAGE,
    GROUP_ENV,
    GROUP_NETWORK,
    GROUP_SCALING,
    GROUP_STORAGE,
    GROUP_VOLUMES,
    GROUP_DEPENDENCIES,
    GROUP_PULL_SECRET,
    GROUP_REVIEW,
]

# Advanced groups hidden by default in the UI unless the user reveals them.
ADVANCED_GROUPS = {GROUP_STORAGE, GROUP_VOLUMES, GROUP_DEPENDENCIES, GROUP_PULL_SECRET}

# Live dropdown data keys — resolved against the cluster at generation time. A
# field naming one of these gets its allowed values baked into the workbook.
DD_NAMESPACES = "namespaces"
DD_CONFIG_MAPS = "configMaps"
DD_SECRETS = "secrets"
DD_TLS_SECRETS = "tlsSecrets"
DD_STORAGE_CLASSES = "storageClasses"
DD_IMAGE_PULL_SECRETS = "imagePullSecrets"

ENV_SOURCE_OPTIONS = ["value", "existingConfigMap", "createConfigMap", "existingSecret", "createSecret"]
# Sources allowed for a sensitive env var — plaintext sources are forbidden by the
# resolver, so we never offer them for a sensitive field.
ENV_SOURCE_OPTIONS_SENSITIVE = ["existingSecret", "createSecret"]

_TRUE_STRINGS = {"true", "yes", "y", "1", "on", "enabled"}


def _slug(value: str, fallback: str) -> str:
    """Lowercase, hyphen-separated slug for autofilled resource names."""
    cleaned = "".join(c if c.isalnum() else "-" for c in str(value).lower())
    cleaned = "-".join(part for part in cleaned.split("-") if part)
    return cleaned or fallback


def template_version(template: Dict[str, Any]) -> str:
    """Stable content-hash of a template detail dict.

    Kubesight has no template version table (versions are name-based), so we hash
    the resolved detail. Any spec/schema change yields a new token, letting import
    detect drift between generation and upload.
    """
    blob = json.dumps(template or {}, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]


def _field(
    key: str,
    label: str,
    group: str,
    *,
    type: str = "text",
    answer_path: Optional[List[Any]] = None,
    locked: bool = False,
    required: bool = False,
    default: Any = "",
    options: Optional[List[str]] = None,
    dropdown: Optional[str] = None,
    sensitive: bool = False,
    help: str = "",
) -> Dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "group": group,
        "type": type,
        "answerPath": answer_path,
        "locked": locked,
        "required": required,
        "default": "" if default is None else default,
        "options": options,
        "dropdown": dropdown,
        "sensitive": sensitive,
        "help": help,
    }


def _first_container(template: Dict[str, Any]) -> Dict[str, Any]:
    containers = template.get("containers") or []
    return containers[0] if containers and isinstance(containers[0], dict) else {}


def _bool_options() -> List[str]:
    return ["Yes", "No"]


def build_form_fields(template: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Derive the ordered form-field list for a template detail dict.

    ``template`` is the flattened detail shape returned by
    ``user_template_service.get_user_template_detail`` (spec keys hoisted to top
    level plus ``schema``).
    """
    template = template or {}
    schema = template.get("schema") or {}
    overrides = schema.get("overrides") or {}
    container = _first_container(template)
    resources = template.get("resources") or {}
    scaling = template.get("scaling") or {}
    hpa = scaling.get("hpa") or {}
    networking = template.get("networking") or {}
    service = networking.get("service") or {}
    ingress = networking.get("ingress") or {}

    # Base name for autofilled created-resource names, mirroring the wizard's
    # ``<app>-secret`` / ``<app>-config`` convention. Derived from the template's
    # default app name; the deployer can override any of them in the form.
    base = _slug(template.get("name") or template.get("id") or "app", "app")

    fields: List[Dict[str, Any]] = []

    # --- Basic Info -------------------------------------------------------
    fields.append(_field(
        "basics.clusterId", "Cluster", GROUP_BASIC,
        type="select", answer_path=["basics", "clusterId"], required=True,
        dropdown="clusters", help="Target cluster you can access.",
    ))
    fields.append(_field(
        "basics.namespace", "Namespace", GROUP_BASIC,
        type="select", answer_path=["basics", "namespace"], required=True,
        dropdown=DD_NAMESPACES, help="Namespace to deploy into.",
    ))
    fields.append(_field(
        "basics.appName", "Deployment name", GROUP_BASIC,
        type="text", answer_path=["basics", "appName"], required=True,
        default=template.get("name") or "",
        help="DNS-1123 name for the workload (lowercase letters, digits, '-').",
    ))
    fields.append(_field(
        "basics.version", "Version label", GROUP_BASIC,
        type="text", answer_path=["basics", "version"], required=False,
        help="Optional app.kubernetes.io/version label.",
    ))

    # --- Template Defaults (locked, display only) -------------------------
    fields.append(_field(
        "locked.workloadType", "Workload kind", GROUP_LOCKED,
        locked=True, default=template.get("workloadType") or "Deployment",
    ))
    if container.get("image") and not overrides.get("image"):
        fields.append(_field(
            "locked.image", "Container image", GROUP_LOCKED,
            locked=True, default=str(container.get("image") or ""),
        ))
    if container.get("pullPolicy"):
        fields.append(_field(
            "locked.pullPolicy", "Image pull policy", GROUP_LOCKED,
            locked=True, default=str(container.get("pullPolicy")),
        ))
    if service.get("type") and not overrides.get("serviceType"):
        fields.append(_field(
            "locked.serviceType", "Service type", GROUP_LOCKED,
            locked=True, default=str(service.get("type")),
        ))
    if not overrides.get("replicas") and scaling.get("replicas") is not None:
        fields.append(_field(
            "locked.replicas", "Replicas", GROUP_LOCKED,
            locked=True, default=str(scaling.get("replicas")),
        ))

    # --- Image Version ----------------------------------------------------
    if overrides.get("image"):
        fields.append(_field(
            "overrides.image", "Container image", GROUP_IMAGE,
            type="text", answer_path=["overrides", "image"],
            default=str(container.get("image") or ""),
            help="Full image reference (must be an allowed registry).",
        ))
    if overrides.get("tag"):
        fields.append(_field(
            "overrides.tag", "Image tag / version", GROUP_IMAGE,
            type="text", answer_path=["overrides", "tag"], required=True,
            default=str(container.get("tag") or ""),
            help="The version to deploy, e.g. 1.4.2.",
        ))

    # --- Environment Variables -------------------------------------------
    # Each variable gets separate, self-explanatory cells; the *source* is inferred
    # on import from whichever is filled (see ``_infer_env_entry``). Existing
    # references are dropdowns; a value creates a new ConfigMap/Secret with an
    # autofilled name. Precedence: existing Secret > existing ConfigMap > value.
    for entry in schema.get("env") or []:
        if not isinstance(entry, dict):
            continue
        key = str(entry.get("key") or "").strip()
        if not key:
            continue
        sensitive = bool(entry.get("sensitive"))
        allowed = set(entry.get("allowedSources") or (
            ENV_SOURCE_OPTIONS_SENSITIVE if sensitive else ENV_SOURCE_OPTIONS
        ))
        if sensitive:
            # A sensitive var can only come from a Secret — never plaintext.
            allowed &= set(ENV_SOURCE_OPTIONS_SENSITIVE)
            if not allowed:
                allowed = set(ENV_SOURCE_OPTIONS_SENSITIVE)
        req = bool(entry.get("required"))
        desc = str(entry.get("description") or "")
        prefix = (desc + " ") if desc else ""

        def _env_field(suffix, label, role, **kw):
            f = _field(f"env.{key}.{suffix}", f"{key} — {label}", GROUP_ENV, **kw)
            f["envKey"] = key
            f["envRole"] = role
            return f

        # Create-only: one value cell. On import Kubesight creates a Secret (sensitive)
        # or a ConfigMap (non-sensitive) from the value, with an autofilled name — no
        # existing-reference pickers. Leaving it blank falls back to the template
        # default (Kubesight fills it in).
        if sensitive:
            v_label = "secret value"
            v_help = (
                "Value for a new Secret Kubesight creates (name autofilled). "
                "Leave blank to keep the template default. Sensitive — avoid sharing this file."
            )
        else:
            v_label = "value"
            v_help = (
                "Value stored in a new ConfigMap Kubesight creates (name autofilled). "
                "Leave blank to keep the template default."
            )
        vf = _env_field(
            "value", v_label, "value",
            type="text", sensitive=sensitive, required=req,
            default="" if sensitive else str(entry.get("default") or ""),
            help=prefix + v_help,
        )
        # Context the importer needs to choose the create source + autofill the name.
        vf["envAllowed"] = sorted(allowed)
        vf["envSensitive"] = sensitive
        vf["createSecretName"] = f"{base}-secret"
        vf["createConfigMapName"] = f"{base}-config"
        fields.append(vf)

        # The data key only applies when the value lands in a ConfigMap/Secret. A
        # plain-value-only variable never does, so we omit the cell to reduce clutter.
        if allowed & {"createConfigMap", "createSecret", "existingConfigMap", "existingSecret"}:
            fields.append(_env_field(
                "key", "data key", "dataKey",
                type="text", help=f"Key inside the created ConfigMap/Secret (defaults to {key}).",
            ))

    # --- Network / Ingress ------------------------------------------------
    allowed_types = overrides.get("serviceType")
    if isinstance(allowed_types, list) and allowed_types:
        fields.append(_field(
            "overrides.serviceType", "Service type", GROUP_NETWORK,
            type="select", answer_path=["overrides", "serviceType"],
            options=list(allowed_types), default=str(service.get("type") or allowed_types[0]),
            help="How the Service is exposed.",
        ))
    if ingress:
        fields.append(_field(
            "ingress.host", "Ingress host / FQDN", GROUP_NETWORK,
            type="text", answer_path=["ingress", "host"],
            default=str(ingress.get("host") or ""),
            help="External hostname routed to this service (leave blank to skip).",
        ))
        fields.append(_field(
            "ingress.path", "Ingress path", GROUP_NETWORK,
            type="text", answer_path=["ingress", "path"],
            default=str(ingress.get("path") or "/"),
        ))
        fields.append(_field(
            "ingress.tls.mode", "TLS mode", GROUP_NETWORK,
            type="select", answer_path=["ingress", "tls", "mode"],
            options=["none", "existing"],
            default="existing" if ingress.get("tlsEnabled") else "none",
            help="Use an existing TLS secret (no cert/key material in this form).",
        ))
        fields.append(_field(
            "ingress.tls.secret", "TLS secret", GROUP_NETWORK,
            type="select", answer_path=["ingress", "tls", "secret"],
            dropdown=DD_TLS_SECRETS, default=str(ingress.get("tlsSecret") or ""),
            help="Existing TLS secret to use when TLS mode is 'existing'.",
        ))

    # --- Scaling / HPA ----------------------------------------------------
    if overrides.get("replicas"):
        fields.append(_field(
            "overrides.replicas", "Replicas", GROUP_SCALING,
            type="number", answer_path=["overrides", "replicas"],
            default=str(scaling.get("replicas") if scaling.get("replicas") is not None else 1),
            help="Number of pod replicas.",
        ))
    # HPA toggle + values are always offerable (resolver disables HPA when the
    # deployment lacks CPU+memory requests, which the validator surfaces).
    if hpa or overrides.get("replicas"):
        fields.append(_field(
            "overrides.hpaEnabled", "Enable autoscaling (HPA)", GROUP_SCALING,
            type="bool", answer_path=["overrides", "hpaEnabled"],
            options=_bool_options(),
            default="Yes" if hpa.get("enabled") else "No",
            help="Requires CPU and memory requests to be set.",
        ))
        for f_key, label in (
            ("minReplicas", "HPA min replicas"),
            ("maxReplicas", "HPA max replicas"),
            ("cpuThreshold", "HPA CPU target %"),
            ("memoryThreshold", "HPA memory target %"),
        ):
            fields.append(_field(
                f"overrides.hpa.{f_key}", label, GROUP_SCALING,
                type="number", answer_path=["overrides", "hpa", f_key],
                default=str(hpa.get(f_key)) if hpa.get(f_key) is not None else "",
            ))

    if overrides.get("resources"):
        for f_key, label in (
            ("cpuRequest", "CPU request"),
            ("cpuLimit", "CPU limit"),
            ("memoryRequest", "Memory request"),
            ("memoryLimit", "Memory limit"),
        ):
            fields.append(_field(
                f"overrides.resources.{f_key}", label, GROUP_SCALING,
                type="text", answer_path=["overrides", "resources", f_key],
                default=str(resources.get(f_key) or ""),
                help="e.g. 250m / 512Mi. Blank keeps the template default.",
            ))

    # --- Storage (advanced) ----------------------------------------------
    if overrides.get("storageSize"):
        fields.append(_field(
            "overrides.storageSize", "Storage size", GROUP_STORAGE,
            type="text", answer_path=["overrides", "storageSize"],
            default=str((template.get("storage") or {}).get("newPvc", {}).get("size") or ""),
            help="PVC size, e.g. 5Gi.",
        ))

    # --- Volumes ---------------------------------------------------------
    # Volume mounts are intentionally NOT exposed in the Excel form — they are
    # configured only in Kubesight (the Deploy Wizard's Volumes step) after import.

    # --- Dependencies (advanced) -----------------------------------------
    for dep in schema.get("dependencies") or []:
        if not isinstance(dep, dict):
            continue
        name = str(dep.get("name") or dep.get("kind") or "").strip()
        if not name or not (dep.get("wiring") or []):
            continue
        req = bool(dep.get("required"))
        provisioning = dep.get("provisioning") or ["existing", "create"]
        fields.append(_field(
            f"dependencies.{name}.mode", f"{name} — mode", GROUP_DEPENDENCIES,
            type="select", answer_path=["dependencies", name, "mode"], required=req,
            options=[m for m in ("existing", "create") if m in provisioning] or ["existing", "create"],
            help=f"Provision or reuse the {name} dependency.",
        ))
        # One column per distinct wiring source (host/port/password/...).
        seen_from: set = set()
        for wire in dep.get("wiring") or []:
            src = str(wire.get("from") or "").strip()
            if not src or src in seen_from:
                continue
            seen_from.add(src)
            if (wire.get("as") or "value") == "secret":
                # Secret-backed wiring: only the secret name is referenced here.
                fields.append(_field(
                    f"dependencies.{name}.secretName", f"{name} — secret name", GROUP_DEPENDENCIES,
                    type="select", answer_path=["dependencies", name, "secretName"],
                    dropdown=DD_SECRETS, default=f"{base}-{_slug(name, 'dep')}",
                    help="Existing secret holding the credential (no value in this form).",
                ))
            else:
                fields.append(_field(
                    f"dependencies.{name}.{src}", f"{name} — {src}", GROUP_DEPENDENCIES,
                    type="text", answer_path=["dependencies", name, src],
                ))

    # --- Image Pull Secret (advanced, reference only) --------------------
    ips = schema.get("imagePullSecret") or {}
    if ips.get("overridable"):
        fields.append(_field(
            "imagePullSecret.mode", "Image pull secret mode", GROUP_PULL_SECRET,
            type="select", answer_path=["imagePullSecret", "mode"],
            options=["none", "existing"], default=str(ips.get("mode") or "none"),
        ))
        fields.append(_field(
            "imagePullSecret.name", "Image pull secret name", GROUP_PULL_SECRET,
            type="select", answer_path=["imagePullSecret", "name"],
            dropdown=DD_IMAGE_PULL_SECRETS, default=str(ips.get("name") or ""),
        ))

    # --- Review Notes -----------------------------------------------------
    fields.append(_field(
        "changeSummary", "Deployment note / change reason", GROUP_REVIEW,
        type="text", answer_path=["changeSummary"],
        help="Why this deployment is being made (shown to approvers).",
    ))

    return fields


def _coerce(value: Any, ftype: str) -> Any:
    if value is None:
        return None
    if ftype == "number":
        try:
            if isinstance(value, bool):
                return None
            return int(float(str(value).strip()))
        except (TypeError, ValueError):
            return None
    if ftype == "bool":
        return str(value).strip().lower() in _TRUE_STRINGS
    text = str(value).strip()
    return text


def _set_path(root: Dict[str, Any], path: List[Any], value: Any) -> None:
    node = root
    for seg in path[:-1]:
        nxt = node.get(seg)
        if not isinstance(nxt, dict):
            nxt = {}
            node[seg] = nxt
        node = nxt
    node[path[-1]] = value


def _infer_env_entry(roles: Dict[str, Any], value_meta: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Turn the filled env cells for one variable into a resolver env answer.

    Create-only: a provided value creates a Secret (sensitive) or a ConfigMap
    (non-sensitive) with an autofilled name. A blank value returns ``None`` so the
    template default is used. If the template forbids creating (only ``value`` or an
    existing reference is allowed), fall back to a literal value where permitted.
    """
    if roles.get("value") in (None, ""):
        return None

    meta = value_meta or {}
    allowed = set(meta.get("envAllowed") or [])
    value = roles["value"]
    entry: Optional[Dict[str, Any]]
    if meta.get("envSensitive"):
        entry = {"source": "createSecret", "secretName": meta.get("createSecretName") or "", "value": value}
    elif "createConfigMap" in allowed:
        entry = {"source": "createConfigMap", "configMapName": meta.get("createConfigMapName") or "", "value": value}
    elif "value" in allowed:
        entry = {"source": "value", "value": value}
    elif "createSecret" in allowed:
        entry = {"source": "createSecret", "secretName": meta.get("createSecretName") or "", "value": value}
    else:
        return None

    data_key = roles.get("dataKey")
    if data_key:
        entry["key"] = data_key
    return entry


def assemble_answers(values: Dict[str, Any], fields: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Fold a flat ``{field_key: raw_value}`` map into the wizard ``answers`` dict.

    Locked/display fields (``answerPath is None``) are ignored — locked values are
    always re-sourced from the live template, never trusted from the file. Blank
    optional values are dropped so template defaults win. Environment variables use
    role-tagged cells whose source is inferred by :func:`_infer_env_entry`.
    """
    answers: Dict[str, Any] = {}
    by_key = {f["key"]: f for f in fields}

    # Collect env role cells per variable, plus the value cell's metadata.
    env_filled: Dict[str, Dict[str, Any]] = {}
    env_value_meta: Dict[str, Dict[str, Any]] = {}

    for key, raw in (values or {}).items():
        field = by_key.get(key)
        if not field:
            continue
        role = field.get("envRole")
        if role:
            if role == "value":
                env_value_meta[field["envKey"]] = field
            coerced = _coerce(raw, field.get("type") or "text")
            if coerced not in (None, ""):
                env_filled.setdefault(field["envKey"], {})[role] = coerced
            continue
        if not field.get("answerPath"):
            continue
        coerced = _coerce(raw, field.get("type") or "text")
        if coerced in (None, ""):
            # Keep explicit False for bool toggles; drop blanks otherwise.
            if not (field.get("type") == "bool" and coerced is False):
                continue
        _set_path(answers, list(field["answerPath"]), coerced)

    for env_key, roles in env_filled.items():
        entry = _infer_env_entry(roles, env_value_meta.get(env_key))
        if entry:
            answers.setdefault("env", {})[env_key] = entry

    return answers
