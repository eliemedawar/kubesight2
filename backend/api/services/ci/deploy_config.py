"""What a Deploy stage is configured to do — normalized, checked, rendered.

Pure functions, no database and no cluster: the pipeline validator calls them on
save and the executor (``deploy_stage.py``) calls them again on the snapshot a
build carries, so a stage is held to the same rules whichever side reads it.

A Deploy stage names ONE target — cluster, namespace, deployment, container —
and says what to do when the deployment is not there yet. When it is there,
only that container's image changes. When it is not, the stage's own manifest
creates it: a Deployment (and optionally its Service), in the target namespace,
and nothing else — a build is not the place to create Secrets or RBAC.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any, Dict, List, Optional, Tuple

import yaml

from ...k8s_names import (  # noqa: F401 — validate_namespace is re-used by deploy_stage
    K8sNameError,
    validate_container_name,
    validate_namespace,
    validate_resource_name,
)

MAX_MANIFEST_CHARS = 64000
# What the stage's manifest may create. A Deployment is the point; a Service is
# what makes one reachable. Anything else belongs in a reviewed apply.
MANIFEST_KINDS = ("Deployment", "Service")
SERVICE_TYPES = ("ClusterIP", "NodePort")
# Where a Deploy stage deploys. ``fixed``: the cluster / namespace / deployment
# saved on the stage. ``linked``: the deployment linked to the service being
# built (services/ci/deployment_links.py), resolved when the build starts — how
# one shared pipeline deploys each service that uses it to that service's own
# deployment.
TARGET_MODES = ("fixed", "linked")
_ENVIRONMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")
# The placeholder the generated manifest shows where the image goes. The
# executor sets the image on the target container whatever the text says, so
# this is for the reader, not a template language.
IMAGE_PLACEHOLDER = "${IMAGE}"

_IMAGE_RE = re.compile(r"^[A-Za-z0-9._/:@${}-]{1,512}$")
_QUANTITY_RE = re.compile(r"^[0-9]+(\.[0-9]+)?(m|Ki|Mi|Gi|Ti|k|M|G|T)?$")
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")


class DeployConfigError(ValueError):
    """A Deploy stage's configuration was rejected. Message is user-facing."""


def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _int_or_none(value: Any, *, low: int, high: int, what: str, stage_name: str) -> Optional[int]:
    if value in (None, ""):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise DeployConfigError(f"Stage '{stage_name}': {what} must be a whole number.")
    if not low <= number <= high:
        raise DeployConfigError(f"Stage '{stage_name}': {what} must be between {low} and {high}.")
    return number


def _quantity(value: Any, what: str, stage_name: str) -> str:
    text = _clean(value, 32)
    if text and not _QUANTITY_RE.match(text):
        raise DeployConfigError(
            f"Stage '{stage_name}': {what} '{text}' is not a Kubernetes quantity "
            "(for example 250m, 1, 512Mi, 1Gi)."
        )
    return text


def _create_form(value: Any, stage_name: str) -> Dict[str, Any]:
    """The form the manifest was generated from, kept so it can be re-edited.

    Only the fields the editor offers survive; the manifest — not this — is what
    a build applies.
    """
    value = value if isinstance(value, dict) else {}
    env_in = value.get("env") if isinstance(value.get("env"), dict) else {}
    env: Dict[str, str] = {}
    for key, raw in list(env_in.items())[:50]:
        name = _clean(key, 128)
        if not name:
            continue
        if not _ENV_NAME_RE.match(name):
            raise DeployConfigError(f"Stage '{stage_name}': '{name}' is not a usable variable name.")
        env[name] = str(raw if raw is not None else "")[:4000]
    service_in = value.get("service") if isinstance(value.get("service"), dict) else {}
    service_type = _clean(service_in.get("type"), 16) or "ClusterIP"
    if service_type not in SERVICE_TYPES:
        raise DeployConfigError(
            f"Stage '{stage_name}': Service type must be one of {', '.join(SERVICE_TYPES)}."
        )
    return {
        "port": _int_or_none(value.get("port"), low=1, high=65535, what="the container port", stage_name=stage_name),
        "replicas": _int_or_none(value.get("replicas"), low=0, high=100, what="replicas", stage_name=stage_name) or 1,
        "cpuRequest": _quantity(value.get("cpuRequest"), "CPU request", stage_name),
        "cpuLimit": _quantity(value.get("cpuLimit"), "CPU limit", stage_name),
        "memoryRequest": _quantity(value.get("memoryRequest"), "memory request", stage_name),
        "memoryLimit": _quantity(value.get("memoryLimit"), "memory limit", stage_name),
        "env": env,
        # The manifest was edited by hand, so the editor stops regenerating it
        # from these fields — a reload must not quietly undo those edits.
        "customManifest": bool(value.get("customManifest")),
        "service": {
            "enabled": bool(service_in.get("enabled")),
            "port": _int_or_none(service_in.get("port"), low=1, high=65535, what="the Service port", stage_name=stage_name),
            "type": service_type,
        },
    }


def parse_manifest(text: str) -> List[Dict[str, Any]]:
    try:
        documents = [doc for doc in yaml.safe_load_all(text or "") if doc]
    except yaml.YAMLError as exc:
        raise DeployConfigError(f"The manifest is not valid YAML: {exc}")
    for doc in documents:
        if not isinstance(doc, dict):
            raise DeployConfigError("Every document in the manifest must be a Kubernetes object.")
    return documents


def _containers(deployment: Dict[str, Any]) -> List[Dict[str, Any]]:
    spec = ((deployment.get("spec") or {}).get("template") or {}).get("spec") or {}
    containers = spec.get("containers") or []
    return [c for c in containers if isinstance(c, dict)]


def check_manifest(
    text: str, *, namespace: str, deployment_name: str, container_name: str, stage_name: str
) -> List[Dict[str, Any]]:
    """The create-if-missing manifest, parsed and held to what a build may create."""
    if len(text) > MAX_MANIFEST_CHARS:
        raise DeployConfigError(
            f"Stage '{stage_name}': the manifest is {len(text)} characters; the limit is {MAX_MANIFEST_CHARS}."
        )
    documents = parse_manifest(text)
    if not documents:
        raise DeployConfigError(
            f"Stage '{stage_name}' creates the deployment when it is missing, but its manifest is empty."
        )
    deployments = []
    for doc in documents:
        kind = str(doc.get("kind") or "")
        name = str((doc.get("metadata") or {}).get("name") or "")
        if kind not in MANIFEST_KINDS:
            raise DeployConfigError(
                f"Stage '{stage_name}': the manifest may only create a Deployment and its Service, "
                f"not {kind or 'an object with no kind'}. Apply anything else through a reviewed change."
            )
        if not name:
            raise DeployConfigError(f"Stage '{stage_name}': a {kind} in the manifest has no name.")
        doc_namespace = (doc.get("metadata") or {}).get("namespace")
        if doc_namespace not in (None, "", namespace):
            raise DeployConfigError(
                f"Stage '{stage_name}': {kind}/{name} names namespace '{doc_namespace}', but this stage "
                f"deploys to '{namespace}'. Remove the namespace line or make them match."
            )
        if kind == "Deployment":
            deployments.append(doc)
    if len(deployments) != 1:
        raise DeployConfigError(
            f"Stage '{stage_name}': the manifest must contain exactly one Deployment (it has {len(deployments)})."
        )
    deployment = deployments[0]
    actual = str((deployment.get("metadata") or {}).get("name") or "")
    if actual != deployment_name:
        raise DeployConfigError(
            f"Stage '{stage_name}': the manifest's Deployment is called '{actual}', but the stage deploys "
            f"'{deployment_name}'. They must be the same, or the next build would create it again."
        )
    containers = _containers(deployment)
    if not containers:
        raise DeployConfigError(f"Stage '{stage_name}': the manifest's Deployment has no containers.")
    if container_name and not any(c.get("name") == container_name for c in containers):
        names = ", ".join(str(c.get("name")) for c in containers)
        raise DeployConfigError(
            f"Stage '{stage_name}': the manifest has no container named '{container_name}' (it has {names})."
        )
    return documents


def normalize(value: Any, stage_type: str, stage_name: str) -> Optional[Dict[str, Any]]:
    """A Deploy stage's target and create-if-missing settings, or None.

    ``authorizedBy`` is never taken from the payload — the pipeline validator
    decides it (see pipelines._stamp_deploy_authority).
    """
    if stage_type != "deploy":
        if value not in (None, "", {}):
            raise DeployConfigError(
                f"Stage '{stage_name}' is a {stage_type} stage, so it deploys nothing. "
                "Deployment targets are set on a Deploy stage."
            )
        return None
    if not isinstance(value, dict):
        raise DeployConfigError(
            f"Stage '{stage_name}' is a Deploy stage but has no target. Pick the cluster, "
            "namespace and deployment it deploys to."
        )

    mode = _clean(value.get("target"), 16).lower() or "fixed"
    if mode not in TARGET_MODES:
        raise DeployConfigError(
            f"Stage '{stage_name}': '{mode}' is not a deploy target. Use fixed or linked."
        )
    if mode == "linked":
        return _normalize_linked(value, stage_name)

    cluster_id = _clean(value.get("clusterId"), 128)
    if not cluster_id:
        raise DeployConfigError(f"Stage '{stage_name}' needs a cluster to deploy to.")
    try:
        namespace = validate_namespace(_clean(value.get("namespace"), 63))
    except K8sNameError as exc:
        raise DeployConfigError(f"Stage '{stage_name}': {exc}")
    try:
        deployment_name = validate_resource_name(
            _clean(value.get("deploymentName"), 253), "deployment name"
        )
    except K8sNameError as exc:
        raise DeployConfigError(f"Stage '{stage_name}': {exc}")
    container_name = _clean(value.get("containerName"), 63)
    if container_name:
        try:
            validate_container_name(container_name)
        except K8sNameError as exc:
            raise DeployConfigError(f"Stage '{stage_name}': {exc}")

    image = _clean(value.get("image"), 512)
    if image and not _IMAGE_RE.match(image):
        raise DeployConfigError(
            f"Stage '{stage_name}': '{image}' is not an image reference. Leave it empty to deploy "
            "the image this build pushed."
        )

    create_if_missing = bool(value.get("createIfMissing"))
    manifest = str(value.get("manifest") or "").strip()
    if create_if_missing:
        check_manifest(
            manifest,
            namespace=namespace,
            deployment_name=deployment_name,
            container_name=container_name,
            stage_name=stage_name,
        )

    return {
        "target": "fixed",
        "clusterId": cluster_id,
        "namespace": namespace,
        "deploymentName": deployment_name,
        "containerName": container_name,
        "image": image,
        # Record this deployment as the service's (the inventory link) when the
        # pipeline is saved and whenever the stage deploys. On unless switched
        # off, so a pipeline saved before links existed links on its next save.
        "linkToService": value.get("linkToService") is not False,
        "createIfMissing": create_if_missing,
        "create": _create_form(value.get("create"), stage_name),
        # Kept when create-if-missing is off, so turning it back on does not lose
        # what was written. It is only ever applied while the switch is on.
        "manifest": manifest[:MAX_MANIFEST_CHARS],
    }


def _image(value: Dict[str, Any], stage_name: str) -> str:
    image = _clean(value.get("image"), 512)
    if image and not _IMAGE_RE.match(image):
        raise DeployConfigError(
            f"Stage '{stage_name}': '{image}' is not an image reference. Leave it empty to deploy "
            "the image this build pushed."
        )
    return image


def _normalize_linked(value: Dict[str, Any], stage_name: str) -> Dict[str, Any]:
    """A stage that deploys to the deployment linked to the service being built.

    No cluster, namespace or manifest: those come from the link when a build
    starts. Never creates anything — the link names a deployment that exists.
    ``environment`` picks one link when a service has several (SIT/UAT/PROD).
    """
    environment = _clean(value.get("environment"), 64)
    if environment and not _ENVIRONMENT_RE.match(environment):
        raise DeployConfigError(
            f"Stage '{stage_name}': '{environment}' is not an environment label "
            "(letters, digits, spaces, dots, dashes and underscores)."
        )
    container_name = _clean(value.get("containerName"), 63)
    if container_name:
        try:
            validate_container_name(container_name)
        except K8sNameError as exc:
            raise DeployConfigError(f"Stage '{stage_name}': {exc}")
    return {
        "target": "linked",
        "environment": environment,
        "clusterId": "",
        "namespace": "",
        "deploymentName": "",
        "containerName": container_name,
        "image": _image(value, stage_name),
        "linkToService": False,
        "createIfMissing": False,
        "create": _create_form({}, stage_name),
        "manifest": "",
    }


def is_linked(config: Optional[Dict[str, Any]]) -> bool:
    return isinstance(config, dict) and config.get("target") == "linked"


def signature(config: Optional[Dict[str, Any]]) -> str:
    """Everything a person authorizes by saving the target.

    If any of it changes, the stage is deploying somewhere, something or in a
    way nobody has authorized, and whoever saves it next must be able to deploy
    there themselves.
    """
    if not isinstance(config, dict):
        return ""
    create = bool(config.get("createIfMissing"))
    material = {
        "clusterId": config.get("clusterId") or "",
        "namespace": config.get("namespace") or "",
        "deploymentName": config.get("deploymentName") or "",
        "containerName": config.get("containerName") or "",
        "image": config.get("image") or "",
        "createIfMissing": create,
        "manifest": (config.get("manifest") or "") if create else "",
    }
    if is_linked(config):
        # Only for linked stages, so every fixed target saved before linked
        # mode existed keeps the signature its authority stamp was taken on.
        material["target"] = "linked"
        material["environment"] = config.get("environment") or ""
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode("utf-8")).hexdigest()


def render_manifest(config: Dict[str, Any], image: str) -> Tuple[str, List[Dict[str, str]]]:
    """The create-if-missing manifest with the image set on the target container.

    Returns the YAML to apply and the objects it creates (``[{kind, name}]``),
    which is what a rollback deletes if the new deployment never comes up.
    """
    documents = check_manifest(
        str(config.get("manifest") or ""),
        namespace=config["namespace"],
        deployment_name=config["deploymentName"],
        container_name=config.get("containerName") or "",
        stage_name="Deploy",
    )
    rendered: List[Dict[str, Any]] = []
    created: List[Dict[str, str]] = []
    for doc in documents:
        doc = copy.deepcopy(doc)
        meta = doc.setdefault("metadata", {})
        meta["namespace"] = config["namespace"]
        if doc.get("kind") == "Deployment":
            containers = _containers(doc)
            target = next(
                (c for c in containers if c.get("name") == config.get("containerName")),
                containers[0],
            )
            target["image"] = image
        rendered.append(doc)
        created.append({"kind": str(doc.get("kind")), "name": str(meta.get("name"))})
    text = "\n---\n".join(
        yaml.safe_dump(doc, sort_keys=False, default_flow_style=False) for doc in rendered
    )
    return text, created


def swap_image(deployment: Dict[str, Any], container_name: str, image: str) -> str:
    """A live Deployment with only one container's image changed, as YAML.

    The object comes from ``kubectl get -o json``; the caller's apply path strips
    the server-managed fields. Nothing but ``image`` is touched.
    """
    doc = copy.deepcopy(deployment)
    doc.pop("status", None)
    containers = _containers(doc)
    target = next((c for c in containers if c.get("name") == container_name), None)
    if target is None:
        raise DeployConfigError(f"The deployment has no container named '{container_name}'.")
    target["image"] = image
    return yaml.safe_dump(doc, sort_keys=False, default_flow_style=False)


def pick_container(deployment: Dict[str, Any], wanted: str) -> Tuple[Optional[Dict[str, Any]], str]:
    """The container a stage deploys to, or (None, why not)."""
    containers = _containers(deployment)
    names = [str(c.get("name")) for c in containers]
    if not containers:
        return None, "The deployment has no containers."
    if wanted:
        match = next((c for c in containers if c.get("name") == wanted), None)
        if match is None:
            return None, (
                f"The deployment has no container named '{wanted}' (it has {', '.join(names)}). "
                "Pick one of those on the stage."
            )
        return match, ""
    if len(containers) > 1:
        return None, (
            f"The deployment runs {len(containers)} containers ({', '.join(names)}). Pick the one "
            "this stage deploys to on the stage."
        )
    return containers[0], ""
