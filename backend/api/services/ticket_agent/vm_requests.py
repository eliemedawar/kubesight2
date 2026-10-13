"""VMs asked for on a ticket: Hermes asks, KubeSight plans, a person approves.

A ticket says "two RHEL VMs, 4 vCPU and 8 GB, in DR" — or "a small Kubernetes
cluster for the payments team". Hermes reads it and calls
``kubesight_ticket_request_vms`` with a **VM request profile** (an admin-made
placement: vCenter, datacenter, cluster, folder, datastore, network, VM
template, SSH route, and the limits a ticket may ask within), a name, how many
and how big. Nothing is created from that call:

1. KubeSight creates a VMs-only Cluster Builder build from the profile and
   starts the OpenTofu plan — the same plan a person would make in the wizard.
2. When the plan is ready, the approval goes out with what the plan really
   says: VM names, the addresses it reserved, sizes and vCenter's warnings —
   on Telegram to the VM approver list (not the deploy approvers), and in
   KubeSight to anyone allowed to run cluster builds.
3. Approving applies exactly that plan. Rejecting, expiry, a plan that failed,
   or Hermes being asked again throws the draft away and frees its addresses.
4. When the VMs answer SSH the request is done — or, when the ticket asked for
   Kubernetes, KubeSight installs it on them (Install Kubernetes) and waits
   for the cluster. Then Hermes gets ONE follow-up with names and addresses and
   writes the requester the outcome.

Apps: a request may name **app profiles** (admin-made Inventory Helm charts
with their values) — with new VMs and Kubernetes, the charts are attached to the
build and installed as its last phase, under the same single approval; or, with
``kubesight_ticket_install_apps``, onto a cluster the Cluster Builder already
built (``vmRequest.kind == "apps"``: approval first, then the build's day-two
install, which still honours that cluster's own change-approval rule).

The state lives on the ticket task (``decision["vmRequest"]``) and the build;
:func:`advance` runs on every scheduler tick and moves each request along from
what the build and its OpenTofu job say. That is restart-safe for free: the
provisioning jobs recover themselves, and this only ever reads where they are.
"""

from __future__ import annotations

import logging
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import func

from ...audit import log_audit
from ...db import db
from ...models import ClusterBuild, TicketInterpretation, VmRequestProfile, ZohoInboundTicket
from . import schema, settings as agent_settings, telegram

logger = logging.getLogger(__name__)

ROUTE = "vm_request"
PLANNING = "vm_planning"
SIZE_KEYS = ("cpu", "memoryGb", "diskGb")
DEFAULT_SIZE = {"cpu": 2, "memoryGb": 4, "diskGb": 40}
DEFAULT_MAX_SIZE = {"cpu": 8, "memoryGb": 32, "diskGb": 200}
# Kubernetes needs every machine at least this big (a control plane's minimum),
# and a VMs-only build gives every VM the same size.
KUBERNETES_MINIMUM = {"cpu": 2, "memoryGb": 4, "diskGb": 40}
# What a profile keeps from the wizard's provisioning spec.
PLACEMENT_KEYS = (
    "vsphereConnectionId", "datacenterId", "datacenterName", "clusterId", "clusterName",
    "resourcePoolId", "resourcePoolName", "folderParentId", "folderParent", "folderMode",
    "datastoreId", "datastoreName", "networkId", "networkName", "template", "sizeMode",
    "antiAffinity",
)
# Outcomes after which nothing more happens to the request.
FINISHED = ("ready", "cluster_ready", "apps_ready", "needs_attention", "failed", "plan_failed",
            "rejected", "expired", "withdrawn")
# vmRequest.kind for a request that installs apps on a cluster that exists.
APPS_KIND = "apps"
# A started Kubernetes install leaves 'vms_ready' within seconds (the build
# turns into a draft and preflights). Still there after this long, the install
# thread died — a backend restart, or a crash — and nobody would ever be told.
INSTALL_STALL = timedelta(minutes=10)


class VmRequestError(ValueError):
    """A refusal worth showing (to Hermes through the tool, or an operator)."""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------------------
# Profiles
# ---------------------------------------------------------------------------

def _whole(value: Any, path: str) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise VmRequestError(f"{path} must be a whole number.") from exc
    if isinstance(value, float) and value != number:
        raise VmRequestError(f"{path} must be a whole number.")
    return number


def _size(raw: Any, path: str, base: Optional[Dict[str, int]] = None) -> Dict[str, int]:
    """CPU, memory and disk; keys left out come from ``base``."""
    from ..cluster_build.provisioning import templates

    raw = raw if isinstance(raw, dict) else {}
    base = base or DEFAULT_SIZE
    size = {key: _whole(raw.get(key, base[key]), f"{path}.{key}") for key in SIZE_KEYS}
    for key, unit in (("cpu", "vCPU"), ("memoryGb", "GB of memory"), ("diskGb", "GB of disk")):
        if size[key] < templates.MINIMUM_VM_SIZE[key]:
            raise VmRequestError(f"{path}: a VM needs at least {templates.MINIMUM_VM_SIZE[key]} {unit}.")
        if size[key] > templates.MAXIMUM_SIZE[key]:
            raise VmRequestError(f"{path}: {size[key]} {unit} is more than one VM may have here.")
    return size


def _describe_size(size: Dict[str, int]) -> str:
    return f"{size['cpu']} vCPU · {size['memoryGb']} GB · {size['diskGb']} GB disk"


def get_profile(profile_id: int) -> VmRequestProfile:
    row = db.session.get(VmRequestProfile, int(profile_id))
    if row is None:
        raise LookupError("VM request profile not found.")
    return row


def _template_size(placement: Dict[str, Any]) -> Optional[Dict[str, int]]:
    from ..cluster_build.provisioning import templates

    try:
        size = templates.template_size(placement.get("template") or {})
    except ValueError:
        return None
    return {key: size[key] for key in SIZE_KEYS}


def _apply_profile(row: VmRequestProfile, payload: Dict[str, Any]) -> None:
    from ..cluster_build.provisioning import service as provisioning_service
    from .. import ssh_profile_service

    name = str(payload.get("name", row.name or "")).strip()
    if not name or len(name) > 120:
        raise VmRequestError("Name the profile (at most 120 characters).")
    clash = VmRequestProfile.query.filter(
        func.lower(VmRequestProfile.name) == name.lower(), VmRequestProfile.id != (row.id or -1)
    ).first()
    if clash:
        raise VmRequestError(f"A VM request profile called {clash.name} already exists.")
    row.name = name
    if "description" in payload:
        row.description = str(payload.get("description") or "").strip()[:1000] or None
    if "enabled" in payload:
        row.enabled = bool(payload.get("enabled"))

    if "placement" in payload or not row.placement_json:
        raw = payload.get("placement")
        if not isinstance(raw, dict):
            raise VmRequestError("Choose where in vCenter the VMs go.")
        # The wizard's own checks: a transient VMs-only build, never saved.
        probe = ClusterBuild(name="profile-check", machine_source="vmware", vms_only=True)
        try:
            spec = provisioning_service.normalize_spec(
                probe, {**raw, "vmCount": 1, "sizes": {"vm": DEFAULT_SIZE}}
            )
        except ValueError as exc:
            raise VmRequestError(str(exc)) from exc
        finally:
            if probe in db.session:
                db.session.expunge(probe)
        row.placement_json = {key: spec.get(key) for key in PLACEMENT_KEYS}

    if "connectionProfileId" in payload or not row.connection_profile_id:
        try:
            route_id = int(payload.get("connectionProfileId") or 0)
        except (TypeError, ValueError):
            route_id = 0
        if not route_id:
            raise VmRequestError("Choose the SSH route KubeSight reaches the new VMs by.")
        try:
            ssh_profile_service.get_profile(route_id)
        except LookupError as exc:
            raise VmRequestError(str(exc)) from exc
        row.connection_profile_id = route_id

    if "buildProfileId" in payload:
        from ...models import BuildProfile

        try:
            source_id = int(payload.get("buildProfileId") or 0)
        except (TypeError, ValueError):
            source_id = 0
        if source_id and db.session.get(BuildProfile, source_id) is None:
            raise VmRequestError("That source profile does not exist.")
        row.build_profile_id = source_id or None

    if "maxVms" in payload or not row.max_vms:
        from ..cluster_build.provisioning import templates

        max_vms = _whole(payload.get("maxVms", row.max_vms or 3), "maxVms")
        if not 1 <= max_vms <= templates.MAX_VMS:
            raise VmRequestError(f"maxVms must be between 1 and {templates.MAX_VMS}.")
        row.max_vms = max_vms
    if "allowKubernetes" in payload:
        row.allow_kubernetes = bool(payload.get("allowKubernetes"))

    if (row.placement_json or {}).get("sizeMode") == "template":
        # Every clone keeps the template's size: there is nothing to choose.
        kept = _template_size(row.placement_json)
        if kept is None:
            raise VmRequestError("vCenter did not report this VM template's size, so it cannot be kept.")
        row.default_size_json = kept
        row.max_size_json = kept
    else:
        if "maxSize" in payload or not row.max_size_json:
            row.max_size_json = _size(payload.get("maxSize") or row.max_size_json, "maxSize", DEFAULT_MAX_SIZE)
        if "defaultSize" in payload or not row.default_size_json:
            row.default_size_json = _size(payload.get("defaultSize") or row.default_size_json, "defaultSize")
        over = [k for k in SIZE_KEYS if row.default_size_json[k] > row.max_size_json[k]]
        if over:
            raise VmRequestError("The default size cannot be larger than the most a ticket may ask for.")
    if row.allow_kubernetes:
        small = [k for k in SIZE_KEYS if row.max_size_json[k] < KUBERNETES_MINIMUM[k]]
        if small:
            raise VmRequestError(
                "A profile that allows Kubernetes must allow VMs of at least "
                f"{_describe_size(KUBERNETES_MINIMUM)} (a control plane's minimum)."
            )


def create_profile(payload: Dict[str, Any], *, created_by: str = "") -> Dict[str, Any]:
    row = VmRequestProfile(created_by=created_by or None, enabled=True)
    _apply_profile(row, payload or {})
    db.session.add(row)
    db.session.commit()
    return serialize_profile(row)


def update_profile(profile_id: int, payload: Dict[str, Any]) -> Dict[str, Any]:
    row = get_profile(profile_id)
    try:
        _apply_profile(row, payload or {})
    except VmRequestError:
        db.session.rollback()
        raise
    db.session.commit()
    return serialize_profile(row)


def delete_profile(profile_id: int) -> Dict[str, Any]:
    row = get_profile(profile_id)
    summary = {"id": row.id, "name": row.name}
    db.session.delete(row)
    db.session.commit()
    return summary


def _where(placement: Dict[str, Any]) -> str:
    from ...models import VSphereConnection

    connection = db.session.get(VSphereConnection, placement.get("vsphereConnectionId") or 0)
    folder = placement.get("folderParent") or ""
    parts = [
        connection.name if connection else None,
        placement.get("datacenterName"),
        placement.get("clusterName"),
        f"folder {folder}" if folder else None,
        placement.get("datastoreName"),
        placement.get("networkName"),
    ]
    return " / ".join(p for p in parts if p)


def serialize_profile(row: VmRequestProfile) -> Dict[str, Any]:
    from ...models import BuildProfile, SshConnectionProfile

    placement = dict(row.placement_json or {})
    template = placement.get("template") or {}
    route = db.session.get(SshConnectionProfile, row.connection_profile_id) if row.connection_profile_id else None
    source = db.session.get(BuildProfile, row.build_profile_id) if row.build_profile_id else None
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description or "",
        "enabled": bool(row.enabled),
        "placement": placement,
        "where": _where(placement),
        "templateName": template.get("name") or template.get("path") or "",
        "os": template.get("guestFullName") or template.get("guestId") or "",
        "sizeMode": placement.get("sizeMode") or "custom",
        "connectionProfileId": row.connection_profile_id,
        "connectionProfileName": route.name if route else None,
        "buildProfileId": row.build_profile_id,
        "buildProfileName": source.name if source else None,
        "defaultSize": row.default_size_json or dict(DEFAULT_SIZE),
        "maxSize": row.max_size_json or dict(DEFAULT_MAX_SIZE),
        "maxVms": row.max_vms,
        "allowKubernetes": bool(row.allow_kubernetes),
        "createdBy": row.created_by,
        "updatedAt": row.updated_at.isoformat() if row.updated_at else None,
    }


def list_profiles() -> List[Dict[str, Any]]:
    return [serialize_profile(r) for r in VmRequestProfile.query.order_by(VmRequestProfile.name.asc()).all()]


def catalog() -> List[Dict[str, Any]]:
    """The profiles Hermes may choose from — enabled ones, in its words."""
    if not agent_settings.vm_requests_enabled():
        return []
    out = []
    for row in VmRequestProfile.query.filter_by(enabled=True).order_by(VmRequestProfile.name.asc()).all():
        data = serialize_profile(row)
        fixed = data["sizeMode"] == "template"
        out.append({
            "profile": data["name"],
            "description": data["description"],
            "where": data["where"],
            "os": data["os"] or data["templateName"],
            "sizes": (
                f"fixed: every VM is {_describe_size(data['defaultSize'])} (do not pass size)" if fixed
                else f"default {_describe_size(data['defaultSize'])}; at most {_describe_size(data['maxSize'])} per VM"
            ),
            "maxVms": data["maxVms"],
            "kubernetes": data["allowKubernetes"],
        })
    return out


def app_catalog() -> List[Dict[str, Any]]:
    """App profiles Hermes may name — offered when VM requests are switched on
    (the same approvers decide both)."""
    from ..cluster_build import app_profiles

    if not agent_settings.vm_requests_enabled():
        return []
    return app_profiles.catalog()


def _app_profiles(names: Any):
    from ..cluster_build import app_profiles

    try:
        return app_profiles.named(names)
    except ValueError as exc:
        raise VmRequestError(str(exc), 422) from exc


# ---------------------------------------------------------------------------
# The request (kubesight_ticket_request_vms)
# ---------------------------------------------------------------------------

def _shape_counts(raw: Any, count: Optional[int]) -> Optional[Dict[str, int]]:
    """Which Kubernetes roles the VMs take, from a named shape or counts."""
    from ..cluster_build.provisioning import templates

    if raw in (None, {}, "", False):
        return None
    if not isinstance(raw, dict):
        raise VmRequestError("kubernetes must be an object: {shape} or {counts}, plus an optional version.")
    shape = str(raw.get("shape") or "").strip().lower()
    counts_raw = raw.get("counts")
    if shape and counts_raw:
        raise VmRequestError("kubernetes: give a shape OR counts, not both.")
    if shape:
        found = next((t for t in templates.BUILTIN_TEMPLATES if t["id"] == shape), None)
        if found is None:
            raise VmRequestError(
                "kubernetes.shape must be one of "
                + ", ".join(t["id"] for t in templates.BUILTIN_TEMPLATES) + "."
            )
        counts = dict(found["spec"]["counts"])
    elif isinstance(counts_raw, dict):
        aliases = {"loadbalancer": ("loadbalancer", "loadBalancers", "loadbalancers"),
                   "controlPlane": ("controlPlane", "controlPlanes"),
                   "worker": ("worker", "workers")}
        counts = {}
        for role, names in aliases.items():
            value = next((counts_raw[n] for n in names if n in counts_raw), 0)
            counts[role] = _whole(value or 0, f"kubernetes.counts.{role}")
    else:
        raise VmRequestError("kubernetes needs a shape (lab, small, standard-ha) or counts.")
    total = sum(counts.values())
    if count is not None and count != total:
        raise VmRequestError(
            f"count is {count}, but that Kubernetes shape uses {total} VMs. Leave count out "
            "or make them agree."
        )
    try:
        return templates.counts_for_vms(counts, total)
    except ValueError as exc:
        raise VmRequestError(f"kubernetes: {exc}") from exc


def _k8s_version(raw: Any) -> str:
    from ..cluster_build import k8s_versions

    wanted = str(raw or "").strip().lstrip("v")
    offered = k8s_versions.supported_versions()
    if not wanted:
        if not offered:
            raise VmRequestError("No Kubernetes version is enabled in Cluster Builder.")
        return offered[0]
    if wanted.count(".") == 1:  # "1.32" → the newest patch offered for it
        match = next((v for v in offered if v.startswith(wanted + ".")), None)
        if match:
            return match
    try:
        return k8s_versions.validate_version(wanted)
    except ValueError as exc:
        raise VmRequestError(
            f"kubernetes.version: {exc} Offered: {', '.join(offered) or 'none'}."
        ) from exc


def _profile_named(name: Any) -> VmRequestProfile:
    wanted = str(name or "").strip()
    if not wanted:
        raise VmRequestError("profile is required: copy one exactly from vmProfiles.")
    row = VmRequestProfile.query.filter(func.lower(VmRequestProfile.name) == wanted.lower()).first()
    if row is None or not row.enabled:
        names = [p["profile"] for p in catalog()]
        raise VmRequestError(
            f"There is no VM request profile called {wanted!r}. Copy one exactly from vmProfiles: "
            + (", ".join(names) if names else "none are set up — set the ticket to impediment.")
            , 422,
        )
    return row


# What Hermes must not fall back to when the ticket names no VMs: the VMs
# would carry the ticket's number for their whole life.
ASK_FOR_A_NAME = (
    "The ticket must say what to call the VMs. Set it to impediment and ask the requester "
    "for a name (lowercase letters, digits and hyphens; the VMs become <name>-vm-1, "
    "<name>-vm-2 …) — never use the ticket number."
)


def _ticket_names(ticket: Optional[ZohoInboundTicket]) -> set:
    """The ticket's own number and id, as a VM name would spell them."""
    names = set()
    for value in (getattr(ticket, "ticket_number", None), getattr(ticket, "ticket_id", None)):
        text = re.sub(r"[^a-z0-9]+", "-", str(value or "").strip().lower()).strip("-")
        if text:
            names.add(text)
    return names


def _name(raw: Any, ticket: Optional[ZohoInboundTicket] = None) -> str:
    from ..cluster_build.provisioning import service as provisioning_service

    name = str(raw or "").strip().lower()
    if not name:
        raise VmRequestError(f"name is required. {ASK_FOR_A_NAME}", 422)
    if name in _ticket_names(ticket):
        raise VmRequestError(f"name: {name} is the ticket's number. {ASK_FOR_A_NAME}", 422)
    try:
        provisioning_service.validate_cluster_name(name)
    except ValueError as exc:
        raise VmRequestError(f"name: {exc}") from exc
    taken = ClusterBuild.query.filter(
        func.lower(ClusterBuild.name) == name, ClusterBuild.status != "destroyed"
    ).first()
    if taken is not None:
        raise VmRequestError(
            f"name: a Cluster Builder build called {name} already exists (#{taken.id}, {taken.status}). "
            "Set the ticket to impediment and ask the requester for another name.",
            409,
        )
    return name


def parse(arguments: Dict[str, Any], ticket: Optional[ZohoInboundTicket] = None) -> Dict[str, Any]:
    """The request, checked against its profile. Raises VmRequestError."""
    from ..cluster_build.provisioning import templates

    profile = _profile_named(arguments.get("profile"))
    placement = dict(profile.placement_json or {})
    fixed = placement.get("sizeMode") == "template"

    raw_count = arguments.get("count")
    count = _whole(raw_count, "count") if raw_count not in (None, "") else None
    counts = _shape_counts(arguments.get("kubernetes"), count)
    if counts is not None:
        if not profile.allow_kubernetes:
            raise VmRequestError(
                f"Profile {profile.name} creates plain VMs only; Kubernetes cannot be installed "
                "from a ticket there.", 422,
            )
        count = sum(counts.values())
    if count is None:
        raise VmRequestError("count is required: how many VMs (or a kubernetes shape).")
    if count < 1:
        raise VmRequestError("count must be at least 1.")
    limit = min(int(profile.max_vms or 1), templates.MAX_VMS)
    if count > limit:
        raise VmRequestError(
            f"Profile {profile.name} allows at most {limit} VM{'' if limit == 1 else 's'} per ticket; "
            f"this asks for {count}.", 422,
        )

    default = profile.default_size_json or dict(DEFAULT_SIZE)
    ceiling = profile.max_size_json or dict(DEFAULT_MAX_SIZE)
    raw_size = arguments.get("size")
    if fixed:
        size = dict(default)
        if isinstance(raw_size, dict) and any(
            raw_size.get(k) not in (None, "") and _whole(raw_size[k], f"size.{k}") != size[k]
            for k in SIZE_KEYS
        ):
            raise VmRequestError(
                f"Profile {profile.name} clones every VM at its template's own size "
                f"({_describe_size(size)}) and cannot change it. Leave size out, or say so on the ticket.",
                422,
            )
    else:
        size = _size(raw_size, "size", default)
        over = [k for k in SIZE_KEYS if size[k] > ceiling[k]]
        if over:
            raise VmRequestError(
                f"Profile {profile.name} allows at most {_describe_size(ceiling)} per VM; "
                f"this asks for {_describe_size(size)}.", 422,
            )
    kubernetes = None
    if counts is not None:
        small = [k for k in SIZE_KEYS if size[k] < KUBERNETES_MINIMUM[k]]
        if small:
            raise VmRequestError(
                f"Kubernetes needs every VM at least {_describe_size(KUBERNETES_MINIMUM)}; "
                f"this asks for {_describe_size(size)}.", 422,
            )
        asked = (arguments.get("kubernetes") or {}).get("version")
        kubernetes = {
            "counts": counts,
            "version": _k8s_version(asked),
            # Not named on the ticket: the VMs' own Kubernetes is used when
            # their template carries a version the Cluster Builder can build.
            "versionAsked": bool(str(asked or "").strip()),
        }
    app_rows = _app_profiles(arguments.get("apps"))
    if app_rows and kubernetes is None:
        raise VmRequestError("apps need a Kubernetes cluster: add kubernetes, or leave apps out.")
    try:
        confidence = str(arguments.get("confidence") or "").strip().capitalize()
        if confidence not in schema.CONFIDENCES:
            raise schema.ContractError("confidence must be High, Medium or Low.")
        understanding = schema.opt_str(arguments.get("understanding"), "understanding", schema.MAX_UNDERSTANDING)
        if not understanding:
            raise schema.ContractError("understanding is required.")
        concerns = schema.str_list(arguments.get("concerns"), "concerns")
        reasons = schema.str_list(arguments.get("reasons"), "reasons")
    except schema.ContractError as exc:
        raise VmRequestError(str(exc)) from exc
    return {
        "profile": profile,
        "name": _name(arguments.get("name"), ticket),
        "count": count,
        "size": size,
        "kubernetes": kubernetes,
        "apps": app_rows,
        "confidence": confidence,
        "understanding": understanding,
        "concerns": concerns,
        "reasons": reasons,
    }


def _vm(task: TicketInterpretation) -> Dict[str, Any]:
    return dict((task.decision or {}).get("vmRequest") or {})


def _save(task: TicketInterpretation, vm: Dict[str, Any], commit: bool = True) -> None:
    task.decision = {**(task.decision or {}), "vmRequest": vm}
    if commit:
        db.session.commit()


def _open_vm_task(ticket: ZohoInboundTicket) -> Optional[TicketInterpretation]:
    for task in (
        TicketInterpretation.query.filter_by(ticket_record_id=ticket.id, route=ROUTE)
        .order_by(TicketInterpretation.id.desc()).limit(20).all()
    ):
        vm = _vm(task)
        if task.status in (PLANNING, "awaiting_approval", "deciding", "executed") and vm.get("phase") not in FINISHED:
            return task
    return None


def open_request(ticket: ZohoInboundTicket) -> Optional[TicketInterpretation]:
    """The ticket's VM request that is still on its way, if any."""
    return _open_vm_task(ticket)


def request(record_id: Any, arguments: Dict[str, Any], user=None) -> Dict[str, Any]:
    """kubesight_ticket_request_vms: make the draft and its plan; approval follows."""
    from . import engine
    from ..cluster_build import service as build_service
    from ..cluster_build.provisioning import service as provisioning_service

    if not agent_settings.vm_requests_enabled():
        raise engine.AgentError(
            "VM requests from tickets are switched off in KubeSight. Set the ticket to impediment "
            "and say the DevOps team will create the VMs.",
            409,
        )
    ticket = engine._ticket(record_id)
    try:
        text = schema.comment(arguments.get("comment"))
        on_approve = schema.comment(arguments.get("commentOnApprove"), "commentOnApprove")
    except schema.ContractError as exc:
        raise engine.AgentError(str(exc), 400)
    try:
        req = parse(arguments, ticket)
    except VmRequestError as exc:
        raise engine.AgentError(
            f"Refused: {exc} Fix the arguments, or set the ticket to impediment and ask the requester.",
            exc.status,
        )
    waiting = _open_vm_task(ticket)
    if waiting is not None:
        raise engine.AgentError(
            f"Task #{waiting.id} is already a VM request for this ticket ({_vm(waiting).get('phase')}).", 409
        )
    approval = TicketInterpretation.query.filter_by(
        ticket_record_id=ticket.id, status="awaiting_approval"
    ).first()
    if approval is not None:
        raise engine.AgentError(f"Task #{approval.id} is already waiting for approval on this ticket.", 409)

    profile: VmRequestProfile = req["profile"]
    number = ticket.ticket_number or ticket.ticket_id
    actor = f"hermes · {number}"[:120]
    payload = {
        "name": req["name"],
        "machineSource": "vmware",
        "vmsOnly": True,
        "templateId": "custom",
        "k8sVersion": (req["kubernetes"] or {}).get("version") or _k8s_version(None),
        "connectionProfileId": profile.connection_profile_id,
        "buildProfileId": profile.build_profile_id,
        "provisioning": {
            **(profile.placement_json or {}),
            "vmCount": req["count"],
            "sizes": {"vm": req["size"]},
        },
    }
    try:
        created = build_service.create_build(payload, created_by=actor, user=user)
    except (ValueError, LookupError, PermissionError) as exc:
        db.session.rollback()
        raise engine.AgentError(
            f"KubeSight could not prepare the VMs from profile {profile.name}: {exc} "
            "Set the ticket to impediment and say the DevOps team will look.",
            422,
        )
    build = db.session.get(ClusterBuild, created["id"])
    if req["apps"]:
        # The charts ride with the build: installed as its last phase, once
        # Kubernetes is up, under the one approval of this request.
        from ..cluster_build import app_profiles
        from ..cluster_build import apps as build_apps

        try:
            build_apps.append_apps(build, app_profiles.items_for(req["apps"]), actor=actor,
                                   profile_ids=[row.id for row in req["apps"]])
            db.session.commit()
        except build_apps.AppsError as exc:
            db.session.rollback()
            _delete_draft(build.id)
            raise engine.AgentError(f"Refused: the apps cannot be installed: {exc}", 422)
    try:
        job = provisioning_service.request_create_plan(build, actor=actor, user=user)
    except ValueError as exc:
        db.session.rollback()
        _delete_draft(build.id)
        raise engine.AgentError(
            f"KubeSight could not plan the VMs: {exc} Set the ticket to impediment and say the "
            "DevOps team will look.",
            422,
        )

    task = engine._current_task(ticket, user)
    vm = {
        "phase": "planning",
        "profileId": profile.id,
        "profile": profile.name,
        "where": _where(profile.placement_json or {}),
        "os": ((profile.placement_json or {}).get("template") or {}).get("guestFullName")
        or ((profile.placement_json or {}).get("template") or {}).get("name"),
        "name": req["name"],
        "count": req["count"],
        "size": req["size"],
        "kubernetes": req["kubernetes"],
        "buildId": build.id,
        "jobId": job.id,
        "requestedByUserId": getattr(user, "id", None),
    }
    if req["apps"]:
        from ..cluster_build import app_profiles

        vm["apps"] = app_profiles.describe(req["apps"])
    task.decision = {
        "vmRequest": vm,
        "commentOnApprove": on_approve,
        "confidence": req["confidence"],
        "understanding": req["understanding"],
        "concerns": req["concerns"],
    }
    task.confidence = req["confidence"]
    task.understanding = req["understanding"]
    task.reasons = (req["reasons"] + req["concerns"])[:10] or None
    task.route, task.status = ROUTE, PLANNING
    task.comment = text
    task.error = None
    task.finished_at = None
    db.session.commit()
    engine._post(ticket, text)
    log_audit(
        "ticket_agent_vm_requested",
        actor=user,
        target_type="ticket_interpretation",
        target_id=str(task.id),
        details={"ticket": task.ticket_number, "profile": profile.name, "name": req["name"],
                 "count": req["count"], "size": req["size"], "buildId": build.id, "jobId": job.id,
                 "kubernetes": bool(req["kubernetes"])},
    )
    # Under TESTING the plan is already made: hand it to the approvers now.
    _advance_one(task)
    db.session.refresh(task)
    return {
        "requested": True,
        "taskId": task.id,
        "buildId": build.id,
        "vmNames": [f"{req['name']}-vm-{i}" for i in range(1, req["count"] + 1)],
        "status": _vm(task).get("phase"),
        "note": (
            "Your comment is posted. Nothing is created yet: KubeSight is making the OpenTofu plan, "
            "then a VM approver approves it. You get ONE follow-up when the VMs are ready (with their "
            "addresses), or when the request is rejected, expires or fails. Do not set the ticket to "
            "done yourself now."
        ),
    }


def _target_build(raw: Any) -> ClusterBuild:
    """The Cluster Builder build behind a cluster a ticket names."""
    wanted = str(raw or "").strip()
    if not wanted:
        raise VmRequestError("cluster is required: the name of a cluster the Cluster Builder built.")
    build = (
        ClusterBuild.query.filter(
            (func.lower(ClusterBuild.name) == wanted.lower())
            | (ClusterBuild.result_cluster_id == wanted)
        )
        .filter(ClusterBuild.status != "destroyed")
        .order_by(ClusterBuild.id.desc())
        .first()
    )
    if build is None or not build.result_cluster_id:
        raise VmRequestError(
            f"There is no cluster built by the Cluster Builder called {wanted!r}. Apps can be "
            "installed from a ticket only on those.", 422,
        )
    if build.status != "completed":
        raise VmRequestError(
            f"Cluster {build.name} is {build.status.replace('_', ' ')} right now; apps can be "
            "added once it is completed.", 409,
        )
    return build


def request_apps(record_id: Any, arguments: Dict[str, Any], user=None) -> Dict[str, Any]:
    """kubesight_ticket_install_apps: apps onto a cluster that exists; approval first."""
    from . import engine

    if not agent_settings.vm_requests_enabled():
        raise engine.AgentError(
            "App requests from tickets are switched off in KubeSight (with VM requests). Set the "
            "ticket to impediment and say the DevOps team will install it.",
            409,
        )
    ticket = engine._ticket(record_id)
    try:
        text = schema.comment(arguments.get("comment"))
        on_approve = schema.comment(arguments.get("commentOnApprove"), "commentOnApprove")
        confidence = str(arguments.get("confidence") or "").strip().capitalize()
        if confidence not in schema.CONFIDENCES:
            raise schema.ContractError("confidence must be High, Medium or Low.")
        understanding = schema.opt_str(arguments.get("understanding"), "understanding", schema.MAX_UNDERSTANDING)
        if not understanding:
            raise schema.ContractError("understanding is required.")
        concerns = schema.str_list(arguments.get("concerns"), "concerns")
        reasons = schema.str_list(arguments.get("reasons"), "reasons")
    except schema.ContractError as exc:
        raise engine.AgentError(str(exc), 400)
    try:
        build = _target_build(arguments.get("cluster"))
        rows = _app_profiles(arguments.get("apps"))
        if not rows:
            raise VmRequestError("apps is required: one or more names copied from appProfiles.")
        taken = {(a.namespace, a.release_name) for a in build.apps}
        clash = [r.release_name for r in rows if (r.namespace, r.release_name) in taken]
        if clash:
            raise VmRequestError(
                f"Cluster {build.name} already has {', '.join(clash)} from its build.", 409,
            )
    except VmRequestError as exc:
        raise engine.AgentError(
            f"Refused: {exc} Fix the arguments, or set the ticket to impediment and ask the requester.",
            exc.status,
        )
    waiting = _open_vm_task(ticket)
    if waiting is not None:
        raise engine.AgentError(
            f"Task #{waiting.id} is already a VM or app request for this ticket ({_vm(waiting).get('phase')}).", 409
        )
    approval = TicketInterpretation.query.filter_by(
        ticket_record_id=ticket.id, status="awaiting_approval"
    ).first()
    if approval is not None:
        raise engine.AgentError(f"Task #{approval.id} is already waiting for approval on this ticket.", 409)

    from ..cluster_build import app_profiles

    task = engine._current_task(ticket, user)
    vm = {
        "kind": APPS_KIND,
        "phase": "awaiting_approval",
        "name": build.name,
        "buildId": build.id,
        "clusterId": build.result_cluster_id,
        "apps": app_profiles.describe(rows),
        "requestedByUserId": getattr(user, "id", None),
    }
    task.decision = {
        "vmRequest": vm,
        "commentOnApprove": on_approve,
        "confidence": confidence,
        "understanding": understanding,
        "concerns": concerns,
    }
    task.confidence, task.understanding = confidence, understanding
    task.reasons = (reasons + concerns)[:10] or None
    task.route, task.status = ROUTE, PLANNING
    task.comment, task.error, task.finished_at = text, None, None
    db.session.commit()
    engine._post(ticket, text)
    log_audit(
        "ticket_agent_apps_requested",
        actor=user,
        target_type="ticket_interpretation",
        target_id=str(task.id),
        details={"ticket": task.ticket_number, "buildId": build.id, "cluster": build.result_cluster_id,
                 "apps": [a["profile"] for a in vm["apps"]]},
    )
    _send_for_approval(task)
    db.session.refresh(task)
    return {
        "requested": True,
        "taskId": task.id,
        "cluster": build.result_cluster_id,
        "apps": [f"{a['release']} in {a['namespace']}" for a in vm["apps"]],
        "status": _vm(task).get("phase"),
        "note": (
            "Your comment is posted. Nothing is installed yet: an approver approves first. If the "
            "cluster itself needs approval for changes, its approvers are asked after that. You get "
            "ONE follow-up when the apps are installed, or when the request is rejected, expires or "
            "fails. Do not set the ticket to done yourself now."
        ),
    }


# ---------------------------------------------------------------------------
# Moving requests along (every scheduler tick)
# ---------------------------------------------------------------------------

def _machines(build: ClusterBuild) -> List[Dict[str, Any]]:
    """The VMs as the build has them: names, addresses, sizes, roles."""
    spec = build.provisioning_json or {}
    out = []
    for machine in spec.get("machines") or []:
        out.append({
            "name": machine.get("name"),
            "address": machine.get("ip"),
            "cpu": machine.get("cpu"),
            "memoryGb": machine.get("memoryGb"),
            "diskGb": machine.get("diskGb"),
            "role": machine.get("role"),
        })
    return out


def _latest_job(build_id: int):
    from ..cluster_build.provisioning import jobs

    return jobs.open_job(build_id) or (jobs.jobs_for(build_id) or [None])[0]


def advance() -> int:
    """Move every VM request one step. Returns how many changed."""
    from sqlalchemy import Text, cast, or_

    changed = 0
    # Only requests something can still happen to: planning, executing, or a
    # finished one whose draft build still has to be deleted (cleanup flag).
    # Reading every request of the route reloaded up to 200 rows a tick.
    decision = cast(TicketInterpretation.decision, Text)
    rows = (
        TicketInterpretation.query.filter(
            TicketInterpretation.route == ROUTE,
            or_(
                # awaiting_approval: its plan may have been applied in Cluster Builder.
                TicketInterpretation.status.in_((PLANNING, "executed", "awaiting_approval")),
                decision.like('%"cleanup": true%'),
                decision.like('%"cleanup":true%'),
            ),
        )
        .order_by(TicketInterpretation.id.desc()).limit(200).all()
    )
    for task in rows:
        vm = _vm(task)
        if vm.get("phase") in FINISHED and not vm.get("cleanup"):
            continue
        try:
            if _advance_one(task):
                changed += 1
        except Exception:  # noqa: BLE001 — one request must not stall the others
            db.session.rollback()
            logger.exception("VM request on task %s could not advance", task.id)
    return changed


def _advance_one(task: TicketInterpretation) -> bool:
    # Provisioning workers commit from their own sessions: read what they wrote
    # (this task only — expiring the whole session reloaded every row).
    db.session.refresh(task)
    vm = _vm(task)
    if vm.get("cleanup"):
        return _finish_cleanup(task, vm)
    if vm.get("phase") in FINISHED:
        return False
    build = db.session.get(ClusterBuild, vm.get("buildId") or 0)
    if build is None:
        return _finish(task, "failed", error="The build was deleted in Cluster Builder before the request finished.")
    if vm.get("kind") == APPS_KIND:
        return _advance_apps(task, vm, build)

    if task.status == PLANNING:
        job = _latest_job(build.id)
        if job is not None and job.status == "planning":
            return False
        blocked = (job.plan_summary_json or {}).get("blocked") if job is not None else None
        if job is not None and job.status == "planned" and not blocked:
            _offer(task, build, job)
            return True
        error = blocked or (job.error if job is not None else None) or (
            f"the plan ended '{job.status}'" if job is not None else "no plan was made"
        )
        return _release(task, "plan_failed", note=f"The OpenTofu plan did not come out: {error}", followup={
            "type": "vm_request_failed",
            "stage": "plan",
            "error": error[:1500],
        })

    if task.status == "awaiting_approval" and _plan_applied_elsewhere(vm):
        # Someone applied this plan in Cluster Builder instead of approving the
        # ticket: that is the approval. Without this the VMs got created and
        # the request sat "awaiting approval", never installing Kubernetes.
        return _adopt_applied_plan(task, vm)
    if task.status in ("awaiting_approval", "deciding"):
        return False  # a person decides; expiry is engine.expire_approvals
    if task.status != "executed":
        return False

    status = build.status
    if status == "provisioning":
        if vm.get("phase") != "creating":
            vm["phase"] = "creating"
            _save(task, vm)
            return True
        return False
    if status == "provision_failed":
        return _finish(task, "failed", build=build, error=build.error or "OpenTofu could not create the VMs.")
    if status == "vms_ready":
        if vm.get("kubernetes") and not vm.get("installStarted"):
            vm["installStarted"] = True
            vm["installStartedAt"] = engine_now().isoformat()
            vm["phase"] = "installing"
            _save(task, vm)
            _start_install(task.id)
            return True
        if vm.get("kubernetes"):
            if _install_stalled(vm):
                _install_failed(
                    task.id,
                    "The Kubernetes install stopped before it started on the VMs (the KubeSight "
                    "backend restarted, or the install crashed — its log says why). Use Install "
                    "Kubernetes on the build to continue; its Helm apps install after it.",
                )
                return True
            return False  # the install thread is on it
        return _finish(task, "ready", build=build)
    if status in ("preflighting", "preflight_passed", "building"):
        if vm.get("phase") != "installing":
            vm["phase"] = "installing"
            _save(task, vm)
            return True
        return False
    if status == "completed":
        return _finish(task, "cluster_ready", build=build)
    if status in ("failed", "cancelled"):
        return _finish(task, "failed", build=build,
                       error=build.error or f"The Kubernetes build ended {status}.")
    if status in ("preflight_failed", "draft") and vm.get("installStarted"):
        note = vm.get("installNote") or (
            "Preflight found something a DevOps engineer has to look at before Kubernetes is installed."
        )
        if status == "draft" and not vm.get("installNote"):
            return False  # the install thread has not reported yet
        return _finish(task, "needs_attention", build=build, note=note)
    if status in ("destroying", "destroyed"):
        return _finish(task, "failed", build=build, error="The VMs were destroyed before the request finished.")
    return False


def _apps_of(build: ClusterBuild, vm: Dict[str, Any]):
    ids = set(vm.get("appIds") or [])
    return [app for app in build.apps if app.id in ids]


def _advance_apps(task: TicketInterpretation, vm: Dict[str, Any], build: ClusterBuild) -> bool:
    """An apps request after approval: wait for the charts' outcome."""
    if task.status != "executed":
        return False
    db.session.refresh(build)
    rows = _apps_of(build, vm)
    if any(app.status == "awaiting_approval" for app in rows):
        # The cluster's approvers may have decided since: take the bundle's
        # outcome now rather than on the Cluster Builder's own tick.
        from ..cluster_build import apps as build_apps

        build_apps.advance_queued()
        db.session.refresh(build)
        rows = _apps_of(build, vm)
    if not rows:
        return _finish(task, "failed", build=build, error="The charts were withdrawn in Cluster Builder.")
    statuses = {app.status for app in rows}
    if statuses == {"installed"}:
        return _finish(task, "apps_ready", build=build)
    bad = [app for app in rows if app.status in ("failed", "rejected")]
    if bad and build.status != "building":
        return _finish(task, "failed", build=build, error="; ".join(
            f"{app.release_name}: {app.error or app.status}" for app in bad
        ))
    phase = "waiting_cluster_approval" if "awaiting_approval" in statuses else "installing"
    if vm.get("phase") != phase:
        vm["phase"] = phase
        _save(task, vm)
        return True
    return False


def _approve_apps(task: TicketInterpretation, by: str) -> None:
    """Install the profiles' charts as the day-two install of that build."""
    from ...models import AppProfile, User
    from ..cluster_build import app_profiles
    from ..cluster_build import service as build_service

    vm = _vm(task)
    build = db.session.get(ClusterBuild, vm.get("buildId") or 0)
    if build is None:
        raise VmRequestError("The cluster's build is gone (deleted in Cluster Builder).", 409)
    rows = []
    for entry in vm.get("apps") or []:
        row = db.session.get(AppProfile, entry.get("profileId") or 0)
        if row is None or not row.enabled:
            raise VmRequestError(f"App profile {entry.get('profile')} was deleted or switched off.", 409)
        rows.append(row)
    # The account Hermes asked with owns it: if the cluster needs approval for
    # changes, its own approvers are asked next, in that account's name.
    requester = db.session.get(User, vm.get("requestedByUserId") or 0)
    before = {app.id for app in build.apps}
    vm.update(phase="installing", approvedBy=by)
    _save(task, vm)
    try:
        data = build_service.add_apps(
            build.id, {"apps": app_profiles.items_for(rows)}, user=requester,
            actor=f"{by} (approved {task.ticket_number or 'ticket'})"[:120],
            profile_ids=[row.id for row in rows],
        )
    except (ValueError, LookupError) as exc:
        db.session.rollback()
        raise VmRequestError(str(exc), 409) from exc
    task = db.session.get(TicketInterpretation, task.id)
    vm = _vm(task)
    vm["appIds"] = [app["id"] for app in data.get("apps") or [] if app["id"] not in before]
    if data.get("pendingApproval"):
        vm["phase"] = "waiting_cluster_approval"
        vm["bundleIds"] = data.get("bundleIds") or []
    _save(task, vm)


def engine_now() -> datetime:
    return datetime.now(timezone.utc)


def _install_stalled(vm: Dict[str, Any]) -> bool:
    raw = vm.get("installStartedAt")
    if not raw:
        return True  # started before the time was recorded, and still not moving
    try:
        started = datetime.fromisoformat(raw)
    except ValueError:
        return True
    if started.tzinfo is None:
        started = started.replace(tzinfo=timezone.utc)
    return engine_now() - started > INSTALL_STALL


def _install_failed(task_id: int, note: str) -> None:
    """Kubernetes did not go on: say why on the request, on the build page,
    and to Hermes (one follow-up) — never a request that just stops."""
    from ..cluster_build.provisioning import jobs
    from ..cluster_build.scrub import scrub

    note = scrub(note)[:1500]
    task = db.session.get(TicketInterpretation, task_id)
    vm = _vm(task)
    vm["installNote"] = note
    _save(task, vm)
    build = db.session.get(ClusterBuild, vm.get("buildId") or 0)
    if build is not None:
        # The build page shows the VM job's hand-off note under OpenTofu.
        job = next((j for j in jobs.jobs_for(build.id) if j.operation == "create"), None)
        if job is not None:
            job.progress_json = {**(job.progress_json or {}), "handoffNote": note[:1000]}
            db.session.commit()
    _finish(task, "needs_attention", build=build, note=note)


def _install_guarded(task_id: int) -> None:
    """The install, with any crash reported instead of only logged."""
    try:
        _install(task_id)
    except Exception as exc:  # noqa: BLE001
        logger.exception("Installing Kubernetes for VM request task %s crashed", task_id)
        db.session.rollback()
        try:
            _install_failed(task_id, f"Kubernetes could not be installed: {exc}")
        except Exception:  # noqa: BLE001
            db.session.rollback()
            logger.exception("Could not record the failed install of VM request task %s", task_id)


def _plan_applied_elsewhere(vm: Dict[str, Any]) -> bool:
    from ...models import ClusterProvisionJob

    if vm.get("kind") == APPS_KIND or not vm.get("jobId"):
        return False
    job = db.session.get(ClusterProvisionJob, vm["jobId"])
    return job is not None and job.status in ("applying", "connecting", "succeeded")


def _adopt_applied_plan(task: TicketInterpretation, vm: Dict[str, Any]) -> bool:
    """The plan this request waited on was applied from the build page: carry
    on as if the ticket's approval had been given — by whoever applied it."""
    from ...models import ClusterProvisionJob
    from . import engine

    job = db.session.get(ClusterProvisionJob, vm["jobId"])
    by = (job.applied_by or "Cluster Builder") if job is not None else "Cluster Builder"
    won = TicketInterpretation.query.filter_by(id=task.id, status="awaiting_approval").update(
        {"status": "executed", "decided_by": by[:120], "decided_at": engine._now(),
         "finished_at": engine._now(), "error": None},
        synchronize_session=False,
    )
    db.session.commit()
    if not won:
        return False  # an approval through the ticket got there first
    db.session.refresh(task)
    vm = _vm(task)
    vm.update(phase="creating", approvedBy=by, approvedIn="cluster_builder")
    _save(task, vm)
    ticket = db.session.get(ZohoInboundTicket, task.ticket_record_id) if task.ticket_record_id else None
    on_approve = (task.decision or {}).get("commentOnApprove")
    if ticket is not None and on_approve:
        engine._post(ticket, on_approve)
    engine._edit_telegram(task, f"✅ Applied by {by} in Cluster Builder — the VMs are being created.")
    log_audit(
        "ticket_agent_vms_approved",
        actor=None,
        target_type="ticket_interpretation",
        target_id=str(task.id),
        details={"ticket": task.ticket_number, "by": by, "buildId": vm.get("buildId"),
                 "jobId": vm.get("jobId"), "via": "cluster_builder"},
    )
    return True


def _install_in_app(app, task_id: int) -> None:
    with app.app_context():
        try:
            _install_guarded(task_id)
        finally:
            db.session.remove()


def _start_install(task_id: int) -> None:
    """Install Kubernetes on the VMs — preflight runs in the call, so off the tick."""
    from flask import current_app

    app = current_app._get_current_object()
    if app.config.get("TESTING"):
        _install_guarded(task_id)
        return
    threading.Thread(target=_install_in_app, args=(app, task_id),
                     name=f"vm-request-{task_id}-install", daemon=True).start()


def _install(task_id: int) -> None:
    from ..cluster_build.provisioning import service as provisioning_service

    task = db.session.get(TicketInterpretation, task_id)
    vm = _vm(task)
    build = db.session.get(ClusterBuild, vm.get("buildId") or 0)
    if build is None:
        return
    kubernetes = vm.get("kubernetes") or {}
    try:
        note = provisioning_service.install_kubernetes(
            build,
            {"counts": kubernetes.get("counts"),
             "k8sVersion": kubernetes.get("version") if kubernetes.get("versionAsked", True) else None},
            actor=f"hermes · {task.ticket_number}"[:120],
            user=None,
        )
    except ValueError as exc:
        db.session.rollback()
        _install_failed(task_id, f"Kubernetes could not be installed: {exc}")
        return
    task = db.session.get(TicketInterpretation, task_id)
    vm = _vm(task)
    if note:
        vm["installNote"] = note
        _save(task, vm)
        build = db.session.get(ClusterBuild, vm.get("buildId") or 0)
        _finish(task, "needs_attention", build=build, note=note)


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------

def _plan_warnings(job) -> List[str]:
    checks = (job.plan_summary_json or {}).get("checks") or []
    return [
        f"{c.get('label')}: {c.get('detail')}".strip(": ")[:300]
        for c in checks if c.get("status") in ("warn", "bad")
    ][:8]


def _offer(task: TicketInterpretation, build: ClusterBuild, job) -> None:
    """The plan is ready: send it to the VM approvers."""
    vm = _vm(task)
    summary = job.plan_summary_json or {}
    vm.update(
        phase="awaiting_approval",
        jobId=job.id,
        machines=_machines(build),
        warnings=_plan_warnings(job),
        plan={"add": summary.get("add"), "change": summary.get("change"), "destroy": summary.get("destroy")},
    )
    _save(task, vm, commit=False)
    _send_for_approval(task)
    log_audit(
        "ticket_agent_vm_plan_ready",
        actor=None,
        target_type="ticket_interpretation",
        target_id=str(task.id),
        details={"ticket": task.ticket_number, "buildId": build.id, "jobId": job.id,
                 "vms": [m["name"] for m in vm["machines"]]},
    )


def _send_for_approval(task: TicketInterpretation) -> None:
    """Open the approval: KubeSight's inbox, and Telegram to the VM approvers."""
    row = agent_settings.get_or_create()
    vm = _vm(task)
    vm["phase"] = "awaiting_approval"
    _save(task, vm, commit=False)
    task.status = "awaiting_approval"
    task.approval_nonce = secrets.token_hex(4)
    from . import engine

    task.approval_requested_at = engine._now()
    task.expires_at = task.approval_requested_at + timedelta(hours=int(row.approval_timeout_hours or 24))
    db.session.commit()
    ticket = db.session.get(ZohoInboundTicket, task.ticket_record_id) if task.ticket_record_id else None
    if agent_settings.telegram_ready(row):
        approvers = agent_settings.vm_approvers(row)
        buttons = [[
            {"text": "✅ Approve and install" if vm.get("kind") == APPS_KIND else "✅ Approve and create",
             "callback_data": f"ka:{task.id}:a:{task.approval_nonce}"},
            {"text": "❌ Reject", "callback_data": f"ka:{task.id}:r:{task.approval_nonce}"},
        ]] if approvers else None
        body = approval_text(task, ticket)
        if not approvers:
            body += "\n\nNo VM approvers are set for Telegram — approve it in KubeSight."
        try:
            message_id = telegram.send_message(agent_settings.bot_token(row), row.telegram_chat_id, body,
                                               buttons=buttons)
            task.telegram_chat_id, task.telegram_message_id = row.telegram_chat_id, message_id
        except telegram.TelegramError as exc:
            task.error = f"Telegram: {exc} — approve it in KubeSight instead."
        db.session.commit()


def _app_lines(apps: List[Dict[str, Any]]) -> List[str]:
    lines = []
    for app in apps or []:
        keys = ", ".join(app.get("valueKeys") or []) or "chart defaults"
        lines.append(
            f"• {app.get('release')} → {app.get('namespace')}: {app.get('chart')} {app.get('version')}"
            + (f", from {app['valuesFile']}" if app.get("valuesFile") else "")
            + f" (profile {app.get('profile')}; values set: {keys})"
        )
    return lines


def _apps_approval_text(task: TicketInterpretation, ticket: Optional[ZohoInboundTicket]) -> str:
    from . import engine

    vm = _vm(task)
    lines = [
        f"📦 {task.ticket_number or 'Ticket'} — Hermes asks to install apps",
        ((ticket.subject if ticket else "") or "").strip(),
        "",
        f"Understood: {task.understanding or '-'}",
        f"On cluster {vm.get('clusterId')} (Cluster Builder build {vm.get('name')}), with Helm:",
        *_app_lines(vm.get("apps")),
        "If the cluster needs approval for changes, its approvers are asked after you.",
        f"Confidence: {task.confidence or '-'}",
    ]
    if task.reasons:
        lines += ["", "Hermes notes:"] + [f"• {r}" for r in task.reasons]
    hours = max(1, int(((engine._aware(task.expires_at) or engine._now()) - engine._now()).total_seconds() // 3600))
    lines += ["", f"Expires in {hours}h."]
    return "\n".join(lines)


def approval_text(task: TicketInterpretation, ticket: Optional[ZohoInboundTicket]) -> str:
    from . import engine

    vm = _vm(task)
    if vm.get("kind") == APPS_KIND:
        return _apps_approval_text(task, ticket)
    machines = vm.get("machines") or []
    lines = [
        f"🖥 {task.ticket_number or 'Ticket'} — Hermes asks for new VMs",
        ((ticket.subject if ticket else "") or "").strip(),
        "",
        f"Understood: {task.understanding or '-'}",
        f"Profile: {vm.get('profile')} ({vm.get('where') or '-'})",
        f"Will create {len(machines) or vm.get('count')} VM{'' if (len(machines) or vm.get('count')) == 1 else 's'}"
        + (f" from {vm['os']}" if vm.get("os") else "") + ":",
    ]
    for machine in machines:
        lines.append(
            f"• {machine['name']}  {machine.get('address') or '-'}  "
            f"{machine.get('cpu')} vCPU · {machine.get('memoryGb')} GB · {machine.get('diskGb')} GB"
        )
    kubernetes = vm.get("kubernetes")
    if kubernetes:
        counts = kubernetes.get("counts") or {}
        version = (
            kubernetes.get("version") if kubernetes.get("versionAsked", True)
            else f"(the version already on the VMs, else {kubernetes.get('version')})"
        )
        lines.append(
            f"Then Kubernetes {version}: {counts.get('controlPlane', 0)} control plane(s), "
            f"{counts.get('worker', 0)} worker(s), {counts.get('loadbalancer', 0)} load balancer(s)."
        )
    if vm.get("apps"):
        lines.append("Then these apps, with Helm:")
        lines += _app_lines(vm["apps"])
    plan = vm.get("plan") or {}
    lines.append(f"OpenTofu plan #{vm.get('jobId')}: {plan.get('add', 0)} to add, "
                 f"{plan.get('change', 0)} to change, {plan.get('destroy', 0)} to destroy.")
    if vm.get("warnings"):
        lines += ["", "vCenter warnings:"] + [f"• {w}" for w in vm["warnings"]]
    lines.append(f"Confidence: {task.confidence or '-'}")
    if task.reasons:
        lines += ["", "Hermes notes:"] + [f"• {r}" for r in task.reasons]
    hours = max(1, int(((engine._aware(task.expires_at) or engine._now()) - engine._now()).total_seconds() // 3600))
    lines += ["", f"Expires in {hours}h."]
    return "\n".join(lines)


def describe(task: TicketInterpretation) -> str:
    vm = _vm(task)
    apps = ", ".join(f"{a.get('release')} ({a.get('profile')})" for a in vm.get("apps") or [])
    if vm.get("kind") == APPS_KIND:
        return f"install {apps} on cluster {vm.get('clusterId')}"
    count = vm.get("count") or len(vm.get("machines") or [])
    what = f"create {count} VM{'' if count == 1 else 's'} ({vm.get('name')}-vm-1…) from profile {vm.get('profile')}"
    if vm.get("kubernetes"):
        what += f" and install Kubernetes {vm['kubernetes'].get('version')} on them"
    if apps:
        what += f", then the apps {apps}"
    return what


def check_approver(task: TicketInterpretation, user) -> None:
    """Before claiming: who may approve a request Hermes made.

    Anyone with the approval permissions — including the account whose API
    token Hermes uses. Hermes asks; it has no tool to approve, so the person
    who approves is always a person, and an installation that runs Hermes on
    an administrator's token must still be able to approve its requests.
    (Kept as the one place this rule lives, should it ever tighten again.)
    """
    return None


def approve(task: TicketInterpretation, by: str, user=None) -> None:
    """Apply exactly the plan the approver saw. Raises VmRequestError."""
    from ..cluster_build.provisioning import service as provisioning_service
    from ..cluster_build.provisioning import jobs

    task_id = task.id
    vm = _vm(task)
    if vm.get("kind") == APPS_KIND:
        _approve_apps(task, by)
        return
    build = db.session.get(ClusterBuild, vm.get("buildId") or 0)
    if build is None:
        raise VmRequestError("The draft build is gone (deleted in Cluster Builder).", 409)
    if _plan_applied_elsewhere(vm):
        # Already applied from the build page: approving just agrees with it.
        vm.update(phase="creating", approvedBy=by)
        _save(task, vm)
        return
    job = jobs.open_job(build.id)
    if job is None or job.id != vm.get("jobId") or job.status != "planned":
        raise VmRequestError(
            "The plan the approval was for is no longer the one waiting (it was replaced, discarded "
            "or applied in Cluster Builder).", 409,
        )
    # Marked before the apply starts: under TESTING the whole apply runs inside
    # apply_plan, and the next tick must see an approved request.
    vm.update(phase="creating", approvedBy=by)
    _save(task, vm)
    try:
        provisioning_service.apply_plan(
            build, job, actor=f"{by} (approved {task.ticket_number or 'ticket'})"[:120], user=user
        )
    except ValueError as exc:
        db.session.rollback()
        task = db.session.get(TicketInterpretation, task_id)
        vm = _vm(task)
        vm.update(phase="awaiting_approval", approvedBy=None)
        _save(task, vm)
        raise VmRequestError(str(exc), 409) from exc


# ---------------------------------------------------------------------------
# Ending a request
# ---------------------------------------------------------------------------

def _delete_draft(build_id: int) -> Optional[str]:
    """Throw away a draft nobody will apply: plan, addresses, the build.

    Returns why it was kept, or None when it is gone. VMs that exist are never
    touched — a request that got that far ends with its VMs, not without them.
    """
    from ..cluster_build import service as build_service
    from ..cluster_build.provisioning import jobs, state_store

    build = db.session.get(ClusterBuild, build_id)
    if build is None:
        return None
    if state_store.vm_instances(build.id) or state_store.has_resources(build.id):
        return "its VMs already exist"
    job = jobs.open_job(build.id)
    if job is not None and job.status in jobs.ACTIVE_STATUSES:
        return "OpenTofu is still working on it"
    try:
        build_service.delete_build(build.id)
    except (ValueError, LookupError) as exc:
        db.session.rollback()
        return str(exc)
    return None


def _release(task: TicketInterpretation, phase: str, *, note: str = "",
             followup: Optional[Dict[str, Any]] = None) -> bool:
    """End a request that never created anything, and free what it held."""
    from . import engine

    vm = _vm(task)
    build_id = vm.get("buildId") or 0
    # An apps request points at a cluster that exists: there is no draft.
    kept = None if vm.get("kind") == APPS_KIND else _delete_draft(build_id)
    vm["phase"] = phase
    vm["finished"] = True
    if note:
        vm["note"] = note[:1500]
    if vm.get("kind") == APPS_KIND:
        pass
    elif kept and kept == "OpenTofu is still working on it":
        vm["cleanup"] = True  # the plan worker is still running: next tick
    elif kept:
        vm["draftKept"] = kept
    else:
        vm["draftDeleted"] = True
    _save(task, vm, commit=False)
    if task.status in (PLANNING, "executed"):
        task.status = "impediment"
        task.finished_at = engine._now()
        task.decision_note = task.decision_note or (note[:1000] if note else None)
    db.session.commit()
    if followup is not None:
        fallback_comment = (
            "KubeSight could not prepare the VMs for this request"
            + (f": {followup.get('error')}" if followup.get("error") else ".")
            + " The DevOps team will look into it."
        )
        engine.queue_followup(task.ticket_record_id, {
            **followup,
            "request": describe(task),
            "fallback": {"outcome": "impediment", "comment": fallback_comment[:2000]},
        })
    return True


def release(task: TicketInterpretation, phase: str, note: str = "") -> None:
    """An approval that ended without running (rejected, expired, superseded)."""
    if task.route != ROUTE:
        return
    vm = _vm(task)
    if vm.get("phase") in FINISHED:
        return
    _release(task, phase, note=note)


def _finish_cleanup(task: TicketInterpretation, vm: Dict[str, Any]) -> bool:
    kept = _delete_draft(vm.get("buildId") or 0)
    if kept == "OpenTofu is still working on it":
        return False
    vm.pop("cleanup", None)
    if kept:
        vm["draftKept"] = kept
    else:
        vm["draftDeleted"] = True
    _save(task, vm)
    return True


def _finish(task: TicketInterpretation, outcome: str, *, build: Optional[ClusterBuild] = None,
            error: Optional[str] = None, note: Optional[str] = None) -> bool:
    """The VMs (and cluster) are done, or failed: one follow-up for Hermes."""
    from . import engine

    vm = _vm(task)
    if vm.get("phase") in FINISHED:
        return False
    machines = _machines(build) if build is not None else vm.get("machines") or []
    vm.update(phase=outcome, finished=True, finishedAt=engine._now().isoformat(), machines=machines)
    if error:
        vm["error"] = str(error)[:1500]
    if note:
        vm["note"] = str(note)[:1500]
    if build is not None and build.result_cluster_id:
        vm["clusterId"] = build.result_cluster_id
    if build is not None and vm.get("apps"):
        wanted = set(vm.get("appIds") or []) or {a.get("profileId") for a in vm["apps"]}
        by_id = vm.get("appIds")
        vm["appResults"] = [
            {"release": app.release_name, "namespace": app.namespace, "status": app.status,
             "error": app.error}
            for app in build.apps
            if (app.id in wanted if by_id else app.profile_id in wanted)
        ]
    _save(task, vm)
    log_audit(
        "ticket_agent_vm_request_finished",
        actor=None,
        target_type="ticket_interpretation",
        target_id=str(task.id),
        details={"ticket": task.ticket_number, "outcome": outcome, "buildId": vm.get("buildId"),
                 "error": vm.get("error")},
    )
    listed = "\n".join(
        f"{m['name']}: {m.get('address')} ({m.get('cpu')} vCPU, {m.get('memoryGb')} GB, {m.get('diskGb')} GB disk)"
        for m in machines
    )
    apps_listed = "\n".join(
        f"{a['release']} in {a['namespace']}: {a['status']}" for a in vm.get("appResults") or []
    )
    if outcome == "apps_ready":
        fallback = {"outcome": "deployed",
                    "comment": f"Installed on cluster {vm.get('clusterId')}:\n{apps_listed}"}
    elif outcome == "ready":
        fallback = {"outcome": "deployed",
                    "comment": f"The VMs are ready:\n{listed}"}
    elif outcome == "cluster_ready":
        fallback = {"outcome": "deployed",
                    "comment": f"The Kubernetes cluster {vm.get('clusterId') or vm.get('name')} is ready on:\n{listed}"
                               + (f"\nApps installed:\n{apps_listed}" if apps_listed else "")}
    elif outcome == "needs_attention":
        fallback = {"outcome": "on_hold",
                    "comment": f"The VMs are created:\n{listed}\nKubernetes is waiting for the DevOps team: "
                               f"{vm.get('note') or ''}".strip()}
    elif vm.get("kind") == APPS_KIND:
        fallback = {"outcome": "failed",
                    "comment": f"Installing the apps failed: {vm.get('error') or 'see KubeSight'}"[:2000]}
    else:
        fallback = {"outcome": "failed",
                    "comment": f"Creating the VMs failed: {vm.get('error') or 'see KubeSight'}"[:2000]}
    engine.queue_followup(task.ticket_record_id, {
        "type": "vm_request_finished",
        "result": outcome,
        "request": describe(task),
        "vms": machines,
        "clusterId": vm.get("clusterId"),
        "kubernetesVersion": (vm.get("kubernetes") or {}).get("version"),
        "apps": vm.get("appResults") or None,
        "error": vm.get("error"),
        "note": vm.get("note"),
        "fallback": fallback,
    })
    return True
