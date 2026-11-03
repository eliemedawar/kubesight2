"""Moving one pipeline between KubeSight installations (UAT → prod).

A pipeline is exported as a YAML file and imported onto a pipeline of a
service that already exists on the other side, where it is saved through
``pipelines.update_pipeline`` — the same validation and the same "deploys as
whoever saved it" stamp as a save in the editor.

What cannot travel as-is between installations:

* **Secrets.** Only their names are in the file, never a value. Import lists
  the ones the target service does not have, and creates each as a service
  secret with the value typed there (or empty, to be set later).
* **Clusters.** Deploy stages name a cluster by id, and the two installations
  have their own. Import asks which cluster each one becomes.
* **Authority stamps.** Never exported: whoever imports is who the Deploy and
  App store upload stages run as, if they may.
* **A shared pipeline.** A pipeline that builds with one carries its slug and
  the stages it ran with; import uses the shared pipeline of that slug if the
  other side has it, and otherwise imports the stages as the service's own.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml

from ...audit import log_audit
from ...db import db
from ...models_ci import CiPipeline, CiSecret, CiService
from ...secret_encryption import encrypt_secret
from . import deploy_config
from . import pipelines as pipelines_service
from .serializers import pipeline_stage_to_dict

KIND = "KubeSightPipeline"
FORMAT_VERSION = 1
MAX_FILE_BYTES = 2 * 1024 * 1024


class TransferError(ValueError):
    """The file or the import was refused. Message is user-facing."""


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

def _stage_payload(stage: Dict[str, Any]) -> Dict[str, Any]:
    """A stage as a save payload, minus what belongs to this installation."""
    from .shared_pipelines import _as_payload

    stage = _as_payload(stage)
    stage.pop("position", None)
    return stage


def _effective(pipeline: CiPipeline):
    """(stages, parameters, post actions) a build of ``pipeline`` runs with —
    the shared pipeline's when linked, the generated default when nothing was
    ever saved."""
    effective = pipeline.effective()
    if effective.stages:
        return (
            [_stage_payload(pipeline_stage_to_dict(stage)) for stage in effective.stages],
            pipelines_service.parameter_definitions(effective),
            list(effective.post_actions or []),
        )
    if (pipeline.purpose or "build") == "build" and pipeline.service is not None:
        try:
            generated, _ = pipelines_service.resolve_for_build(pipeline.service, pipeline_id=pipeline.id)
        except (pipelines_service.PipelineError, TypeError):
            generated = None
        if generated is not None and generated.stages:
            return (
                [_stage_payload(pipeline_stage_to_dict(stage)) for stage in generated.stages],
                list(generated.parameters or []),
                list(pipeline.post_actions or []),
            )
    return [], pipelines_service.parameter_definitions(effective), list(effective.post_actions or [])


def secret_names(stages: Iterable[Dict[str, Any]], post_actions: Iterable[Dict[str, Any]]) -> List[str]:
    """Every CI secret the stages and post actions refer to, by name."""
    names = set()
    for item in list(stages or []) + list(post_actions or []):
        if not isinstance(item, dict):
            continue
        for ref in item.get("secretRefs") or []:
            name = ref.get("name") if isinstance(ref, dict) else ref
            if name:
                names.add(str(name))
        if item.get("urlSecret"):
            names.add(str(item["urlSecret"]))
    return sorted(names)


def _deploy_clusters(stages: Iterable[Dict[str, Any]]) -> List[str]:
    found: List[str] = []
    for stage in stages or []:
        deploy = stage.get("deploy") if isinstance(stage, dict) else None
        cluster = (deploy or {}).get("clusterId") if isinstance(deploy, dict) else None
        if cluster and not deploy_config.is_linked(deploy) and cluster not in found:
            found.append(str(cluster))
    return found


def export_pipeline(pipeline: CiPipeline, *, cluster_names: Dict[str, str], actor=None) -> Tuple[str, str]:
    """``(file name, YAML text)`` for one pipeline."""
    from .shared_pipelines import home_of

    service = pipeline.service
    stages, parameters, post_actions = _effective(pipeline)
    home = home_of(pipeline.linked_pipeline) if pipeline.linked_pipeline_id else None
    document = {
        "kind": KIND,
        "formatVersion": FORMAT_VERSION,
        "exportedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "exportedBy": getattr(actor, "username", None),
        "source": {
            "service": service.slug if service else None,
            "serviceName": service.name if service else None,
            "pipeline": pipeline.name,
            "version": pipeline.version,
        },
        "pipeline": {
            "name": pipeline.name,
            "description": pipeline.description or "",
            "purpose": pipeline.purpose or "build",
            # Builds with a shared pipeline: imported as a link to the one of the
            # same slug when the other side has it; the stages below otherwise.
            "sharedPipeline": {"slug": home.slug, "name": home.name} if home else None,
            "parameters": parameters,
            "postActions": post_actions,
            "stages": stages,
        },
        # Names only: values never leave this installation.
        "secrets": secret_names(stages, post_actions),
        # Deploy stages name these; import maps each to a cluster over there.
        "clusters": [
            {"id": cluster, "name": cluster_names.get(cluster) or cluster}
            for cluster in _deploy_clusters(stages)
        ],
    }
    header = (
        "# KubeSight CI pipeline export. Import it from the Pipeline tab of a service on\n"
        "# another KubeSight. Secret values and authority stamps are never in this file.\n"
    )
    text = header + yaml.safe_dump(document, sort_keys=False, default_flow_style=False, allow_unicode=True)
    slug = (service.slug if service else "pipeline") or "pipeline"
    log_audit(
        "ci_pipeline_exported",
        actor=actor,
        target_type="ci_pipeline",
        target_id=str(pipeline.id),
        details={"service": slug, "pipeline": pipeline.name, "stageCount": len(stages)},
    )
    return f"{slug}-{pipeline.name}.pipeline.yaml", text


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

def parse(text: str) -> Dict[str, Any]:
    """The export document in ``text``, checked for shape. Raises TransferError."""
    raw = str(text or "")
    if not raw.strip():
        raise TransferError("The file is empty.")
    if len(raw.encode("utf-8")) > MAX_FILE_BYTES:
        raise TransferError("The file is larger than 2 MiB — that is not a pipeline export.")
    try:
        document = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        where = f" (line {mark.line + 1})" if mark is not None else ""
        raise TransferError(f"The file is not valid YAML{where}.") from exc
    if not isinstance(document, dict) or document.get("kind") != KIND:
        raise TransferError(
            "This is not a KubeSight pipeline export. Export one from the Pipeline tab first."
        )
    try:
        version = int(document.get("formatVersion") or 0)
    except (TypeError, ValueError):
        version = 0
    if version < 1 or version > FORMAT_VERSION:
        raise TransferError(
            f"This export is format {document.get('formatVersion')}; this KubeSight reads "
            f"format {FORMAT_VERSION}. Update the older side first."
        )
    pipeline = document.get("pipeline")
    if not isinstance(pipeline, dict) or not isinstance(pipeline.get("stages"), list):
        raise TransferError("The file has no pipeline stages.")
    return document


def _target_secrets(pipeline: CiPipeline) -> Dict[str, str]:
    """Secret key → scope the target can resolve (a service secret wins)."""
    rows = CiSecret.query.filter(
        db.or_(CiSecret.service_id == pipeline.service_id, CiSecret.scope == "global")
    ).all()
    scopes: Dict[str, str] = {}
    for row in rows:
        if row.scope == "service" or row.key not in scopes:
            scopes[row.key] = row.scope
    return scopes


def _usable_home(pipeline: CiPipeline, shared: Optional[Dict[str, Any]]) -> Optional[CiService]:
    """The shared pipeline here that ``pipeline`` can build with in place of
    the file's stages: same slug, and only for a service's default build
    pipeline (a shared pipeline cannot use another; only the default links)."""
    slug = (shared or {}).get("slug")
    service = pipeline.service
    if not slug or service is None or service.is_pipeline_home:
        return None
    if service.default_pipeline() is not pipeline:
        return None
    return CiService.query.filter_by(slug=str(slug), kind="pipeline").first()


def preview(pipeline: CiPipeline, text: str) -> Dict[str, Any]:
    """What importing ``text`` onto ``pipeline`` would do. Changes nothing."""
    document = parse(text)
    incoming = document["pipeline"]
    stages = [stage for stage in incoming.get("stages") or [] if isinstance(stage, dict)]
    shared = incoming.get("sharedPipeline") if isinstance(incoming.get("sharedPipeline"), dict) else None
    home = _usable_home(pipeline, shared)
    have = _target_secrets(pipeline)
    # Linked to a shared pipeline here, the secrets are that pipeline's own.
    secrets = [] if home is not None else [
        {"name": name, "scope": have.get(name, ""), "missing": name not in have}
        for name in secret_names(stages, incoming.get("postActions") or [])
    ]
    warnings: List[str] = []
    if shared and home is None:
        warnings.append(
            f"It builds with the shared pipeline '{shared.get('name') or shared.get('slug')}', "
            "which is not on this KubeSight — its stages are imported as this service's own."
        )
    purpose = str(incoming.get("purpose") or "build")
    if purpose != (pipeline.purpose or "build"):
        warnings.append(
            f"It was a {purpose.replace('_', ' ')} pipeline and is imported onto a "
            f"{(pipeline.purpose or 'build').replace('_', ' ')} pipeline."
        )
    return {
        "source": document.get("source") or {},
        "exportedAt": document.get("exportedAt"),
        "exportedBy": document.get("exportedBy"),
        "stages": [
            {"name": stage.get("name") or "", "stageType": stage.get("stageType") or "command"}
            for stage in stages
        ],
        "parameterCount": len(incoming.get("parameters") or []),
        "postActionCount": len(incoming.get("postActions") or []),
        "sharedPipeline": (
            {**shared, "found": home is not None, "id": home.id if home else None} if shared else None
        ),
        "secrets": secrets,
        # Only when the file's own stages are imported.
        "clusters": [] if home is not None else [
            {"id": str(item.get("id")), "name": str(item.get("name") or item.get("id"))}
            for item in document.get("clusters") or []
            if isinstance(item, dict) and item.get("id")
        ],
        "target": {
            "pipelineId": pipeline.id,
            "name": pipeline.name,
            "version": pipeline.version,
            "stageCount": len(pipeline.effective().stages),
            "linked": bool(pipeline.linked_pipeline_id),
        },
        "warnings": warnings,
    }


def _remap_clusters(stages: List[Dict[str, Any]], cluster_map: Dict[str, str], known: Iterable[str]) -> None:
    known = set(known)
    for stage in stages:
        deploy = stage.get("deploy")
        if not isinstance(deploy, dict) or deploy_config.is_linked(deploy) or not deploy.get("clusterId"):
            continue
        source = str(deploy["clusterId"])
        target = str(cluster_map.get(source) or "").strip()
        if not target:
            raise TransferError(
                f"Stage '{stage.get('name')}' deploys to the cluster '{source}'. "
                "Pick the cluster it deploys to here."
            )
        if target not in known:
            raise TransferError(f"There is no cluster '{target}' here.")
        stage["deploy"] = {**deploy, "clusterId": target}


def apply(
    pipeline: CiPipeline,
    text: str,
    *,
    cluster_map: Dict[str, str],
    known_clusters: Iterable[str],
    secret_values: Dict[str, str],
    actor=None,
) -> Dict[str, Any]:
    """Import ``text`` onto ``pipeline``: one save, all or nothing.

    Missing secrets are created as service secrets (with the value given, or
    empty) in the same transaction as the pipeline save, so a refused save
    leaves nothing behind.
    """
    from . import shared_pipelines

    document = parse(text)
    incoming = document["pipeline"]
    stages = [dict(stage) for stage in incoming.get("stages") or [] if isinstance(stage, dict)]
    service = pipeline.service
    shared = incoming.get("sharedPipeline") if isinstance(incoming.get("sharedPipeline"), dict) else None
    home = _usable_home(pipeline, shared)
    if home is None:
        _remap_clusters(stages, cluster_map or {}, known_clusters)

    have = _target_secrets(pipeline)
    created: List[Dict[str, Any]] = []
    # Linked to a shared pipeline, the secrets are its own: an empty service
    # secret here would shadow the shared pipeline's real one.
    wanted = [] if home is not None else secret_names(stages, incoming.get("postActions") or [])
    try:
        for name in wanted:
            if name in have:
                continue
            value = str((secret_values or {}).get(name) or "")
            db.session.add(
                CiSecret(
                    scope="service",
                    service_id=pipeline.service_id,
                    key=name,
                    value_cipher=encrypt_secret(value),
                    description=(
                        "Created by a pipeline import."
                        if value
                        else "Created empty by a pipeline import — set its value before building."
                    ),
                    created_by_user_id=getattr(actor, "id", None),
                )
            )
            created.append({"name": name, "empty": not value})
        db.session.flush()

        if home is not None:
            # Build with the shared pipeline of the same slug over here; its
            # stages are its own business, edited on the Pipelines page.
            target = shared_pipelines.shared_pipeline_of(home)
            if target is None or not target.stages:
                raise TransferError(
                    f"The shared pipeline '{home.name}' here has no stages yet. Import or write "
                    "its stages on the Pipelines page first."
                )
            db.session.commit()
            if pipeline.linked_pipeline_id == target.id:
                result = pipelines_service.list_pipelines(service)[0]
            else:
                result = shared_pipelines.attach(service, home, actor=actor)
        else:
            if pipeline.linked_pipeline_id:
                # Stop using the shared pipeline; the imported stages replace the
                # service's own below, in the same save.
                pipeline.linked_pipeline_id = None
            result = pipelines_service.update_pipeline(
                pipeline,
                {
                    "stages": stages,
                    "parameters": incoming.get("parameters") or [],
                    "postActions": incoming.get("postActions") or [],
                    **(
                        {"description": incoming.get("description")}
                        if incoming.get("description")
                        else {}
                    ),
                },
                actor=actor,
            )
    except Exception:
        db.session.rollback()
        raise

    log_audit(
        "ci_pipeline_imported",
        actor=actor,
        target_type="ci_pipeline",
        target_id=str(pipeline.id),
        details={
            "service": service.slug if service else None,
            "from": document.get("source") or {},
            "stageCount": len(stages),
            "sharedPipeline": home.slug if home else None,
            "secretsCreated": [item["name"] for item in created],
            "clusterMap": dict(cluster_map or {}),
        },
    )
    return {"pipeline": result, "secretsCreated": created, "sharedPipeline": home.slug if home else None}
