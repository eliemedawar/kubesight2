"""Deploy stages that create their deployment from an inventory template.

The inventory's deployment templates (Inventory → Templates, the Application
Builder's catalog) describe an application in full: containers, ports,
resources, storage, probes, env. A Deploy stage can point at one instead of the
hand-filled "create it if it is missing" form. The rule is the same either way:

* the deployment exists → only the target container's image changes;
* it does not → it is created from the template, with the image this build
  pushed in place of the template's.

The template is read when the build deploys, not when the pipeline is saved,
so the inventory stays the source of truth: edit the template, and the next
deployment created from it follows. Saving still renders it once, so a template
a build could never use is refused while someone is looking at the editor.

What a build may NOT do through a template: create Secrets (a build never
writes credentials — the template's secret-backed variables must be answered
with the Deploy Wizard), or deploy anything but a Deployment (the stage reads,
swaps and watches Deployments). Answers a template requires and nobody can give
in a build — a required variable with no default — fail the same way, naming
the Deploy Wizard as the way to create the first copy.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import yaml

# Created on the build's behalf. Mirrors what the Deploy Wizard emits for a
# Deployment template, minus Secrets.
ALLOWED_KINDS = (
    "Deployment",
    "Service",
    "ConfigMap",
    "PersistentVolumeClaim",
    "PersistentVolume",
    "Ingress",
    "HorizontalPodAutoscaler",
)
# Used when a save renders the template without a build behind it.
PREVIEW_IMAGE = "registry.local/the-image-this-build-pushes:latest"


class DeployTemplateError(ValueError):
    """A template cannot be used by a Deploy stage. Message is user-facing."""


def lookup(template_id: str) -> Optional[Dict[str, Any]]:
    """A template's full definition, built-in or from the inventory."""
    from ..user_template_service import get_user_template_detail
    from ..wizard_templates import get_template

    template_id = str(template_id or "").strip()
    if not template_id:
        return None
    return get_template(template_id) or get_user_template_detail(template_id)


def summaries() -> List[Dict[str, Any]]:
    """Every template a Deploy stage could create from, for the picker."""
    from ..user_template_service import list_user_template_summaries
    from ..wizard_templates import list_templates

    items = []
    for item in list(list_templates()) + list(list_user_template_summaries()):
        detail = lookup(item["id"]) or {}
        containers = [c for c in detail.get("containers") or [] if isinstance(c, dict)]
        items.append(
            {
                "id": item["id"],
                "name": item.get("name") or item["id"],
                "description": item.get("description") or "",
                "category": item.get("category") or "Custom",
                "workloadType": item.get("workloadType") or "Deployment",
                "usable": (item.get("workloadType") or "Deployment") == "Deployment",
                "containers": [str(c.get("name") or "") for c in containers],
                "image": _image_of(containers[0]) if containers else "",
                "deploymentName": _default_name(detail or item),
                **questions(detail),
            }
        )
    return items


def questions(template: Dict[str, Any]) -> Dict[str, Any]:
    """What a build has to be told to render this template: its variables and
    its mounted files, with only the sources a build may use."""
    from .deploy_config import ENV_ANSWER_SOURCES, VOLUME_ANSWER_SOURCES

    schema = (template or {}).get("schema") or {}
    env = []
    for field in schema.get("env") or []:
        if not isinstance(field, dict) or not field.get("key"):
            continue
        allowed = field.get("allowedSources") or list(ENV_ANSWER_SOURCES)
        sources = [s for s in allowed if s in ENV_ANSWER_SOURCES]
        if field.get("sensitive"):
            sources = [s for s in sources if s == "existingSecret"]
        env.append(
            {
                "key": str(field["key"]),
                "required": bool(field.get("required")),
                "sensitive": bool(field.get("sensitive")),
                "default": "" if field.get("default") is None else str(field.get("default")),
                "sources": sources,
            }
        )
    volumes = []
    for mount in schema.get("volumeMounts") or []:
        if not isinstance(mount, dict) or not mount.get("mountPath"):
            continue
        kind = "secret" if mount.get("kind") == "secret" else "configMap"
        allowed = mount.get("allowedSources") or []
        wanted = "existingSecret" if kind == "secret" else "existingConfigMap"
        volumes.append(
            {
                "mountPath": str(mount["mountPath"]),
                "kind": kind,
                "sources": [wanted] if (not allowed or wanted in allowed) else [],
            }
        )
    return {"env": env, "volumes": volumes}


def _image_of(container: Dict[str, Any]) -> str:
    image = str(container.get("image") or "").strip()
    tag = str(container.get("tag") or "").strip()
    if image and tag and ":" not in image.rsplit("/", 1)[-1]:
        return f"{image}:{tag}"
    return image


def _default_name(template: Dict[str, Any]) -> str:
    import re

    raw = str(template.get("name") or template.get("id") or "app").lower()
    return re.sub(r"[^a-z0-9-]+", "-", raw).strip("-")[:63] or "app"


def template_label(template_id: str) -> str:
    template = lookup(template_id)
    return str((template or {}).get("name") or template_id)


def render(
    template_id: str,
    *,
    namespace: str,
    deployment_name: str,
    container_name: str = "",
    image: str = PREVIEW_IMAGE,
    answers: Optional[Dict[str, Any]] = None,
) -> Tuple[str, List[Dict[str, str]]]:
    """The template as YAML for one namespace and deployment name, with
    ``image`` on the target container. Returns (yaml, [{kind, name}])."""
    from ..template_resolver import resolve_template
    from ..wizard_manifest_generator import generate_wizard_manifests

    template = lookup(template_id)
    if template is None:
        raise DeployTemplateError(
            f"The inventory template '{template_id}' no longer exists. Pick another one."
        )
    name = template.get("name") or template_id
    if (template.get("workloadType") or "Deployment") != "Deployment":
        raise DeployTemplateError(
            f"The template '{name}' describes a {template.get('workloadType')}; a Deploy stage "
            "deploys Deployments. Pick a Deployment template."
        )

    given = answers if isinstance(answers, dict) else {}
    resolved_answers = {
        "basics": {"appName": deployment_name, "namespace": namespace},
        "env": dict(given.get("env") or {}),
        "volumes": dict(given.get("volumes") or {}),
    }
    payload, error = resolve_template(template, resolved_answers)
    if error:
        raise DeployTemplateError(
            f"The template '{name}' needs an answer: {error} Answer it under “What the template "
            "asks” (an existing Secret or ConfigMap for credentials and files)."
        )
    containers = [c for c in payload.get("containers") or [] if isinstance(c, dict)]
    if not containers:
        raise DeployTemplateError(f"The template '{name}' has no container to deploy.")
    target = next(
        (c for c in containers if container_name and c.get("name") == container_name), None
    )
    if container_name and target is None:
        names = ", ".join(str(c.get("name")) for c in containers)
        raise DeployTemplateError(
            f"The template '{name}' has no container named '{container_name}' (it has {names})."
        )
    target = target or containers[0]
    target["image"] = image
    target.pop("tag", None)

    text, _summary, error = generate_wizard_manifests(payload)
    if error:
        raise DeployTemplateError(f"The template '{name}' could not be rendered: {error}")
    documents = [doc for doc in yaml.safe_load_all(text or "") if isinstance(doc, dict)]
    created: List[Dict[str, str]] = []
    for doc in documents:
        kind = str(doc.get("kind") or "")
        doc_name = str((doc.get("metadata") or {}).get("name") or "")
        if kind == "Secret":
            raise DeployTemplateError(
                f"The template '{name}' creates the Secret '{doc_name}'. A build never writes "
                "credentials: create the first copy with the Deploy Wizard, after which builds only "
                "change its image."
            )
        if kind not in ALLOWED_KINDS:
            raise DeployTemplateError(
                f"The template '{name}' creates a {kind} ('{doc_name}'), which a build does not create."
            )
        created.append({"kind": kind, "name": doc_name})
    deployments = [item for item in created if item["kind"] == "Deployment"]
    if len(deployments) != 1 or deployments[0]["name"] != deployment_name:
        raise DeployTemplateError(
            f"The template '{name}' must render exactly one Deployment named '{deployment_name}'."
        )
    return text, created


def check(config: Dict[str, Any], stage_name: str) -> None:
    """On save: the stage's template renders for its target. Raises with the
    stage named, so the editor can say which stage is wrong."""
    try:
        render(
            config["create"]["templateId"],
            namespace=config["namespace"],
            deployment_name=config["deploymentName"],
            container_name=config.get("containerName") or "",
            answers=(config.get("create") or {}).get("answers"),
        )
    except DeployTemplateError as exc:
        raise DeployTemplateError(f"Stage '{stage_name}': {exc}")


def uses_template(config: Optional[Dict[str, Any]]) -> bool:
    create = (config or {}).get("create") if isinstance(config, dict) else None
    return bool(
        isinstance(create, dict)
        and create.get("source") == "template"
        and create.get("templateId")
    )


def builders_by_template() -> Dict[str, List[Dict[str, Any]]]:
    """Which CI services create their deployment from each template — for the
    inventory's "Built by" on a template card. From fixed Deploy targets and
    from deployment links that name a template."""
    from ...db import db
    from ...models_ci import CiPipeline, CiPipelineStage, CiService, CiServiceDeployment

    found: Dict[str, Dict[int, Dict[str, Any]]] = {}

    def add(template_id: str, service: CiService) -> None:
        if not template_id or service is None or service.is_pipeline_home:
            return
        found.setdefault(template_id, {})[service.id] = {
            "id": service.id,
            "name": service.name,
            "slug": service.slug,
        }

    rows = (
        db.session.query(CiPipelineStage, CiService)
        .join(CiPipeline, CiPipelineStage.pipeline_id == CiPipeline.id)
        .join(CiService, CiPipeline.service_id == CiService.id)
        .filter(CiPipelineStage.stage_type == "deploy")
        .all()
    )
    for stage, service in rows:
        config = stage.deploy if isinstance(stage.deploy, dict) else None
        if uses_template(config):
            add(str(config["create"]["templateId"]), service)
    for link in CiServiceDeployment.query.filter(CiServiceDeployment.template_id.isnot(None)).all():
        add(str(link.template_id), link.service)
    # Services that build with a shared pipeline whose stage names a template
    # get it through their own links; the shared pipeline itself is not listed.
    return {key: list(value.values()) for key, value in found.items()}
