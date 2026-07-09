"""Scheduled execution of approved Change Bundles.

When an approved bundle's deployment window opens, the background scheduler calls
:func:`process_due_bundles`, which re-validates and applies each item in order.
Stop-on-failure is the safe default. Everything is re-validated against live
cluster state at execution time because the cluster may have changed since the
bundle was approved.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..audit import log_audit
from ..db import db
from ..k8s_provider import K8sCommandError, resolve_cluster_access, should_use_real_k8s
from ..models import ChangeBundle, ChangeBundleItem
from .deployment_service import (
    _cleanup_temp,
    _run_kubectl_for_cluster,
    _write_temp_yaml,
    check_registry_images,
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


def _revalidate(item: ChangeBundleItem, mode: str) -> Tuple[Optional[str], Optional[str]]:
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
        _checks, blocking, image_err = check_registry_images(item.yaml_preview or "")
        if blocking:
            return image_err, "image"

    if not should_use_real_k8s(item.cluster_id):
        return None, None

    # For changes to an existing object, confirm the target still exists.
    if mode in ("scale", "delete"):
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


def _apply_item(item: ChangeBundleItem, mode: str) -> str:
    """Execute one item against the cluster. Returns kubectl output (or mock note)."""
    if not should_use_real_k8s(item.cluster_id):
        return f"[mock] {mode} {_resource_arg(item)} in {item.namespace}"

    if mode == "apply":
        path = _write_temp_yaml(sanitize_for_apply(item.yaml_preview or ""))
        try:
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

    if mode == "delete":
        return _run_kubectl_for_cluster(
            item.cluster_id,
            ["delete", (item.resource_kind or "deployment").lower(), item.resource_name, "-n", item.namespace],
        ).strip()

    raise K8sCommandError(f"Unknown execution mode: {mode}")


def execute_bundle(bundle: ChangeBundle) -> str:
    """Execute every item of an approved bundle in order. Returns the final status."""
    if bundle.status not in ("approved", "scheduled"):
        raise RuntimeError(f"Bundle #{bundle.id} is not executable (status={bundle.status}).")

    now = _now()
    start = _aware(bundle.requested_start_time)
    end = _aware(bundle.requested_end_time)
    if start and now < start:
        raise RuntimeError("Window has not started yet.")
    if end and now > end:
        return _mark_expired(bundle)

    bundle.status = "deploying"
    bundle.execution_started_at = now
    db.session.commit()
    log_audit(
        ACTION_EXEC_STARTED,
        actor=None,
        target_type="change_bundle",
        target_id=str(bundle.id),
        details={"bundleId": bundle.id, "itemCount": len(bundle.items)},
    )

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
        db.session.commit()

        err, reason = _revalidate(item, mode)
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

    Deploy-automation bundles are skipped — their run already watches the same
    deployment (with its own rollback + notification path), and two watchers
    would race to `rollout undo` twice. Best-effort: a watch problem must never
    disturb the just-finished execution.
    """
    try:
        from ..models import BundleRolloutWatch, DeployAutomationRun

        if DeployAutomationRun.query.filter_by(bundle_id=bundle.id).first():
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

    Each watch is isolated in try/except so one broken watch can't stall the
    rest. Returns how many watches were looked at.
    """
    from ..models import BundleRolloutWatch

    watches = BundleRolloutWatch.query.filter_by(status="watching").all()
    for watch in watches:
        try:
            _advance_watch(watch)
            db.session.commit()
        except Exception:  # noqa: BLE001 — defensive; next tick retries
            logger.exception("Bundle rollout watch #%s failed to advance", watch.id)
            db.session.rollback()
    return len(watches)


def _advance_watch(watch) -> None:
    from .deployment_service import rollout_health

    if not should_use_real_k8s(watch.cluster_id):
        _finish_watch(watch, "healthy", "[mock] 1/1 ready")
        return

    timeout_min, rollback_enabled = _watch_settings()
    detail = ""
    try:
        health = rollout_health(watch.cluster_id, watch.namespace, watch.deployment_name)
        if not health["observedCurrent"]:
            # Stale status — the controller hasn't reconciled the applied spec
            # yet, so the counters still describe the previous template.
            detail = "waiting for the controller to observe the new spec"
        else:
            desired, ready, updated = health["desired"], health["ready"], health["updated"]
            if desired == 0:
                _finish_watch(watch, "healthy", "deployment is scaled to 0 — no pods expected")
                return
            if ready >= desired and updated >= desired:
                _finish_watch(watch, "healthy", f"{ready}/{desired} ready")
                return
            detail = f"{ready}/{desired} ready ({updated}/{desired} on the new spec)"
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
        for address in sorted(recipients):
            try:
                send_email(address, subject, body)
            except Exception:  # noqa: BLE001 — keep sending to the rest
                continue
    except Exception:  # noqa: BLE001 — notification must never break the tick
        logger.exception("Rollout-failure notification failed for bundle #%s", watch.bundle_id)


def process_due_bundles(now: Optional[datetime] = None) -> Dict[str, int]:
    """Execute approved bundles whose window has opened; expire those whose window passed.

    Safe to call repeatedly (idempotent on already-terminal bundles).
    """
    now = now or _now()
    executed = 0
    expired = 0

    candidates = ChangeBundle.query.filter(
        ChangeBundle.status.in_(["approved", "scheduled"])
    ).all()
    for bundle in candidates:
        start = _aware(bundle.requested_start_time)
        end = _aware(bundle.requested_end_time)
        if end is not None and now > end:
            _mark_expired(bundle)
            expired += 1
            continue
        if start is None or now >= start:
            try:
                execute_bundle(bundle)
                executed += 1
            except Exception:
                logger.exception("Change bundle #%s execution failed", bundle.id)

    # Bundles still awaiting approval once their window has passed can never run.
    stale_pending = ChangeBundle.query.filter(
        ChangeBundle.status == "pending_approval",
        ChangeBundle.requested_end_time.isnot(None),
    ).all()
    for bundle in stale_pending:
        end = _aware(bundle.requested_end_time)
        if end is not None and now > end:
            _mark_expired(bundle)
            expired += 1

    return {"executed": executed, "expired": expired}
