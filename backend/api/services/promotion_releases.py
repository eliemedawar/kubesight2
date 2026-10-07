"""Promoting applications — one at a time or as a release.

A release is what a person reviews and submits from the Promote view: several
applications moved into one environment together ("UAT drop · 6 Oct"). Each
workload goes the way any deploy goes — namespace access, the ladder, the
cluster's approval rule — but the release keeps the batch together:

* workloads on clusters that let this person deploy now are applied directly;
* workloads on clusters that need approval go into ONE change bundle, so the
  approvers see the release as one decision;
* applications that skip an environment (picked from further down the ladder)
  go, with the release's written reason, into one exception bundle that always
  needs somebody else's approval.

A single-application promotion (the MCP tool, the old endpoint) is a release of
one.
"""

from __future__ import annotations

import copy
import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import yaml

from ..audit import log_audit
from ..db import db
from ..models_promotion import PromotionEnvironment, PromotionRelease
from . import promotion_service as core
from .promotion_service import PromotionError

logger = logging.getLogger(__name__)

MAX_ITEMS = 200
MAX_TARGETS_PER_ITEM = 50
MIN_REASON = 10

_BUNDLE_OUTCOME = {
    "pending_approval": "pending_approval",
    "approved": "pending_approval",
    "scheduled": "pending_approval",
    "deploying": "pending_approval",
    "completed": "applied",
    "rejected": "rejected",
    "expired": "expired",
    "failed": "failed",
    "partially_failed": "failed",
    "draft": "pending_approval",
}


# ---------------------------------------------------------------------------
# Workloads
# ---------------------------------------------------------------------------

def read_workload(cluster_id: str, namespace: str, kind: str, name: str) -> Dict[str, Any]:
    from ..k8s_provider import K8sCommandError, should_use_real_k8s

    if not should_use_real_k8s(cluster_id):
        from . import promotion_mock

        item = promotion_mock.get(cluster_id, namespace, name)
        if item is None:
            raise PromotionError(f"{kind} {namespace}/{name} was not found.", 404)
        return item
    from .deployment_service import _run_kubectl_for_cluster

    try:
        raw = _run_kubectl_for_cluster(cluster_id, ["get", kind.lower(), name, "-n", namespace, "-o", "json"])
        return json.loads(raw)
    except (K8sCommandError, ValueError) as exc:
        raise PromotionError(f"Could not read {kind} {namespace}/{name}: {exc}", 502)


def swap_repository_image(
    item: Dict[str, Any], repository: str, image: str, container: str = ""
) -> Tuple[str, List[str], List[str]]:
    """``(manifest, changed containers, previous tags)`` — the workload with
    every container of ``repository`` (or the named container) set to ``image``."""
    doc = copy.deepcopy(item)
    doc.pop("status", None)
    pod_spec = ((doc.get("spec") or {}).get("template") or {}).get("spec") or {}
    changed: List[str] = []
    previous: List[str] = []
    for field in ("containers", "initContainers"):
        for c in pod_spec.get(field) or []:
            parsed = core.parse_image(c.get("image") or "")
            if not parsed:
                continue
            if (container and c.get("name") == container) or (not container and parsed["repository"] == repository):
                if c.get("image") != image:
                    previous.append(parsed["tag"])
                    c["image"] = image
                    changed.append(str(c.get("name")))
    return yaml.safe_dump(doc, sort_keys=False), changed, previous


def _apply_now(user, target: Dict[str, Any], manifest: str, image: str, note: str) -> Tuple[str, str, Optional[int]]:
    """Apply one workload now. ``(status, message, bundle id)``."""
    from ..k8s_provider import should_use_real_k8s

    cluster_id, namespace = target["clusterId"], target["namespace"]
    if should_use_real_k8s(cluster_id):
        from .deployment_service import apply_yaml

        data, error, status = apply_yaml(user, cluster_id, namespace, manifest, "", change_note=note)
        if error:
            return "refused", error, None
        if (data or {}).get("pendingApproval"):
            return "pending_approval", data.get("message") or "Sent for approval.", data.get("bundleId")
        core.invalidate_scan(cluster_id)
        return "applied", "Applied — the new pods are rolling out.", None

    # A mock cluster has no API server: the validation and the ladder still
    # hold, and the in-memory workload moves so the page shows the result.
    from . import promotion_mock
    from .deployment_service import validate_yaml

    _validation, err, _code = validate_yaml(manifest, namespace, user=user)
    if err:
        return "refused", err, None
    promotion_mock.set_image(cluster_id, namespace, target["name"], image)
    core.record_rollout(
        cluster_id, namespace, [image], kind=target["kind"], name=target["name"],
        source="promotion", actor=core._actor_name(user),
    )
    return "applied", "[mock] applied", None


# ---------------------------------------------------------------------------
# Releases
# ---------------------------------------------------------------------------

def _default_name(env: PromotionEnvironment) -> str:
    return f"{env.name} · {datetime.now(timezone.utc).strftime('%d %b %H:%M')}"


def create_release(
    user,
    *,
    environment_id: int,
    items: List[Dict[str, Any]],
    name: str = "",
    reference: str = "",
    note: str = "",
    exception_reason: str = "",
    code: Optional[str] = None,
    version: Optional[str] = None,
    departs_at: Optional[datetime] = None,
    slot: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Promote a set of applications into one environment. See the module doc.

    ``departs_at`` (a scheduled departure) sends every workload as one change
    bundle whose window opens then, instead of deploying now. ``slot`` is the
    scheduled departure this release closes — given when someone promotes it
    early, or by the scheduler at its cut-off."""
    from ..access_engine import can_access_namespace
    from .change_bundle_service import ChangeBundleError, queue_many_for_approval
    from .deployment_request_service import check_cluster_change_allowed

    env = db.session.get(PromotionEnvironment, environment_id)
    if env is None:
        raise PromotionError("That environment no longer exists.", 404)
    if not items:
        raise PromotionError("Choose at least one application to promote.")
    if len(items) > MAX_ITEMS:
        raise PromotionError(f"A release holds at most {MAX_ITEMS} applications.")
    prev = core.previous_environment(env)
    from . import promotion_timetable as tt

    if slot is not None:
        identity = tt.slot_identity(env, slot)
        if identity is None:
            raise PromotionError("There is no departure at that time on the schedule.", 404)
        row = tt._departure_row(env.id, identity["departsAt"])
        if row.state == "closed":
            raise PromotionError("That departure has already closed.", 409)
        code = code or identity["code"]
        version = version or identity["version"]
    if code is None:
        code, version = tt.manual_identity(env)
    later = departs_at is not None and departs_at > datetime.now(timezone.utc) + timedelta(minutes=1)
    name = (name or "").strip()[:160] or _default_name(env)
    reference = (reference or "").strip()[:120]
    note = (note or "").strip()[:2000]
    reason = (exception_reason or "").strip()
    actor = core._actor_name(user)
    change_note = f"Promotion '{name}' to {env.name}" + (f" ({reference})" if reference else "")

    release_items: List[Dict[str, Any]] = []
    direct: List[Tuple[Dict[str, Any], str, str]] = []
    queued: List[Tuple[Dict[str, Any], str]] = []
    excepted: List[Tuple[Dict[str, Any], str]] = []
    blocked_images: List[str] = []

    for raw in items:
        parsed = core.parse_image(str(raw.get("image") or ""))
        if parsed is None:
            raise PromotionError("Every application needs the image to promote.")
        entry = {
            "repository": parsed["repository"],
            "name": str(raw.get("name") or core.repo_short_name(parsed["repository"])),
            "image": parsed["image"],
            "tag": parsed["tag"],
            "fromTags": [],
            "exception": False,
            "targets": [],
        }
        release_items.append(entry)
        for target in (raw.get("targets") or [])[:MAX_TARGETS_PER_ITEM]:
            t = {
                "clusterId": str(target.get("clusterId") or ""),
                "clusterName": core.cluster_name(str(target.get("clusterId") or "")),
                "namespace": str(target.get("namespace") or ""),
                "kind": str(target.get("kind") or "Deployment"),
                "name": str(target.get("name") or ""),
                "container": str(target.get("container") or ""),
                "status": "pending",
                "message": "",
                "bundleId": None,
            }
            entry["targets"].append(t)
            label = f"{t['namespace']}/{t['name']}"
            resolved = core.environment_for(t["clusterId"], t["namespace"])
            if resolved is None or resolved.id != env.id:
                t.update(status="refused", message=f"{label} is not in {env.name}.")
                continue
            if t["kind"] not in ("Deployment", "StatefulSet", "DaemonSet"):
                t.update(status="refused", message=f"{t['kind']} cannot be promoted.")
                continue
            if user is not None and not can_access_namespace(user, t["clusterId"], t["namespace"]):
                t.update(status="refused", message=f"You do not have access to {t['namespace']}.")
                continue
            try:
                live = read_workload(t["clusterId"], t["namespace"], t["kind"], t["name"])
            except PromotionError as exc:
                t.update(status="refused", message=str(exc))
                continue
            manifest, changed, previous = swap_repository_image(
                live, parsed["repository"], parsed["original"], t["container"]
            )
            entry["fromTags"] = sorted(set(entry["fromTags"]) | set(previous))
            if not changed:
                t.update(status="unchanged", message=f"Already runs {parsed['tag']}.")
                continue
            verdict = core.evaluate(t["clusterId"], t["namespace"], core.images_in_yaml(manifest))
            if verdict["applies"] and not verdict["allowed"]:
                if reason:
                    entry["exception"] = True
                    t["exception"] = True
                    excepted.append((t, manifest))
                else:
                    t.update(status="refused", message=verdict["message"], blocked=True)
                    blocked_images.append(parsed["image"])
                    core.record_event(
                        "blocked", verdict, path="promote", actor=actor, workload=t["name"], commit=False
                    )
                continue
            if verdict.get("warning"):
                t["warning"] = verdict["message"]
            if later:
                # A scheduled release: one bundle, opened at departure.
                queued.append((t, manifest))
                continue
            denied = check_cluster_change_allowed(
                user, t["clusterId"], action="apply", target_type="namespace",
                target_id=f"{t['clusterId']}/{t['namespace']}",
            )
            if denied is None:
                direct.append((t, manifest, parsed["original"]))
            elif denied[1] == 403 and user is not None:
                queued.append((t, manifest))
            else:
                t.update(status="refused", message=denied[0])

    if reason and len(reason) < MIN_REASON and excepted:
        raise PromotionError("Say why these have to skip the ladder (at least a sentence) — approvers read it.")

    for t, manifest, image in direct:
        status, message, bundle_id = _apply_now(user, t, manifest, image, change_note)
        t.update(status=status, message=message, bundleId=bundle_id)

    bundle_ids: List[int] = []
    if queued:
        try:
            bundle = queue_many_for_approval(
                user,
                [
                    {"actionType": "apply_yaml", "clusterId": t["clusterId"], "namespace": t["namespace"], "yaml": m}
                    for t, m in queued
                ],
                source=change_note,
                note=f"{change_note}: {len(queued)} workload(s)." + (f" {note}" if note else ""),
                start_at=departs_at if later else None,
            )
            bundle_ids.append(bundle["id"])
            for t, _m in queued:
                t.update(
                    status="pending_approval",
                    bundleId=bundle["id"],
                    message=(
                        f"Deploys at departure in change bundle #{bundle['id']}."
                        if later
                        else f"Waiting for approval in change bundle #{bundle['id']}."
                    ),
                )
        except ChangeBundleError as exc:
            for t, _m in queued:
                t.update(status="refused", message=f"Could not send it for approval: {exc}")

    if excepted:
        exception = {
            "reason": reason,
            "environment": env.name,
            "previousEnvironment": prev.name if prev else None,
            "images": sorted({i["image"] for i in release_items if i["exception"]}),
            "requestedBy": actor,
            "release": name,
        }
        try:
            bundle = queue_many_for_approval(
                user,
                [
                    {"actionType": "apply_yaml", "clusterId": t["clusterId"], "namespace": t["namespace"], "yaml": m}
                    for t, m in excepted
                ],
                source=f"promotion exception '{name}' to {env.name}",
                promotion_exception=exception,
                start_at=departs_at if later else None,
            )
            bundle_ids.append(bundle["id"])
            for t, _m in excepted:
                t.update(
                    status="pending_approval",
                    bundleId=bundle["id"],
                    message=f"Exception — waiting for approval in change bundle #{bundle['id']}.",
                )
        except ChangeBundleError as exc:
            for t, _m in excepted:
                t.update(status="refused", message=f"Could not send the exception for approval: {exc}")

    row = PromotionRelease(
        name=name,
        reference=reference or None,
        note=note or None,
        kind="exception" if excepted else "promotion",
        exception_reason=reason or None,
        environment_id=env.id,
        environment_name=env.name,
        from_environment_name=prev.name if prev else None,
        actor=actor,
        items=release_items,
        bundle_ids=bundle_ids,
        code=code,
        version=version,
        departs_at=departs_at if later else None,
    )
    db.session.add(row)
    db.session.flush()
    if slot is not None:
        tt.link_release(env, tt.slot_identity(env, slot)["departsAt"], row.id)

    moved = [
        {"image": i["image"], "status": "promoted"}
        for i in release_items
        if any(t["status"] in ("applied", "pending_approval") and not t.get("exception") for t in i["targets"])
    ]
    if moved:
        core.record_event(
            "promoted", None, path="promote", actor=actor, images=moved,
            message=note or None, bundle_id=bundle_ids[0] if queued and bundle_ids else None,
            release_id=row.id, environment=env, from_environment=prev, workload=name, commit=False,
        )
    if excepted:
        core.record_event(
            "exception_requested", None, path="promote", actor=actor,
            images=[{"image": i, "status": "exception"} for i in sorted({i["image"] for i in release_items if i["exception"]})],
            message=reason, bundle_id=bundle_ids[-1] if bundle_ids else None, release_id=row.id,
            environment=env, from_environment=prev, workload=name, commit=False,
        )
    db.session.commit()
    log_audit(
        "promotion_release_created",
        actor=user,
        target_type="promotion_release",
        target_id=str(row.id),
        details={
            "name": name,
            "reference": reference,
            "environment": env.name,
            "applications": len(release_items),
            "bundles": bundle_ids,
            "exception": bool(excepted),
        },
    )
    return serialize_release(row)


def _bundle_states(ids: List[int]) -> Dict[int, str]:
    if not ids:
        return {}
    from ..models import ChangeBundle

    return {b.id: b.status for b in ChangeBundle.query.filter(ChangeBundle.id.in_(ids)).all()}


def serialize_release(row: PromotionRelease, bundle_states: Optional[Dict[int, str]] = None) -> Dict[str, Any]:
    """A release with each workload's fate read from its bundle, so a release
    sent for approval reads "applied" once the bundle has run."""
    items = copy.deepcopy(row.items or [])
    ids = [t.get("bundleId") for i in items for t in i.get("targets", []) if t.get("bundleId")]
    states = bundle_states if bundle_states is not None else _bundle_states(sorted(set(ids)))
    counts = {"applied": 0, "pending_approval": 0, "refused": 0, "unchanged": 0, "rejected": 0, "failed": 0, "expired": 0}
    for item in items:
        item_states = set()
        for t in item.get("targets", []):
            if t.get("status") == "pending_approval" and t.get("bundleId") in states:
                t["status"] = _BUNDLE_OUTCOME.get(states[t["bundleId"]], "pending_approval")
            status = t.get("status") or "refused"
            counts[status] = counts.get(status, 0) + 1
            item_states.add(status)
        item["status"] = (
            "applied" if item_states and item_states <= {"applied", "unchanged"}
            else "pending_approval" if "pending_approval" in item_states
            else "partial" if "applied" in item_states
            else "refused" if item_states
            else "empty"
        )
    done = counts["applied"] + counts["unchanged"]
    total = sum(counts.values())
    status = (
        "pending_approval" if counts["pending_approval"]
        else "applied" if total and done == total
        else "partial" if done
        else "refused"
    )
    return {
        "id": row.id,
        "name": row.name,
        "reference": row.reference,
        "note": row.note,
        "kind": row.kind,
        "exceptionReason": row.exception_reason,
        "environmentId": row.environment_id,
        "environmentName": row.environment_name,
        "fromEnvironmentName": row.from_environment_name,
        "actor": row.actor,
        "status": status,
        "counts": counts,
        "applications": len(items),
        "items": items,
        "bundleIds": list(row.bundle_ids or []),
        "code": row.code,
        "version": row.version,
        "departsAt": core._iso(row.departs_at),
        "createdAt": core._iso(row.created_at),
    }


def list_releases(limit: int = 100, environment_id: Optional[int] = None) -> List[Dict[str, Any]]:
    query = PromotionRelease.query
    if environment_id:
        query = query.filter_by(environment_id=environment_id)
    rows = query.order_by(PromotionRelease.created_at.desc(), PromotionRelease.id.desc()).limit(max(1, min(limit, 300))).all()
    ids = sorted({t.get("bundleId") for r in rows for i in (r.items or []) for t in i.get("targets", []) if t.get("bundleId")})
    states = _bundle_states(ids)
    return [serialize_release(r, states) for r in rows]


def get_release(release_id: int) -> Dict[str, Any]:
    row = db.session.get(PromotionRelease, release_id)
    if row is None:
        raise PromotionError("Release not found.", 404)
    return serialize_release(row)


def promote(user, *, image: str, environment_id: int, targets: List[Dict[str, Any]], note: str = "") -> Dict[str, Any]:
    """One application as a release of one — what the MCP tool and the first
    version's endpoint call. Returns the per-workload results flat."""
    if not targets:
        raise PromotionError("Choose at least one workload to promote to.")
    release = create_release(
        user, environment_id=environment_id, items=[{"image": image, "targets": targets}], note=note
    )
    item = release["items"][0]
    return {
        "image": item["image"],
        "environment": {"id": release["environmentId"], "name": release["environmentName"]},
        "releaseId": release["id"],
        "results": item["targets"],
    }


# ---------------------------------------------------------------------------
# Exceptions from a deploy form — a refused manifest sent to approvers
# ---------------------------------------------------------------------------

def request_exception(user, *, changes: List[Dict[str, Any]], reason: str) -> Dict[str, Any]:
    """Queue refused deploys (``{clusterId, namespace, yaml}``) as ONE change
    bundle that needs somebody else's approval. Board promotions use
    :func:`create_release` with ``exception_reason`` instead."""
    from .change_bundle_service import ChangeBundleError, queue_many_for_approval

    reason = (reason or "").strip()
    if len(reason) < MIN_REASON:
        raise PromotionError("Say why this has to skip the ladder (at least a sentence) — approvers read it.")
    if user is None:
        raise PromotionError("An exception needs a signed-in requester.", 403)
    if not changes:
        raise PromotionError("Nothing to send for approval.")

    prepared, verdicts = [], []
    for change in changes[:25]:
        cluster_id = str(change.get("clusterId") or "")
        namespace = str(change.get("namespace") or "")
        manifest = change.get("yaml")
        if not manifest:
            parsed = core.parse_image(change.get("image") or "")
            if parsed is None:
                raise PromotionError("Each change needs a manifest or an image.")
            live = read_workload(cluster_id, namespace, str(change.get("kind") or "Deployment"), str(change.get("name") or ""))
            manifest, changed, _prev = swap_repository_image(
                live, parsed["repository"], parsed["original"], str(change.get("container") or "")
            )
            if not changed:
                continue
        verdict = core.evaluate(cluster_id, namespace, core.images_in_yaml(manifest))
        if verdict["allowed"] and not verdict["warning"]:
            continue
        prepared.append({"clusterId": cluster_id, "namespace": namespace, "yaml": manifest})
        verdicts.append(verdict)
    if not prepared:
        raise PromotionError("None of these changes is refused by the ladder — deploy them normally.", 409)

    first = verdicts[0]
    exception = {
        "reason": reason,
        "environment": (first.get("environment") or {}).get("name"),
        "previousEnvironment": (first.get("previous") or {}).get("name"),
        "images": sorted({j["image"] for v in verdicts for j in v["images"] if j.get("status") in core.BLOCKING_STATUSES}),
        "requestedBy": core._actor_name(user),
    }
    try:
        bundle = queue_many_for_approval(
            user,
            [{"actionType": "apply_yaml", **change} for change in prepared],
            source=f"promotion exception to {exception['environment']}",
            promotion_exception=exception,
        )
    except ChangeBundleError as exc:
        raise PromotionError(str(exc), exc.status_code)
    core.record_event(
        "exception_requested",
        first,
        path="deploy",
        actor=core._actor_name(user),
        message=reason,
        bundle_id=bundle.get("id"),
        workload=", ".join(filter(None, (core.first_workload_name(c["yaml"]) for c in prepared)))[:250],
    )
    log_audit(
        "promotion_exception_requested",
        actor=user,
        target_type="change_bundle",
        target_id=str(bundle.get("id")),
        details={**exception},
    )
    return {
        "bundleId": bundle.get("id"),
        "status": bundle.get("status"),
        "requiredApprovals": bundle.get("requiredApprovals"),
        "message": (
            f"Sent to approvers as change bundle #{bundle.get('id')}. It is deployed automatically "
            "once approved — you cannot approve it yourself."
        ),
    }
