"""Approvals in one place: everything waiting on a person, and every rule that says so.

KubeSight asks people to approve six different things, each with its own
rules living next to the thing it protects:

  deployment_request  a deploy on an approval-gated cluster   (cluster count + approver pool)
  change_bundle       a set of cluster changes, or a queued direct change (same)
  hermes              a ticket action Hermes was unsure about, or new VMs it asked for
  ci_stage            an Approval stage in a CI pipeline        (the stage's own approvers)
  cluster_destroy     destroying VMs KubeSight created          (a second person; admins need none)

This module does not decide anything. The **inbox** reads each kind's pending
rows and asks that kind's own eligibility rule whether the viewer may decide
now — the votes themselves still go to each kind's existing route, so no
self-approval, quorum or approver-list rule is copied here. The **rules** view
gathers the settings that make those rules, for the Approvals page's Rules tab;
it is written through the existing settings routes, plus the approver-group
endpoints below (approver groups are alert-routing receiver groups, managed
here as people, roles and emails).
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from ..access_engine import is_admin, user_has_permission
from ..db import db

logger = logging.getLogger(__name__)

KINDS = ("deployment_request", "change_bundle", "hermes", "ci_stage", "cluster_destroy", "cluster_upgrade")
KIND_LABELS = {
    "deployment_request": "Deployment request",
    "change_bundle": "Change bundle",
    "hermes": "Hermes ticket",
    "ci_stage": "CI approval stage",
    "cluster_destroy": "Cluster Builder destroy",
    "cluster_upgrade": "Cluster upgrade",
}
_LIMIT = 200
# Sorts an item with no time last.
_EPOCH = datetime.min.replace(tzinfo=timezone.utc)


def _iso(dt: Optional[datetime]) -> Optional[str]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _can(user, key: str) -> bool:
    try:
        return bool(user is not None and user_has_permission(user, key))
    except Exception:  # noqa: BLE001 — a permission lookup must not break the inbox
        return False


def _admin(user) -> bool:
    try:
        return bool(user is not None and is_admin(user))
    except Exception:  # noqa: BLE001 — same as _can
        return False


def _name(user) -> Optional[str]:
    if user is None:
        return None
    return getattr(user, "full_name", None) or getattr(user, "username", None)


def _email(user) -> str:
    return (getattr(user, "email", "") or "").strip().lower()


def _item(kind: str, row_id: int, **fields) -> Dict[str, Any]:
    out = {
        "key": f"{kind}:{row_id}",
        "kind": kind,
        "kindLabel": KIND_LABELS[kind],
        "id": row_id,
        "title": "",
        "summary": "",
        "where": None,
        "requestedBy": None,
        "requestedAt": None,
        "expiresAt": None,
        "approvals": None,
        "required": None,
        "details": [],
        "href": None,
        "canDecide": False,
        "why": "",
        "actions": None,
    }
    out.update(fields)
    return out


def _actions(approve: str, reject: str, *, reject_field: Optional[str] = None,
             approve_field: Optional[str] = None, reject_label: str = "Reject") -> Dict[str, Any]:
    return {
        "approve": approve,
        "reject": reject,
        # The body key the reject route reads a reason from (None: no reason).
        "rejectField": reject_field,
        # The body key the approve route reads a comment from (None: none).
        "approveField": approve_field,
        "rejectLabel": reject_label,
    }


# ---------------------------------------------------------------------------
# The inbox — one collector per kind
#
# Each kind has a "why not" rule shared by its collector (the inbox, which
# serializes) and its counter (the sidebar badge, which only counts): the
# badge must always equal the inbox's "waiting on me".
# ---------------------------------------------------------------------------

def _why_deployment_request(manage: bool, own: bool, voted: bool) -> str:
    return (
        "" if manage and not own and not voted
        else "Your request — another approver decides it." if own
        else "You already voted." if voted
        else "Needs deployment_requests:manage."
    )


def _why_change_bundle(manage: bool, own: bool, voted: bool) -> str:
    return (
        "" if manage and not own and not voted
        else "Your bundle — another approver decides it." if own
        else "You already approved it." if voted
        else "Needs change_bundles:manage."
    )


def _why_hermes(task, user, manage: bool, builds: bool) -> str:
    from .ticket_agent import vm_requests

    why = "" if manage else "Needs ticketing:manage."
    if (task.decision or {}).get("vmRequest") and not why:
        if not builds:
            why = "Approving new VMs needs cluster_builds:execute."
        else:
            try:
                vm_requests.check_approver(task, user)
            except vm_requests.VmRequestError as exc:
                why = str(exc)
    return why


def _why_destroy(execute: bool, own: bool) -> str:
    return (
        "" if execute and not own
        else "You asked for this destroy — someone else has to approve it." if own
        else "Needs cluster_builds:execute."
    )


def _is_own(owner_id, user) -> bool:
    return owner_id is not None and owner_id == getattr(user, "id", None)


def _pending_requests_query():
    from ..models import DeploymentRequest

    return (
        DeploymentRequest.query.filter_by(status="pending")
        .order_by(DeploymentRequest.created_at.desc()).limit(_LIMIT)
    )


def _pending_bundles_query():
    from ..models import ChangeBundle

    return (
        ChangeBundle.query.filter_by(status="pending_approval")
        .order_by(ChangeBundle.created_at.desc()).limit(_LIMIT)
    )


def _pending_hermes():
    from ..models import TicketInterpretation

    return (
        TicketInterpretation.query.filter_by(status="awaiting_approval")
        .order_by(TicketInterpretation.created_at.desc()).limit(_LIMIT).all()
    )


def _waiting_ci_stages():
    """(stage, state, build) for every approval stage waiting now, builds in one query."""
    from ..models_ci import CiBuild, CiBuildStage
    from .ci import server_stage_base as base

    rows = (
        CiBuildStage.query.filter_by(stage_type="approval", status="running")
        .order_by(CiBuildStage.started_at.desc()).limit(_LIMIT).all()
    )
    waiting = [(stage, base.state(stage)) for stage in rows]
    waiting = [(stage, current) for stage, current in waiting if current.get("phase") == "waiting_approval"]
    build_ids = {stage.build_id for stage, _ in waiting}
    builds = {b.id: b for b in CiBuild.query.filter(CiBuild.id.in_(build_ids)).all()} if build_ids else {}
    return [(stage, current, builds[stage.build_id]) for stage, current in waiting if stage.build_id in builds]


def _destroy_job_columns():
    """The scalar columns the approvals views read off a provision job — never the
    plan cipher, config, plan text or log tail."""
    from ..models import ClusterProvisionJob as J

    return (
        J.id, J.build_id, J.operation, J.status, J.requested_by, J.requested_by_user_id, J.reason,
        J.plan_summary_json, J.approved_by, J.approved_at, J.decision_note, J.created_at, J.finished_at,
    )


def _pending_destroys():
    """(job, build) for every destroy waiting on its second person."""
    from sqlalchemy.orm import load_only

    from ..models import ClusterBuild, ClusterProvisionJob

    rows = (
        ClusterProvisionJob.query.options(load_only(*_destroy_job_columns()))
        .filter_by(operation="destroy", status="awaiting_approval")
        .order_by(ClusterProvisionJob.created_at.desc()).limit(_LIMIT).all()
    )
    build_ids = {job.build_id for job in rows}
    builds = (
        {b.id: b for b in ClusterBuild.query.filter(ClusterBuild.id.in_(build_ids)).all()}
        if build_ids else {}
    )
    return [(job, builds[job.build_id]) for job in rows if job.build_id in builds]


def _deployment_requests(user) -> List[Dict[str, Any]]:
    from .deployment_request_service import _LIST_USER_LOADERS, serialize_request

    if not (_can(user, "deployment_requests:view") or _can(user, "deployment_requests:manage")):
        return []
    manage = _can(user, "deployment_requests:manage")
    me = _email(user)
    out = []
    for row in _pending_requests_query().options(*_LIST_USER_LOADERS).all():
        data = serialize_request(row)
        voted = any((v.get("email") or "").lower() == me for v in data["votes"]) if me else False
        why = _why_deployment_request(manage, _is_own(row.requester_id, user), voted)
        details = [f"Window {data['requestedWindowLabel']}"] if data.get("requestedWindowLabel") else []
        out.append(_item(
            "deployment_request", row.id,
            title=f"Deploy to {data['clusterName']}",
            summary=data.get("message") or "Permission to deploy to this cluster.",
            where=data["clusterName"],
            requestedBy=data["requesterName"],
            requestedAt=data["createdAt"],
            expiresAt=data.get("requestedWindowStart"),
            approvals=data["approvals"],
            required=data["requiredApprovals"],
            details=details,
            href="#/approvals/history",
            canDecide=not why,
            why=why,
            actions=_actions(f"/api/deployment-requests/{row.id}/approve",
                             f"/api/deployment-requests/{row.id}/decline", reject_label="Decline"),
        ))
    return out


def _bundle_summary_loaders(*item_columns) -> tuple:
    """Votes, the requester/approver names, and only the named item columns —
    never an item's payloads or YAML preview (a Helm item carries its whole
    chart archive)."""
    from sqlalchemy.orm import selectinload

    from ..models import ChangeBundle, ChangeBundleItem, user_display_loader

    columns = [ChangeBundleItem.id, ChangeBundleItem.bundle_id, ChangeBundleItem.position,
               ChangeBundleItem.cluster_id, ChangeBundleItem.cluster_name]
    columns += [getattr(ChangeBundleItem, name) for name in item_columns]
    return (
        selectinload(ChangeBundle.items).load_only(*columns),
        selectinload(ChangeBundle.votes),
        user_display_loader(ChangeBundle.requester),
        user_display_loader(ChangeBundle.approved_by),
    )


def _change_bundles(user) -> List[Dict[str, Any]]:
    from .change_bundle_service import _active_cluster_names, _resolve_cluster_name, serialize_bundle

    if not (_can(user, "change_bundles:view") or _can(user, "change_bundles:manage")):
        return []
    manage = _can(user, "change_bundles:manage")
    me = _email(user)
    out = []
    rows = (
        _pending_bundles_query()
        .options(*_bundle_summary_loaders("action_type", "resource_kind", "resource_name", "namespace"))
        .all()
    )
    names = _active_cluster_names(item.cluster_id for row in rows for item in row.items)
    for row in rows:
        data = serialize_bundle(row, include_items=False, known_cluster_names=names)
        voted = bool(me) and any(
            (v.voter_email or "").lower() == me and v.decision == "approve" for v in row.votes
        )
        why = _why_change_bundle(manage, _is_own(row.requester_user_id, user), voted)
        items = sorted(row.items, key=lambda i: i.position)
        details = [
            f"{(i.action_type or '').replace('_', ' ')} {i.resource_kind or ''} "
            f"{i.resource_name or ''} in {i.namespace or '-'} "
            f"({_resolve_cluster_name(i.cluster_id, i.cluster_name, names)})".strip()
            for i in items[:6]
        ]
        if len(items) > 6:
            details.append(f"… and {len(items) - 6} more")
        if data.get("promotionException"):
            details.insert(0, "Promotion exception: deploys past the promotion ladder.")
        out.append(_item(
            "change_bundle", row.id,
            title=f"Change bundle #{row.id} — {data['itemCount']} change{'' if data['itemCount'] == 1 else 's'}",
            summary=data.get("note") or "",
            where=", ".join(data.get("clusterNames") or []) or None,
            requestedBy=data["requesterName"],
            # The wait counts from submission: a draft (the cart) can be
            # opened months before it is sent.
            requestedAt=data.get("submittedAt") or data["createdAt"],
            expiresAt=data.get("requestedEndTime"),
            approvals=data["approvals"],
            required=data["requiredApprovals"],
            details=details,
            href="#/change-bundles/all",
            canDecide=not why,
            why=why,
            actions=_actions(f"/api/change-bundles/{row.id}/approve",
                             f"/api/change-bundles/{row.id}/reject", reject_field="reason"),
        ))
    return out


def _hermes(user) -> List[Dict[str, Any]]:
    from .ticket_agent import engine

    if not _can(user, "ticketing:view"):
        return []
    manage = _can(user, "ticketing:manage")
    builds = _can(user, "cluster_builds:execute")
    out = []
    for task in _pending_hermes():
        data = engine.serialize(task)
        vm = data.get("vmRequest")
        number = task.ticket_number or f"ticket {task.ticket_record_id}"
        why = _why_hermes(task, user, manage, builds)
        if vm:
            machines = vm.get("machines") or []
            title = f"{number}: {len(machines) or vm.get('count')} new VM(s)" + (
                " + Kubernetes" if vm.get("kubernetes") else ""
            )
            where = vm.get("profile")
            details = [
                f"{m.get('name')}  {m.get('address') or '-'}  "
                f"{m.get('cpu')} vCPU · {m.get('memoryGb')} GB · {m.get('diskGb')} GB"
                for m in machines
            ] + [f"vCenter: {w}" for w in vm.get("warnings") or []]
        else:
            changes = [c for c in data.get("changes") or [] if c.get("deploymentName")]
            title = f"{number}: {engine._describe_change(task)}"
            where = ", ".join(sorted({c.get("namespace") for c in changes if c.get("namespace")})) or None
            details = list(data.get("reasons") or [])
        out.append(_item(
            "hermes", task.id,
            title=title,
            summary=task.understanding or "",
            where=where,
            requestedBy="Hermes",
            # When the approval was asked for, not when the run started.
            requestedAt=_iso(task.approval_requested_at) or data.get("createdAt"),
            expiresAt=data.get("expiresAt"),
            approvals=0,
            required=1,
            details=details,
            href=f"#/ticketing/{task.provider or 'zoho'}/tickets",
            canDecide=not why,
            why=why,
            actions=_actions(f"/api/ticket-agent/tasks/{task.id}/approve",
                             f"/api/ticket-agent/tasks/{task.id}/reject", reject_field="note"),
            vmRequest=vm,
            confidence=task.confidence,
        ))
    return out


def _ci_stages(user) -> List[Dict[str, Any]]:
    from ..models_ci import CiService
    from .ci import approval_config, approval_stage

    if not _can(user, "ci_builds:view"):
        return []
    out = []
    waiting = _waiting_ci_stages()
    service_ids = {build.service_id for _, _, build in waiting if build.service_id}
    services = (
        {s.id: s for s in CiService.query.filter(CiService.id.in_(service_ids)).all()}
        if service_ids else {}
    )
    for stage, current, build in waiting:
        service = services.get(build.service_id) if build.service_id else None
        ok, reason, _ = approval_stage.eligibility(build, stage, user, "approve")
        decisions = current.get("decisions") or []
        started = current.get("startedBy") or {}
        if service is not None and service.kind == "pipeline":
            href = f"#/pipelines/{service.id}/builds?build={build.id}"
        elif service is not None:
            href = f"#/service-catalog/{service.id}/builds?build={build.id}"
        else:
            href = None
        details = [
            f"Approvers: {approval_config.describe_approvers(current.get('approvers') or {})}",
        ]
        if build.branch:
            details.append(f"Branch {build.branch}" + (f" @ {build.commit_sha[:10]}" if build.commit_sha else ""))
        details += [
            f"{d.get('username')} {'approved' if d.get('decision') == 'approve' else 'rejected'}"
            + (f": {d.get('comment')}" if d.get("comment") else "")
            for d in decisions
        ]
        out.append(_item(
            "ci_stage", stage.id,
            title=f"{service.name if service else 'Build'} #{build.number} — {stage.name}",
            summary=current.get("instructions") or "The build waits here until it is approved.",
            where=service.name if service else None,
            requestedBy=started.get("username"),
            requestedAt=current.get("waitingSince") or _iso(stage.started_at),
            expiresAt=current.get("deadlineAt"),
            approvals=sum(1 for d in decisions if d.get("decision") == "approve"),
            required=int(current.get("required") or 1),
            details=details,
            href=href,
            canDecide=ok,
            why="" if ok else reason,
            actions=_actions(
                f"/api/ci/builds/{build.id}/stages/{stage.id}/approve",
                f"/api/ci/builds/{build.id}/stages/{stage.id}/reject",
                reject_field="comment", approve_field="comment",
            ),
        ))
    return out


def _cluster_destroys(user) -> List[Dict[str, Any]]:
    if not _can(user, "cluster_builds:view"):
        return []
    execute = _can(user, "cluster_builds:execute")
    out = []
    for job, build in _pending_destroys():
        why = _why_destroy(execute, _is_own(job.requested_by_user_id, user) and not _admin(user))
        summary = job.plan_summary_json or {}
        vms = [
            r.get("title") for r in summary.get("resources") or []
            if r.get("kind") == "vm" and r.get("action") == "delete"
        ]
        details = [f"Deletes {len(vms)} VM(s): {', '.join(vms)}" if vms else "Deletes the build's VMs."]
        if build.result_cluster_id:
            details.append(f"The cluster {build.result_cluster_id} goes with them.")
        if summary.get("blocked"):
            details.append(f"Blocked: {summary['blocked']}")
        out.append(_item(
            "cluster_destroy", job.id,
            title=f"Destroy {build.name}",
            summary=job.reason or "Destroy the VMs KubeSight created for this build.",
            where=build.result_cluster_id or build.name,
            requestedBy=job.requested_by,
            requestedAt=_iso(job.created_at),
            approvals=0,
            required=1,
            details=details,
            href=f"#/cluster-builder/builds/{build.id}",
            canDecide=not why,
            why=why,
            actions=_actions(
                f"/api/cluster-builds/{build.id}/provision/jobs/{job.id}/approve",
                f"/api/cluster-builds/{build.id}/provision/jobs/{job.id}/reject",
                reject_field="note", approve_field="note",
            ),
            buildId=build.id,
        ))
    return out


def _upgrade_when(job: Dict[str, Any]) -> str:
    sched = job.get("schedule") or {}
    start = _parse_before(sched.get("start")) if sched.get("mode") == "window" else None
    if start is None:
        return "As soon as it is approved"
    end = _parse_before(sched.get("end"))
    text = f"In the window starting {start.strftime('%a %d %b, %H:%M')} UTC"
    return text + (f" until {end.strftime('%H:%M')} UTC" if end else "")


def _pending_upgrades(user) -> List[Dict[str, Any]]:
    """Upgrade requests waiting for approval on clusters this person can see."""
    from ..upgrade_jobs import list_jobs
    from .upgrade_center import _can_access

    if not _can(user, "upgrades:precheck"):
        return []
    return [
        job for job in list_jobs(statuses=("awaiting_approval",), limit=_LIMIT)
        if _can_access(user, str(job.get("clusterId")))
    ]


def _cluster_upgrades(user) -> List[Dict[str, Any]]:
    from .upgrade_center import why_cannot_decide

    out = []
    for job in _pending_upgrades(user):
        why = why_cannot_decide(job, user) or ""
        approvals = job.get("approvals") or {}
        votes = approvals.get("votes") or []
        options = job.get("options") or {}
        readiness = job.get("readiness") or {}
        details = [
            f"{job.get('currentVersion')} → {job.get('targetVersion')}",
            f"Workers {options.get('waveSize', 1)} at a time; drain timeout {options.get('drainTimeoutMinutes', 10)} min"
            + ("; etcd snapshot first" if options.get("etcdSnapshot", True) else "; no etcd snapshot"),
            _upgrade_when(job),
        ]
        warnings = readiness.get("warnings") or []
        if warnings:
            details.append(f"Readiness warnings: {', '.join(warnings)}")
        for waiver in job.get("waivers") or []:
            details.append(f"Blocker waived by {waiver.get('by')}: {waiver.get('name')} — {waiver.get('reason')}")
        out.append(_item(
            "cluster_upgrade", 0,
            key=f"cluster_upgrade:{job['jobId']}",
            id=job["jobId"],
            title=f"Upgrade {job.get('clusterName') or job.get('clusterId')} to {job.get('targetVersion')}",
            summary=job.get("note") or "Kubernetes upgrade run by KubeSight.",
            where=job.get("clusterName") or job.get("clusterId"),
            requestedBy=(job.get("requestedBy") or {}).get("name"),
            requestedAt=job.get("requestedAt"),
            approvals=sum(1 for v in votes if v.get("decision") == "approve"),
            required=approvals.get("required"),
            details=details,
            href=f"#/upgrade/cluster/{job.get('clusterId')}?job={job['jobId']}",
            canDecide=not why,
            why=why,
            actions=_actions(
                f"/api/upgrades/requests/{job['jobId']}/approve",
                f"/api/upgrades/requests/{job['jobId']}/reject",
                reject_field="note", approve_field="note",
            ),
        ))
    return out


_COLLECTORS: Dict[str, Callable] = {
    "deployment_request": _deployment_requests,
    "change_bundle": _change_bundles,
    "hermes": _hermes,
    "ci_stage": _ci_stages,
    "cluster_destroy": _cluster_destroys,
    "cluster_upgrade": _cluster_upgrades,
}


# ---------------------------------------------------------------------------
# Counters — the same rules as the collectors, without serializing anything
# ---------------------------------------------------------------------------

def _count_deployment_requests(user) -> int:
    from sqlalchemy import func

    from ..models import DeploymentRequest, DeploymentRequestVote

    if not _can(user, "deployment_requests:manage"):
        return 0  # without manage nothing is decidable (and view-only sees no badge)
    rows = (
        _pending_requests_query()
        .with_entities(DeploymentRequest.id, DeploymentRequest.requester_id).all()
    )
    ids = [rid for rid, owner in rows if not _is_own(owner, user)]
    me = _email(user)
    if not ids or not me:
        return len(ids)
    voted = {
        rid for (rid,) in db.session.query(DeploymentRequestVote.request_id).filter(
            DeploymentRequestVote.request_id.in_(ids),
            func.lower(DeploymentRequestVote.voter_email) == me,
        )
    }
    return sum(1 for rid in ids if rid not in voted)


def _count_change_bundles(user) -> int:
    from sqlalchemy import func

    from ..models import ChangeBundle, ChangeBundleVote

    if not _can(user, "change_bundles:manage"):
        return 0
    rows = _pending_bundles_query().with_entities(ChangeBundle.id, ChangeBundle.requester_user_id).all()
    ids = [bid for bid, owner in rows if not _is_own(owner, user)]
    me = _email(user)
    if not ids or not me:
        return len(ids)
    voted = {
        bid for (bid,) in db.session.query(ChangeBundleVote.bundle_id).filter(
            ChangeBundleVote.bundle_id.in_(ids),
            func.lower(ChangeBundleVote.voter_email) == me,
            ChangeBundleVote.decision == "approve",
        )
    }
    return sum(1 for bid in ids if bid not in voted)


def _count_hermes(user) -> int:
    if not (_can(user, "ticketing:view") and _can(user, "ticketing:manage")):
        return 0
    builds = _can(user, "cluster_builds:execute")
    return sum(1 for task in _pending_hermes() if not _why_hermes(task, user, True, builds))


def _count_ci_stages(user) -> int:
    from .ci import approval_stage

    if not _can(user, "ci_builds:view"):
        return 0
    return sum(
        1 for stage, _current, build in _waiting_ci_stages()
        if approval_stage.eligibility(build, stage, user, "approve")[0]
    )


def _count_destroys(user) -> int:
    from ..models import ClusterBuild, ClusterProvisionJob

    if not (_can(user, "cluster_builds:view") and _can(user, "cluster_builds:execute")):
        return 0
    rows = (
        db.session.query(ClusterProvisionJob.requested_by_user_id, ClusterBuild.id)
        .outerjoin(ClusterBuild, ClusterBuild.id == ClusterProvisionJob.build_id)
        .filter(ClusterProvisionJob.operation == "destroy", ClusterProvisionJob.status == "awaiting_approval")
        .order_by(ClusterProvisionJob.created_at.desc()).limit(_LIMIT).all()
    )
    # Same rule as the inbox: an admin decides their own destroy too.
    admin = _admin(user)
    return sum(1 for owner, build_id in rows if build_id is not None and (admin or not _is_own(owner, user)))


def _count_upgrades(user) -> int:
    from .upgrade_center import why_cannot_decide

    if not _can(user, "upgrades:start"):
        return 0
    return sum(1 for job in _pending_upgrades(user) if why_cannot_decide(job, user) is None)


_COUNTERS: Dict[str, Callable] = {
    "deployment_request": _count_deployment_requests,
    "change_bundle": _count_change_bundles,
    "hermes": _count_hermes,
    "ci_stage": _count_ci_stages,
    "cluster_destroy": _count_destroys,
    "cluster_upgrade": _count_upgrades,
}


# The sidebar asks every minute from every open tab; a short per-user cache
# answers most of those. Any flush that adds, decides or withdraws something
# approvable bumps the generation, so a vote shows in the badge at once.
_COUNT_TTL_SECONDS = 20.0
_count_lock = threading.Lock()
_count_cache: Dict[int, tuple] = {}  # user id -> (expires monotonic, generation, count)
_count_generation = 0


def invalidate_waiting_counts() -> None:
    """Forget every cached badge count (something approvable changed)."""
    global _count_generation
    with _count_lock:
        _count_generation += 1
        _count_cache.clear()


def _count_cache_enabled() -> bool:
    # Tests reset the database under the same user ids; they opt in explicitly.
    try:
        from flask import current_app

        config = current_app.config
        return bool(config.get("APPROVALS_COUNT_CACHE", not config.get("TESTING")))
    except Exception:  # noqa: BLE001 — outside an app: no cache
        return False


def _cache_count(user, value: int, generation: int) -> None:
    uid = getattr(user, "id", None)
    if uid is None or not _count_cache_enabled():
        return
    with _count_lock:
        if generation == _count_generation:
            _count_cache[uid] = (time.monotonic() + _COUNT_TTL_SECONDS, generation, value)


# Rows whose insert/delete changes what waits on someone, and the columns
# whose update does.
_WATCHED_COLUMNS = {
    "DeploymentRequest": ("status",),
    "DeploymentRequestVote": ("decision", "voter_email"),
    "ChangeBundle": ("status",),
    "ChangeBundleVote": ("decision", "voter_email"),
    "TicketInterpretation": ("status", "decision"),
    "CiBuildStage": ("status", "server_state"),
    "ClusterProvisionJob": ("status",),
    "UpgradeJob": ("status",),
}


def _touches_approvals(session) -> bool:
    from sqlalchemy import inspect as sa_inspect

    for obj in list(session.new) + list(session.deleted):
        if type(obj).__name__ in _WATCHED_COLUMNS:
            if type(obj).__name__ != "CiBuildStage" or getattr(obj, "stage_type", None) == "approval":
                return True
    for obj in session.dirty:
        columns = _WATCHED_COLUMNS.get(type(obj).__name__)
        if not columns:
            continue
        if type(obj).__name__ == "CiBuildStage" and getattr(obj, "stage_type", None) != "approval":
            continue
        try:
            attrs = sa_inspect(obj).attrs
            if any(attrs[name].history.has_changes() for name in columns):
                return True
        except Exception:  # noqa: BLE001 — when unsure, forget the counts
            return True
    return False


def _after_flush(session, _flush_context) -> None:
    try:
        if _count_cache and _touches_approvals(session):
            invalidate_waiting_counts()
    except Exception:  # noqa: BLE001 — a cache must never break a flush
        logger.debug("Approvals count invalidation failed", exc_info=True)


def _register_invalidation() -> None:
    from sqlalchemy import event
    from sqlalchemy.orm import Session

    if not event.contains(Session, "after_flush", _after_flush):
        event.listen(Session, "after_flush", _after_flush)


_register_invalidation()


def inbox(user, scope: str = "mine") -> Dict[str, Any]:
    """Everything pending the viewer may see; ``mine`` = what they may decide now."""
    generation = _count_generation
    items: List[Dict[str, Any]] = []
    failed: List[str] = []
    for kind, collect in _COLLECTORS.items():
        try:
            items.extend(collect(user))
        except Exception:  # noqa: BLE001 — one broken kind must not empty the inbox
            db.session.rollback()
            logger.exception("Approvals inbox: %s could not be read", kind)
            failed.append(KIND_LABELS[kind])
    mine = [i for i in items if i["canDecide"]]
    shown = mine if scope == "mine" else items
    # By moment, not by string: the kinds format their times differently.
    shown.sort(key=lambda i: _parse_before(i.get("requestedAt")) or _EPOCH, reverse=True)
    _cache_count(user, len(mine), generation)
    return {
        "items": shown,
        "counts": {"waitingOnMe": len(mine), "pending": len(items)},
        "unavailable": failed,
    }


def count_waiting(user) -> int:
    """What :func:`inbox` would count as waiting on ``user``, without building it."""
    total = 0
    for kind, count in _COUNTERS.items():
        try:
            total += count(user)
        except Exception:  # noqa: BLE001 — same as the inbox: a broken kind counts nothing
            db.session.rollback()
            logger.exception("Approvals count: %s could not be read", kind)
    return total


def waiting_count(user) -> int:
    """The sidebar badge: :func:`count_waiting`, cached per user for a few seconds."""
    uid = getattr(user, "id", None)
    if uid is not None and _count_cache_enabled():
        with _count_lock:
            hit = _count_cache.get(uid)
            if hit and hit[0] > time.monotonic() and hit[1] == _count_generation:
                return hit[2]
    generation = _count_generation
    value = count_waiting(user)
    _cache_count(user, value, generation)
    return value


# ---------------------------------------------------------------------------
# History
#
# Every kind orders by the same moment the merged list sorts on ("decidedAt"),
# so ``limit`` and the ``before`` cursor go into each kind's own query.
# ---------------------------------------------------------------------------

def _parse_before(value: Any) -> Optional[datetime]:
    if not value:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        try:
            dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _page(query, key, limit: int, before: Optional[datetime]):
    if before is not None:
        query = query.filter(key < before)
    return query.order_by(key.desc()).limit(limit).all()


def _history_requests(user, limit: int, before: Optional[datetime]) -> List[Dict[str, Any]]:
    from sqlalchemy import func

    from ..models import DeploymentRequest
    from .deployment_request_service import _LIST_USER_LOADERS, serialize_request

    if not (_can(user, "deployment_requests:view") or _can(user, "deployment_requests:manage")):
        return []
    key = func.coalesce(DeploymentRequest.decided_at, DeploymentRequest.created_at)
    rows = _page(
        DeploymentRequest.query.options(*_LIST_USER_LOADERS).filter(DeploymentRequest.status != "pending"),
        key, limit, before,
    )
    out = []
    for row in rows:
        data = serialize_request(row)
        out.append({
            "key": f"deployment_request:{row.id}", "kind": "deployment_request",
            "kindLabel": KIND_LABELS["deployment_request"],
            "title": f"Deploy to {data['clusterName']}", "summary": data.get("message") or "",
            "where": data["clusterName"], "requestedBy": data["requesterName"],
            "requestedAt": data["createdAt"], "outcome": data["status"],
            "decidedBy": data.get("decidedByName") or ", ".join(v["email"] for v in data["votes"]) or None,
            "decidedAt": data.get("decidedAt") or data["createdAt"], "href": None,
        })
    return out


def _history_bundles(user, limit: int, before: Optional[datetime]) -> List[Dict[str, Any]]:
    from sqlalchemy import func

    from ..models import ChangeBundle
    from .change_bundle_service import _active_cluster_names, serialize_bundle

    if not (_can(user, "change_bundles:view") or _can(user, "change_bundles:manage")):
        return []
    key = func.coalesce(ChangeBundle.approved_at, ChangeBundle.updated_at)
    rows = _page(
        ChangeBundle.query.options(*_bundle_summary_loaders())
        .filter(ChangeBundle.status.notin_(("draft", "pending_approval"))),
        key, limit, before,
    )
    names = _active_cluster_names(item.cluster_id for row in rows for item in row.items)
    out = []
    for row in rows:
        data = serialize_bundle(row, include_items=False, known_cluster_names=names)
        out.append({
            "key": f"change_bundle:{row.id}", "kind": "change_bundle",
            "kindLabel": KIND_LABELS["change_bundle"],
            "title": f"Change bundle #{row.id} — {data['itemCount']} change(s)",
            "summary": data.get("rejectionReason") or data.get("note") or "",
            "where": ", ".join(data.get("clusterNames") or []) or None,
            "requestedBy": data["requesterName"],
            "requestedAt": data.get("submittedAt") or data["createdAt"],
            "outcome": data["status"], "decidedBy": data.get("approvedByName"),
            "decidedAt": data.get("approvedAt") or data.get("updatedAt"),
            "href": "#/change-bundles/all",
        })
    return out


def _history_hermes(user, limit: int, before: Optional[datetime]) -> List[Dict[str, Any]]:
    from ..models import TicketInterpretation as T
    from .ticket_agent import engine

    if not _can(user, "ticketing:view"):
        return []
    out = []
    for task in _page(T.query.filter(T.decided_by.isnot(None)), T.decided_at, limit, before):
        vm = (task.decision or {}).get("vmRequest")
        approved = task.status in ("executed",) or (vm and vm.get("approvedBy"))
        out.append({
            "key": f"hermes:{task.id}", "kind": "hermes", "kindLabel": KIND_LABELS["hermes"],
            "title": f"{task.ticket_number or 'Ticket'}: {engine._describe_change(task)}",
            "summary": task.decision_note or task.understanding or "",
            "where": (vm or {}).get("profile") or task.namespace,
            "requestedBy": "Hermes",
            "requestedAt": _iso(task.approval_requested_at or task.created_at),
            "outcome": "approved" if approved else ("error" if task.status == "error" else "rejected"),
            "decidedBy": task.decided_by, "decidedAt": _iso(task.decided_at),
            "href": f"#/ticketing/{task.provider or 'zoho'}/tickets",
        })
    # Approvals nobody decided: expired and withdrawn ones.
    unanswered = T.query.filter(
        T.decided_by.is_(None),
        T.approval_nonce.isnot(None),
        T.status.in_(("impediment", "superseded")),
    )
    for task in _page(unanswered, T.finished_at, limit, before):
        out.append({
            "key": f"hermes:{task.id}", "kind": "hermes", "kindLabel": KIND_LABELS["hermes"],
            "title": f"{task.ticket_number or 'Ticket'}: {engine._describe_change(task)}",
            "summary": task.decision_note or "",
            "where": task.namespace, "requestedBy": "Hermes",
            "requestedAt": _iso(task.approval_requested_at or task.created_at),
            "outcome": "withdrawn" if task.status == "superseded" else "expired",
            "decidedBy": None, "decidedAt": _iso(task.finished_at),
            "href": f"#/ticketing/{task.provider or 'zoho'}/tickets",
        })
    return out


def _history_ci(user, limit: int, before: Optional[datetime]) -> List[Dict[str, Any]]:
    from ..models_ci import CiBuild, CiBuildStage, CiService
    from .ci import server_stage_base as base

    if not _can(user, "ci_builds:view"):
        return []
    rows = _page(
        CiBuildStage.query.filter(
            CiBuildStage.stage_type == "approval",
            CiBuildStage.status.in_(("success", "failed", "cancelled")),
        ),
        CiBuildStage.finished_at, limit, before,
    )
    decided = [(stage, base.state(stage)) for stage in rows]
    decided = [(stage, current) for stage, current in decided if current.get("outcome") not in (None, "skipped")]
    build_ids = {stage.build_id for stage, _ in decided}
    builds = {b.id: b for b in CiBuild.query.filter(CiBuild.id.in_(build_ids)).all()} if build_ids else {}
    service_ids = {b.service_id for b in builds.values() if b.service_id}
    services = (
        {s.id: s for s in CiService.query.filter(CiService.id.in_(service_ids)).all()}
        if service_ids else {}
    )
    out = []
    for stage, current in decided:
        build = builds.get(stage.build_id)
        service = services.get(build.service_id) if build and build.service_id else None
        decisions = current.get("decisions") or []
        out.append({
            "key": f"ci_stage:{stage.id}", "kind": "ci_stage", "kindLabel": KIND_LABELS["ci_stage"],
            "title": f"{service.name if service else 'Build'} #{build.number if build else '?'} — {stage.name}",
            "summary": current.get("message") or stage.error or "",
            "where": service.name if service else None,
            "requestedBy": (current.get("startedBy") or {}).get("username"),
            "requestedAt": current.get("waitingSince") or _iso(stage.started_at),
            "outcome": current.get("outcome"),
            "decidedBy": ", ".join(d.get("username") or "" for d in decisions) or None,
            "decidedAt": _iso(stage.finished_at),
            "href": (f"#/service-catalog/{service.id}/builds?build={build.id}" if service and build else None),
        })
    return out


def _history_destroys(user, limit: int, before: Optional[datetime]) -> List[Dict[str, Any]]:
    from sqlalchemy import func
    from sqlalchemy.orm import load_only

    from ..models import ClusterBuild, ClusterProvisionJob as J

    if not _can(user, "cluster_builds:view"):
        return []
    key = func.coalesce(J.approved_at, J.finished_at, J.created_at)
    rows = _page(
        J.query.options(load_only(*_destroy_job_columns())).filter(
            J.operation == "destroy",
            J.status.notin_(("awaiting_approval", "planning", "planned")),
        ),
        key, limit, before,
    )
    build_ids = {job.build_id for job in rows}
    builds = (
        {b.id: b for b in ClusterBuild.query.filter(ClusterBuild.id.in_(build_ids)).all()}
        if build_ids else {}
    )
    out = []
    for job in rows:
        build = builds.get(job.build_id)
        outcome = (
            "rejected" if job.status == "rejected"
            else "withdrawn" if job.status == "discarded"
            else "approved" if job.approved_by else job.status
        )
        out.append({
            "key": f"cluster_destroy:{job.id}", "kind": "cluster_destroy",
            "kindLabel": KIND_LABELS["cluster_destroy"],
            "title": f"Destroy {build.name if build else f'build #{job.build_id}'}",
            "summary": job.decision_note or job.reason or "",
            "where": build.result_cluster_id if build else None,
            "requestedBy": job.requested_by, "requestedAt": _iso(job.created_at),
            "outcome": outcome, "decidedBy": job.approved_by,
            "decidedAt": _iso(job.approved_at or job.finished_at or job.created_at),
            "href": f"#/cluster-builder/builds/{job.build_id}" if build else None,
        })
    return out


def _history_upgrades(user, limit: int, before: Optional[datetime]) -> List[Dict[str, Any]]:
    from ..upgrade_jobs import list_jobs
    from .upgrade_center import _can_access

    if not _can(user, "upgrades:precheck"):
        return []
    out = []
    for job in list_jobs(statuses=("scheduled", "queued", "running", "paused", "completed", "failed",
                                   "stopped", "rejected", "expired", "withdrawn"), limit=_LIMIT):
        approvals = job.get("approvals") or {}
        votes = approvals.get("votes") or []
        if not approvals.get("required") or not _can_access(user, str(job.get("clusterId"))):
            continue
        if not votes and job.get("status") != "withdrawn":
            continue
        decided_at = job.get("approvedAt") or (votes[-1].get("at") if votes else job.get("finishedAt"))
        when = _parse_before(decided_at)
        if before is not None and when is not None and when >= before:
            continue
        outcome = ("rejected" if job.get("status") == "rejected"
                   else "withdrawn" if job.get("status") == "withdrawn" else "approved")
        out.append({
            "key": f"cluster_upgrade:{job['jobId']}", "kind": "cluster_upgrade",
            "kindLabel": KIND_LABELS["cluster_upgrade"],
            "title": f"Upgrade {job.get('clusterName') or job.get('clusterId')} to {job.get('targetVersion')}",
            "summary": next((v.get("note") for v in reversed(votes) if v.get("note")), "") or job.get("message") or "",
            "where": job.get("clusterName") or job.get("clusterId"),
            "requestedBy": (job.get("requestedBy") or {}).get("name"),
            "requestedAt": job.get("requestedAt"),
            "outcome": outcome,
            "decidedBy": ", ".join(v.get("name") or "" for v in votes) or None,
            "decidedAt": decided_at,
            "href": f"#/upgrade/cluster/{job.get('clusterId')}?job={job['jobId']}",
        })
        if len(out) >= limit:
            break
    return out


_HISTORY: Dict[str, Callable] = {
    "deployment_request": _history_requests,
    "change_bundle": _history_bundles,
    "hermes": _history_hermes,
    "ci_stage": _history_ci,
    "cluster_destroy": _history_destroys,
    "cluster_upgrade": _history_upgrades,
}


def _decided_sort_key(item: Dict[str, Any]) -> datetime:
    return _parse_before(item.get("decidedAt")) or _EPOCH


def history(user, kind: Optional[str] = None, limit: int = _LIMIT, before: Any = None) -> Dict[str, Any]:
    """What was decided, newest first. ``before`` (a ``decidedAt`` from a previous
    page, or ``nextBefore``) returns the page after it."""
    limit = max(1, min(int(limit or _LIMIT), 500))
    cursor = _parse_before(before)
    items: List[Dict[str, Any]] = []
    for name, collect in _HISTORY.items():
        if kind and kind != name:
            continue
        try:
            items.extend(collect(user, limit, cursor))
        except Exception:  # noqa: BLE001
            db.session.rollback()
            logger.exception("Approvals history: %s could not be read", name)
    items.sort(key=_decided_sort_key, reverse=True)
    page = items[:limit]
    next_before = page[-1].get("decidedAt") if len(page) == limit else None
    return {"items": page, "nextBefore": next_before}


# ---------------------------------------------------------------------------
# Rules
# ---------------------------------------------------------------------------

# What the per-cluster approval count gates, in words, for the Rules tab.
CLUSTER_GATES = [
    "Deploys and YAML applies (a direct change is queued as a change bundle)",
    "Change bundles",
    "CI Deploy stages",
    "Helm install, upgrade and rollback",
    "Restart, scale and rollback from Inventory",
    "Ticket deploys (Hermes and deploy automation)",
    "Promotion releases and promotion exceptions",
    "Cluster Builder day-two changes (add-ons, machines, workloads)",
    "Kubernetes upgrades run from the Upgrade Center (an admin's upgrade needs none)",
]


def _cluster_rows(config: Dict[str, Any]) -> List[Dict[str, Any]]:
    from ..routes.clusters import _list_clusters_payload

    overrides = config.get("clusterApprovals") or {}
    default = int(config.get("requiredApprovals") or 0)
    rows = []
    try:
        payload = _list_clusters_payload()
        clusters = (payload.get("items") if isinstance(payload, dict) else None) or []
    except Exception:  # noqa: BLE001 — the rule rows still show the overrides
        logger.warning("Approvals rules: cluster list unavailable", exc_info=True)
        clusters = []
    seen = set()
    for cluster in clusters:
        cid = cluster.get("id") or cluster.get("name")
        if not cid or cid in seen:
            continue
        seen.add(cid)
        override = overrides.get(cid)
        rows.append({
            "id": cid,
            "name": cluster.get("name") or cid,
            "override": override,
            "effective": default if override is None else int(override),
        })
    for cid, override in overrides.items():
        if cid not in seen:
            rows.append({"id": cid, "name": cid, "override": override, "effective": int(override),
                         "missing": True})
    return rows


def _ci_overview() -> List[Dict[str, Any]]:
    from ..models_ci import CiPipeline, CiPipelineStage, CiService
    from .ci import approval_config

    out = []
    rows = (
        db.session.query(CiPipelineStage, CiPipeline, CiService)
        .join(CiPipeline, CiPipeline.id == CiPipelineStage.pipeline_id)
        .join(CiService, CiService.id == CiPipeline.service_id)
        .filter(CiPipelineStage.stage_type == "approval")
        .order_by(CiService.name.asc(), CiPipeline.name.asc(), CiPipelineStage.position.asc())
        .limit(500).all()
    )
    for stage, pipeline, service in rows:
        config = stage.approval if isinstance(stage.approval, dict) else {}
        base = f"#/pipelines/{service.id}" if service.kind == "pipeline" else f"#/service-catalog/{service.id}"
        out.append({
            "service": service.name,
            "pipeline": pipeline.name,
            "stage": stage.name,
            "approvers": approval_config.describe_approvers(config) if config else "nobody (not set up)",
            "minApprovals": int(config.get("minApprovals") or 1) if config else None,
            "allowSelfApproval": bool(config.get("allowSelfApproval")),
            "timeoutMinutes": int((stage.timeout_seconds or 1800) // 60),
            "enabled": bool(pipeline.enabled),
            "href": f"{base}/pipeline",
        })
    return out


def _promotion_overview() -> List[Dict[str, Any]]:
    from ..models_promotion import PromotionEnvironment

    return [
        {
            "environment": env.name,
            "mode": env.mode,
            "minSoakMinutes": env.min_soak_minutes or 0,
            "href": "#/promotions/ladder",
        }
        for env in PromotionEnvironment.query.order_by(PromotionEnvironment.position.asc()).all()
    ]


def rules(user) -> Dict[str, Any]:
    from .ticket_agent import settings as agent_settings

    out: Dict[str, Any] = {
        "permissions": {
            "editClusters": _can(user, "deployment_requests:manage"),
            "editGroups": _can(user, "deployment_requests:manage"),
            "editHermes": _can(user, "ticketing:manage"),
        },
    }
    if _can(user, "deployment_requests:view") or _can(user, "deployment_requests:manage"):
        from .deployment_request_service import get_recipient_config

        config = get_recipient_config()
        out["clusters"] = {
            **config,
            "clusters": _cluster_rows(config),
            "gates": CLUSTER_GATES,
            "changeApprovalTtlHours": _ttl_hours(),
        }
        out["groups"] = list_groups()
    if _can(user, "ticketing:view"):
        row = agent_settings.get_or_create()
        out["hermes"] = {
            "active": agent_settings.is_active(),
            "enabled": bool(row.enabled),
            "minConfidence": row.min_confidence or "High",
            "approvalTimeoutHours": int(row.approval_timeout_hours or 24),
            "telegramReady": agent_settings.telegram_ready(row),
            "telegramApprovers": row.telegram_approvers or "",
            "vmRequestsEnabled": bool(row.vm_requests_enabled),
            "vmApprovers": row.vm_approvers or "",
            "integrationHref": "#/integrations/ticket_agent",
        }
    if _can(user, "cluster_builds:view"):
        out["clusterBuilder"] = {
            "destroy": "An admin destroys VMs directly, with no approval. Anyone else's destroy waits for "
                       "a second person with cluster_builds:execute; they can withdraw it but never approve it.",
            "vmRequests": "VMs asked for on a ticket wait for a VM approver (Hermes, below).",
            "dayTwo": "Changes to a built cluster follow that cluster's approval count above, and are "
                      "refused (not queued) without an approved deployment request.",
        }
    elsewhere: Dict[str, Any] = {}
    if _can(user, "ci_builds:view") or _can(user, "ci_pipelines:edit"):
        try:
            elsewhere["ciStages"] = _ci_overview()
        except Exception:  # noqa: BLE001
            db.session.rollback()
            logger.exception("Approvals rules: CI overview failed")
            elsewhere["ciStages"] = []
    if _can(user, "promotions:view") or _can(user, "promotions:manage"):
        try:
            elsewhere["promotions"] = _promotion_overview()
        except Exception:  # noqa: BLE001
            db.session.rollback()
            logger.exception("Approvals rules: promotion overview failed")
            elsewhere["promotions"] = []
    out["elsewhere"] = elsewhere
    return out


def _ttl_hours() -> int:
    from .change_bundle_service import _approval_ttl_hours

    return _approval_ttl_hours()


# ---------------------------------------------------------------------------
# Approver groups — receiver groups, as people, roles and emails
# ---------------------------------------------------------------------------

_PEOPLE_TYPES = ("user", "role", "email")


def _serialize_group(group, selected: List[int]) -> Dict[str, Any]:
    from .alert_routing_service import resolve_receiver_emails

    users, roles, emails, other = [], [], [], []
    resolved: List[str] = []
    for member in group.members or []:
        if member.receiver_type == "user":
            user = member.user
            users.append({"id": member.user_id, "name": _name(user) or f"user #{member.user_id}",
                          "email": getattr(user, "email", None), "active": bool(user and user.is_active)})
        elif member.receiver_type == "role":
            role = member.role
            roles.append({"id": member.role_id, "name": role.name if role else f"role #{member.role_id}"})
        elif member.receiver_type == "email":
            emails.append(member.email_address)
        else:
            other.append({"id": member.id, "name": member.name, "type": member.receiver_type})
        for address in resolve_receiver_emails(member):
            if address.lower() not in {a.lower() for a in resolved}:
                resolved.append(address)
    return {
        "id": group.id,
        "name": group.name,
        "description": group.description or "",
        "enabled": bool(group.enabled),
        "users": users,
        "roles": roles,
        "emails": [e for e in emails if e],
        # Slack/webhook members an alert policy put there: kept, not edited here.
        "otherMembers": other,
        "resolvedEmails": resolved,
        "approvesDeploys": group.id in selected,
        "alertPolicies": [p.name for p in group.policies.all()] if hasattr(group.policies, "all") else [],
    }


def list_groups() -> Dict[str, Any]:
    from ..models import AlertRoutingReceiverGroup, Role, User
    from .deployment_request_service import _configured_group_ids

    selected = _configured_group_ids()
    groups = AlertRoutingReceiverGroup.query.order_by(AlertRoutingReceiverGroup.name.asc()).all()
    return {
        "items": [_serialize_group(g, selected) for g in groups],
        "options": {
            "users": [
                {"id": u.id, "name": _name(u), "email": u.email}
                for u in User.query.filter(User.is_active.is_(True)).order_by(User.username.asc()).all()
                if (u.email or "").strip() and not getattr(u, "is_service_account", False)
            ],
            "roles": [{"id": r.id, "name": r.name} for r in Role.query.order_by(Role.name.asc()).all()],
        },
    }


def _receiver_for(kind: str, value: Any):
    from ..models import AlertRoutingReceiver, Role, User
    from .alert_routing_service import _find_or_create_email_receiver

    if kind == "email":
        return _find_or_create_email_receiver(str(value))
    model, column = (User, "user_id") if kind == "user" else (Role, "role_id")
    target = db.session.get(model, int(value))
    if target is None:
        raise ValueError(f"{kind.capitalize()} #{value} not found.")
    if kind == "user" and not (target.email or "").strip():
        raise ValueError(f"{_name(target)} has no email address, so they cannot be emailed approvals.")
    existing = AlertRoutingReceiver.query.filter_by(receiver_type=kind, **{column: target.id}).first()
    if existing is not None:
        if not existing.enabled:
            existing.enabled = True
        return existing
    label = _name(target) if kind == "user" else f"{target.name} (role)"
    name, suffix = label, 2
    while AlertRoutingReceiver.query.filter_by(name=name).first():
        name, suffix = f"{label} {suffix}", suffix + 1
    row = AlertRoutingReceiver(name=name, receiver_type=kind, enabled=True, **{column: target.id})
    db.session.add(row)
    db.session.flush()
    return row


def save_group(payload: Dict[str, Any], group_id: Optional[int] = None) -> Dict[str, Any]:
    from ..models import AlertRoutingReceiverGroup
    from .alert_routing_service import _parse_email_list
    from .deployment_request_service import _configured_group_ids

    payload = payload or {}
    group = db.session.get(AlertRoutingReceiverGroup, group_id) if group_id else AlertRoutingReceiverGroup()
    if group is None:
        raise LookupError("Approver group not found.")
    name = str(payload.get("name", group.name or "")).strip()
    if not name:
        raise ValueError("Name the group.")
    clash = AlertRoutingReceiverGroup.query.filter(
        AlertRoutingReceiverGroup.name == name, AlertRoutingReceiverGroup.id != (group.id or -1)
    ).first()
    if clash:
        raise ValueError(f"A group called {name} already exists.")
    group.name = name
    if "description" in payload:
        group.description = str(payload.get("description") or "").strip()[:1000] or None
    if "enabled" in payload or group.enabled is None:
        group.enabled = bool(payload.get("enabled", True))
    if not group_id:
        db.session.add(group)
        db.session.flush()

    if any(key in payload for key in ("userIds", "roleIds", "emails")):
        # A kind left out of the payload keeps its members; Slack/webhook
        # members an alert policy relies on are never touched here.
        replaced = {kind for kind, key in (("user", "userIds"), ("role", "roleIds"), ("email", "emails"))
                    if key in payload}
        members = [m for m in group.members or [] if m.receiver_type not in replaced]
        for user_id in payload.get("userIds") or []:
            members.append(_receiver_for("user", user_id))
        for role_id in payload.get("roleIds") or []:
            members.append(_receiver_for("role", role_id))
        raw_emails = payload.get("emails") or []
        if isinstance(raw_emails, str):
            raw_emails = _parse_email_list(raw_emails)
        for address in raw_emails:
            if "@" not in str(address):
                raise ValueError(f"{address} is not an email address.")
            members.append(_receiver_for("email", str(address).strip()))
        unique, seen = [], set()
        for member in members:
            if member.id not in seen:
                seen.add(member.id)
                unique.append(member)
        group.members = unique

    if "approvesDeploys" in payload:
        from .deployment_request_service import _get_or_create_setting

        setting = _get_or_create_setting()
        selected = [g for g in _configured_group_ids() if g != group.id]
        if payload.get("approvesDeploys"):
            selected.append(group.id)
        setting.group_ids = selected
    db.session.commit()
    return _serialize_group(group, _configured_group_ids())


def delete_group(group_id: int) -> Dict[str, Any]:
    from ..models import AlertRoutingReceiverGroup
    from .deployment_request_service import _configured_group_ids, _get_or_create_setting

    group = db.session.get(AlertRoutingReceiverGroup, group_id)
    if group is None:
        raise LookupError("Approver group not found.")
    if _configured_group_ids() and group_id in _configured_group_ids():
        setting = _get_or_create_setting()
        setting.group_ids = [g for g in _configured_group_ids() if g != group_id]
    summary = {"id": group.id, "name": group.name}
    db.session.delete(group)
    db.session.commit()
    return summary
