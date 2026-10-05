"""Which deployments in the inventory a CI service builds.

Until these links existed the answer was inferred, in order: a Deploy stage
aimed at the deployment, a service whose slug is the deployment's name, one
whose slug is the image's name. That still works for whatever is not linked —
a link is the explicit answer and is asked first.

A link is made three ways, and all three land here:

* by hand, on the service (Settings → Deployments) or from the inventory;
* by a fixed-target Deploy stage, when its pipeline is saved
  (:func:`record_from_stages`) and whenever it deploys
  (:func:`record_from_deploy`) — unless the stage switches it off.

What reads them:

* the inventory, which shows "built by <service>" on each linked row;
* the deploy automation (Zoho/Jira/Hermes), which builds the linked service;
* a Deploy stage in "linked" mode, which deploys to the building service's
  linked deployment (:func:`resolve_snapshot_targets`) — how one shared
  pipeline deploys each service that uses it to that service's own workload.

A workload belongs to one service at most. Linking it elsewhere is refused
with the name of the service that has it; a Deploy stage never steals one.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ...audit import log_audit
from ...db import db
from ...k8s_names import (
    K8sNameError,
    validate_container_name,
    validate_namespace,
    validate_resource_name,
)
from ...models_ci import DEPLOYMENT_LINK_SOURCES, CiService, CiServiceDeployment
from . import deploy_config


class DeploymentLinkError(ValueError):
    """A link was refused. Message is user-facing."""


WORKLOAD_KINDS = ("Deployment", "StatefulSet", "DaemonSet")
MAX_LINKS_PER_SERVICE = 50


def _clean(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


def _iso(value) -> Optional[str]:
    from .serializers import _iso as iso

    return iso(value)


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------

def inventory_id_for(cluster_id: str, namespace: str, workload: str, live: Optional[Dict[str, Any]] = None) -> str:
    """The inventory's id for a linked workload. The inventory groups workloads
    by application name, which is usually the workload's name; when the live
    read found the row, its own id wins."""
    if isinstance(live, dict) and live.get("inventoryId"):
        return str(live["inventoryId"])
    from ..inventory_service import make_inventory_id

    return make_inventory_id(str(cluster_id), namespace, workload)


def link_to_dict(row: CiServiceDeployment, live: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    stamp = row.authorized_by if isinstance(row.authorized_by, dict) else None
    data = {
        "id": row.id,
        "serviceId": row.service_id,
        "clusterId": row.cluster_id,
        "namespace": row.namespace,
        "workloadKind": row.workload_kind or "Deployment",
        "workloadName": row.workload_name,
        "containerName": row.container_name or "",
        "environment": row.environment or "",
        "source": row.source or "manual",
        # Whether a linked-mode Deploy stage can deploy here: only when whoever
        # linked it could deploy there. Re-checked when a build uses it.
        "canDeployThrough": bool(stamp and stamp.get("userId")),
        "authorizedBy": stamp.get("username") if stamp else None,
        "createdBy": row.created_by.username if row.created_by else None,
        "createdAt": _iso(row.created_at),
        "updatedAt": _iso(row.updated_at),
        "inventoryId": inventory_id_for(row.cluster_id, row.namespace, row.workload_name, live),
    }
    if live is not None:
        data["live"] = live
    return data


def service_ref(service: CiService, link: Optional[CiServiceDeployment] = None) -> Dict[str, Any]:
    """How the inventory names the service that builds a row."""
    ref = {"id": service.id, "name": service.name, "slug": service.slug}
    if link is not None:
        ref["linkId"] = link.id
        ref["environment"] = link.environment or ""
        ref["source"] = link.source or "manual"
    return ref


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def find_link(cluster_id: str, namespace: str, workload_name: str) -> Optional[CiServiceDeployment]:
    if not (cluster_id and namespace and workload_name):
        return None
    return CiServiceDeployment.query.filter_by(
        cluster_id=str(cluster_id), namespace=namespace, workload_name=workload_name
    ).first()


def service_for(cluster_id: str, namespace: str, workload_name: str) -> Optional[CiService]:
    """The CI service linked to this workload, if it is active."""
    link = find_link(cluster_id, namespace, workload_name)
    if link is None or link.service is None or link.service.status != "active":
        return None
    return link.service


def index_all() -> Dict[Tuple[str, str, str], CiServiceDeployment]:
    """Every link by (cluster, namespace, workload) — one query for a whole
    inventory listing."""
    return {
        (str(row.cluster_id), row.namespace, row.workload_name): row
        for row in CiServiceDeployment.query.all()
    }


def list_links(service: CiService, *, user=None, live: bool = True) -> List[Dict[str, Any]]:
    rows = list(service.deployment_links)
    lives = _live_rows(rows, user) if live and rows else {}
    return [link_to_dict(row, lives.get(row.id)) for row in rows]


def _live_rows(rows: List[CiServiceDeployment], user) -> Dict[int, Dict[str, Any]]:
    """What the inventory says about each linked workload right now.

    Best effort, one inventory read per cluster (cached there). A cluster that
    cannot be read, or a workload the reader may not see, gets no live block —
    the link still lists, and "unknown" is never drawn as "missing".
    """
    from ..inventory_service import list_inventory

    out: Dict[int, Dict[str, Any]] = {}
    for cluster_id in sorted({str(row.cluster_id) for row in rows}):
        try:
            items, error, _ = list_inventory(user, {"cluster": cluster_id})
        except Exception as exc:  # A broken cluster must not hide the links.
            items, error = [], str(exc) or "The cluster could not be read."
        for row in rows:
            if str(row.cluster_id) != cluster_id:
                continue
            if error:
                out[row.id] = {"state": "unknown", "error": error}
                continue
            match = _match_item(items, row.namespace, row.workload_name)
            if match is None:
                out[row.id] = {"state": "missing"}
                continue
            out[row.id] = {
                "state": "found",
                "inventoryId": match.get("id"),
                "appName": match.get("name"),
                "status": match.get("status"),
                "image": match.get("image"),
                "versionTag": match.get("versionTag"),
                "replicas": match.get("replicas"),
                "readyReplicas": match.get("readyReplicas"),
                "lastUpdated": match.get("lastUpdated"),
            }
    return out


def _match_item(items: Iterable[Dict[str, Any]], namespace: str, workload: str) -> Optional[Dict[str, Any]]:
    for item in items:
        if item.get("namespace") != namespace:
            continue
        names = item.get("workloadNames") or []
        if workload in names or item.get("name") == workload:
            return item
    return None


# ---------------------------------------------------------------------------
# Writes
# ---------------------------------------------------------------------------

def _target_fields(payload: Dict[str, Any]) -> Dict[str, str]:
    cluster_id = _clean(payload.get("clusterId"), 128)
    if not cluster_id:
        raise DeploymentLinkError("Pick the cluster the deployment runs on.")
    try:
        namespace = validate_namespace(_clean(payload.get("namespace"), 63))
        workload = validate_resource_name(
            _clean(payload.get("workloadName") or payload.get("deploymentName"), 253),
            "deployment name",
        )
    except K8sNameError as exc:
        raise DeploymentLinkError(str(exc))
    kind = _clean(payload.get("workloadKind"), 32) or "Deployment"
    if kind not in WORKLOAD_KINDS:
        raise DeploymentLinkError(f"'{kind}' is not a workload kind ({', '.join(WORKLOAD_KINDS)}).")
    return {"cluster_id": cluster_id, "namespace": namespace, "workload_name": workload, "workload_kind": kind}


def _editable_fields(payload: Dict[str, Any], current: Optional[CiServiceDeployment] = None) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    if "containerName" in payload or current is None:
        container = _clean(payload.get("containerName"), 63)
        if container:
            try:
                validate_container_name(container)
            except K8sNameError as exc:
                raise DeploymentLinkError(str(exc))
        out["container_name"] = container or None
    if "environment" in payload or current is None:
        environment = _clean(payload.get("environment"), 64)
        if environment and not deploy_config._ENVIRONMENT_RE.match(environment):
            raise DeploymentLinkError(
                f"'{environment}' is not an environment label (letters, digits, spaces, dots, "
                "dashes and underscores)."
            )
        out["environment"] = environment or None
    return out


def _deploy_stamp(actor, cluster_id: str, namespace: str) -> Optional[Dict[str, Any]]:
    """Who a linked-mode Deploy stage may deploy as here — the person linking,
    if they can deploy there; otherwise nobody."""
    if actor is None:
        return None
    from ...access_engine import can_access_namespace, user_has_permission

    if user_has_permission(actor, "apps:deploy") and can_access_namespace(actor, cluster_id, namespace):
        return {
            "userId": actor.id,
            "username": actor.username,
            "at": datetime.now(timezone.utc).isoformat(),
        }
    return None


def _check_can_see(actor, cluster_id: str, namespace: str) -> None:
    if actor is None:
        return
    from ...access_engine import can_access_namespace

    if not can_access_namespace(actor, cluster_id, namespace):
        raise DeploymentLinkError(
            f"You cannot see {cluster_id}/{namespace}, so you cannot link a deployment there."
        )


def _clash(target: Dict[str, str], service: CiService) -> Optional[CiServiceDeployment]:
    row = find_link(target["cluster_id"], target["namespace"], target["workload_name"])
    if row is not None and row.service_id != service.id:
        return row
    return None


def add_link(service: CiService, payload: Dict[str, Any], *, actor=None, source: str = "manual") -> Dict[str, Any]:
    if service.is_pipeline_home:
        raise DeploymentLinkError(
            "Link deployments to the CI services that build them — a pipeline on the Pipelines "
            "page is not an application."
        )
    source = source if source in DEPLOYMENT_LINK_SOURCES else "manual"
    target = _target_fields(payload)
    _check_can_see(actor, target["cluster_id"], target["namespace"])
    where = f"{target['cluster_id']}/{target['namespace']}/{target['workload_name']}"
    other = _clash(target, service)
    if other is not None:
        raise DeploymentLinkError(
            f"{where} is already linked to {other.service.name}. A deployment belongs to one CI "
            "service — unlink it there first."
        )
    existing = find_link(target["cluster_id"], target["namespace"], target["workload_name"])
    if existing is not None:
        raise DeploymentLinkError(f"{where} is already linked to this service.")
    if len(service.deployment_links) >= MAX_LINKS_PER_SERVICE:
        raise DeploymentLinkError(f"A service can be linked to at most {MAX_LINKS_PER_SERVICE} deployments.")

    row = CiServiceDeployment(
        service_id=service.id,
        source=source,
        authorized_by=_deploy_stamp(actor, target["cluster_id"], target["namespace"]),
        created_by_user_id=getattr(actor, "id", None),
        **target,
        **_editable_fields(payload),
    )
    db.session.add(row)
    db.session.commit()
    _audit("ci_deployment_linked", row, actor, source=source)
    return link_to_dict(row)


def update_link(row: CiServiceDeployment, payload: Dict[str, Any], *, actor=None) -> Dict[str, Any]:
    _check_can_see(actor, row.cluster_id, row.namespace)
    for key, value in _editable_fields(payload, row).items():
        setattr(row, key, value)
    if payload.get("reauthorize"):
        # "Deploy through this link as me" — what fixes a link made by someone
        # who could not deploy, or who has since left.
        stamp = _deploy_stamp(actor, row.cluster_id, row.namespace)
        if stamp is None:
            raise DeploymentLinkError(
                f"You cannot deploy to {row.cluster_id}/{row.namespace}, so builds cannot deploy "
                "there as you."
            )
        row.authorized_by = stamp
    row.updated_at = datetime.now(timezone.utc)
    db.session.add(row)
    db.session.commit()
    _audit("ci_deployment_link_updated", row, actor)
    return link_to_dict(row)


def remove_link(row: CiServiceDeployment, *, actor=None) -> None:
    _audit("ci_deployment_unlinked", row, actor, commit=False)
    db.session.delete(row)
    db.session.commit()


def get_link(service: CiService, link_id: int) -> CiServiceDeployment:
    row = db.session.get(CiServiceDeployment, int(link_id))
    if row is None or row.service_id != service.id:
        raise LookupError("Deployment link not found.")
    return row


def _audit(action: str, row: CiServiceDeployment, actor, *, commit: bool = True, **extra) -> None:
    log_audit(
        action,
        actor=actor,
        target_type="ci_service",
        target_id=str(row.service_id),
        details={
            "service": row.service.slug if row.service else row.service_id,
            "cluster": row.cluster_id,
            "namespace": row.namespace,
            "workload": row.workload_name,
            "environment": row.environment or "",
            **extra,
        },
        commit=commit,
    )


# ---------------------------------------------------------------------------
# Deploy stages
# ---------------------------------------------------------------------------

def _upsert_from_target(service: CiService, config: Dict[str, Any], actor, *, stamp=None) -> Optional[CiServiceDeployment]:
    """Record the link a fixed Deploy target implies. Never raises, never steals
    a workload another service has, never overwrites a person's choices."""
    if service is None or service.is_pipeline_home:
        return None
    if not isinstance(config, dict) or deploy_config.is_linked(config):
        return None
    if config.get("linkToService") is False:
        return None
    cluster_id = str(config.get("clusterId") or "")
    namespace = config.get("namespace") or ""
    workload = config.get("deploymentName") or ""
    if not (cluster_id and namespace and workload):
        return None
    row = find_link(cluster_id, namespace, workload)
    if row is not None:
        if row.service_id != service.id:
            return None
        if not row.container_name and config.get("containerName"):
            row.container_name = config["containerName"]
        if not row.authorized_by and stamp:
            row.authorized_by = dict(stamp)
        db.session.add(row)
        return row
    if len(service.deployment_links) >= MAX_LINKS_PER_SERVICE:
        return None
    row = CiServiceDeployment(
        service_id=service.id,
        cluster_id=cluster_id,
        namespace=namespace,
        workload_kind="Deployment",
        workload_name=workload,
        container_name=config.get("containerName") or None,
        source="deploy_stage",
        # The stage's own authority stamp is exactly "may deploy there".
        authorized_by=dict(stamp) if stamp else None,
        created_by_user_id=getattr(actor, "id", None),
    )
    db.session.add(row)
    service.deployment_links.append(row)
    return row


def record_from_stages(pipeline, normalized: List[Dict[str, Any]], actor) -> None:
    """On pipeline save: link each fixed Deploy target to the pipeline's service.

    Runs inside the save's transaction (no commit). Pipelines on the Pipelines
    page link nothing — the services using them have their own links.
    """
    service = getattr(pipeline, "service", None)
    if service is None and getattr(pipeline, "service_id", None):
        service = db.session.get(CiService, int(pipeline.service_id))
    if service is None or service.is_pipeline_home:
        return
    for stage in normalized:
        if stage.get("stage_type") != "deploy" or stage.get("enabled") is False:
            continue
        config = stage.get("deploy")
        stamp = config.get("authorizedBy") if isinstance(config, dict) else None
        try:
            with db.session.no_autoflush:
                _upsert_from_target(service, config, actor, stamp=stamp)
        except Exception:  # A link is a convenience; it must never fail a save.
            continue


def record_from_deploy(build, config: Dict[str, Any]) -> None:
    """After a Deploy stage rolled out: the service is that deployment.

    Covers pipelines saved before links existed, and deployments the stage
    just created. Added to the engine's transaction, which commits it with the
    stage's own result.
    """
    try:
        service = getattr(build, "service", None)
        stamp = config.get("authorizedBy") if isinstance(config, dict) else None
        with db.session.no_autoflush:
            _upsert_from_target(service, config, None, stamp=stamp)
    except Exception:  # Never let a convenience fail a deploy that succeeded.
        pass


def resolve_snapshot_targets(service: CiService, stage_definitions: List[Dict[str, Any]]) -> None:
    """Fill each linked-mode Deploy stage of a new build's snapshot with the
    building service's linked deployment.

    Done once, when the build is created, so the snapshot says where this build
    deployed even after links change, and a retry deploys to the same place.
    What cannot be resolved is written as ``unresolved`` — the stage fails with
    that message when it is reached, rather than the whole build refusing to
    start over a stage that may be switched off by a run condition.
    """
    for definition in stage_definitions:
        if (definition.get("stageType") or "") != "deploy":
            continue
        config = definition.get("deploy")
        if not deploy_config.is_linked(config):
            continue
        config = dict(config)
        definition["deploy"] = config
        config["stageAuthorizedBy"] = config.pop("authorizedBy", None)
        link, problem = _pick_link(service, config.get("environment") or "")
        if problem:
            config["unresolved"] = problem
            continue
        stamp = link.authorized_by if isinstance(link.authorized_by, dict) else None
        where = f"{link.cluster_id}/{link.namespace}/{link.workload_name}"
        if not stamp or not stamp.get("userId"):
            config["unresolved"] = (
                f"{service.name} is linked to {where}, but whoever linked it could not deploy there, "
                "so a build cannot deploy through that link. Someone who can deploy to "
                f"{link.cluster_id}/{link.namespace} must open the service's Settings → Deployments "
                "and choose “Deploy as me”."
            )
            continue
        config.update(
            {
                "clusterId": link.cluster_id,
                "namespace": link.namespace,
                "deploymentName": link.workload_name,
                "containerName": config.get("containerName") or link.container_name or "",
                "authorizedBy": dict(stamp),
                "linkId": link.id,
            }
        )


def _pick_link(service: CiService, environment: str) -> Tuple[Optional[CiServiceDeployment], str]:
    links = list(service.deployment_links) if service is not None else []
    if service is not None and service.is_pipeline_home:
        return None, (
            "This Deploy stage deploys to the deployment linked to the service being built. "
            "Run on its own, this pipeline has no service behind it, so there is nowhere to deploy. "
            "It deploys when a CI service that uses it builds."
        )
    if environment:
        wanted = environment.strip().lower()
        matching = [row for row in links if (row.environment or "").strip().lower() == wanted]
        if len(matching) == 1:
            return matching[0], ""
        if not matching:
            return None, (
                f"{service.name} has no linked deployment labelled “{environment}”. Link one under "
                "the service's Settings → Deployments, or change the stage's environment."
            )
        return None, (
            f"{service.name} has {len(matching)} deployments labelled “{environment}”; a Deploy stage "
            "deploys to exactly one. Give them distinct labels."
        )
    if len(links) == 1:
        return links[0], ""
    if not links:
        return None, (
            f"{service.name} is not linked to a deployment yet, so this stage has nowhere to deploy. "
            "Link one under the service's Settings → Deployments."
        )
    labels = ", ".join(sorted({row.environment or "(no label)" for row in links}))
    return None, (
        f"{service.name} is linked to {len(links)} deployments ({labels}), and this stage does not say "
        "which. Set the stage's environment to one of those labels."
    )
