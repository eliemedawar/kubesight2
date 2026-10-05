"""The Deploy stage: KubeSight rolls a build's image out to one deployment.

No runner executes this stage. The engine hands it here once every runner stage
has finished (Deploy stages are last — pipelines.check_deploy_stages_last), and
it goes through the same doors as any other deploy in KubeSight:

1. **Authority.** The stage deploys with the rights of whoever saved its target
   (``deploy.authorizedBy``), re-checked now: a person who has since lost
   ``apps:deploy`` or the namespace stops deploying through the pipeline too.
2. **Registry.** The image must be *found* in one of the cluster's registries —
   stricter than an ordinary apply, where an image no registry covers is let
   through. A missing or unconfirmable image never reaches the cluster.
3. **Namespace.** It must exist. A build does not create namespaces: they carry
   quotas, RBAC and approval rules somebody should set on purpose.
4. **The change.** An existing deployment gets exactly one thing changed — the
   target container's image. A missing one is created from the stage's own
   manifest (Deployment + optional Service), if the stage allows it.
5. **Approval.** Applied through ``deployment_service.apply_yaml``. On a cluster
   that needs approval the change is queued as a change bundle, and the stage
   waits — visibly — until it is approved, declined, or the stage times out.
6. **Rollout.** The stage passes only when every replica runs the new image and
   is available. Crash loops and image-pull failures fail it early. A failed or
   timed-out rollout puts the previous image back (or removes what the stage
   created) before the stage is failed.

Everything is persisted on the stage row's ``deploy_state``, so a backend
restart resumes a rollout watch rather than forgetting it.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ...audit import log_audit
from ...db import db
from ...k8s_provider import K8sCommandError, should_use_real_k8s
from ...models_ci import CiArtifact, CiBuild, CiBuildStage
from . import deploy_config
from . import logs as logs_service

logger = logging.getLogger(__name__)

# How often the rollout is read from the cluster. The engine passes every
# second or two while a build runs; the API server does not need to hear from
# every one of them.
_CHECK_SECONDS = 5.0
# A still-waiting stage says so at least this often, which is also what keeps
# the engine's idle reaper from mistaking a long approval wait for a lost build.
_HEARTBEAT_SECONDS = 300.0
# Image pull errors are retried by the kubelet, and the first ErrImagePull can
# be a registry hiccup. One that is still there after this long is not.
_PULL_ERROR_GRACE_SECONDS = 60.0
# A container that has crashed this many times on the new image is not coming up.
_CRASH_RESTARTS = 3
# A registry that cannot be reached is retried for this long before the stage
# gives up; a definite "not found" fails at once.
_REGISTRY_RETRY_SECONDS = 120.0

_FATAL_WAITING = ("InvalidImageName", "CreateContainerConfigError", "CreateContainerError")
_PULL_WAITING = ("ImagePullBackOff", "ErrImagePull")

TERMINAL_BUNDLE_OK = ("completed",)


# ---------------------------------------------------------------------------
# Engine entry points
# ---------------------------------------------------------------------------

def start(build: CiBuild, stage: CiBuildStage, definition: Dict[str, Any]) -> None:
    """Begin a Deploy stage. Leaves it running, or closes it with the reason."""
    stage.status = "running"
    stage.started_at = _now()
    stage.runner_id = None
    config = definition.get("deploy") if isinstance(definition.get("deploy"), dict) else None
    target = _target(config)
    _save(stage, {"phase": "starting", "target": target, "outcome": None, "message": ""})
    db.session.add(stage)
    db.session.flush()

    earlier = _earlier_failure(build, stage)
    if earlier:
        _finish(stage, "skipped", f"Nothing was deployed: {earlier}", outcome="skipped")
        return
    if config is None:
        _fail(stage, "This Deploy stage has no target. Pick a cluster, namespace and deployment.")
        return
    if deploy_config.is_linked(config):
        if config.get("unresolved"):
            _fail(stage, f"Nothing was deployed: {config['unresolved']}")
            return
        if not config.get("clusterId"):
            # A snapshot taken before linked targets were resolved at build time.
            _fail(stage, "Nothing was deployed: this stage's linked deployment was never resolved.")
            return
        label = f" ({config['environment']})" if config.get("environment") else ""
        _log(stage, f"Deploying to the service's linked deployment{label}")

    where = f"{config['clusterId']} / {config['namespace']} / {config['deploymentName']}"
    _log(stage, f"Deploying to {where}")
    _advance_resolve(build, stage, config, definition)


def advance(build: CiBuild, stage: CiBuildStage, definition: Dict[str, Any]) -> None:
    """Move a running Deploy stage on by one step. Cheap when nothing is due."""
    state = _state(stage)
    config = definition.get("deploy") if isinstance(definition.get("deploy"), dict) else {}
    phase = state.get("phase")

    timeout = int(definition.get("timeoutSeconds") or 1800)
    elapsed = _elapsed(stage)
    if elapsed > timeout:
        _on_timeout(build, stage, config, timeout)
        return

    if phase == "resolving":
        # Only reached when the registry was unreachable on the last try.
        _advance_resolve(build, stage, config, definition)
    elif phase == "waiting_approval":
        _advance_approval(build, stage, config)
    elif phase == "rolling_out":
        _advance_rollout(build, stage, config)
    else:
        # A state this module never writes — a restart mid-transition. Fail
        # honestly rather than guess what was applied.
        _fail(stage, f"The Deploy stage lost track of its progress (phase '{phase}').")


def cancel(build: CiBuild, stage: CiBuildStage) -> None:
    """The build was cancelled while this stage ran."""
    state = _state(stage)
    phase = state.get("phase")
    note = ""
    if phase == "waiting_approval" and state.get("bundleId"):
        note = " " + _withdraw_bundle(int(state["bundleId"]), build)
    elif phase == "rolling_out":
        note = (
            " The change was already applied, so it stays on the cluster; the rollout "
            "was not watched to the end and nothing was rolled back."
        )
    _finish(stage, "cancelled", f"Cancelled by request.{note}", outcome="cancelled")


def owns_bundle(bundle_id: int) -> bool:
    """Whether a Deploy stage queued this change bundle.

    The bundle executor starts its own rollout watch (with its own rollback)
    after it applies a Deployment. For a bundle a Deploy stage queued, the stage
    is already that watch — two of them would race to roll back twice.

    Any status, not only running: the CI ticker can see the bundle complete,
    watch a quick rollout and finish the stage before the executor gets to
    this question. The stage queues its bundle within minutes of starting, so
    stages started in the hours before the bundle existed are the only
    candidates.
    """
    from datetime import timedelta

    from ...models import ChangeBundle

    bundle = db.session.get(ChangeBundle, int(bundle_id))
    if bundle is None or bundle.created_at is None:
        return False
    created = bundle.created_at if bundle.created_at.tzinfo else bundle.created_at.replace(tzinfo=timezone.utc)
    rows = CiBuildStage.query.filter(
        CiBuildStage.stage_type == "deploy",
        CiBuildStage.started_at >= created - timedelta(hours=2),
        CiBuildStage.started_at <= created + timedelta(minutes=5),
    ).all()
    for row in rows:
        state = row.deploy_state if isinstance(row.deploy_state, dict) else {}
        if state.get("bundleId") and int(state["bundleId"]) == int(bundle_id):
            return True
    return False


def summarize(stage: CiBuildStage) -> str:
    """One sentence on what a Deploy stage did — for tickets and emails."""
    state = _state(stage)
    target = state.get("target") or {}
    where = "/".join(
        str(target.get(key) or "?") for key in ("clusterId", "namespace", "deploymentName")
    )
    message = state.get("message") or stage.error or ""
    image = state.get("image") or ""
    if state.get("outcome") == "deployed":
        return f"Deploy stage '{stage.name}' rolled {image} out to {where}. {message}".strip()
    return f"Deploy stage '{stage.name}' ({where}): {message}".strip()


def describe_target(user, cluster_id: str, namespace: str) -> Dict[str, Any]:
    """What the stage editor needs to point a Deploy stage somewhere.

    The deployments in a namespace with their containers (so the right one can
    be picked), whether the cluster queues changes for approval, whether its
    registries can confirm images at all, and whether the person looking could
    authorize this target — saving it requires that.
    """
    from ...access_engine import can_access_namespace, user_has_permission
    from ..deployment_request_service import cluster_required_approvals
    from ..registry_service import cluster_registry_ids

    cluster_id = str(cluster_id or "").strip()
    namespace = str(namespace or "").strip()
    if not cluster_id:
        raise ValueError("Pick a cluster.")
    can_deploy = bool(
        user is not None
        and user_has_permission(user, "apps:deploy")
        and (not namespace or can_access_namespace(user, cluster_id, namespace))
    )
    try:
        required = int(cluster_required_approvals(cluster_id))
    except Exception:  # noqa: BLE001 — informational only
        required = 0
    data: Dict[str, Any] = {
        "clusterId": cluster_id,
        "namespace": namespace,
        "requiredApprovals": required,
        "linkedRegistries": len(cluster_registry_ids(cluster_id) or []),
        "canDeploy": can_deploy,
        "namespaceExists": None,
        "deployments": [],
        "error": "",
    }
    if not namespace:
        return data
    if user is not None and not can_access_namespace(user, cluster_id, namespace):
        data["error"] = "You cannot see this namespace."
        return data
    try:
        deploy_config.validate_namespace(namespace)
    except Exception as exc:  # noqa: BLE001
        data["error"] = str(exc)
        return data

    real = should_use_real_k8s(cluster_id)
    config = {"clusterId": cluster_id, "namespace": namespace}
    exists, problem = _namespace_exists(config, real)
    if problem:
        data["error"] = problem
        return data
    data["namespaceExists"] = exists
    if not exists:
        return data

    items: List[Dict[str, Any]] = []
    if real:
        from ...k8s_provider import list_namespaced_resources_json, resolve_cluster_access

        access = resolve_cluster_access(cluster_id)
        if access is None:
            data["error"] = f"Cluster '{cluster_id}' was not found."
            return data
        try:
            documents = list_namespaced_resources_json(access, "deployments", namespace)
        except (K8sCommandError, ValueError) as exc:
            data["error"] = f"Could not list deployments: {exc}"
            return data
        for doc in documents:
            status = doc.get("status") or {}
            items.append(
                {
                    "name": (doc.get("metadata") or {}).get("name") or "",
                    "containers": [
                        {"name": c.get("name") or "", "image": c.get("image") or ""}
                        for c in deploy_config._containers(doc)
                    ],
                    "desired": int((doc.get("spec") or {}).get("replicas") or 0),
                    "ready": int(status.get("readyReplicas") or 0),
                }
            )
    else:
        from ...mock_data import NAMESPACE_RESOURCES

        entries = ((NAMESPACE_RESOURCES.get(cluster_id) or {}).get(namespace) or {}).get("deployments") or []
        for entry in entries:
            replicas = entry.get("replicas") or {}
            items.append(
                {
                    "name": entry.get("name") or "",
                    "containers": [{"name": entry.get("name") or "", "image": entry.get("image") or ""}],
                    "desired": int(replicas.get("desired") or 0),
                    "ready": int(replicas.get("ready") or 0),
                }
            )
    data["deployments"] = sorted((i for i in items if i["name"]), key=lambda i: i["name"])
    return data


# ---------------------------------------------------------------------------
# Phase: resolve → apply
# ---------------------------------------------------------------------------

def _advance_resolve(
    build: CiBuild, stage: CiBuildStage, config: Dict[str, Any], definition: Dict[str, Any]
) -> None:
    state = _state(stage)
    real = should_use_real_k8s(config["clusterId"])

    user, problem = _authorizing_user(config)
    if problem:
        _fail(stage, problem)
        return

    image = state.get("image")
    if not image:
        image, problem = _resolve_image(build, stage, config, definition)
        if problem:
            _fail(stage, problem)
            return
        _save(stage, {"image": image})
        _log(stage, f"Image: {image}")

    # 2. Registry — the image must be FOUND in one of the cluster's registries.
    verdict = _check_registry(image, config["clusterId"], real)
    if verdict == "retry":
        first = state.get("registryRetryAt") or time.time()
        if time.time() - float(first) > _REGISTRY_RETRY_SECONDS:
            _fail(
                stage,
                f"The registry could not be reached for {_REGISTRY_RETRY_SECONDS // 60:.0f} minutes, "
                f"so {image} could not be confirmed. Nothing was deployed.",
            )
            return
        if not state.get("registryRetryAt"):
            _log(stage, "The registry did not answer — retrying for up to two minutes.")
        _save(stage, {"phase": "resolving", "registryRetryAt": first})
        return
    if verdict:
        _fail(stage, verdict)
        return

    # 3. Namespace — must exist; a build never creates one.
    exists, problem = _namespace_exists(config, real)
    if problem:
        _fail(stage, problem)
        return
    if not exists:
        _fail(
            stage,
            f"Namespace '{config['namespace']}' does not exist on {config['clusterId']}. "
            "Create it first — a build does not create namespaces. Nothing was deployed.",
        )
        return

    # 4. The change.
    live, problem = _read_deployment(config, real)
    if problem:
        _fail(stage, problem)
        return

    note = _change_note(build, stage, config, image)
    if live is not None:
        container, why = deploy_config.pick_container(live, config.get("containerName") or "")
        if container is None:
            _fail(stage, why)
            return
        previous = str(container.get("image") or "")
        _save(stage, {"containerName": container.get("name"), "previousImage": previous, "created": False})
        if previous == image:
            _log(stage, f"{config['deploymentName']} already runs {image} — nothing to change.")
            _finish(
                stage, "success", f"Already running {image}; nothing was changed.", outcome="unchanged"
            )
            _audit("ci_deploy_unchanged", build, stage, user, config, image)
            return
        _log(stage, f"Existing deployment — changing {container.get('name')}: {previous} → {image}")
        manifest = deploy_config.swap_image(live, str(container.get("name")), image)
        created: List[Dict[str, str]] = []
    else:
        if not config.get("createIfMissing"):
            _fail(
                stage,
                f"Deployment '{config['deploymentName']}' does not exist in {config['namespace']}, and "
                "this stage is set not to create it. Turn on \"Create it if missing\", or create it first.",
            )
            return
        try:
            manifest, planned = deploy_config.render_manifest(config, image)
        except deploy_config.DeployConfigError as exc:
            _fail(stage, str(exc))
            return
        created = [item for item in planned if not _object_exists(config, item, real)]
        _save(
            stage,
            {
                "containerName": config.get("containerName") or config["deploymentName"],
                "previousImage": None,
                "created": True,
                "createdResources": created,
            },
        )
        _log(
            stage,
            f"Deployment '{config['deploymentName']}' is not there yet — creating "
            + ", ".join(f"{item['kind']}/{item['name']}" for item in planned),
        )

    # 5. Apply — through the approval gate.
    data, error, status = _apply(user, config, manifest, note, real)
    if error:
        _fail(stage, f"The change was refused: {error}")
        return
    if status == 202 or (data or {}).get("pendingApproval"):
        bundle_id = (data or {}).get("bundleId")
        _save(stage, {"phase": "waiting_approval", "bundleId": bundle_id, "heartbeatAt": time.time()})
        required = (data or {}).get("requiredApprovals")
        _log(
            stage,
            f"{config['clusterId']} requires approval — sent as change bundle #{bundle_id}"
            + (f" ({required} approval(s) needed)" if required else "")
            + ". Waiting; it is applied automatically once approved.",
        )
        _audit("ci_deploy_queued_for_approval", build, stage, user, config, image, bundleId=bundle_id)
        return

    _log(stage, "Applied." + (" [mock]" if not real else ""))
    _audit("ci_deploy_applied", build, stage, user, config, image, created=bool(created))
    _begin_rollout(stage)


# ---------------------------------------------------------------------------
# Phase: waiting for approval
# ---------------------------------------------------------------------------

def _advance_approval(build: CiBuild, stage: CiBuildStage, config: Dict[str, Any]) -> None:
    from ...models import ChangeBundle

    state = _state(stage)
    bundle = db.session.get(ChangeBundle, int(state.get("bundleId") or 0))
    if bundle is None:
        _fail(stage, "The change bundle this stage was waiting on was deleted. Nothing was deployed.")
        return
    status = bundle.status
    if status == "pending_approval":
        _heartbeat(stage, f"Still waiting for change bundle #{bundle.id} to be approved.")
        return
    if status in ("approved", "scheduled", "deploying"):
        if state.get("approvalSeen") != status:
            _save(stage, {"approvalSeen": status})
            _log(stage, f"Change bundle #{bundle.id} approved — {'applying' if status == 'deploying' else 'waiting for the executor'}.")
        return
    if status in TERMINAL_BUNDLE_OK:
        item = next(iter(sorted(bundle.items, key=lambda i: i.position)), None)
        if item is not None and item.status != "succeeded":
            detail = (item.execution_result or {}).get("error") or item.validation_message or item.status
            _fail(stage, f"Change bundle #{bundle.id} did not apply the change: {detail}")
            return
        _log(stage, f"Change bundle #{bundle.id} applied the change.")
        _begin_rollout(stage)
        return
    if status == "rejected":
        reason = f": {bundle.rejection_reason}" if bundle.rejection_reason else "."
        _fail(stage, f"Change bundle #{bundle.id} was declined{reason} Nothing was deployed.")
        return
    if status == "expired":
        _fail(stage, f"Change bundle #{bundle.id} expired before it was approved. Nothing was deployed.")
        return
    if status in ("failed", "partially_failed"):
        item = next(iter(bundle.items), None)
        detail = ((item.execution_result or {}).get("error") if item else "") or status.replace("_", " ")
        _fail(stage, f"Change bundle #{bundle.id} failed to apply: {detail}")
        return
    _heartbeat(stage, f"Change bundle #{bundle.id} is {status.replace('_', ' ')}.")


def _withdraw_bundle(bundle_id: int, build: CiBuild) -> str:
    """Stop a queued change so a cancelled build cannot deploy later."""
    from ...models import ChangeBundle
    from ..change_bundle_service import ChangeBundleError, decide_bundle

    bundle = db.session.get(ChangeBundle, bundle_id)
    if bundle is None:
        return ""
    reason = f"CI build #{build.number} was cancelled."
    if bundle.status == "pending_approval":
        try:
            decide_bundle(bundle.id, "decline", actor=None, reason=reason)
            return f"Change bundle #{bundle.id} was withdrawn."
        except ChangeBundleError:
            db.session.rollback()
            bundle = db.session.get(ChangeBundle, bundle_id)
            if bundle is None:
                return ""
    if bundle.status in ("approved", "scheduled"):
        bundle.status = "rejected"
        bundle.rejection_reason = reason
        db.session.add(bundle)
        return f"Change bundle #{bundle.id} was withdrawn before it ran."
    if bundle.status == "deploying":
        return f"Change bundle #{bundle.id} was already applying and could not be stopped."
    return ""


# ---------------------------------------------------------------------------
# Phase: rollout
# ---------------------------------------------------------------------------

def _begin_rollout(stage: CiBuildStage) -> None:
    _save(
        stage,
        {"phase": "rolling_out", "rolloutStartedAt": time.time(), "nextCheckAt": 0, "lastDetail": ""},
    )
    _log(stage, "Waiting for the new pods to become ready…")


def _advance_rollout(build: CiBuild, stage: CiBuildStage, config: Dict[str, Any]) -> None:
    state = _state(stage)
    if time.time() < float(state.get("nextCheckAt") or 0):
        return
    _save(stage, {"nextCheckAt": time.time() + _CHECK_SECONDS})

    if not should_use_real_k8s(config["clusterId"]):
        _succeed(build, stage, config, "[mock] 1/1 ready")
        return

    from ..deployment_service import rollout_health

    detail = ""
    try:
        health = rollout_health(config["clusterId"], config["namespace"], config["deploymentName"])
        if not health["observedCurrent"]:
            detail = "waiting for the controller to pick up the change"
        else:
            desired, updated = health["desired"], health["updated"]
            total, available = health["total"], health["available"]
            if desired == 0:
                _succeed(build, stage, config, "scaled to 0 — no pods expected")
                return
            # The full `kubectl rollout status` condition. ready/updated alone
            # read green on a stuck rolling update: the OLD pod keeps ready
            # satisfied while the crashlooping NEW one counts as updated.
            if updated >= desired and total <= updated and available >= updated:
                _succeed(build, stage, config, f"{available}/{desired} ready")
                return
            if updated < desired:
                detail = f"{updated}/{desired} pods on the new image"
            elif total > updated:
                detail = f"waiting for {total - updated} old pod(s) to stop"
            else:
                detail = f"{available}/{updated} new pods available"
    except (K8sCommandError, ValueError) as exc:
        # Pods churn and API servers hiccup mid-rollout; only a real pod
        # problem or the stage timeout ends it.
        detail = f"could not read the rollout — retrying ({exc})"

    problem = _pod_problem(stage, config)
    if problem:
        _rollback_and_fail(build, stage, config, f"The new pods are failing: {problem}", status="failed")
        return

    if detail != state.get("lastDetail"):
        _save(stage, {"lastDetail": detail, "heartbeatAt": time.time()})
        _log(stage, detail)
    else:
        _heartbeat(stage, f"Still rolling out: {detail}")


def _pod_problem(stage: CiBuildStage, config: Dict[str, Any]) -> str:
    """A failure on a pod running the NEW image that will not fix itself."""
    from ..deployment_service import _run_kubectl_for_cluster

    state = _state(stage)
    image = state.get("image") or ""
    container_name = state.get("containerName") or ""
    try:
        raw = _run_kubectl_for_cluster(
            config["clusterId"],
            ["get", "deployment", config["deploymentName"], "-n", config["namespace"], "-o", "json"],
        )
        selector = ((json.loads(raw).get("spec") or {}).get("selector") or {}).get("matchLabels") or {}
        if not selector:
            return ""
        label = ",".join(f"{key}={value}" for key, value in sorted(selector.items()))
        raw = _run_kubectl_for_cluster(
            config["clusterId"], ["get", "pods", "-n", config["namespace"], "-l", label, "-o", "json"]
        )
        pods = json.loads(raw).get("items") or []
    except (K8sCommandError, ValueError):
        return ""

    pull_error = ""
    for pod in pods:
        name = (pod.get("metadata") or {}).get("name") or "pod"
        spec_images = {
            c.get("name"): c.get("image") for c in ((pod.get("spec") or {}).get("containers") or [])
        }
        # Only pods on the new image: an old pod that was already unhealthy
        # says nothing about this rollout.
        if spec_images.get(container_name) != image:
            continue
        for status in (pod.get("status") or {}).get("containerStatuses") or []:
            if status.get("name") != container_name:
                continue
            waiting = (status.get("state") or {}).get("waiting") or {}
            reason = waiting.get("reason") or ""
            if reason in _FATAL_WAITING:
                return f"{name}: {reason} — {waiting.get('message') or ''}".strip(" —")
            if reason == "CrashLoopBackOff" and int(status.get("restartCount") or 0) >= _CRASH_RESTARTS:
                return f"{name} keeps crashing (CrashLoopBackOff, {status.get('restartCount')} restarts)"
            if reason in _PULL_WAITING:
                pull_error = f"{name}: {reason} — {waiting.get('message') or 'the image cannot be pulled'}"
    if not pull_error:
        if state.get("pullErrorSince"):
            _save(stage, {"pullErrorSince": None})
        return ""
    since = state.get("pullErrorSince")
    if not since:
        _save(stage, {"pullErrorSince": time.time()})
        return ""
    if time.time() - float(since) >= _PULL_ERROR_GRACE_SECONDS:
        return pull_error
    return ""


def _succeed(build: CiBuild, stage: CiBuildStage, config: Dict[str, Any], detail: str) -> None:
    state = _state(stage)
    image = state.get("image") or ""
    _log(stage, f"✓ Rolled out: {detail}")
    user, _ = _authorizing_user(config)
    _audit("ci_deploy_succeeded", build, stage, user, config, image, pods=detail)
    # The service is that deployment now: record the inventory link (fixed
    # targets only, unless the stage opts out; never takes another service's).
    from . import deployment_links

    deployment_links.record_from_deploy(build, config)
    _finish(stage, "success", f"{image} is running on {config['deploymentName']} ({detail}).", outcome="deployed")


def _on_timeout(build: CiBuild, stage: CiBuildStage, config: Dict[str, Any], timeout: int) -> None:
    state = _state(stage)
    phase = state.get("phase")
    limit = f"{max(1, round(timeout / 60))} min"
    if phase == "waiting_approval":
        note = _withdraw_bundle(int(state.get("bundleId") or 0), build) if state.get("bundleId") else ""
        _finish(
            stage,
            "timeout",
            f"Not approved within the stage's {limit} limit. Nothing was deployed. {note}".strip(),
            outcome="failed",
        )
        return
    if phase == "rolling_out":
        detail = state.get("lastDetail") or "the pods did not become ready"
        _rollback_and_fail(
            build, stage, config,
            f"The rollout did not finish within the stage's {limit} limit ({detail}).",
            status="timeout",
        )
        return
    _finish(stage, "timeout", f"The stage exceeded its {limit} limit before it could deploy.", outcome="failed")


def _rollback_and_fail(
    build: CiBuild, stage: CiBuildStage, config: Dict[str, Any], reason: str, *, status: str
) -> None:
    """Put back what was running before, then fail the stage with both facts."""
    from ..deployment_service import _run_kubectl_for_cluster

    state = _state(stage)
    real = should_use_real_k8s(config["clusterId"])
    _save(stage, {"phase": "rolling_back"})
    _log(stage, reason)

    try:
        if state.get("created"):
            removed = []
            for item in state.get("createdResources") or []:
                if real:
                    _run_kubectl_for_cluster(
                        config["clusterId"],
                        ["delete", item["kind"].lower(), item["name"], "-n", config["namespace"], "--ignore-not-found=true"],
                    )
                removed.append(f"{item['kind']}/{item['name']}")
            note = (
                "Removed what this stage created (" + ", ".join(removed) + ")."
                if removed
                else "Nothing this stage created needed removing."
            )
        elif state.get("previousImage"):
            if real:
                _run_kubectl_for_cluster(
                    config["clusterId"],
                    [
                        "set", "image", f"deployment/{config['deploymentName']}",
                        f"{state.get('containerName')}={state['previousImage']}",
                        "-n", config["namespace"],
                    ],
                )
            note = f"Rolled back to {state['previousImage']}."
        else:
            note = "There was no previous image to go back to."
        outcome = "rolled_back"
    except K8sCommandError as exc:
        note = f"The automatic rollback FAILED: {exc}. The deployment needs attention now."
        outcome = "rollback_failed"

    _log(stage, note)
    user, _ = _authorizing_user(config)
    _audit(
        "ci_deploy_rolled_back" if outcome == "rolled_back" else "ci_deploy_rollback_failed",
        build, stage, user, config, state.get("image") or "", reason=reason, result=note,
    )
    _finish(stage, status, f"{reason} {note}", outcome=outcome)


# ---------------------------------------------------------------------------
# Checks
# ---------------------------------------------------------------------------

def _authorizing_user(config: Dict[str, Any]):
    """The person this stage deploys as, still able to — or (None, why not)."""
    from ...access_engine import can_access_namespace, user_has_permission
    from ...models import User

    stamp = config.get("authorizedBy") if isinstance(config.get("authorizedBy"), dict) else None
    if not stamp or not stamp.get("userId"):
        return None, (
            "Nobody has authorized this Deploy stage's target. Someone who can deploy to "
            f"{config.get('clusterId')}/{config.get('namespace')} must save the pipeline."
        )
    user = db.session.get(User, int(stamp["userId"]))
    who = stamp.get("username") or f"user #{stamp['userId']}"
    if user is None or not getattr(user, "is_active", True):
        return None, (
            f"The target was authorized by {who}, whose account is no longer active. Someone who "
            f"can deploy to {config['clusterId']}/{config['namespace']} must save the stage again."
        )
    if not (
        user_has_permission(user, "apps:deploy")
        and can_access_namespace(user, config["clusterId"], config["namespace"])
    ):
        return None, (
            f"The target was authorized by {who}, who can no longer deploy to "
            f"{config['clusterId']}/{config['namespace']}. Someone who can must save the stage again."
        )
    return user, ""


def _resolve_image(
    build: CiBuild, stage: CiBuildStage, config: Dict[str, Any], definition: Dict[str, Any]
) -> Tuple[str, str]:
    from . import engine

    fixed = config.get("image") or ""
    if fixed:
        env = {
            **((build.pipeline_snapshot or {}).get("variables") or {}),
            "KUBESIGHT_BUILD_NUMBER": str(build.number),
            "KUBESIGHT_BRANCH": build.branch or "",
            "KUBESIGHT_COMMIT": build.commit_sha or "",
        }
        try:
            return engine._resolve_stage_image(fixed, env, stage.name) or "", ""
        except engine.BuildError as exc:
            return "", str(exc)

    artifact = (
        CiArtifact.query.filter(
            CiArtifact.build_id == build.id,
            CiArtifact.artifact_type == "container-image",
            CiArtifact.uri.isnot(None),
        )
        .order_by(CiArtifact.id.desc())
        .first()
    )
    if artifact is None or not artifact.uri:
        return "", (
            "This build pushed no image, so there is nothing to deploy. Add a \"Build an image\" "
            "stage before this one (and check it was not skipped), or set a fixed image on this stage."
        )
    return artifact.uri, ""


def _check_registry(image: str, cluster_id: str, real: bool) -> str:
    """"" when the image is confirmed, "retry" when the registry did not answer,
    otherwise why the image may not be deployed."""
    from ..registry_service import check_image

    try:
        result = check_image(image, cluster_id=cluster_id)
    except Exception as exc:  # noqa: BLE001 — a crashed check is not a pass
        logger.exception("Registry check failed for %s", image)
        return f"The image could not be checked against the registry ({exc}). Nothing was deployed."
    status = result.get("status")
    if status == "found":
        return ""
    if status == "unreachable":
        return "retry"
    if status == "no_connection" and not real:
        # Mock clusters have no registries to ask; say so rather than pretend.
        return ""
    if status == "no_connection":
        return (
            f"No registry linked to {cluster_id} holds {image}, so it cannot be confirmed. Link the "
            "registry this build pushes to with the cluster (Registries → cluster links). Nothing was deployed."
        )
    return f"{result.get('message') or image + ' was not found.'} Nothing was deployed."


def _namespace_exists(config: Dict[str, Any], real: bool) -> Tuple[bool, str]:
    if not real:
        from ...mock_data import NAMESPACE_RESOURCES

        return config["namespace"] in (NAMESPACE_RESOURCES.get(config["clusterId"]) or {}), ""
    from ..deployment_service import _run_kubectl_for_cluster

    try:
        _run_kubectl_for_cluster(config["clusterId"], ["get", "namespace", config["namespace"], "-o", "name"])
        return True, ""
    except K8sCommandError as exc:
        if _not_found(exc):
            return False, ""
        return False, f"Could not reach {config['clusterId']} to check the namespace: {exc}"


def _read_deployment(config: Dict[str, Any], real: bool) -> Tuple[Optional[Dict[str, Any]], str]:
    """The live Deployment, None when it does not exist, or an error."""
    if not real:
        from ...mock_data import NAMESPACE_RESOURCES

        entries = ((NAMESPACE_RESOURCES.get(config["clusterId"]) or {}).get(config["namespace"]) or {}).get(
            "deployments"
        ) or []
        entry = next((d for d in entries if d.get("name") == config["deploymentName"]), None)
        if entry is None:
            return None, ""
        container = config.get("containerName") or config["deploymentName"]
        return {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": entry["name"], "namespace": config["namespace"]},
            "spec": {"template": {"spec": {"containers": [{"name": container, "image": entry.get("image") or ""}]}}},
        }, ""
    from ..deployment_service import _run_kubectl_for_cluster

    try:
        raw = _run_kubectl_for_cluster(
            config["clusterId"],
            ["get", "deployment", config["deploymentName"], "-n", config["namespace"], "-o", "json"],
        )
        return json.loads(raw), ""
    except K8sCommandError as exc:
        if _not_found(exc):
            return None, ""
        return None, f"Could not read deployment '{config['deploymentName']}': {exc}"
    except ValueError:
        return None, f"The cluster returned an unreadable deployment '{config['deploymentName']}'."


def _object_exists(config: Dict[str, Any], item: Dict[str, str], real: bool) -> bool:
    """Whether a manifest object is already there — so a rollback leaves it alone."""
    if item["kind"] == "Deployment":
        return False  # Only created when it was missing; that is why we are here.
    if not real:
        return False
    from ..deployment_service import _run_kubectl_for_cluster

    try:
        _run_kubectl_for_cluster(
            config["clusterId"], ["get", item["kind"].lower(), item["name"], "-n", config["namespace"], "-o", "name"]
        )
        return True
    except K8sCommandError:
        return False


def _apply(user, config: Dict[str, Any], manifest: str, note: str, real: bool):
    """Apply through the one gate every deploy in KubeSight uses."""
    from ..deployment_service import apply_yaml, validate_yaml

    if real:
        return apply_yaml(
            user, config["clusterId"], config["namespace"], manifest, "", change_note=note
        )
    # Mock clusters have no API server to apply to, but the approval rule is
    # database state and still holds: a gated mock cluster queues the change
    # exactly like a real one (the bundle executor "applies" it as [mock]).
    from ..change_bundle_service import gate_or_queue

    validation, err, code = validate_yaml(manifest, config["namespace"], user=user)
    if err:
        return None, err, code
    queued = gate_or_queue(
        user,
        config["clusterId"],
        bundle_payload={"actionType": "apply_yaml", "namespace": config["namespace"], "yaml": manifest},
        what=note,
        action="apply",
        target_type="namespace",
        target_id=f"{config['clusterId']}/{config['namespace']}",
    )
    if queued is not None:
        return queued
    return {"applied": True, "output": "[mock] applied", **(validation or {})}, None, 200


def _earlier_failure(build: CiBuild, stage: CiBuildStage) -> str:
    """Why an earlier stage means this build must not deploy, or ""."""
    for other in sorted(build.stages, key=lambda s: s.position):
        if other.position >= stage.position:
            break
        # continueOnFailure lets later stages run to gather information; it
        # never makes a failed build fit to ship.
        if other.status in ("failed", "timeout", "cancelled"):
            return f"stage '{other.name}' {other.status}."
    return ""


def _not_found(exc: Exception) -> bool:
    text = str(exc)
    return "NotFound" in text or "not found" in text


# ---------------------------------------------------------------------------
# State, logs, audit
# ---------------------------------------------------------------------------

def _state(stage: CiBuildStage) -> Dict[str, Any]:
    return dict(stage.deploy_state) if isinstance(stage.deploy_state, dict) else {}


def _save(stage: CiBuildStage, patch: Dict[str, Any]) -> None:
    # A new dict every time: JSON columns only notice reassignment.
    stage.deploy_state = {**_state(stage), **patch}
    db.session.add(stage)


def _target(config: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    config = config or {}
    return {
        "clusterId": config.get("clusterId") or "",
        "namespace": config.get("namespace") or "",
        "deploymentName": config.get("deploymentName") or "",
        "containerName": config.get("containerName") or "",
        "authorizedBy": (config.get("authorizedBy") or {}).get("username") or "",
    }


def _log(stage: CiBuildStage, message: str) -> None:
    logs_service.append_system(stage, f"[deploy] {message}", commit=False)


def _heartbeat(stage: CiBuildStage, message: str) -> None:
    state = _state(stage)
    if time.time() - float(state.get("heartbeatAt") or 0) >= _HEARTBEAT_SECONDS:
        _save(stage, {"heartbeatAt": time.time()})
        _log(stage, message)


def _fail(stage: CiBuildStage, message: str) -> None:
    _log(stage, message)
    _finish(stage, "failed", message, outcome="failed")


def _finish(stage: CiBuildStage, status: str, message: str, *, outcome: str) -> None:
    from . import engine

    _save(stage, {"phase": "done", "outcome": outcome, "message": message, "finishedAt": _now().isoformat()})
    engine._close_stage(stage, status, message if status != "success" else None)


def _change_note(build: CiBuild, stage: CiBuildStage, config: Dict[str, Any], image: str) -> str:
    service = build.service.slug if build.service else "service"
    return (
        f"CI build #{build.number} of {service} (stage '{stage.name}'): deploy {image} "
        f"to {config['deploymentName']} in {config['namespace']}"
    )


def _audit(action: str, build: CiBuild, stage: CiBuildStage, user, config, image: str, **extra) -> None:
    log_audit(
        action,
        actor=user,
        target_type="ci_build",
        target_id=str(build.id),
        details={
            "service": build.service.slug if build.service else None,
            "buildNumber": build.number,
            "stage": stage.name,
            "cluster": config.get("clusterId"),
            "namespace": config.get("namespace"),
            "deployment": config.get("deploymentName"),
            "image": image,
            **extra,
        },
        commit=False,
    )


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _elapsed(stage: CiBuildStage) -> int:
    started = stage.started_at
    if started is None:
        return 0
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return max(0, int((_now() - started).total_seconds()))
