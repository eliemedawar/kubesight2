"""Approvals in one place: everything waiting on a person, and every rule that says so.

KubeSight asks people to approve six different things, each with its own
rules living next to the thing it protects:

  deployment_request  a deploy on an approval-gated cluster   (cluster count + approver pool)
  change_bundle       a set of cluster changes, or a queued direct change (same)
  hermes              a ticket action Hermes was unsure about, or new VMs it asked for
  ci_stage            an Approval stage in a CI pipeline        (the stage's own approvers)
  cluster_destroy     destroying VMs KubeSight created          (a second person)

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
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Dict, List, Optional

from ..access_engine import user_has_permission
from ..db import db

logger = logging.getLogger(__name__)

KINDS = ("deployment_request", "change_bundle", "hermes", "ci_stage", "cluster_destroy")
KIND_LABELS = {
    "deployment_request": "Deployment request",
    "change_bundle": "Change bundle",
    "hermes": "Hermes ticket",
    "ci_stage": "CI approval stage",
    "cluster_destroy": "Cluster Builder destroy",
}
_LIMIT = 200


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
# ---------------------------------------------------------------------------

def _deployment_requests(user) -> List[Dict[str, Any]]:
    from ..models import DeploymentRequest
    from .deployment_request_service import serialize_request

    if not (_can(user, "deployment_requests:view") or _can(user, "deployment_requests:manage")):
        return []
    manage = _can(user, "deployment_requests:manage")
    me = _email(user)
    out = []
    rows = (
        DeploymentRequest.query.filter_by(status="pending")
        .order_by(DeploymentRequest.created_at.desc()).limit(_LIMIT).all()
    )
    for row in rows:
        data = serialize_request(row)
        voted = any((v.get("email") or "").lower() == me for v in data["votes"]) if me else False
        own = row.requester_id is not None and row.requester_id == getattr(user, "id", None)
        why = (
            "" if manage and not own and not voted
            else "Your request — another approver decides it." if own
            else "You already voted." if voted
            else "Needs deployment_requests:manage."
        )
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


def _change_bundles(user) -> List[Dict[str, Any]]:
    from ..models import ChangeBundle
    from .change_bundle_service import serialize_bundle

    if not (_can(user, "change_bundles:view") or _can(user, "change_bundles:manage")):
        return []
    manage = _can(user, "change_bundles:manage")
    me = _email(user)
    out = []
    rows = (
        ChangeBundle.query.filter_by(status="pending_approval")
        .order_by(ChangeBundle.created_at.desc()).limit(_LIMIT).all()
    )
    for row in rows:
        data = serialize_bundle(row)
        voted = me and any(
            (v.voter_email or "").lower() == me and v.decision == "approve" for v in row.votes
        )
        own = row.requester_user_id is not None and row.requester_user_id == getattr(user, "id", None)
        why = (
            "" if manage and not own and not voted
            else "Your bundle — another approver decides it." if own
            else "You already approved it." if voted
            else "Needs change_bundles:manage."
        )
        items = data.get("items") or []
        details = [
            f"{(i.get('actionType') or '').replace('_', ' ')} {i.get('resourceKind') or ''} "
            f"{i.get('resourceName') or ''} in {i.get('namespace') or '-'} ({i.get('clusterName')})".strip()
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
            requestedAt=data["createdAt"],
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
    from ..models import TicketInterpretation
    from .ticket_agent import engine, vm_requests

    if not _can(user, "ticketing:view"):
        return []
    manage = _can(user, "ticketing:manage")
    builds = _can(user, "cluster_builds:execute")
    out = []
    rows = (
        TicketInterpretation.query.filter_by(status="awaiting_approval")
        .order_by(TicketInterpretation.created_at.desc()).limit(_LIMIT).all()
    )
    for task in rows:
        data = engine.serialize(task)
        vm = data.get("vmRequest")
        number = task.ticket_number or f"ticket {task.ticket_record_id}"
        why = "" if manage else "Needs ticketing:manage."
        if vm and not why:
            if not builds:
                why = "Approving new VMs needs cluster_builds:execute."
            else:
                try:
                    vm_requests.check_approver(task, user)
                except vm_requests.VmRequestError as exc:
                    why = str(exc)
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
            requestedAt=data.get("createdAt"),
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
    from ..models_ci import CiBuild, CiBuildStage, CiService
    from .ci import approval_config, approval_stage
    from .ci import server_stage_base as base

    if not _can(user, "ci_builds:view"):
        return []
    out = []
    rows = (
        CiBuildStage.query.filter_by(stage_type="approval", status="running")
        .order_by(CiBuildStage.started_at.desc()).limit(_LIMIT).all()
    )
    for stage in rows:
        current = base.state(stage)
        if current.get("phase") != "waiting_approval":
            continue
        build = db.session.get(CiBuild, stage.build_id)
        if build is None:
            continue
        service = db.session.get(CiService, build.service_id) if build.service_id else None
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
    from ..models import ClusterBuild, ClusterProvisionJob

    if not _can(user, "cluster_builds:view"):
        return []
    execute = _can(user, "cluster_builds:execute")
    out = []
    rows = (
        ClusterProvisionJob.query.filter_by(operation="destroy", status="awaiting_approval")
        .order_by(ClusterProvisionJob.created_at.desc()).limit(_LIMIT).all()
    )
    for job in rows:
        build = db.session.get(ClusterBuild, job.build_id)
        if build is None:
            continue
        own = job.requested_by_user_id is not None and job.requested_by_user_id == getattr(user, "id", None)
        why = (
            "" if execute and not own
            else "You asked for this destroy — someone else has to approve it." if own
            else "Needs cluster_builds:execute."
        )
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


_COLLECTORS: Dict[str, Callable] = {
    "deployment_request": _deployment_requests,
    "change_bundle": _change_bundles,
    "hermes": _hermes,
    "ci_stage": _ci_stages,
    "cluster_destroy": _cluster_destroys,
}


def inbox(user, scope: str = "mine") -> Dict[str, Any]:
    """Everything pending the viewer may see; ``mine`` = what they may decide now."""
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
    shown.sort(key=lambda i: i.get("requestedAt") or "", reverse=True)
    return {
        "items": shown,
        "counts": {"waitingOnMe": len(mine), "pending": len(items)},
        "unavailable": failed,
    }


def waiting_count(user) -> int:
    return inbox(user, "mine")["counts"]["waitingOnMe"]


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

def _history_requests(user) -> List[Dict[str, Any]]:
    from ..models import DeploymentRequest
    from .deployment_request_service import serialize_request

    if not (_can(user, "deployment_requests:view") or _can(user, "deployment_requests:manage")):
        return []
    rows = (
        DeploymentRequest.query.filter(DeploymentRequest.status != "pending")
        .order_by(DeploymentRequest.created_at.desc()).limit(_LIMIT).all()
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


def _history_bundles(user) -> List[Dict[str, Any]]:
    from ..models import ChangeBundle
    from .change_bundle_service import serialize_bundle

    if not (_can(user, "change_bundles:view") or _can(user, "change_bundles:manage")):
        return []
    rows = (
        ChangeBundle.query.filter(ChangeBundle.status.notin_(("draft", "pending_approval")))
        .order_by(ChangeBundle.updated_at.desc()).limit(_LIMIT).all()
    )
    out = []
    for row in rows:
        data = serialize_bundle(row, include_items=False)
        out.append({
            "key": f"change_bundle:{row.id}", "kind": "change_bundle",
            "kindLabel": KIND_LABELS["change_bundle"],
            "title": f"Change bundle #{row.id} — {data['itemCount']} change(s)",
            "summary": data.get("rejectionReason") or data.get("note") or "",
            "where": ", ".join(data.get("clusterNames") or []) or None,
            "requestedBy": data["requesterName"], "requestedAt": data["createdAt"],
            "outcome": data["status"], "decidedBy": data.get("approvedByName"),
            "decidedAt": data.get("approvedAt") or data.get("updatedAt"),
            "href": "#/change-bundles/all",
        })
    return out


def _history_hermes(user) -> List[Dict[str, Any]]:
    from ..models import TicketInterpretation
    from .ticket_agent import engine

    if not _can(user, "ticketing:view"):
        return []
    rows = (
        TicketInterpretation.query.filter(TicketInterpretation.decided_by.isnot(None))
        .order_by(TicketInterpretation.decided_at.desc()).limit(_LIMIT).all()
    )
    out = []
    for task in rows:
        vm = (task.decision or {}).get("vmRequest")
        approved = task.status in ("executed",) or (vm and vm.get("approvedBy"))
        out.append({
            "key": f"hermes:{task.id}", "kind": "hermes", "kindLabel": KIND_LABELS["hermes"],
            "title": f"{task.ticket_number or 'Ticket'}: {engine._describe_change(task)}",
            "summary": task.decision_note or task.understanding or "",
            "where": (vm or {}).get("profile") or task.namespace,
            "requestedBy": "Hermes", "requestedAt": _iso(task.created_at),
            "outcome": "approved" if approved else ("error" if task.status == "error" else "rejected"),
            "decidedBy": task.decided_by, "decidedAt": _iso(task.decided_at),
            "href": f"#/ticketing/{task.provider or 'zoho'}/tickets",
        })
    # Approvals nobody decided: expired and withdrawn ones.
    for task in (
        TicketInterpretation.query.filter(
            TicketInterpretation.decided_by.is_(None),
            TicketInterpretation.approval_nonce.isnot(None),
            TicketInterpretation.status.in_(("impediment", "superseded")),
        ).order_by(TicketInterpretation.finished_at.desc()).limit(_LIMIT).all()
    ):
        out.append({
            "key": f"hermes:{task.id}", "kind": "hermes", "kindLabel": KIND_LABELS["hermes"],
            "title": f"{task.ticket_number or 'Ticket'}: {engine._describe_change(task)}",
            "summary": task.decision_note or "",
            "where": task.namespace, "requestedBy": "Hermes", "requestedAt": _iso(task.created_at),
            "outcome": "withdrawn" if task.status == "superseded" else "expired",
            "decidedBy": None, "decidedAt": _iso(task.finished_at),
            "href": f"#/ticketing/{task.provider or 'zoho'}/tickets",
        })
    return out


def _history_ci(user) -> List[Dict[str, Any]]:
    from ..models_ci import CiBuild, CiBuildStage, CiService
    from .ci import server_stage_base as base

    if not _can(user, "ci_builds:view"):
        return []
    rows = (
        CiBuildStage.query.filter(
            CiBuildStage.stage_type == "approval",
            CiBuildStage.status.in_(("success", "failed", "cancelled")),
        ).order_by(CiBuildStage.finished_at.desc()).limit(_LIMIT).all()
    )
    out = []
    for stage in rows:
        current = base.state(stage)
        if current.get("outcome") in (None, "skipped"):
            continue
        build = db.session.get(CiBuild, stage.build_id)
        service = db.session.get(CiService, build.service_id) if build and build.service_id else None
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


def _history_destroys(user) -> List[Dict[str, Any]]:
    from ..models import ClusterBuild, ClusterProvisionJob

    if not _can(user, "cluster_builds:view"):
        return []
    rows = (
        ClusterProvisionJob.query.filter(
            ClusterProvisionJob.operation == "destroy",
            ClusterProvisionJob.status.notin_(("awaiting_approval", "planning", "planned")),
        ).order_by(ClusterProvisionJob.created_at.desc()).limit(_LIMIT).all()
    )
    out = []
    for job in rows:
        build = db.session.get(ClusterBuild, job.build_id)
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
            "decidedAt": _iso(job.approved_at or job.finished_at),
            "href": f"#/cluster-builder/builds/{job.build_id}" if build else None,
        })
    return out


_HISTORY: Dict[str, Callable] = {
    "deployment_request": _history_requests,
    "change_bundle": _history_bundles,
    "hermes": _history_hermes,
    "ci_stage": _history_ci,
    "cluster_destroy": _history_destroys,
}


def history(user, kind: Optional[str] = None, limit: int = _LIMIT) -> Dict[str, Any]:
    items: List[Dict[str, Any]] = []
    for name, collect in _HISTORY.items():
        if kind and kind != name:
            continue
        try:
            items.extend(collect(user))
        except Exception:  # noqa: BLE001
            db.session.rollback()
            logger.exception("Approvals history: %s could not be read", name)
    items.sort(key=lambda i: i.get("decidedAt") or "", reverse=True)
    return {"items": items[: max(1, min(int(limit or _LIMIT), 500))]}


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
            "destroy": "A second person with cluster_builds:execute approves every destroy; "
                       "the person who asked can withdraw it but never approve it.",
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
