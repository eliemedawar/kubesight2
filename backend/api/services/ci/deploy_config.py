"""What a Deploy stage is configured to do — normalized, checked, rendered.

Pure functions, no database and no cluster: the pipeline validator calls them on
save and the executor (``deploy_stage.py``) calls them again on the snapshot a
build carries, so a stage is held to the same rules whichever side reads it.

A Deploy stage names ONE target — cluster, namespace, deployment, container —
and says what to do when the deployment is not there yet. When it is there,
only that container's image changes. When it is not, the stage's own manifest
creates it: a Deployment (and optionally its Service), in the target namespace,
and nothing else — a build is not the place to create Secrets or RBAC. A
pipeline that builds several images (a frontend and a backend) can instead name
several deployments of one namespace, each with the image stage it takes its
image from; they must all exist, and change together.

Or it names a Helm release and an Inventory chart: the first build installs
the chart (nothing has to be filled in), later builds upgrade the release
keeping the values it runs with. Either way the build's image goes into the
chart value(s) the stage names — or, for a chart that runs several images,
each image stage's image into its own values, all in one install/upgrade.
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
# deployment. ``helm``: a release of an Inventory chart (see _normalize_helm).
TARGET_MODES = ("fixed", "linked", "helm")
# Where a Helm target puts the build's image: ``split`` — a repository value and
# a tag value (image.repository / image.tag, the common chart layout);
# ``full`` — one value holding the whole reference.
HELM_IMAGE_MODES = ("split", "full")
# How many images a Helm target sets: ``one`` — the build's image (or the
# stage's fixed one) into ``imageValues``; ``several`` — ``images``, one row per
# image, each naming the image stage it comes from (or a fixed image) and its
# own chart values, all set in the same install/upgrade.
HELM_IMAGE_SETS = ("one", "several")
# Where a row of a ``several`` target takes its image from.
HELM_IMAGE_SOURCES = ("stage", "fixed")
MAX_HELM_IMAGES = 12
# How many deployments a picked (``fixed``) target changes: ``one`` — the
# deployment saved on the stage; ``several`` — ``deployments``, one row per
# deployment (and container) in the stage's namespace, each with the image
# stage its image comes from (or a fixed image), applied as one change.
DEPLOYMENT_SETS = ("one", "several")
MAX_DEPLOYMENTS = 12
_VALUE_PATH_RE = re.compile(r"^[A-Za-z0-9_-]+(\.[A-Za-z0-9_-]+){0,15}$")
# helm's own rule: a DNS label, and short enough for the names charts derive.
_RELEASE_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
MAX_RELEASE_CHARS = 53
# Where a missing deployment comes from: the stage's own form/manifest, or an
# inventory template.
CREATE_SOURCES = ("form", "template")
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


# What a build may answer for an inventory template, by where the value comes
# from. Never a Secret it would have to write: a credential is referenced by
# the name of a Secret that already exists, so no value is stored here.
ENV_ANSWER_SOURCES = ("value", "existingSecret", "existingConfigMap", "createConfigMap")
VOLUME_ANSWER_SOURCES = ("existingConfigMap", "existingSecret")
_ANSWER_NAME_RE = re.compile(r"^[a-z0-9]([-a-z0-9.]*[a-z0-9])?$")


def template_answers(value: Any, where: str) -> Dict[str, Any]:
    """Answers for an inventory template's questions, kept on a Deploy stage
    or a deployment link so a build can render the template unattended.

    ``where`` names the stage/link in messages. Raises DeployConfigError.
    """
    value = value if isinstance(value, dict) else {}
    env_in = value.get("env") if isinstance(value.get("env"), dict) else {}
    env: Dict[str, Dict[str, str]] = {}
    for key, raw in list(env_in.items())[:100]:
        name = _clean(key, 128)
        if not name or not isinstance(raw, dict):
            continue
        source = _clean(raw.get("source"), 32)
        if not source:
            continue
        if source == "createSecret":
            raise DeployConfigError(
                f"{where}: '{name}' would create a Secret. A build never writes credentials — "
                "choose an existing Secret by name instead."
            )
        if source not in ENV_ANSWER_SOURCES:
            raise DeployConfigError(f"{where}: '{source}' is not a source for '{name}'.")
        answer = {"source": source}
        if source in ("value", "createConfigMap"):
            answer["value"] = str(raw.get("value") if raw.get("value") is not None else "")[:4000]
        if source in ("existingSecret",):
            answer["secretName"] = _answer_name(raw.get("secretName"), name, "Secret", where)
        if source in ("existingConfigMap", "createConfigMap"):
            answer["configMapName"] = _answer_name(raw.get("configMapName"), name, "ConfigMap", where)
        if source != "value":
            answer["key"] = _clean(raw.get("key"), 253) or name
        env[name] = answer
    volumes_in = value.get("volumes") if isinstance(value.get("volumes"), dict) else {}
    volumes: Dict[str, Dict[str, str]] = {}
    for path, raw in list(volumes_in.items())[:50]:
        mount = _clean(path, 512)
        if not mount or not isinstance(raw, dict):
            continue
        source = _clean(raw.get("source"), 32)
        if not source:
            continue
        if source not in VOLUME_ANSWER_SOURCES:
            raise DeployConfigError(
                f"{where}: the volume at '{mount}' must come from an existing ConfigMap or Secret — "
                "a build does not create the files it mounts."
            )
        field = "configMapName" if source == "existingConfigMap" else "secretName"
        kind = "ConfigMap" if source == "existingConfigMap" else "Secret"
        volumes[mount] = {"source": source, field: _answer_name(raw.get(field), mount, kind, where)}
    return {"env": env, "volumes": volumes}


def _answer_name(value: Any, what: str, kind: str, where: str) -> str:
    name = _clean(value, 253)
    if not name:
        raise DeployConfigError(f"{where}: name the {kind} '{what}' comes from.")
    if not _ANSWER_NAME_RE.match(name):
        raise DeployConfigError(f"{where}: '{name}' is not a valid {kind} name.")
    return name


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
    source = _clean(value.get("source"), 16).lower() or "form"
    if source not in CREATE_SOURCES:
        raise DeployConfigError(
            f"Stage '{stage_name}': '{source}' is not a way to create the deployment."
        )
    template_id = _clean(value.get("templateId"), 120)
    if source == "template" and not template_id:
        raise DeployConfigError(
            f"Stage '{stage_name}': pick the inventory template to create the deployment from."
        )
    return {
        # "form": the manifest below. "template": an inventory template, read
        # when the build deploys (services/ci/deploy_templates.py).
        "source": source,
        "templateId": template_id if source == "template" else "",
        "templateName": _clean(value.get("templateName"), 160) if source == "template" else "",
        "answers": (
            template_answers(value.get("answers"), f"Stage '{stage_name}'")
            if source == "template"
            else {"env": {}, "volumes": {}}
        ),
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
            f"Stage '{stage_name}': '{mode}' is not a deploy target. Use fixed, linked or helm."
        )
    if mode == "linked":
        return _normalize_linked(value, stage_name)
    if mode == "helm":
        return _normalize_helm(value, stage_name)

    cluster_id = _clean(value.get("clusterId"), 128)
    if not cluster_id:
        raise DeployConfigError(f"Stage '{stage_name}' needs a cluster to deploy to.")
    try:
        namespace = validate_namespace(_clean(value.get("namespace"), 63))
    except K8sNameError as exc:
        raise DeployConfigError(f"Stage '{stage_name}': {exc}")
    deployment_set = _clean(value.get("deploymentSet"), 16).lower() or "one"
    if deployment_set not in DEPLOYMENT_SETS:
        raise DeployConfigError(
            f"Stage '{stage_name}': '{deployment_set}' is not a number of deployments. Use one or several."
        )
    if deployment_set == "several":
        return {
            "target": "fixed",
            "clusterId": cluster_id,
            "namespace": namespace,
            # The rows say where; nothing on the stage itself is deployed to.
            "deploymentName": "",
            "containerName": "",
            "image": "",
            "linkToService": value.get("linkToService") is not False,
            # Each deployment must exist: one stage creating several from one
            # manifest is not a change a build should make.
            "createIfMissing": False,
            "create": _create_form(value.get("create"), stage_name),
            "manifest": str(value.get("manifest") or "").strip()[:MAX_MANIFEST_CHARS],
            "deploymentSet": "several",
            "deployments": _deployment_rows(value.get("deployments"), stage_name),
        }
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
    create = _create_form(value.get("create"), stage_name)
    # A template is rendered and checked by the pipeline validator, which can
    # read the inventory; this module stays free of the database.
    if create_if_missing and create["source"] != "template":
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
        "create": create,
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


def _value_path(value: Any, what: str, stage_name: str) -> str:
    path = _clean(value, 200)
    if not path:
        raise DeployConfigError(f"Stage '{stage_name}': name the chart value for the {what}.")
    if not _VALUE_PATH_RE.match(path):
        raise DeployConfigError(
            f"Stage '{stage_name}': '{path}' is not a chart value path (for example image.repository)."
        )
    return path


def _normalize_helm(value: Dict[str, Any], stage_name: str) -> Dict[str, Any]:
    """A stage that installs or upgrades a release of an Inventory Helm chart.

    The release is installed with the chart's values the first time — nothing
    has to be filled in — and upgraded keeping the values it runs with after
    that. Each build changes only the image value(s) named here.
    """
    cluster_id = _clean(value.get("clusterId"), 128)
    if not cluster_id:
        raise DeployConfigError(f"Stage '{stage_name}' needs a cluster to deploy to.")
    try:
        namespace = validate_namespace(_clean(value.get("namespace"), 63))
    except K8sNameError as exc:
        raise DeployConfigError(f"Stage '{stage_name}': {exc}")
    release = _clean(value.get("releaseName"), 64).lower()
    if not release:
        raise DeployConfigError(f"Stage '{stage_name}': name the Helm release it deploys.")
    if len(release) > MAX_RELEASE_CHARS or not _RELEASE_RE.match(release):
        raise DeployConfigError(
            f"Stage '{stage_name}': '{release}' is not a release name (lowercase letters, digits "
            f"and '-', at most {MAX_RELEASE_CHARS} characters)."
        )
    helm_in = value.get("helm") if isinstance(value.get("helm"), dict) else {}
    chart_id = _clean(helm_in.get("chartTemplateId"), 120)
    if not chart_id:
        raise DeployConfigError(f"Stage '{stage_name}': pick the Inventory chart to deploy.")
    image_set = _clean(helm_in.get("imageSet"), 16).lower() or "one"
    if image_set not in HELM_IMAGE_SETS:
        raise DeployConfigError(
            f"Stage '{stage_name}': '{image_set}' is not a number of images. Use one or several."
        )
    image_values = _image_values(helm_in.get("imageValues"), stage_name, "")
    several = image_set == "several"
    images = _helm_images(helm_in.get("images"), stage_name) if several else []
    helm = {
        "chartTemplateId": chart_id,
        # Shown in the editor; the pipeline validator writes the real name.
        "chartName": _clean(helm_in.get("chartName"), 160),
        # Empty: whichever version is the chart's current one when the build runs.
        "chartVersion": _clean(helm_in.get("chartVersion"), 64),
        # A values-*.yaml stored on the chart, for the first install only.
        "valuesFile": _clean(helm_in.get("valuesFile"), 500),
        # Kept while several images are set, so switching back loses nothing.
        "imageValues": image_values,
    }
    if several:
        # Only written when used, so a one-image target reads (and signs) as
        # it did before several images existed.
        helm.update({"imageSet": "several", "images": images})
    return {
        "target": "helm",
        "clusterId": cluster_id,
        "namespace": namespace,
        "releaseName": release,
        "deploymentName": "",
        "containerName": "",
        "image": _image(value, stage_name),
        "linkToService": False,
        "createIfMissing": False,
        "create": _create_form({}, stage_name),
        "manifest": "",
        "helm": helm,
    }


def _image_values(value: Any, stage_name: str, where: str) -> Dict[str, str]:
    """Where one image goes in the chart: a repository and a tag value, or one
    value holding the whole reference. ``where`` names a row in messages."""
    value = value if isinstance(value, dict) else {}
    mode = _clean(value.get("mode"), 16).lower() or "split"
    if mode not in HELM_IMAGE_MODES:
        raise DeployConfigError(
            f"Stage '{stage_name}'{where}: '{mode}' is not a way to set the image. Use split or full."
        )
    if mode == "full":
        return {"mode": "full", "path": _value_path(value.get("path"), f"image{where}", stage_name)}
    values = {
        "mode": "split",
        "repository": _value_path(value.get("repository"), f"image repository{where}", stage_name),
        "tag": _value_path(value.get("tag"), f"image tag{where}", stage_name),
    }
    if values["repository"] == values["tag"]:
        raise DeployConfigError(
            f"Stage '{stage_name}'{where}: the repository and the tag must be different chart values."
        )
    return values


def _value_paths(image_values: Dict[str, str]) -> List[str]:
    if image_values.get("mode") == "full":
        return [image_values["path"]]
    return [image_values["repository"], image_values["tag"]]


def _helm_images(value: Any, stage_name: str) -> List[Dict[str, Any]]:
    """The rows of a target that sets several images.

    Each names where its image comes from — an image stage of this pipeline
    (checked by the pipeline validator, which sees every stage) or a fixed
    reference — and the chart values it goes into. No two rows may write the
    same value, nor one value inside another (``api.image`` and
    ``api.image.tag``): one of them would be lost.
    """
    rows_in = [row for row in (value if isinstance(value, list) else []) if isinstance(row, dict)]
    if not rows_in:
        raise DeployConfigError(
            f"Stage '{stage_name}' sets several images but lists none. Add the images it deploys."
        )
    if len(rows_in) > MAX_HELM_IMAGES:
        raise DeployConfigError(
            f"Stage '{stage_name}' sets {len(rows_in)} images; the limit is {MAX_HELM_IMAGES}."
        )
    rows: List[Dict[str, Any]] = []
    owner: Dict[str, int] = {}
    for number, raw in enumerate(rows_in, start=1):
        where = f", image {number}"
        source, stage, image = _row_source(raw, stage_name, where)
        image_values = _image_values(raw.get("imageValues"), stage_name, where)
        for path in _value_paths(image_values):
            for other, other_row in owner.items():
                if path == other or path.startswith(other + ".") or other.startswith(path + "."):
                    clash = "the same chart value" if path == other else "overlapping chart values"
                    raise DeployConfigError(
                        f"Stage '{stage_name}': image {other_row} and image {number} set {clash} "
                        f"('{other}' and '{path}'). Each image needs values of its own."
                    )
            owner[path] = number
        rows.append({"from": source, "stage": stage, "image": image, "imageValues": image_values})
    return rows


def _row_source(raw: Dict[str, Any], stage_name: str, where: str) -> Tuple[str, str, str]:
    """Where a row's image comes from: ``(from, stage, image)`` — an image
    stage of this pipeline (checked by the pipeline validator, which sees every
    stage) or a fixed reference."""
    source = _clean(raw.get("from"), 16).lower() or "stage"
    if source not in HELM_IMAGE_SOURCES:
        raise DeployConfigError(
            f"Stage '{stage_name}'{where}: '{source}' is not where an image comes from. "
            "Use an image stage or a fixed image."
        )
    stage = _clean(raw.get("stage"), 120) if source == "stage" else ""
    image = _clean(raw.get("image"), 512) if source == "fixed" else ""
    if source == "stage" and not stage:
        raise DeployConfigError(f"Stage '{stage_name}'{where}: pick the image stage it comes from.")
    if source == "fixed" and not image:
        raise DeployConfigError(f"Stage '{stage_name}'{where}: write the image it deploys.")
    if image and not _IMAGE_RE.match(image):
        raise DeployConfigError(f"Stage '{stage_name}'{where}: '{image}' is not an image reference.")
    return source, stage, image


def _deployment_rows(value: Any, stage_name: str) -> List[Dict[str, Any]]:
    """The rows of a target that changes several deployments — a frontend and
    a backend built by the same pipeline, say. Each names a deployment in the
    stage's namespace, optionally its container, and where its image comes
    from. A deployment may appear twice only for two different containers."""
    rows_in = [row for row in (value if isinstance(value, list) else []) if isinstance(row, dict)]
    if not rows_in:
        raise DeployConfigError(
            f"Stage '{stage_name}' deploys to several deployments but lists none. Add the deployments."
        )
    if len(rows_in) > MAX_DEPLOYMENTS:
        raise DeployConfigError(
            f"Stage '{stage_name}' deploys to {len(rows_in)} deployments; the limit is {MAX_DEPLOYMENTS}."
        )
    rows: List[Dict[str, Any]] = []
    for number, raw in enumerate(rows_in, start=1):
        where = f", deployment {number}"
        try:
            name = validate_resource_name(_clean(raw.get("deploymentName"), 253), "deployment name")
        except K8sNameError as exc:
            raise DeployConfigError(f"Stage '{stage_name}'{where}: {exc}")
        container = _clean(raw.get("containerName"), 63)
        if container:
            try:
                validate_container_name(container)
            except K8sNameError as exc:
                raise DeployConfigError(f"Stage '{stage_name}'{where}: {exc}")
        source, stage, image = _row_source(raw, stage_name, where)
        for other_number, other in enumerate(rows, start=1):
            if other["deploymentName"] != name:
                continue
            if not container or not other["containerName"] or container == other["containerName"]:
                raise DeployConfigError(
                    f"Stage '{stage_name}': deployments {other_number} and {number} both change "
                    f"'{name}'. Name a different container on each, or remove one."
                )
        rows.append(
            {"deploymentName": name, "containerName": container, "from": source, "stage": stage, "image": image}
        )
    return rows


def deployment_rows(config: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The rows of a picked target that changes several deployments; [] for one."""
    if not isinstance(config, dict) or config.get("target", "fixed") != "fixed":
        return []
    if config.get("deploymentSet") != "several":
        return []
    return [row for row in config.get("deployments") or [] if isinstance(row, dict)]


def image_rows(config: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Every row that names its own image source — several Helm images or
    several deployments; [] for a stage that deploys one image."""
    return helm_images(config) or deployment_rows(config)


def target_views(config: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The stage's config once per deployment it changes — itself for one,
    a copy naming each row's deployment and container for several — so code
    that knows one deployment (inventory links) handles each in turn."""
    if not isinstance(config, dict):
        return []
    rows = deployment_rows(config)
    if not rows:
        return [config]
    return [
        {**config, "deploymentName": row["deploymentName"], "containerName": row["containerName"]}
        for row in rows
    ]


def helm_images(config: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """The rows of a Helm target that sets several images; [] for one image."""
    helm = (config or {}).get("helm") or {}
    if helm.get("imageSet") != "several":
        return []
    return [row for row in helm.get("images") or [] if isinstance(row, dict)]


def image_value_label(image_values: Dict[str, Any]) -> str:
    """'image.repository / image.tag' — where an image goes, in one line."""
    return " / ".join(_value_paths(image_values)) if image_values else "the image value"


def is_linked(config: Optional[Dict[str, Any]]) -> bool:
    return isinstance(config, dict) and config.get("target") == "linked"


def is_helm(config: Optional[Dict[str, Any]]) -> bool:
    return isinstance(config, dict) and config.get("target") == "helm"


def split_image(image: str) -> Tuple[str, str]:
    """(repository, tag) of an image reference; one without a tag is
    ``latest``. Raises DeployConfigError for one pinned by digest, which a
    repository + tag pair cannot express."""
    if "@" in image:
        raise DeployConfigError(
            f"{image} is pinned by digest, which a repository and a tag value cannot carry. "
            "Set the image as one full value on the stage instead."
        )
    name, _, last = image.rpartition("/")
    if ":" in last:
        repository, tag = last.rsplit(":", 1)
        return (f"{name}/{repository}" if name else repository), tag
    return image, "latest"


def helm_image_values(config: Dict[str, Any], image: str) -> Dict[str, Any]:
    """The values that put ``image`` into a Helm target's chart."""
    settings = ((config.get("helm") or {}).get("imageValues")) or {}
    return helm_values_for([(settings, image)])


def helm_values_for(pairs: List[Tuple[Dict[str, Any], str]]) -> Dict[str, Any]:
    """The values that put each image into the chart, for ``(imageValues,
    image)`` pairs — one for a one-image target, one per row for several."""
    values: Dict[str, Any] = {}

    def put(path: str, value: str) -> None:
        cursor = values
        parts = path.split(".")
        for part in parts[:-1]:
            cursor = cursor.setdefault(part, {})
        cursor[parts[-1]] = value

    for settings, image in pairs:
        if settings.get("mode") == "full":
            put(settings["path"], image)
        else:
            repository, tag = split_image(image)
            put(settings["repository"], repository)
            put(settings["tag"], tag)
    return values


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
    if create and (config.get("create") or {}).get("source") == "template":
        # Creating from an inventory template authorizes that template, not
        # the (unused) manifest text.
        material["manifest"] = ""
        material["templateId"] = (config.get("create") or {}).get("templateId") or ""
        # Which Secret/ConfigMap the template's variables come from is part of
        # what is deployed, so changing an answer needs someone who can deploy.
        material["answers"] = (config.get("create") or {}).get("answers") or {}
    if deployment_rows(config):
        # Which deployments, and which image goes to each. Only for several,
        # so every one-deployment stamp stays valid.
        material["deployments"] = deployment_rows(config)
    if is_linked(config):
        # Only for linked stages, so every fixed target saved before linked
        # mode existed keeps the signature its authority stamp was taken on.
        material["target"] = "linked"
        material["environment"] = config.get("environment") or ""
    if is_helm(config):
        helm = config.get("helm") or {}
        material["target"] = "helm"
        material["releaseName"] = config.get("releaseName") or ""
        material["helm"] = {
            key: helm.get(key) or ""
            for key in ("chartTemplateId", "chartVersion", "valuesFile", "imageValues")
        }
        if helm_images(config):
            # Which images go where — a fixed reference included. Only for
            # several images, so every one-image stamp stays valid.
            material["helm"]["images"] = helm_images(config)
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
    return swap_images(deployment, {container_name: image})


def swap_images(deployment: Dict[str, Any], images: Dict[str, str]) -> str:
    """``swap_image`` for several containers of one Deployment: ``images``
    maps container name to its new image."""
    doc = copy.deepcopy(deployment)
    doc.pop("status", None)
    containers = _containers(doc)
    for container_name, image in images.items():
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
