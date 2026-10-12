"""Scheduled execution of approved Change Bundles.

When an approved bundle's deployment window opens, the background scheduler calls
:func:`process_due_bundles`, which re-validates and applies each item in order.
Stop-on-failure is the safe default. Everything is re-validated against live
cluster state at execution time because the cluster may have changed since the
bundle was approved.
"""

from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import or_

from ..audit import log_audit
from ..db import db
from ..k8s_provider import K8sCommandError, resolve_cluster_access, should_use_real_k8s
from ..models import ChangeBundle, ChangeBundleItem
from .deployment_service import (
    _cleanup_temp,
    _run_kubectl_for_cluster,
    _write_temp_yaml,
    sanitize_for_apply,
    validate_yaml,
)

logger = logging.getLogger(__name__)

ACTION_EXEC_STARTED = "BUNDLE_EXECUTION_STARTED"
ACTION_ITEM_APPLIED = "BUNDLE_ITEM_APPLIED"
ACTION_ITEM_FAILED = "BUNDLE_ITEM_FAILED"
ACTION_COMPLETED = "BUNDLE_COMPLETED"
ACTION_FAILED = "BUNDLE_FAILED"
ACTION_PARTIAL = "BUNDLE_PARTIALLY_FAILED"
ACTION_EXPIRED = "BUNDLE_EXPIRED"
ACTION_ROLLOUT_HEALTHY = "BUNDLE_ROLLOUT_HEALTHY"
ACTION_ROLLOUT_FAILED = "BUNDLE_ROLLOUT_FAILED"
ACTION_ROLLBACK = "BUNDLE_ROLLBACK"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _item_target(item: ChangeBundleItem) -> str:
    return f"{item.cluster_id}/{item.namespace}/{item.resource_kind}/{item.resource_name}".rstrip("/")


def _resource_arg(item: ChangeBundleItem) -> str:
    """kubectl resource selector like deployment/<name> from the item's kind."""
    return f"{(item.resource_kind or 'deployment').lower()}/{item.resource_name}"


_NAMED_TARGET_MODES = ("scale", "delete", "restart", "rollback")


def _target_name_error(item: ChangeBundleItem, mode: str) -> Optional[str]:
    """Kind/name/namespace of a named-target item must be valid K8s names.

    They are positional kubectl args; a flag-shaped value (``--all``,
    ``--server=...``) must be refused, not executed."""
    if mode not in _NAMED_TARGET_MODES:
        return None
    from ..k8s_names import name_error

    return name_error(
        namespace=item.namespace or "",
        names=(
            ((item.resource_kind or "deployment").lower(), "resource kind"),
            (item.resource_name or "", "resource name"),
        ),
    )


def _revalidate(
    item: ChangeBundleItem, mode: str, registry_cache: Optional[Dict[Any, Any]] = None
) -> Tuple[Optional[str], Optional[str]]:
    """Re-check the item against live cluster state at execution time.

    Returns ``(error, reason)``: ``error`` is None when the item is good to apply;
    otherwise ``reason`` is one of ``"validation" | "image" | "target"`` so the
    caller can react (e.g. alert recipients when an image vanished).
    """
    if mode == "apply":
        # The bundle's approval is the authorization, so cluster-scoped/sensitive
        # kinds are not hard-blocked here (preview_mode); the actual apply runs
        # with the cluster's real credentials.
        _result, err, _ = validate_yaml(
            item.yaml_preview or "", item.namespace, user=None, preview_mode=True
        )
        if err:
            return err, "validation"

        # Confirm each image STILL exists in its linked registry — an image that
        # was present when the bundle was staged may have been deleted before the
        # (possibly days-later) deploy window opened. Block enforcement stops the
        # apply so a now-missing image never reaches the cluster.
        from .change_bundle_service import registry_images_check

        _checks, blocking, image_err = registry_images_check(
            item.yaml_preview or "", item.cluster_id, registry_cache
        )
        if blocking:
            return image_err, "image"

    if mode in ("apply", "helm"):
        # The promotion ladder, again: an image that passed the previous
        # environment when this was staged still has (the ledger only grows),
        # but a bundle staged before the environment switched to enforce has
        # not been checked yet. An exception bundle is exempt — its approval
        # is the exception.
        from .change_bundle_service import promotion_refusal

        refusal = promotion_refusal(item)
        if refusal:
            return refusal, "promotion"

    invalid = _target_name_error(item, mode)
    if invalid:
        return invalid, "validation"

    # Helm items are re-checked by helm_service itself when they run (chart,
    # namespace, rendered images), so there is nothing to pre-check here.
    if mode == "helm" or not should_use_real_k8s(item.cluster_id):
        return None, None

    # For changes to an existing object, confirm the target still exists.
    if mode in ("scale", "delete", "restart", "rollback"):
        try:
            _run_kubectl_for_cluster(
                item.cluster_id,
                ["get", _resource_arg(item), "-n", item.namespace],
            )
        except K8sCommandError as exc:
            return f"Target no longer exists or is unreachable: {exc}", "target"
    return None, None


def _capture_diff(item: ChangeBundleItem, mode: str) -> Optional[str]:
    """Best-effort diff snapshot taken just before the item is applied.

    Stored on the item so reviewers can still see what a bundle changed after it
    has run — a live ``kubectl diff`` is empty once the change is applied. Never
    raises: a diff problem must not block the apply itself.
    """
    if not should_use_real_k8s(item.cluster_id):
        return None
    try:
        if mode == "apply":
            from .deployment_service import diff_yaml

            data, err, _ = diff_yaml(None, item.cluster_id, item.namespace, item.yaml_preview or "")
            if err:
                return None
            return (data or {}).get("diff") or "No differences from the cluster state at execution time."
        if mode == "helm":
            from .change_bundle_service import _helm_item_diff

            return _helm_item_diff(item)
        if mode == "scale":
            current = _run_kubectl_for_cluster(
                item.cluster_id,
                ["get", _resource_arg(item), "-n", item.namespace, "-o", "jsonpath={.spec.replicas}"],
            ).strip()
            target = (item.new_payload_json or {}).get("execution", {}).get("replicas")
            return f"{_resource_arg(item)} spec.replicas\n- replicas: {current or '?'}\n+ replicas: {target}\n"
    except Exception:  # noqa: BLE001 — diff capture is informational only
        logger.exception("Diff capture failed for bundle item #%s", item.id)
    return None


def approval_context_for(item: ChangeBundleItem) -> str:
    """The explicit approval an executed item carries into the gated services."""
    return f"change_bundle:{item.bundle_id}"


def _apply_item(item: ChangeBundleItem, mode: str) -> str:
    """Execute one item against the cluster. Returns kubectl output (or mock note)."""
    invalid = _target_name_error(item, mode)
    if invalid:
        raise K8sCommandError(invalid)
    if not should_use_real_k8s(item.cluster_id):
        return f"[mock] {mode} {_resource_arg(item)} in {item.namespace}"

    if mode == "helm":
        # Through helm_service, so a queued Helm change gets exactly the checks
        # of a direct one; approval_context tells its gate this is approved.
        from .helm_service import run_approved_helm_change

        item_input = {
            **((item.new_payload_json or {}).get("input") or {}),
            "clusterId": item.cluster_id,
            "namespace": item.namespace,
            "resourceName": item.resource_name,
        }
        output, _data = run_approved_helm_change(
            item_input, item.action_type, approval_context=approval_context_for(item)
        )
        _record_helm_catalog(item)
        return output

    if mode == "apply":
        path = _write_temp_yaml(sanitize_for_apply(item.yaml_preview or ""))
        try:
            # Same as a direct apply: a change queued for a brand-new namespace
            # must not fail just because the namespace does not exist yet.
            from .deployment_service import _ensure_namespace

            _ensure_namespace(_run_kubectl_for_cluster, item.cluster_id, item.namespace)
            return _run_kubectl_for_cluster(
                item.cluster_id, ["apply", "-f", path, "-n", item.namespace]
            ).strip()
        finally:
            _cleanup_temp(path)

    if mode == "scale":
        replicas = (item.new_payload_json or {}).get("execution", {}).get("replicas", 1)
        return _run_kubectl_for_cluster(
            item.cluster_id,
            ["scale", _resource_arg(item), f"--replicas={int(replicas)}", "-n", item.namespace],
        ).strip()

    if mode == "restart":
        if (item.resource_kind or "").lower() == "pod":
            # A pod restarts by being deleted; its controller recreates it.
            return _run_kubectl_for_cluster(
                item.cluster_id, ["delete", "pod", item.resource_name, "-n", item.namespace]
            ).strip()
        return _run_kubectl_for_cluster(
            item.cluster_id, ["rollout", "restart", _resource_arg(item), "-n", item.namespace]
        ).strip()

    if mode == "rollback":
        args = ["rollout", "undo", _resource_arg(item), "-n", item.namespace]
        revision = (item.new_payload_json or {}).get("execution", {}).get("revision")
        if revision:
            args.append(f"--to-revision={int(revision)}")
        return _run_kubectl_for_cluster(item.cluster_id, args).strip()

    if mode == "delete":
        return _run_kubectl_for_cluster(
            item.cluster_id,
            ["delete", (item.resource_kind or "deployment").lower(), item.resource_name, "-n", item.namespace],
        ).strip()

    raise K8sCommandError(f"Unknown execution mode: {mode}")


def _after_applied_for_promotion(bundle: ChangeBundle, item: ChangeBundleItem, mode: str) -> None:
    """Promotion bookkeeping once an item is applied. Never raises.

    An exception bundle's deploy goes in the activity feed. On a mock cluster
    there is no rollout to watch, so the mock ladder moves to the new image
    here (a real cluster's rollout watch records it once it is healthy)."""
    if mode != "apply":
        return
    try:
        from . import promotion_service

        images = promotion_service.images_in_yaml(item.yaml_preview or "")
        if bundle.promotion_exception:
            promotion_service.record_event(
                "exception_applied",
                None,
                path="bundle",
                actor=(bundle.promotion_exception or {}).get("requestedBy"),
                cluster_id=item.cluster_id,
                namespace=item.namespace,
                workload=item.resource_name,
                images=[{"image": i, "status": "exception"} for i in images],
                message=(bundle.promotion_exception or {}).get("reason"),
                bundle_id=bundle.id,
                environment=promotion_service.environment_for(item.cluster_id, item.namespace),
            )
        if not should_use_real_k8s(item.cluster_id) and item.resource_name and images:
            from . import promotion_mock

            if promotion_mock.get(item.cluster_id, item.namespace, item.resource_name) is not None:
                promotion_mock.set_image(item.cluster_id, item.namespace, item.resource_name, images[0])
                promotion_service.record_rollout(
                    item.cluster_id, item.namespace, images,
                    kind=item.resource_kind, name=item.resource_name, source="bundle",
                )
    except Exception:  # noqa: BLE001
        logger.exception("Promotion bookkeeping failed for bundle item #%s", item.id)


def _record_helm_catalog(item: ChangeBundleItem) -> None:
    """After a queued Helm install/upgrade ran, register it in the App Catalog
    as the direct route would have (best-effort, as the requester)."""
    if item.action_type not in ("helm_install", "helm_upgrade"):
        return
    try:
        from .helm_service import record_helm_catalog_entry

        body = dict(((item.new_payload_json or {}).get("input") or {}).get("helm") or {})
        body.update(
            {"clusterId": item.cluster_id, "namespace": item.namespace, "releaseName": item.resource_name}
        )
        record_helm_catalog_entry(item.bundle.requester if item.bundle else None, body)
    except Exception:  # noqa: BLE001 — the release is installed; the catalog is secondary
        logger.exception("Could not record the catalog entry for bundle item #%s", item.id)


def execute_bundle(bundle: ChangeBundle) -> str:
    """Execute every item of an approved bundle in order. Returns the final status."""
    if bundle.status not in ("approved", "scheduled"):
        raise RuntimeError(f"Bundle #{bundle.id} is not executable (status={bundle.status}).")
    claimed = _claim(bundle.id)
    if claimed != "claimed":
        if claimed == "expired":
            return "expired"
        raise RuntimeError("Window has not started yet." if claimed == "early" else f"Bundle #{bundle.id} was taken.")
    return _run_claimed(bundle.id)


def _claim(bundle_id: int) -> str:
    """Move a due bundle to ``deploying`` (committed) so no other tick runs it.

    Returns ``"claimed"``, ``"expired"`` (its window ended: marked expired),
    ``"early"`` (its window has not opened) or ``"taken"`` (no longer
    approved/scheduled — another tick won it).
    """
    bundle = db.session.get(ChangeBundle, bundle_id)
    if bundle is None or bundle.status not in ("approved", "scheduled"):
        return "taken"
    now = _now()
    start = _aware(bundle.requested_start_time)
    end = _aware(bundle.requested_end_time)
    if start and now < start:
        return "early"
    if end and now > end:
        _mark_expired(bundle)
        return "expired"
    # Conditional claim: two ticks (or two processes) never both win.
    won = ChangeBundle.query.filter(
        ChangeBundle.id == bundle_id, ChangeBundle.status.in_(("approved", "scheduled"))
    ).update({"status": "deploying", "execution_started_at": now, "updated_at": now},
             synchronize_session=False)
    db.session.commit()
    if not won:
        return "taken"
    return "claimed"


def _touch(bundle: ChangeBundle) -> None:
    """Heartbeat of a running bundle: stale-run recovery reads updated_at."""
    bundle.updated_at = _now()


def _run_claimed(bundle_id: int) -> str:
    """Apply every item of a bundle this process claimed. Returns the final status."""
    db.session.expire_all()
    bundle = db.session.get(ChangeBundle, bundle_id)
    if bundle is None or bundle.status != "deploying":
        return getattr(bundle, "status", "missing")
    _touch(bundle)
    db.session.commit()
    log_audit(
        ACTION_EXEC_STARTED,
        actor=None,
        target_type="change_bundle",
        target_id=str(bundle.id),
        details={"bundleId": bundle.id, "itemCount": len(bundle.items)},
    )

    registry_cache: Dict[Any, Any] = {}
    succeeded = 0
    failed = 0
    stopped = False
    items = sorted(bundle.items, key=lambda i: i.position)
    for item in items:
        if stopped:
            item.status = "skipped"
            item.execution_result = {"skipped": True, "reason": "stopped after earlier failure"}
            continue

        mode = (item.new_payload_json or {}).get("execution", {}).get("mode", "apply")
        item.status = "applying"
        _touch(bundle)
        db.session.commit()

        err, reason = _revalidate(item, mode, registry_cache)
        if err:
            failed += 1
            item.status = "failed"
            item.validation_status = "invalid"
            item.validation_message = err
            item.execution_result = {
                "ok": False, "error": err, "phase": "revalidate", "reason": reason,
            }
            db.session.commit()
            _audit_item(ACTION_ITEM_FAILED, bundle, item, {"error": err, "reason": reason})
            # A vanished image is an operational surprise the requester/ops need to
            # know about — notify configured recipients so the skipped deploy is seen.
            if reason == "image":
                _notify_image_unavailable(bundle, item, err)
            if bundle.stop_on_failure:
                stopped = True
            continue

        diff_text = _capture_diff(item, mode)
        try:
            output = _apply_item(item, mode)
            succeeded += 1
            item.status = "succeeded"
            item.execution_result = {"ok": True, "output": output, "mode": mode}
            if diff_text:
                item.execution_result["diff"] = diff_text
            db.session.commit()
            _audit_item(ACTION_ITEM_APPLIED, bundle, item, {"mode": mode})
            _after_applied_for_promotion(bundle, item, mode)
        except (K8sCommandError, Exception) as exc:  # noqa: BLE001 — record any failure
            failed += 1
            item.status = "failed"
            item.execution_result = {"ok": False, "error": str(exc), "mode": mode}
            if diff_text:
                item.execution_result["diff"] = diff_text
            db.session.commit()
            _audit_item(ACTION_ITEM_FAILED, bundle, item, {"error": str(exc)})
            if bundle.stop_on_failure:
                stopped = True

    if failed == 0:
        final, action = "completed", ACTION_COMPLETED
    elif succeeded == 0:
        final, action = "failed", ACTION_FAILED
    else:
        final, action = "partially_failed", ACTION_PARTIAL

    bundle.status = final
    bundle.execution_finished_at = _now()
    db.session.commit()
    log_audit(
        action,
        actor=None,
        target_type="change_bundle",
        target_id=str(bundle.id),
        details={"bundleId": bundle.id, "succeeded": succeeded, "failed": failed},
    )

    from .change_bundle_service import notify_requester_outcome

    notify_requester_outcome(
        bundle, final, detail=f"{succeeded} change(s) applied, {failed} failed." if failed else ""
    )
    _start_rollout_watches(bundle)
    return final


def _notify_image_unavailable(bundle: ChangeBundle, item: ChangeBundleItem, message: str) -> None:
    """Alert configured recipients that a scheduled deploy was blocked on a missing image.

    Best-effort: a delivery problem must never crash bundle execution.
    """
    try:
        from .alert_routing_service import notify_operational_alert

        alert_id = f"bundle-{bundle.id}-item-{item.id}-image-{int(_now().timestamp())}"
        notify_operational_alert(
            {
                "id": alert_id,
                "title": f"Change bundle #{bundle.id} blocked — image unavailable",
                "severity": "warning",
                "status": "firing",
                "clusterId": item.cluster_id,
                "namespace": item.namespace,
                "resourceType": item.resource_kind,
                "deployment": item.resource_name,
                "description": (
                    f"Scheduled deploy of {item.resource_kind}/{item.resource_name} in "
                    f"{item.namespace} ({item.cluster_name or item.cluster_id}) was NOT applied: "
                    f"{message}"
                ),
                "firedAt": _now().isoformat(),
            }
        )
    except Exception:  # noqa: BLE001 — never let a notification failure stop execution
        logger.exception("Failed to send image-unavailable alert for bundle #%s", bundle.id)


def _audit_item(action: str, bundle: ChangeBundle, item: ChangeBundleItem, extra: Dict[str, Any]) -> None:
    log_audit(
        action,
        actor=None,
        target_type="change_bundle_item",
        target_id=_item_target(item),
        details={
            "bundleId": bundle.id,
            "itemId": item.id,
            "actionType": item.action_type,
            "clusterId": item.cluster_id,
            "namespace": item.namespace,
            "resourceName": item.resource_name,
            **extra,
        },
    )


def _mark_expired(bundle: ChangeBundle) -> str:
    bundle.status = "expired"
    bundle.execution_finished_at = _now()
    for item in bundle.items:
        if item.status in ("pending", "applying"):
            item.status = "skipped"
            item.execution_result = {"skipped": True, "reason": "window expired"}
    db.session.commit()
    log_audit(
        ACTION_EXPIRED,
        actor=None,
        target_type="change_bundle",
        target_id=str(bundle.id),
        details={"bundleId": bundle.id},
    )
    from .change_bundle_service import notify_requester_outcome

    notify_requester_outcome(bundle, "expired")
    return "expired"


# ---------------------------------------------------------------------------
# Post-execution pod-health watch — the deploy-automation safety net, applied
# to manually-authored bundles: an applied Deployment must actually roll out.
# ---------------------------------------------------------------------------

def _start_rollout_watches(bundle: ChangeBundle) -> None:
    """Queue a pod-health watch for every Deployment this bundle applied.

    Deploy-automation bundles and bundles a CI Deploy stage is waiting on are
    skipped — their run (or stage) already watches the same deployment, with its
    own rollback + notification path, and two watchers would race to roll back
    twice. Best-effort: a watch problem must never disturb the just-finished
    execution.
    """
    try:
        from ..models import BundleRolloutWatch, DeployAutomationRun

        if DeployAutomationRun.query.filter_by(bundle_id=bundle.id).first():
            return
        # Same for a bundle a CI Deploy stage queued: the stage watches the
        # rollout and rolls back itself.
        from .ci.deploy_stage import owns_bundle

        if owns_bundle(bundle.id):
            return
        created = 0
        for item in bundle.items:
            mode = (item.new_payload_json or {}).get("execution", {}).get("mode", "apply")
            if item.status != "succeeded" or mode != "apply":
                continue
            if (item.resource_kind or "").lower() != "deployment":
                continue
            db.session.add(
                BundleRolloutWatch(
                    bundle_id=bundle.id,
                    item_id=item.id,
                    cluster_id=item.cluster_id,
                    namespace=item.namespace,
                    deployment_name=item.resource_name,
                )
            )
            created += 1
        if created:
            db.session.commit()
    except Exception:  # noqa: BLE001 — watch setup must not fail the execution
        logger.exception("Could not start rollout watches for bundle #%s", bundle.id)
        db.session.rollback()


def _watch_settings() -> Tuple[int, bool]:
    """(timeout minutes, rollback enabled) from the bundle-workflow settings row."""
    from ..models import DeploymentRequestSetting

    row = DeploymentRequestSetting.query.first()
    timeout = int((getattr(row, "rollout_timeout_minutes", None) or 15))
    rollback = row.rollback_on_failure if row is not None else True
    return timeout, (True if rollback is None else bool(rollback))


def watch_bundle_rollouts() -> int:
    """Advance every active post-execution watch one step (scheduler tick).

    Watches are grouped by (cluster, namespace): one ``kubectl get`` per
    group, not per watch, and a cluster that fails to answer is skipped for
    the rest of the pass (its watches read "could not read — retrying", as a
    failed read always did). Settings are read once per pass. Each watch is
    still isolated in try/except so one broken watch can't stall the rest.
    Returns how many watches were looked at.
    """
    from ..models import BundleRolloutWatch

    watches = BundleRolloutWatch.query.filter_by(status="watching").order_by(BundleRolloutWatch.id).all()
    if not watches:
        return 0
    settings = _watch_settings()
    groups: Dict[Tuple[str, str], List[Any]] = {}
    for watch in watches:
        groups.setdefault((watch.cluster_id, watch.namespace), []).append(watch)
    failed_clusters: Dict[str, Exception] = {}
    for (cluster_id, namespace), members in groups.items():
        real = should_use_real_k8s(cluster_id)
        healths: Dict[str, Any] = {}
        if real:
            if cluster_id in failed_clusters:
                healths = {w.deployment_name: failed_clusters[cluster_id] for w in members}
            else:
                healths = _read_group_health(cluster_id, namespace, [w.deployment_name for w in members])
                errors = [h for h in healths.values() if isinstance(h, _ClusterUnreachable)]
                if errors:
                    failed_clusters[cluster_id] = errors[0]
        for watch in members:
            try:
                _advance_watch(watch, health=healths.get(watch.deployment_name) if real else None,
                               settings=settings)
                db.session.commit()
            except Exception:  # noqa: BLE001 — defensive; next tick retries
                logger.exception("Bundle rollout watch #%s failed to advance", watch.id)
                db.session.rollback()
    return len(watches)


class _ClusterUnreachable(K8sCommandError):
    """The whole group's read failed: the cluster is skipped for this pass."""


def _health_of(doc: Dict[str, Any]) -> Dict[str, Any]:
    """deployment_service.rollout_health's reading of one Deployment document."""
    status = doc.get("status") or {}
    generation = int((doc.get("metadata") or {}).get("generation") or 0)
    observed = int(status.get("observedGeneration") or 0)
    return {
        "observedCurrent": observed >= generation,
        "desired": int((doc.get("spec") or {}).get("replicas") or 0),
        "ready": int(status.get("readyReplicas") or 0),
        "updated": int(status.get("updatedReplicas") or 0),
        "total": int(status.get("replicas") or 0),
        "available": int(status.get("availableReplicas") or 0),
    }


def _read_group_health(cluster_id: str, namespace: str, names: List[str]) -> Dict[str, Any]:
    """name -> health dict, or the exception reading it raised.

    One watched deployment in the namespace: the usual single read. Several:
    one ``kubectl get deployments -n <ns> -o json`` for all of them.
    """
    import json

    from . import deployment_service

    if len(set(names)) == 1:
        try:
            return {names[0]: deployment_service.rollout_health(cluster_id, namespace, names[0])}
        except (K8sCommandError, ValueError) as exc:
            return {names[0]: exc}
    from ..k8s_provider import _run_for_access, require_valid_k8s_names

    try:
        require_valid_k8s_names(namespace=namespace, names=())
    except K8sCommandError as exc:  # a bad name is this group's problem, not the cluster's
        return {name: exc for name in names}
    try:
        access = resolve_cluster_access(cluster_id)
        if not access:
            raise K8sCommandError(f"Cluster '{cluster_id}' was not found.")
        raw = _run_for_access(access, ["get", "deployments", "-n", namespace, "-o", "json"])
        docs = {
            (item.get("metadata") or {}).get("name"): item
            for item in (json.loads(raw).get("items") or [])
            if isinstance(item, dict)
        }
    except (K8sCommandError, ValueError) as exc:
        failure = _ClusterUnreachable(str(exc))
        return {name: failure for name in names}
    out: Dict[str, Any] = {}
    for name in names:
        doc = docs.get(name)
        out[name] = (
            _health_of(doc) if doc is not None
            else K8sCommandError(f'deployments.apps "{name}" not found')
        )
    return out


def _advance_watch(watch, *, health: Any = None, settings: Optional[Tuple[int, bool]] = None) -> None:
    """One step of one watch. ``health`` is its pre-read rollout health (or the
    exception reading it raised); None reads it here."""
    from .deployment_service import rollout_health

    if not should_use_real_k8s(watch.cluster_id):
        _finish_watch(watch, "healthy", "[mock] 1/1 ready")
        return

    timeout_min, rollback_enabled = settings or _watch_settings()
    detail = ""
    try:
        if health is None:
            health = rollout_health(watch.cluster_id, watch.namespace, watch.deployment_name)
        if isinstance(health, Exception):
            raise health
        if not health["observedCurrent"]:
            # Stale status — the controller hasn't reconciled the applied spec
            # yet, so the counters still describe the previous template.
            detail = "waiting for the controller to observe the new spec"
        else:
            desired, updated = health["desired"], health["updated"]
            total, available = health["total"], health["available"]
            if desired == 0:
                _finish_watch(watch, "healthy", "deployment is scaled to 0 — no pods expected")
                return
            # ready/updated alone are not enough: during a rolling update the
            # OLD pod keeps readyReplicas satisfied while the crashlooping NEW
            # pod counts as updated. Healthy = the full `kubectl rollout
            # status` condition: all pods on the new template, no old pods
            # left, and the new pods available.
            if updated >= desired and total <= updated and available >= updated:
                _finish_watch(watch, "healthy", f"{available}/{desired} ready")
                return
            if updated < desired:
                detail = f"{updated}/{desired} pods updated to the new spec"
            elif total > updated:
                detail = f"waiting for {total - updated} old pod(s) to terminate"
            else:
                detail = f"{available}/{updated} new pods available"
    except (K8sCommandError, ValueError) as exc:
        # Read errors during a rollout are "still waiting" — pods churn,
        # apiservers hiccup; only the timeout fails the watch.
        detail = f"could not read pod status — retrying: {exc}"

    started = _aware(watch.started_at) or _now()
    if _now() - started > timedelta(minutes=timeout_min):
        _fail_watch(watch, detail, timeout_min, rollback_enabled)
        return
    watch.detail = detail


def _finish_watch(watch, status: str, detail: str) -> None:
    watch.status = status
    watch.detail = detail
    watch.finished_at = _now()
    if status == "healthy" and watch.item_id:
        # Healthy on the new images: they passed this environment.
        item = db.session.get(ChangeBundleItem, watch.item_id)
        if item is not None:
            from . import promotion_service

            promotion_service.record_rollout(
                watch.cluster_id,
                watch.namespace,
                promotion_service.images_in_yaml(item.yaml_preview or ""),
                kind="Deployment",
                name=watch.deployment_name,
                source="bundle",
                commit=False,
            )
    log_audit(
        ACTION_ROLLOUT_HEALTHY if status == "healthy" else ACTION_ROLLOUT_FAILED,
        actor=None,
        target_type="change_bundle",
        target_id=str(watch.bundle_id),
        details={
            "bundleId": watch.bundle_id,
            "deployment": watch.deployment_name,
            "namespace": watch.namespace,
            "clusterId": watch.cluster_id,
            "detail": detail,
        },
    )


def _fail_watch(watch, last_seen: str, timeout_min: int, rollback_enabled: bool) -> None:
    """Rollout timed out: optionally roll back, mark the watch failed, and email
    the requester + admins (mirrors the deploy-automation failure handling)."""
    message = (
        f"Change bundle #{watch.bundle_id} applied {watch.deployment_name} in "
        f"{watch.namespace}, but the pods did not become ready within {timeout_min} min"
        + (f" (last seen: {last_seen})." if last_seen else ".")
    )

    if rollback_enabled:
        try:
            _run_kubectl_for_cluster(
                watch.cluster_id,
                ["rollout", "undo", f"deployment/{watch.deployment_name}", "-n", watch.namespace],
            )
            rollback_note = "Rolled back to the previous version."
            watch.rolled_back = True
        except K8sCommandError as exc:
            rollback_note = f"Automatic rollback FAILED: {exc}"
        log_audit(
            ACTION_ROLLBACK,
            actor=None,
            target_type="change_bundle",
            target_id=str(watch.bundle_id),
            details={
                "bundleId": watch.bundle_id,
                "deployment": watch.deployment_name,
                "namespace": watch.namespace,
                "clusterId": watch.cluster_id,
                "result": rollback_note,
            },
        )
    else:
        rollback_note = "Auto-rollback is disabled — the deployment was left as-is."

    _finish_watch(watch, "failed", f"{message} {rollback_note}")
    _notify_rollout_failure(watch, message, rollback_note)


def _notify_rollout_failure(watch, message: str, rollback_note: str) -> None:
    """Best-effort email to the bundle's requester and every admin."""
    try:
        from ..email_delivery import send_email, smtp_is_configured

        # Shared admin-audience convention with the deploy-automation failures.
        from .deploy_automation_service import _admin_emails

        if not smtp_is_configured():
            return
        bundle = ChangeBundle.query.get(watch.bundle_id)
        recipients = set(_admin_emails())
        requester_email = (getattr(getattr(bundle, "requester", None), "email", "") or "").strip()
        if requester_email:
            recipients.add(requester_email)
        if not recipients:
            return
        subject = (
            f"KubeSight: rollout failed — {watch.deployment_name} "
            f"(change bundle #{watch.bundle_id})"
        )
        body = "\n".join(
            [
                "A change-bundle deployment failed its post-apply health check.",
                "",
                f"Bundle:       #{watch.bundle_id}",
                f"Deployment:   {watch.deployment_name}",
                f"Namespace:    {watch.namespace}",
                f"Cluster:      {watch.cluster_id}",
                "",
                message,
                rollback_note,
                "",
                "Details: KubeSight → Change Bundles.",
            ]
        )
        from .change_bundle_service import _send_in_background

        # Off the scheduler thread: each SMTP send can wait 30 s.
        _send_in_background(
            [(address, subject, body, None) for address in sorted(recipients)],
            f"Change bundle #{watch.bundle_id} rollout-failure email",
            sender=send_email,
        )
    except Exception:  # noqa: BLE001 — notification must never break the tick
        logger.exception("Rollout-failure notification failed for bundle #%s", watch.bundle_id)


# ---------------------------------------------------------------------------
# The scheduler tick: expire, claim, and hand claimed bundles to workers
# ---------------------------------------------------------------------------

_pool_lock = threading.Lock()
_pool: Optional[ThreadPoolExecutor] = None
_running: set = set()  # bundle ids a worker of this process is applying


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "").strip() or default)
    except ValueError:
        return default


def _workers() -> int:
    return max(1, _int_env("CHANGE_BUNDLE_WORKERS", 2))


def _stale_minutes() -> int:
    """A ``deploying`` bundle nobody has advanced for this long was orphaned
    (KubeSight restarted mid-run). Longer than any single item can take."""
    return max(10, _int_env("CHANGE_BUNDLE_STALE_MINUTES", 60))


def _testing() -> bool:
    try:
        from flask import current_app

        return bool(current_app.config.get("TESTING"))
    except Exception:  # noqa: BLE001
        return False


def _executor() -> ThreadPoolExecutor:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=_workers(), thread_name_prefix="change-bundle")
        return _pool


def _run_in_app(app, bundle_id: int) -> None:
    with app.app_context():
        try:
            _run_claimed(bundle_id)
        except Exception:  # noqa: BLE001 — a worker must never die silently
            logger.exception("Change bundle #%s execution failed", bundle_id)
            db.session.rollback()
            _abandon(bundle_id, "KubeSight hit an internal error while applying this bundle.")
        finally:
            with _pool_lock:
                _running.discard(bundle_id)
            db.session.remove()


def _abandon(bundle_id: int, reason: str) -> None:
    """Close a ``deploying`` bundle that will not finish: unfinished items are
    marked failed/skipped — never re-applied, since some may have run."""
    try:
        bundle = db.session.get(ChangeBundle, bundle_id)
        if bundle is None or bundle.status != "deploying":
            return
        succeeded = 0
        for item in bundle.items:
            if item.status == "succeeded":
                succeeded += 1
            elif item.status == "applying":
                item.status = "failed"
                item.execution_result = {"ok": False, "error": reason, "phase": "interrupted"}
            elif item.status == "pending":
                item.status = "skipped"
                item.execution_result = {"skipped": True, "reason": reason}
        final = "partially_failed" if succeeded else "failed"
        bundle.status = final
        bundle.execution_finished_at = _now()
        db.session.commit()
        log_audit(
            ACTION_PARTIAL if succeeded else ACTION_FAILED,
            actor=None,
            target_type="change_bundle",
            target_id=str(bundle.id),
            details={"bundleId": bundle.id, "succeeded": succeeded, "interrupted": reason},
        )
        from .change_bundle_service import notify_requester_outcome

        notify_requester_outcome(bundle, final, detail=reason)
    except Exception:  # noqa: BLE001
        logger.exception("Could not close interrupted change bundle #%s", bundle_id)
        db.session.rollback()


def _recover_stale(now: datetime) -> int:
    """Fail ``deploying`` bundles no worker of this process runs and nobody has
    advanced for :func:`_stale_minutes` — left behind by a restart. They are
    not re-run: some of their items may already be applied."""
    cutoff = now - timedelta(minutes=_stale_minutes())
    with _pool_lock:
        mine = set(_running)
    stale = [
        bundle_id for (bundle_id,) in db.session.query(ChangeBundle.id).filter(
            ChangeBundle.status == "deploying", ChangeBundle.updated_at < cutoff
        )
        if bundle_id not in mine
    ]
    for bundle_id in stale:
        _abandon(
            bundle_id,
            "KubeSight stopped while this bundle was being applied; the unfinished changes were "
            "not re-run. Check the cluster, then stage what is missing again.",
        )
    return len(stale)


def _expire_due(now: datetime) -> int:
    """Expire every bundle whose window ended before it could run or be approved."""
    ids = [
        bundle_id for (bundle_id,) in db.session.query(ChangeBundle.id).filter(
            ChangeBundle.status.in_(("approved", "scheduled", "pending_approval")),
            ChangeBundle.requested_end_time.isnot(None),
            ChangeBundle.requested_end_time < now,
        ).order_by(ChangeBundle.id)
    ]
    for bundle_id in ids:
        bundle = db.session.get(ChangeBundle, bundle_id)
        if bundle is not None:
            _mark_expired(bundle)
    return len(ids)


def process_due_bundles(now: Optional[datetime] = None) -> Dict[str, int]:
    """Expire bundles whose window passed; start approved bundles whose window opened.

    Only ids are read to find what is due. A due bundle is claimed (moved to
    ``deploying``) here and applied by a worker thread, so a large bundle
    never stalls the scheduler's other jobs; at most ``CHANGE_BUNDLE_WORKERS``
    run at once and the rest wait for a later tick. Under TESTING the claimed
    bundle is applied inline. Safe to call repeatedly.
    """
    now = now or _now()
    expired = _expire_due(now)

    due = [
        bundle_id for (bundle_id,) in db.session.query(ChangeBundle.id).filter(
            ChangeBundle.status.in_(("approved", "scheduled")),
            or_(ChangeBundle.requested_start_time.is_(None), ChangeBundle.requested_start_time <= now),
        ).order_by(ChangeBundle.id)
    ]
    executed = 0
    inline = _testing()
    for bundle_id in due:
        if not inline:
            with _pool_lock:
                if len(_running) >= _workers():
                    break  # every worker is busy: the next tick picks it up
        try:
            claimed = _claim(bundle_id)
        except Exception:  # noqa: BLE001
            logger.exception("Change bundle #%s could not be claimed", bundle_id)
            db.session.rollback()
            continue
        if claimed == "expired":
            expired += 1
            continue
        if claimed != "claimed":
            continue
        executed += 1
        if inline:
            try:
                _run_claimed(bundle_id)
            except Exception:  # noqa: BLE001
                logger.exception("Change bundle #%s execution failed", bundle_id)
                db.session.rollback()
                _abandon(bundle_id, "KubeSight hit an internal error while applying this bundle.")
            continue
        from flask import current_app

        with _pool_lock:
            _running.add(bundle_id)
        _executor().submit(_run_in_app, current_app._get_current_object(), bundle_id)

    _recover_stale(now)
    return {"executed": executed, "expired": expired}
