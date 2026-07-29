"""Where a copied PersistentVolumeClaim actually lands.

A copied PVC is only a *request* for storage. Nothing binds it in a cluster
KubeSight has just built — kubeadm ships no StorageClass — so without this
module every stateful workload the builder copies arrives Pending.

The decision is **per claim**, with a copy-wide default, because a migration
usually wants its database's existing data and is perfectly happy for a cache to
start empty:

    fresh   an NFS-backed PV at <exportRoot>/<namespace>/<claim>, whose
            directory this module creates. Empty volume.
    reuse   an NFS-backed PV pointing at the *same* server:path the source
            cluster's bound PV uses. The existing data, and therefore a second
            cluster mounting a live export — warned about, never silent.
    class   no PV; the claim keeps a storageClassName and binds dynamically
            against something already installed in the new cluster.
    none    copied untouched. It will sit Pending, and we say so.

Two rules that are not negotiable:

* **``Retain`` on every PV we author.** Deleting a copied PVC must never be able
  to delete somebody's NFS data.
* **A reused path is never written to.** ``fresh`` creates its directory and
  fixes its ownership; ``reuse`` touches nothing — the workload still running
  over there owns those permissions.

One deliberate exception to "workload copy checks only ever warn" (see
``workloads.preflight_checks``): an unreachable NFS server is a preflight
**failure**. The phase mounts the export to create directories, so it would fail
the build anyway — after the cluster is up. Failing early is strictly kinder.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

# Per-claim decisions.
FRESH = "fresh"
REUSE = "reuse"
CLASS = "class"
NONE = "none"
SOURCES = (FRESH, REUSE, CLASS, NONE)

# Decisions that make this module author a PersistentVolume.
PV_SOURCES = (FRESH, REUSE)

_NFS_PORT = 2049
_PATH_RE = re.compile(r"^/[A-Za-z0-9._/-]*$")
_MOUNT_OPTION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9=._-]*$")
_HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]*[A-Za-z0-9])?$")
_DNS_SAFE_RE = re.compile(r"[^a-z0-9-]+")


def claim_key(namespace: str, name: str) -> str:
    return f"{namespace}/{name}"


# ---------------------------------------------------------------------------
# The storage answers, as stored on the build
# ---------------------------------------------------------------------------

def normalize(payload: Any) -> Optional[Dict[str, Any]]:
    """Validate the storage section of a workload selection.

    Shape only — whether a decision is *possible* needs the claims and the
    source cluster, so that is :func:`resolve`'s job.
    """
    if not payload:
        return None
    if not isinstance(payload, dict):
        raise ValueError("workloads.storage must be an object.")

    default = str(payload.get("default") or NONE).strip()
    if default not in SOURCES:
        raise ValueError(
            f"workloads.storage.default must be one of: {', '.join(SOURCES)}."
        )

    out: Dict[str, Any] = {"default": default}

    server = str(payload.get("nfsServer") or "").strip()
    if server:
        if not _HOST_RE.match(server) or len(server) > 253:
            raise ValueError(
                "The NFS server must be a hostname or IP address."
            )
        out["nfsServer"] = server
    export_root = str(payload.get("nfsExportRoot") or "").strip().rstrip("/")
    if export_root:
        if not _PATH_RE.match(export_root) or ".." in export_root:
            raise ValueError(
                "The NFS export root must be an absolute path without '..'."
            )
        out["nfsExportRoot"] = export_root
    raw_options = str(payload.get("nfsMountOptions") or "").strip()
    if raw_options:
        options = [o for o in re.split(r"[,\s]+", raw_options) if o]
        invalid = [o for o in options if not _MOUNT_OPTION_RE.match(o)]
        if invalid:
            raise ValueError(
                f"Invalid NFS mount option(s): {', '.join(invalid)}."
            )
        out["nfsMountOptions"] = options
    storage_class = str(payload.get("storageClassName") or "").strip()
    if storage_class:
        out["storageClassName"] = storage_class

    claims = payload.get("claims") or {}
    if not isinstance(claims, dict):
        raise ValueError("workloads.storage.claims must be an object keyed by 'namespace/name'.")
    per_claim: Dict[str, Dict[str, Any]] = {}
    for key, entry in claims.items():
        if not isinstance(entry, dict):
            raise ValueError(f"workloads.storage.claims['{key}'] must be an object.")
        source = str(entry.get("source") or "").strip()
        if source and source not in SOURCES:
            raise ValueError(
                f"workloads.storage.claims['{key}'].source must be one of: "
                f"{', '.join(SOURCES)}."
            )
        decision: Dict[str, Any] = {}
        if source:
            decision["source"] = source
        if entry.get("readOnly"):
            decision["readOnly"] = True
        if decision:
            per_claim[str(key)] = decision
    if per_claim:
        out["claims"] = per_claim
    return out


def decision_for(storage: Optional[Dict[str, Any]], key: str) -> Dict[str, Any]:
    """The decision for one claim: its own, else the copy-wide default."""
    storage = storage or {}
    entry = (storage.get("claims") or {}).get(key) or {}
    return {
        "source": entry.get("source") or storage.get("default") or NONE,
        "readOnly": bool(entry.get("readOnly")),
    }


# ---------------------------------------------------------------------------
# What the source cluster already has
# ---------------------------------------------------------------------------

def _volume_kind(pv_spec: Dict[str, Any]) -> str:
    """A human name for how a PV is backed, for "cannot reuse this" messages."""
    if "nfs" in pv_spec:
        return "NFS"
    csi = pv_spec.get("csi")
    if isinstance(csi, dict):
        return f"the CSI driver {csi.get('driver') or 'unknown'}"
    for key, label in (
        ("hostPath", "a host path"),
        ("local", "a local volume"),
        ("iscsi", "iSCSI"),
        ("vsphereVolume", "a vSphere VMDK"),
        ("rbd", "Ceph RBD"),
        ("cephfs", "CephFS"),
        ("azureDisk", "an Azure disk"),
        ("awsElasticBlockStore", "an EBS volume"),
        ("gcePersistentDisk", "a GCE disk"),
    ):
        if key in pv_spec:
            return label
    return "an unrecognised volume type"


def source_volume_index(
    kubectl_json, cluster_id: str
) -> Dict[str, Dict[str, Any]]:
    """Every bound PV in the source cluster, keyed by "namespace/claim".

    One cluster-scoped read rather than one per claim. ``kubectl_json`` is
    injected so this module never imports the k8s provider (and so tests can
    hand it a dict).
    """
    payload = kubectl_json(cluster_id, ["get", "persistentvolumes", "-o", "json"])
    index: Dict[str, Dict[str, Any]] = {}
    for doc in payload.get("items") or []:
        spec = doc.get("spec") or {}
        ref = spec.get("claimRef") or {}
        if ref.get("kind") not in (None, "PersistentVolumeClaim"):
            continue
        namespace, name = ref.get("namespace"), ref.get("name")
        if not namespace or not name:
            continue
        nfs = spec.get("nfs") if isinstance(spec.get("nfs"), dict) else None
        index[claim_key(str(namespace), str(name))] = {
            "pvName": (doc.get("metadata") or {}).get("name") or "",
            "capacity": (spec.get("capacity") or {}).get("storage") or "",
            "accessModes": list(spec.get("accessModes") or []),
            "mountOptions": list(spec.get("mountOptions") or []),
            "kind": _volume_kind(spec),
            "nfs": (
                {"server": str(nfs.get("server") or ""), "path": str(nfs.get("path") or "")}
                if nfs and nfs.get("server") and nfs.get("path") else None
            ),
        }
    return index


def consumers(documents: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Which copied workload mounts each claim, and as which user.

    The fsGroup decides who can write to a freshly created NFS directory, so
    it is read from the pod spec rather than guessed.
    """
    from .workloads import _pod_specs  # local import: workloads owns pod-spec walking

    out: Dict[str, Dict[str, Any]] = {}
    for doc in documents:
        namespace = (doc.get("metadata") or {}).get("namespace") or ""
        name = (doc.get("metadata") or {}).get("name") or ""
        for pod_spec in _pod_specs(doc):
            security = pod_spec.get("securityContext") or {}
            for volume in pod_spec.get("volumes") or []:
                if not isinstance(volume, dict):
                    continue
                claim = (volume.get("persistentVolumeClaim") or {}).get("claimName")
                if not claim:
                    continue
                entry = out.setdefault(
                    claim_key(namespace, str(claim)),
                    {"workloads": [], "fsGroup": None, "runAsUser": None},
                )
                label = f"{doc.get('kind')} {name}"
                if label not in entry["workloads"]:
                    entry["workloads"].append(label)
                if entry["fsGroup"] is None and security.get("fsGroup") is not None:
                    entry["fsGroup"] = int(security["fsGroup"])
                if entry["runAsUser"] is None and security.get("runAsUser") is not None:
                    entry["runAsUser"] = int(security["runAsUser"])
    return out


def source_writers(
    kubectl_json, cluster_id: str, namespace: str
) -> Dict[str, List[str]]:
    """Pods in the source cluster currently mounting each claim in a namespace.

    Turns "two clusters could write the same export" from a caution into a fact:
    "4 pods in areeba-prod-01 are writing to this path right now".
    """
    try:
        payload = kubectl_json(cluster_id, ["get", "pods", "-n", namespace, "-o", "json"])
    except Exception:  # noqa: BLE001 — an advisory count must not break the plan
        return {}
    out: Dict[str, List[str]] = {}
    for pod in payload.get("items") or []:
        spec = pod.get("spec") or {}
        phase = (pod.get("status") or {}).get("phase") or ""
        if phase not in ("Running", "Pending"):
            continue
        name = (pod.get("metadata") or {}).get("name") or ""
        for volume in spec.get("volumes") or []:
            if not isinstance(volume, dict):
                continue
            claim = (volume.get("persistentVolumeClaim") or {}).get("claimName")
            if claim:
                out.setdefault(claim_key(namespace, str(claim)), []).append(name)
    return out


# ---------------------------------------------------------------------------
# Resolving a decision per claim
# ---------------------------------------------------------------------------

@dataclass
class ClaimPlan:
    """One copied PVC and what will back it."""

    namespace: str
    name: str
    source: str = NONE
    capacity: str = ""
    access_modes: List[str] = field(default_factory=list)
    storage_class: str = ""
    read_only: bool = False
    server: str = ""
    path: str = ""
    mount_options: List[str] = field(default_factory=list)
    pv_name: str = ""
    fs_group: Optional[int] = None
    workloads: List[str] = field(default_factory=list)
    # Whether "reuse" is even on the table, and why not.
    reusable: bool = False
    reuse_blocked: str = ""
    source_pv: str = ""
    source_kind: str = ""
    source_target: str = ""
    writers: List[str] = field(default_factory=list)
    error: str = ""

    @property
    def key(self) -> str:
        return claim_key(self.namespace, self.name)

    @property
    def authors_pv(self) -> bool:
        return self.source in PV_SOURCES and not self.error

    def as_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "namespace": self.namespace,
            "name": self.name,
            "source": self.source,
            "capacity": self.capacity,
            "accessModes": self.access_modes,
            "storageClassName": self.storage_class,
            "readOnly": self.read_only,
            "target": (
                f"{self.server}:{self.path}" if self.server and self.path else ""
            ),
            "pvName": self.pv_name,
            "fsGroup": self.fs_group,
            "workloads": self.workloads,
            "reusable": self.reusable,
            "reuseBlocked": self.reuse_blocked,
            "sourcePv": self.source_pv,
            "sourceKind": self.source_kind,
            "sourceTarget": self.source_target,
            "writers": self.writers,
            "error": self.error,
        }


def pv_name_for(namespace: str, name: str) -> str:
    """A PV name derived only from the claim it serves.

    Deliberately not build-scoped: PVs are per-cluster, so the only thing a
    build id would add is a *different* name for the same claim on a second
    copy into the same cluster — which is exactly the case that must stay
    idempotent. It also lets the wizard preview the real name before a build
    row exists.
    """
    def slug(value: str) -> str:
        return _DNS_SAFE_RE.sub("-", str(value).lower()).strip("-") or "x"

    stem = f"kubesight-{slug(namespace)}-{slug(name)}"
    if len(stem) <= 253:
        return stem
    # Keep the tail: the claim name is what a human recognises.
    return stem[:120] + "-" + stem[-120:]


def resolve(
    claim_docs: List[Dict[str, Any]],
    storage: Optional[Dict[str, Any]],
    *,
    source_index: Optional[Dict[str, Dict[str, Any]]] = None,
    consumer_index: Optional[Dict[str, Dict[str, Any]]] = None,
    writer_index: Optional[Dict[str, List[str]]] = None,
) -> List[ClaimPlan]:
    """Turn cleaned PVC documents plus the storage answers into per-claim plans.

    Impossible decisions are recorded on the row (``error``) rather than raised,
    so the UI can show every problem at once instead of one per round trip.
    """
    storage = storage or {}
    source_index = source_index or {}
    consumer_index = consumer_index or {}
    writer_index = writer_index or {}
    export_root = str(storage.get("nfsExportRoot") or "").rstrip("/")
    server = str(storage.get("nfsServer") or "")
    mount_options = list(storage.get("nfsMountOptions") or [])
    default_class = str(storage.get("storageClassName") or "")

    plans: List[ClaimPlan] = []
    for doc in claim_docs:
        metadata = doc.get("metadata") or {}
        spec = doc.get("spec") or {}
        namespace = str(metadata.get("namespace") or "")
        name = str(metadata.get("name") or "")
        key = claim_key(namespace, name)
        requested = str(
            ((spec.get("resources") or {}).get("requests") or {}).get("storage") or ""
        )
        origin = source_index.get(key) or {}
        consumer = consumer_index.get(key) or {}
        decision = decision_for(storage, key)

        plan = ClaimPlan(
            namespace=namespace,
            name=name,
            source=decision["source"],
            capacity=requested or str(origin.get("capacity") or ""),
            access_modes=list(spec.get("accessModes") or []) or ["ReadWriteOnce"],
            storage_class=str(spec.get("storageClassName") or ""),
            read_only=bool(decision["readOnly"]),
            fs_group=consumer.get("fsGroup"),
            workloads=list(consumer.get("workloads") or []),
            source_pv=str(origin.get("pvName") or ""),
            source_kind=str(origin.get("kind") or ""),
        )
        origin_nfs = origin.get("nfs")
        if origin_nfs:
            plan.reusable = True
            plan.source_target = f"{origin_nfs['server']}:{origin_nfs['path']}"
        elif origin:
            plan.reuse_blocked = (
                f"its volume in the source cluster is backed by "
                f"{origin.get('kind') or 'an unknown volume type'}, not NFS"
            )
        else:
            plan.reuse_blocked = "it is not bound to a volume in the source cluster"

        if plan.source == REUSE:
            if not plan.reusable:
                plan.error = (
                    f"{key} cannot reuse the source volume because "
                    f"{plan.reuse_blocked}. Choose a fresh directory, a "
                    "StorageClass, or leave it pending."
                )
            else:
                plan.server = origin_nfs["server"]
                plan.path = origin_nfs["path"]
                # The volume's real size, not the claim's request: a 5Gi claim
                # bound to a 20Gi export must not be relabelled 5Gi.
                plan.capacity = str(origin.get("capacity") or plan.capacity)
                plan.mount_options = mount_options or list(origin.get("mountOptions") or [])
                plan.writers = list(writer_index.get(key) or [])
        elif plan.source == FRESH:
            missing = [
                label for label, value in (("NFS server", server),
                                           ("export root", export_root))
                if not value
            ]
            if missing:
                plan.error = (
                    f"{key} needs a fresh NFS directory, so the "
                    f"{' and '.join(missing)} must be filled in."
                )
            elif not plan.capacity:
                plan.error = (
                    f"{key} requests no storage size, so a PersistentVolume "
                    "cannot be sized for it. Leave it pending or use a "
                    "StorageClass."
                )
            else:
                plan.server = server
                plan.path = f"{export_root}/{namespace}/{name}"
                plan.mount_options = mount_options
        elif plan.source == CLASS:
            plan.storage_class = default_class or plan.storage_class
            if not plan.storage_class:
                plan.error = (
                    f"{key} is set to bind through a StorageClass, but no "
                    "StorageClass name was given and the claim does not name one."
                )
        if plan.authors_pv:
            plan.pv_name = pv_name_for(namespace, name)
        plans.append(plan)
    return plans


def errors(plans: List[ClaimPlan]) -> List[str]:
    return [plan.error for plan in plans if plan.error]


def summarize(plans: List[ClaimPlan]) -> Dict[str, Any]:
    counts: Dict[str, int] = {source: 0 for source in SOURCES}
    for plan in plans:
        counts[plan.source] = counts.get(plan.source, 0) + 1
    return {
        "claims": len(plans),
        "counts": counts,
        "volumesToCreate": sum(1 for plan in plans if plan.authors_pv),
        "reusingData": sum(1 for plan in plans if plan.source == REUSE and not plan.error),
        "pending": counts.get(NONE, 0),
    }


# ---------------------------------------------------------------------------
# What gets applied
# ---------------------------------------------------------------------------

def pv_document(plan: ClaimPlan, *, source_cluster: str = "") -> Dict[str, Any]:
    """The PersistentVolume backing one claim.

    ``claimRef`` without a uid is a *pre-binding*: the claim of that exact
    namespace/name takes this volume and nothing else can. Paired with the
    claim's own ``volumeName`` it is unambiguous in both directions.
    """
    spec: Dict[str, Any] = {
        "capacity": {"storage": plan.capacity},
        "accessModes": list(plan.access_modes),
        "nfs": {"server": plan.server, "path": plan.path},
        # Never Delete. A copied claim being removed must not be able to delete
        # the directory it was pointed at.
        "persistentVolumeReclaimPolicy": "Retain",
        "storageClassName": "",
        "volumeMode": "Filesystem",
        "claimRef": {
            "apiVersion": "v1",
            "kind": "PersistentVolumeClaim",
            "namespace": plan.namespace,
            "name": plan.name,
        },
    }
    if plan.read_only:
        spec["nfs"]["readOnly"] = True
    if plan.mount_options:
        spec["mountOptions"] = list(plan.mount_options)
    return {
        "apiVersion": "v1",
        "kind": "PersistentVolume",
        "metadata": {
            "name": plan.pv_name,
            "labels": {
                "kubesight.io/managed": "cluster-builder",
                "kubesight.io/claim-namespace": plan.namespace,
            },
            "annotations": {
                "kubesight.io/claim": plan.key,
                "kubesight.io/volume-source": plan.source,
                **({"kubesight.io/copied-from": source_cluster} if source_cluster else {}),
            },
        },
        "spec": spec,
    }


def rewrite_claim(doc: Dict[str, Any], plan: ClaimPlan) -> Dict[str, Any]:
    """Point a copied claim at what will actually back it.

    ``spec.selector`` always goes: it matches labels on PVs of the *source*
    cluster, so leaving it in is a guaranteed Pending claim whichever decision
    was made.
    """
    out = {**doc, "spec": {**(doc.get("spec") or {})}}
    spec = out["spec"]
    spec.pop("selector", None)
    if plan.authors_pv:
        # Empty string, not absent: absent means "use the default StorageClass",
        # which would try to dynamically provision instead of taking our PV.
        spec["storageClassName"] = ""
        spec["volumeName"] = plan.pv_name
    elif plan.source == CLASS:
        spec.pop("volumeName", None)
        spec["storageClassName"] = plan.storage_class
    return out


def prepare_script(plans: List[ClaimPlan], storage: Dict[str, Any]) -> Optional[str]:
    """Create (and only create) the directories fresh claims need.

    Mounts the export root once, makes one directory per claim, and hands each
    to its workload's fsGroup. Reused paths are never touched — the cluster
    still running over there owns those permissions.
    """
    fresh = [plan for plan in plans if plan.source == FRESH and not plan.error]
    if not fresh:
        return None
    server = str(storage.get("nfsServer") or "")
    export_root = str(storage.get("nfsExportRoot") or "").rstrip("/")
    options = list(storage.get("nfsMountOptions") or [])
    option_flag = f"-o {shlex.quote(','.join(options))} " if options else ""

    lines = [
        "set -e",
        'MP="$(mktemp -d /tmp/kubesight-nfs-XXXXXX)"',
        # Always unmount, even on failure: a leaked mount blocks a retry.
        "trap 'cd /; umount \"$MP\" 2>/dev/null || true; "
        "rmdir \"$MP\" 2>/dev/null || true' EXIT",
        f"mount -t nfs {option_flag}{shlex.quote(f'{server}:{export_root}')} \"$MP\"",
    ]
    for plan in fresh:
        relative = f"{plan.namespace}/{plan.name}"
        quoted = f'"$MP"/{shlex.quote(relative)}'
        lines.append(f"mkdir -p {quoted}")
        if plan.fs_group is not None:
            # The pod runs with this supplemental group, so the directory it
            # owns is group-writable and nothing wider.
            lines.append(f"chown :{int(plan.fs_group)} {quoted}")
            lines.append(f"chmod 2770 {quoted}")
        else:
            # No fsGroup declared: the pod's uid is unknown, and root_squash
            # means root cannot chown on its behalf either. World-writable is
            # the only setting that reliably works — surfaced in the log.
            lines.append(f"chmod 0777 {quoted}")
            lines.append(
                f"echo 'note: {relative} has no fsGroup, so its directory is "
                "world-writable (0777)'"
            )
        lines.append(f"ls -ld {quoted}")
    return "\n".join(lines) + "\n"


def reachability_script(storage: Dict[str, Any]) -> str:
    """Is the NFS server listening? Uses bash's /dev/tcp — no client packages,
    so this works at preflight, before base_prep has installed anything."""
    server = str(storage.get("nfsServer") or "")
    return (
        f"timeout 5 bash -c 'cat < /dev/null > /dev/tcp/{server}/{_NFS_PORT}' "
        f"&& echo KS_NFS=open || echo KS_NFS=closed"
    )


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

def _check(check_id, label, status, detail, hint=""):
    return {"id": check_id, "label": label, "status": status,
            "detail": detail, "hint": hint}


def preflight_checks(
    plans: List[ClaimPlan],
    storage: Optional[Dict[str, Any]],
    *,
    probe: Optional[Any] = None,
    probe_targets: Optional[List[Tuple[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Storage verdicts for the Verify step.

    ``probe`` runs the reachability one-liner on one node and returns its
    output; ``probe_targets`` is [(label, target)]. Both optional — without them
    the reachability check is skipped rather than faked.
    """
    storage = storage or {}
    if not plans:
        return []
    checks: List[Dict[str, Any]] = []

    invalid = errors(plans)
    if invalid:
        checks.append(_check(
            "workload_storage_invalid", "Storage decisions are complete", "fail",
            "; ".join(invalid[:4]),
            "Fix these in the Workloads step — each claim needs a destination "
            "that can actually exist.",
        ))

    summary = summarize(plans)
    authored = [plan for plan in plans if plan.authors_pv]
    if authored:
        fresh = sum(1 for plan in authored if plan.source == FRESH)
        reused = sum(1 for plan in authored if plan.source == REUSE)
        parts = []
        if fresh:
            parts.append(f"{fresh} new NFS director{'y' if fresh == 1 else 'ies'}")
        if reused:
            parts.append(f"{reused} reusing the source's export")
        checks.append(_check(
            "workload_storage_plan", "Volumes for copied claims", "pass",
            f"{len(authored)} PersistentVolume{'' if len(authored) == 1 else 's'} "
            f"on {storage.get('nfsServer')} ({', '.join(parts)}); reclaim policy "
            "Retain on all of them.",
        ))

    if summary["pending"]:
        pending = [plan.key for plan in plans if plan.source == NONE]
        checks.append(_check(
            "workload_storage_pending", "Copied claims have somewhere to bind", "warn",
            f"{len(pending)} claim{'' if len(pending) == 1 else 's'} will be created "
            f"with nothing to bind to: {', '.join(pending[:5])}.",
            "Their pods stay Pending until a volume exists. Give them an NFS "
            "directory or a StorageClass in the Workloads step.",
        ))

    writers = [plan for plan in plans if plan.source == REUSE and plan.writers]
    if writers:
        detail = "; ".join(
            f"{plan.key} → {plan.path} ({len(plan.writers)} pod"
            f"{'' if len(plan.writers) == 1 else 's'} still mounting it)"
            for plan in writers[:4]
        )
        checks.append(_check(
            "workload_storage_dual_writer",
            "Reused exports are not in active use", "warn", detail,
            "Both clusters will mount these exports read-write. That is what a "
            "migration wants once the old workload is stopped, and data "
            "corruption while it is still running. Stop them over there first, "
            "or tick read-only on those claims.",
        ))

    if authored:
        checks.extend(reachability_checks(
            storage, probe=probe, probe_targets=probe_targets
        ))
    return checks


def needs_nfs(answers: Optional[Dict[str, Any]]) -> bool:
    """Do the stored decisions call for an NFS mount anywhere?

    Answered from the decisions alone, without reading the source cluster, so
    it is usable at the start of a build and when growing one.
    """
    answers = answers or {}
    if answers.get("default") in PV_SOURCES:
        return True
    return any(
        (entry or {}).get("source") in PV_SOURCES
        for entry in (answers.get("claims") or {}).values()
    )


def reachability_checks(
    answers: Optional[Dict[str, Any]],
    *,
    probe: Optional[Any] = None,
    probe_targets: Optional[List[Tuple[str, Any]]] = None,
) -> List[Dict[str, Any]]:
    """Can the machines that will mount the export actually reach it?

    Shared by the build preflight and the grow preflight — a worker joining
    later needs the same answer, and finding out when a pod lands on it is too
    late.
    """
    answers = answers or {}
    server = str(answers.get("nfsServer") or "")
    if not (server and probe and probe_targets and needs_nfs(answers)):
        return []
    closed: List[str] = []
    unknown: List[str] = []
    for label, target in probe_targets:
        try:
            output = probe(target, reachability_script(answers))
        except Exception as exc:  # noqa: BLE001 — a probe failure is a result
            unknown.append(f"{label} ({exc})")
            continue
        if "KS_NFS=open" not in (output or ""):
            closed.append(label)
    if closed:
        return [_check(
            "workload_storage_nfs", "NFS server reachable from the nodes", "fail",
            f"{server}:{_NFS_PORT} did not answer from: {', '.join(closed)}.",
            "The build creates the export directories over this port, so it "
            "would fail after the cluster is already up. Open it, or change "
            "those claims to a StorageClass or leave them pending.",
        )]
    if unknown:
        return [_check(
            "workload_storage_nfs", "NFS server reachable from the nodes", "warn",
            f"Could not test {server}:{_NFS_PORT} from: {', '.join(unknown[:3])}.",
            "The mount is attempted again during the build.",
        )]
    return [_check(
        "workload_storage_nfs", "NFS server reachable from the nodes", "pass",
        f"{server}:{_NFS_PORT} answered from every node that will mount it.",
    )]
