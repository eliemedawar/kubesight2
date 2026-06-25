"""Scheduled execution of approved Change Bundles.

When an approved bundle's deployment window opens, the background scheduler calls
:func:`process_due_bundles`, which re-validates and applies each item in order.
Stop-on-failure is the safe default. Everything is re-validated against live
cluster state at execution time because the cluster may have changed since the
bundle was approved.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

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


def _revalidate(item: ChangeBundleItem, mode: str) -> Optional[str]:
    """Re-check the item against live cluster state. Returns an error string or None."""
    if mode == "apply":
        # The bundle's approval is the authorization, so cluster-scoped/sensitive
        # kinds are not hard-blocked here (preview_mode); the actual apply runs
        # with the cluster's real credentials.
        _result, err, _ = validate_yaml(
            item.yaml_preview or "", item.namespace, user=None, preview_mode=True
        )
        if err:
            return err

    if not should_use_real_k8s(item.cluster_id):
        return None

    # For changes to an existing object, confirm the target still exists.
    if mode in ("scale", "delete"):
        try:
            _run_kubectl_for_cluster(
                item.cluster_id,
                ["get", _resource_arg(item), "-n", item.namespace],
            )
        except K8sCommandError as exc:
            return f"Target no longer exists or is unreachable: {exc}"
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

        err = _revalidate(item, mode)
        if err:
            failed += 1
            item.status = "failed"
            item.validation_status = "invalid"
            item.validation_message = err
            item.execution_result = {"ok": False, "error": err, "phase": "revalidate"}
            db.session.commit()
            _audit_item(ACTION_ITEM_FAILED, bundle, item, {"error": err})
            if bundle.stop_on_failure:
                stopped = True
            continue

        try:
            output = _apply_item(item, mode)
            succeeded += 1
            item.status = "succeeded"
            item.execution_result = {"ok": True, "output": output, "mode": mode}
            db.session.commit()
            _audit_item(ACTION_ITEM_APPLIED, bundle, item, {"mode": mode})
        except (K8sCommandError, Exception) as exc:  # noqa: BLE001 — record any failure
            failed += 1
            item.status = "failed"
            item.execution_result = {"ok": False, "error": str(exc), "mode": mode}
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
    return final


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
    return "expired"


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
