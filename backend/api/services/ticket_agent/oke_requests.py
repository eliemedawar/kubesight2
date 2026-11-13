"""Hermes on OKE (workload identity): a new cluster, or node pools on one,
from a ticket.

Rides the VM request's machinery (``vm_requests``: one task per ticket, the
Approvals inbox, one follow-up for Hermes) with two kinds of its own:

* ``oke_cluster`` — a draft OKE build (account, version, node pools, owner,
  expiry) and its plan;
* ``oke_node_pools`` — the whole new set of node pools of an OKE cluster the
  Cluster Builder built (add, resize, relabel, remove), planned like any change.

ONE approval: approving the request in KubeSight approves the plan's sha256
itself (K13: a person signed in with MFA, never the requester). Telegram cannot
approve these: the hash needs a KubeSight account with MFA.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Dict, List, Optional

from ...audit import log_audit
from ...db import db
from ...models import ClusterBuild, ClusterProvisionJob, CloudAccount, TicketInterpretation

OKE_CLUSTER_KIND = "oke_cluster"
OKE_POOLS_KIND = "oke_node_pools"
OKE_KINDS = (OKE_CLUSTER_KIND, OKE_POOLS_KIND)
DEFAULT_EXPIRY_DAYS = 30
_APPLIED = ("applying", "connecting", "succeeded")
_FAILED = ("plan_failed", "apply_failed", "connect_failed", "rejected", "discarded", "superseded")


def _vr():
    from . import vm_requests

    return vm_requests


# ---------------------------------------------------------------------------
# What Hermes is told (task message: "okeClusters")
# ---------------------------------------------------------------------------

def _wi_accounts() -> List[CloudAccount]:
    rows = CloudAccount.query.filter_by(provider="oci").order_by(CloudAccount.id).all()
    return [row for row in rows if (row.settings_json or {}).get("authMode") == "workload_identity"]


def _is_oke_build(build: ClusterBuild) -> bool:
    return (build.provisioning_json or {}).get("authMode") == "workload_identity"


def catalog() -> Dict[str, Any]:
    """The OKE account(s) a ticket may create clusters with, and the OKE
    clusters it may change node pools on. Empty when OKE is not set up."""
    from ..cluster_build.provisioning import cloud_build
    from ..cluster_build.provisioning.clouds import oci_wi

    accounts = _wi_accounts()
    if not accounts or not oci_wi.enabled():
        return {}
    clusters = []
    for build in ClusterBuild.query.filter(ClusterBuild.status == "completed").order_by(ClusterBuild.id).all():
        if not _is_oke_build(build) or not build.result_cluster_id:
            continue
        clusters.append({
            "name": build.name,
            "clusterId": build.result_cluster_id,
            "kubernetesVersion": (build.provisioning_json.get("managed") or {}).get("kubernetesVersion"),
            "nodePools": [
                {k: p[k] for k in ("name", "count", "ocpus", "memoryGb", "diskGb", "labels", "taints",
                                   "faultDomains")}
                for p in cloud_build.node_pools_for(build, None)
            ],
        })
    return {
        "accounts": [{"name": row.name, "region": row.region} for row in accounts],
        "clusters": clusters,
        "limits": {"shape": oci_wi.SHAPE, "maxPools": oci_wi.MAX_POOLS, "maxNodesPerPool": oci_wi.MAX_POOL_NODES,
                   "maxExpiryDays": oci_wi.MAX_EXPIRY_DAYS, "defaultExpiryDays": DEFAULT_EXPIRY_DAYS},
    }


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------

def _common(arguments: Dict[str, Any]) -> Dict[str, Any]:
    from . import schema

    confidence = str(arguments.get("confidence") or "").strip().capitalize()
    if confidence not in schema.CONFIDENCES:
        raise schema.ContractError("confidence must be High, Medium or Low.")
    understanding = schema.opt_str(arguments.get("understanding"), "understanding", schema.MAX_UNDERSTANDING)
    if not understanding:
        raise schema.ContractError("understanding is required.")
    return {
        "comment": schema.comment(arguments.get("comment")),
        "onApprove": schema.comment(arguments.get("commentOnApprove"), "commentOnApprove"),
        "confidence": confidence,
        "understanding": understanding,
        "concerns": schema.str_list(arguments.get("concerns"), "concerns"),
        "reasons": schema.str_list(arguments.get("reasons"), "reasons"),
    }


def _gate(ticket) -> None:
    from . import engine

    vr = _vr()
    waiting = vr._open_vm_task(ticket)
    if waiting is not None:
        raise engine.AgentError(
            f"Task #{waiting.id} is already a VM, app or OKE request for this ticket "
            f"({vr._vm(waiting).get('phase')}).", 409,
        )
    approval = TicketInterpretation.query.filter_by(ticket_record_id=ticket.id, status="awaiting_approval").first()
    if approval is not None:
        raise engine.AgentError(f"Task #{approval.id} is already waiting for approval on this ticket.", 409)


def _record(ticket, user, common: Dict[str, Any], vm: Dict[str, Any], event: str) -> TicketInterpretation:
    from . import engine

    vr = _vr()
    task = engine._current_task(ticket, user)
    task.decision = {"vmRequest": vm, "commentOnApprove": common["onApprove"],
                     "confidence": common["confidence"], "understanding": common["understanding"],
                     "concerns": common["concerns"]}
    task.confidence, task.understanding = common["confidence"], common["understanding"]
    task.reasons = (common["reasons"] + common["concerns"])[:10] or None
    task.route, task.status = vr.ROUTE, vr.PLANNING
    task.comment, task.error, task.finished_at = common["comment"], None, None
    db.session.commit()
    engine._post(ticket, common["comment"])
    log_audit(event, actor=user, target_type="ticket_interpretation", target_id=str(task.id),
              details={"ticket": task.ticket_number, "buildId": vm.get("buildId"), "jobId": vm.get("jobId")})
    return task


def _refused(exc: Exception, status: int = 422):
    from . import engine

    return engine.AgentError(
        f"Refused: {exc} Fix the arguments, or set the ticket to impediment and ask the requester.", status,
    )


def _merge_pools(current: List[Dict[str, Any]], asked: Any, remove: Any) -> List[Dict[str, Any]]:
    """The cluster's pools with the ticket's: a named pool that exists is
    changed (only the fields given), a new name is added, ``remove`` drops."""
    if asked is not None and not isinstance(asked, list):
        raise ValueError("nodePools must be a list of pools.")
    if remove is not None and not isinstance(remove, list):
        raise ValueError("removePools must be a list of pool names.")
    pools = [dict(p) for p in current]
    for item in asked or []:
        if not isinstance(item, dict) or not str(item.get("name") or "").strip():
            raise ValueError("Each pool in nodePools needs a name.")
        name = str(item["name"]).strip().lower()
        existing = next((p for p in pools if p["name"] == name), None)
        if existing is not None:
            existing.update({k: v for k, v in item.items() if v is not None})
            existing["name"] = name
        else:
            pools.append({"faultDomains": "spread", "diskGb": 50, **item, "name": name})
    gone = {str(n).strip().lower() for n in remove or []}
    unknown = gone - {p["name"] for p in pools}
    if unknown:
        raise ValueError(f"removePools names {', '.join(sorted(unknown))}, which the cluster does not have.")
    return [p for p in pools if p["name"] not in gone]


def request_node_pools(record_id: Any, arguments: Dict[str, Any], user=None) -> Dict[str, Any]:
    """kubesight_ticket_request_oke_node_pools."""
    from . import engine, schema
    from . import settings as agent_settings
    from ..cluster_build.provisioning import cloud_build
    from ..cluster_build.provisioning import service as provisioning_service
    from ..cluster_build.provisioning.clouds import oci_wi

    if not agent_settings.vm_requests_enabled():
        raise engine.AgentError("Cluster requests from tickets are switched off in KubeSight. Set the ticket "
                                "to impediment and say the DevOps team will do it.", 409)
    ticket = engine._ticket(record_id)
    try:
        common = _common(arguments)
    except schema.ContractError as exc:
        raise engine.AgentError(str(exc), 400)
    try:
        build = _vr()._target_build(arguments.get("cluster"))
        if not _is_oke_build(build):
            raise ValueError(f"{build.name} is not an OKE cluster made by workload identity.")
        current = cloud_build.node_pools_for(build, None)
        pools = oci_wi.normalize_node_pools(_merge_pools(current, arguments.get("nodePools"),
                                                         arguments.get("removePools")))
        if pools == current:
            raise ValueError("That is how the node pools are now: nothing to change.")
    except (ValueError, _vr().VmRequestError) as exc:
        raise _refused(exc, getattr(exc, "status", 422))
    _gate(ticket)
    try:
        job = provisioning_service.request_grow_plan(
            build, {"nodePools": pools}, actor=f"hermes · {ticket.ticket_number or 'ticket'}"[:120], user=user,
        )
    except ValueError as exc:
        db.session.rollback()
        raise _refused(exc, 409)
    vm = {
        "kind": OKE_POOLS_KIND, "phase": "planning", "name": build.name, "buildId": build.id,
        "clusterId": build.result_cluster_id, "jobId": job.id,
        "poolsBefore": current, "pools": pools, "requestedByUserId": getattr(user, "id", None),
    }
    task = _record(ticket, user, common, vm, "ticket_agent_oke_node_pools_requested")
    return {
        "requested": True, "taskId": task.id, "cluster": build.result_cluster_id,
        "nodePools": [f"{p['name']}: {p['count']} × {p['ocpus']} OCPU · {p['memoryGb']} GB" for p in pools],
        "status": "planning",
        "note": ("Your comment is posted. KubeSight is making the plan; then a person approves its hash in "
                 "KubeSight (never you). You get ONE follow-up when the node pools are ready, or when the "
                 "request is rejected, expires or fails. Do not set the ticket to done yourself now."),
    }


def request_cluster(record_id: Any, arguments: Dict[str, Any], user=None) -> Dict[str, Any]:
    """kubesight_ticket_request_oke_cluster."""
    from . import engine, schema
    from . import settings as agent_settings
    from ..cluster_build import service as build_service
    from ..cluster_build.provisioning import service as provisioning_service
    from ..cluster_build.provisioning.clouds import accounts as cloud_accounts
    from ..cluster_build.provisioning.clouds import oci_wi

    if not agent_settings.vm_requests_enabled():
        raise engine.AgentError("Cluster requests from tickets are switched off in KubeSight. Set the ticket "
                                "to impediment and say the DevOps team will do it.", 409)
    ticket = engine._ticket(record_id)
    try:
        common = _common(arguments)
    except schema.ContractError as exc:
        raise engine.AgentError(str(exc), 400)
    vr = _vr()
    try:
        name = vr._name(arguments.get("name"), ticket)
        accounts = _wi_accounts()
        wanted = str(arguments.get("account") or "").strip()
        account = next((a for a in accounts if a.name == wanted), None) if wanted else (
            accounts[0] if len(accounts) == 1 else None)
        if account is None:
            raise ValueError("account: copy one exactly from okeClusters.accounts."
                             if accounts else "No OKE workload-identity account is set up in KubeSight.")
        inventory = cloud_accounts.inventory(account.id, account.region)
        offered = list(inventory.get("kubernetesVersions") or [])
        asked = str(arguments.get("kubernetesVersion") or "").strip()
        if asked:
            matches = [v for v in offered if v.lstrip("v") == asked.lstrip("v")
                       or v.lstrip("v").startswith(asked.lstrip("v") + ".")]
            if not matches:
                raise ValueError(f"OKE does not offer Kubernetes {asked} here; it offers {', '.join(offered[:5])}.")
            version = matches[0]
        elif offered:
            version = offered[0]
        else:
            raise ValueError("OKE offers no Kubernetes version to this account.")
        pools = oci_wi.normalize_node_pools(arguments.get("nodePools") or [{"name": "workers", "count": 2,
                                                                             "ocpus": 2, "memoryGb": 16}])
        owner = str(arguments.get("ownerEmail") or "").strip()
        expiry = str(arguments.get("expiresOn") or "").strip() or (
            date.today() + timedelta(days=DEFAULT_EXPIRY_DAYS)).isoformat()
        total = sum(p["count"] for p in pools)
        payload = {
            "name": name, "k8sVersion": version.lstrip("v"), "machineSource": "oci", "templateId": "small",
            "vmsOnly": False, "cniPlugin": "calico", "podCidr": "10.244.0.0/16", "serviceCidr": "10.96.0.0/12",
            "provisioning": {
                "cloudAccountId": account.id, "region": account.region, "kind": "managed",
                "counts": {"loadbalancer": 0, "controlPlane": 0, "worker": total},
                "sizes": {"worker": {"cpu": pools[0]["ocpus"] * 2, "memoryGb": pools[0]["memoryGb"],
                                     "diskGb": pools[0]["diskGb"]}},
                "managed": {"kubernetesVersion": version}, "nodePools": pools,
                "ownerEmail": owner, "expiresOn": expiry,
            },
        }
    except (ValueError, vr.VmRequestError) as exc:
        raise _refused(exc, getattr(exc, "status", 422))
    _gate(ticket)
    actor = f"hermes · {ticket.ticket_number or 'ticket'}"[:120]
    try:
        created = build_service.create_build(payload, created_by=actor, user=user)
        build = db.session.get(ClusterBuild, created["id"])
        job = provisioning_service.request_create_plan(build, actor=actor, user=user)
    except ValueError as exc:
        db.session.rollback()
        raise _refused(exc, 422)
    vm = {
        "kind": OKE_CLUSTER_KIND, "phase": "planning", "name": build.name, "buildId": build.id, "jobId": job.id,
        "account": account.name, "kubernetesVersion": version, "pools": pools,
        "ownerEmail": owner, "expiresOn": expiry, "requestedByUserId": getattr(user, "id", None),
    }
    task = _record(ticket, user, common, vm, "ticket_agent_oke_cluster_requested")
    return {
        "requested": True, "taskId": task.id, "name": build.name, "kubernetesVersion": version,
        "nodePools": [f"{p['name']}: {p['count']} × {p['ocpus']} OCPU · {p['memoryGb']} GB" for p in pools],
        "expiresOn": expiry, "status": "planning",
        "note": ("Your comment is posted. KubeSight is making the OpenTofu plan; then a person approves its "
                 "hash in KubeSight (never you). You get ONE follow-up when the cluster is up and registered, "
                 "or when the request is rejected, expires or fails. Do not set the ticket to done yourself now."),
    }


# ---------------------------------------------------------------------------
# Moving along (from vm_requests._advance_one), approving, releasing
# ---------------------------------------------------------------------------

def _job(vm: Dict[str, Any]) -> Optional[ClusterProvisionJob]:
    return db.session.get(ClusterProvisionJob, vm.get("jobId") or 0)


def advance(task: TicketInterpretation, vm: Dict[str, Any], build: ClusterBuild) -> bool:
    vr = _vr()
    job = _job(vm)
    if job is None:
        return vr._finish(task, "failed", build=build, error="The plan was removed in Cluster Builder.")
    db.session.refresh(job)
    if task.status == vr.PLANNING:
        if job.status == "planning":
            return False
        blocked = (job.plan_summary_json or {}).get("blocked")
        if job.status == "awaiting_approval" and job.plan_sha256 and not blocked:
            vr._offer(task, build, job)
            return True
        error = blocked or job.error or f"the plan ended '{job.status}'"
        return vr._release(task, "plan_failed", note=f"The OpenTofu plan did not come out: {error}", followup={
            "type": "vm_request_failed", "stage": "plan", "error": str(error)[:1500],
        })
    if task.status in ("awaiting_approval", "deciding"):
        if job.status in _APPLIED:
            # Approved on the build page instead: that is the approval.
            vm.update(phase="creating", approvedBy=job.approved_by)
            vr._save(task, vm, commit=False)
            task.status = "executed"
            db.session.commit()
            return True
        return False
    if task.status != "executed":
        return False
    if job.status in ("applying", "connecting"):
        if vm.get("phase") != "creating":
            vm["phase"] = "creating"
            vr._save(task, vm)
            return True
        return False
    if job.status == "succeeded":
        db.session.refresh(build)
        return vr._finish(task, "cluster_ready" if vm.get("kind") == OKE_CLUSTER_KIND else "pools_ready",
                          build=build)
    if job.status in _FAILED:
        return vr._finish(task, "failed", build=build, error=job.error or f"the change ended '{job.status}'")
    return False


def approve(task: TicketInterpretation, by: str, user=None) -> None:
    """The ONE approval: approves the plan's sha256 as this person (MFA)."""
    from ..cluster_build.provisioning import jobs
    from ..cluster_build.provisioning import service as provisioning_service

    vr = _vr()
    vm = vr._vm(task)
    if user is None or getattr(user, "id", None) is None:
        raise vr.VmRequestError(
            "Approve this in KubeSight (Approvals): the plan's hash is approved by a person signed in with MFA.",
            409,
        )
    build = db.session.get(ClusterBuild, vm.get("buildId") or 0)
    job = _job(vm)
    if build is None or job is None:
        raise vr.VmRequestError("The build or its plan is gone (deleted in Cluster Builder).", 409)
    latest = jobs.open_job(build.id)
    if latest is None or latest.id != job.id or job.status != "awaiting_approval":
        raise vr.VmRequestError("The plan this approval was for is no longer the one waiting.", 409)
    task_id = task.id
    vm.update(phase="creating", approvedBy=by)
    vr._save(task, vm)
    try:
        provisioning_service.approve_plan_hash(
            build, job, actor=f"{by} (approved {task.ticket_number or 'ticket'})"[:120], user=user,
            note=f"Hermes request, ticket {task.ticket_number or '-'}", plan_sha256=job.plan_sha256,
        )
    except (ValueError, PermissionError) as exc:
        db.session.rollback()
        task = db.session.get(TicketInterpretation, task_id)
        vm = vr._vm(task)
        vm.update(phase="awaiting_approval", approvedBy=None)
        vr._save(task, vm)
        raise vr.VmRequestError(str(exc), 409) from exc


def discard_plan(vm: Dict[str, Any]) -> None:
    """A node-pool request that ends unapproved: its plan goes (the cluster stays)."""
    from ..cluster_build.provisioning import jobs
    from ..cluster_build.provisioning import service as provisioning_service

    build = db.session.get(ClusterBuild, vm.get("buildId") or 0)
    job = _job(vm)
    if build is None or job is None or job.status not in ("planned", "awaiting_approval"):
        return
    latest = jobs.open_job(build.id)
    if latest is None or latest.id != job.id:
        return
    try:
        provisioning_service.discard(build, job, actor="hermes request ended")
    except (ValueError, PermissionError):
        db.session.rollback()


def _pool_line(p: Dict[str, Any]) -> str:
    extra = []
    if p.get("labels"):
        extra.append("labels " + ", ".join(f"{k}={v}" for k, v in p["labels"].items()))
    if p.get("taints"):
        extra.append("taints " + ", ".join(f"{t['key']}={t.get('value', '')}:{t['effect']}" for t in p["taints"]))
    if p.get("faultDomains") == "spread":
        extra.append("spread over 3 fault domains")
    return (f"{p['name']}: {p['count']} × {p['ocpus']} OCPU · {p['memoryGb']} GB"
            + (f" ({'; '.join(extra)})" if extra else ""))


def describe(vm: Dict[str, Any]) -> str:
    pools = "; ".join(_pool_line(p) for p in vm.get("pools") or [])
    if vm.get("kind") == OKE_CLUSTER_KIND:
        return (f"create the OKE cluster {vm.get('name')} (Kubernetes {vm.get('kubernetesVersion')}, "
                f"account {vm.get('account')}) with node pools {pools}")
    return f"set the node pools of OKE cluster {vm.get('clusterId') or vm.get('name')} to {pools}"


def approval_text(task: TicketInterpretation, ticket) -> str:
    vm = _vr()._vm(task)
    job = _job(vm)
    plan = vm.get("plan") or {}
    lines = [
        (f"☸ {task.ticket_number or 'Ticket'} — Hermes asks for a new OKE cluster" if vm.get("kind") == OKE_CLUSTER_KIND
         else f"☸ {task.ticket_number or 'Ticket'} — Hermes asks to change OKE node pools"),
        ((getattr(ticket, "subject", "") or "") if ticket else "").strip(),
        "",
        f"Understood: {task.understanding or '-'}",
        f"Cluster: {vm.get('name')}" + (f" · Kubernetes {vm.get('kubernetesVersion')}" if vm.get("kubernetesVersion") else ""),
    ]
    if vm.get("kind") == OKE_CLUSTER_KIND:
        lines.append(f"Owner: {vm.get('ownerEmail') or '-'} · expires {vm.get('expiresOn')}")
    lines.append("Node pools:")
    lines.extend(f"  {_pool_line(p)}" for p in vm.get("pools") or [])
    lines.append(f"Plan: {plan.get('add', 0)} to add, {plan.get('change', 0)} to change, "
                 f"{plan.get('destroy', 0)} to destroy")
    if job is not None and job.plan_sha256:
        lines.append(f"Plan sha256: {job.plan_sha256}")
    lines.append("")
    lines.append("Approving here approves this plan's hash (MFA). Approve it in KubeSight → Approvals.")
    return "\n".join(line for line in lines if line is not None)


def finish_comment(vm: Dict[str, Any], outcome: str, build: Optional[ClusterBuild]) -> Dict[str, str]:
    pools = "\n".join(_pool_line(p) for p in vm.get("pools") or [])
    if outcome == "cluster_ready":
        endpoint = (build.control_plane_endpoint if build is not None else "") or "-"
        return {"outcome": "deployed",
                "comment": (f"The OKE cluster {vm.get('clusterId') or vm.get('name')} is up (Kubernetes "
                            f"{vm.get('kubernetesVersion')}, private API {endpoint}), registered in KubeSight.\n"
                            f"Node pools:\n{pools}")[:2000]}
    if outcome == "pools_ready":
        return {"outcome": "deployed",
                "comment": f"The node pools of {vm.get('clusterId') or vm.get('name')} are now:\n{pools}"[:2000]}
    what = "Creating the OKE cluster" if vm.get("kind") == OKE_CLUSTER_KIND else "Changing the node pools"
    return {"outcome": "failed", "comment": f"{what} failed: {vm.get('error') or 'see KubeSight'}"[:2000]}
