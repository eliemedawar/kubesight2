"""Upgrade Center: the fleet, readiness, upgrade requests, runbooks and history.

How a cluster upgrades (``mode``):

* ``automatic`` — KubeSight runs every step (kubeadm with
  ``KUBESIGHT_AUTO_UPGRADE`` on, or minikube with its CLI). A run starts from a
  *request*: the plan (target, waves, drain timeout, snapshot, window), the
  readiness result it was made against and any blockers an admin waived. A
  request from an admin needs no approval; anyone else's waits for the
  cluster's approvals (the same per-cluster count every other cluster change
  uses; nobody approves their own request). Then it waits for its window, then
  runs.
* ``guided`` — KubeSight writes the commands with this cluster's values and
  ticks each step off when it sees the cluster change (cloud providers, and
  kubeadm while automation is off).
* ``manual`` — the platform upgrades itself (Docker Desktop, kind); KubeSight
  tracks the support clock.

Mock mode has two clusters: ``prod-us-east`` behaves as Docker Desktop (what
the tests expect) and ``staging-eu-west`` is a simulated kubeadm cluster whose
runs step through every phase without touching anything.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from ..audit import log_audit
from ..db import db
from ..upgrade_config import auto_upgrade_enabled
from ..upgrade_executor import (
    NODE_PHASES,
    RunStopped,
    RunTracker,
    initial_nodes,
    initial_stages,
    launch,
    normalize_options,
    validate_automated_target,
)
from ..upgrade_jobs import (
    OPEN_STATUSES,
    PENDING_STATUSES,
    adopt_job,
    change_stored,
    get_job,
    list_jobs,
    new_job_id,
    run_job_async,
    store_request,
    update_job,
)
from ..upgrade_provider import (
    PROVIDER_DISPLAY,
    describe_nodes,
    detect_cluster_provider,
    get_provider_support,
    nodes_behind,
    normalize_version,
    parse_k8s_version,
    peek_latest_patch,
    peek_latest_stable,
    release_context,
    validate_target_version,
)

logger = logging.getLogger(__name__)

Result = Tuple[Optional[Dict[str, Any]], Optional[str], int]

_FLEET_WORKERS = int(os.getenv("UPGRADE_FLEET_WORKERS", "8"))
_NODES_TTL = int(os.getenv("UPGRADE_NODES_CACHE_SECONDS", "60"))
MOCK_STEP_SECONDS = float(os.getenv("UPGRADE_MOCK_STEP_SECONDS", "1.2"))


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: Optional[datetime] = None) -> str:
    return (dt or _now()).isoformat()


def _parse_iso(value: Any) -> Optional[datetime]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _actor(user) -> Dict[str, Any]:
    if user is None:
        return {"id": None, "name": "system"}
    return {
        "id": getattr(user, "id", None),
        "name": getattr(user, "full_name", None) or getattr(user, "username", None) or "unknown",
    }


def _is_admin(user) -> bool:
    if user is None:
        return False
    from ..access_engine import is_admin

    try:
        return bool(is_admin(user))
    except Exception:  # noqa: BLE001
        return False


def _has(user, permission: str) -> bool:
    from ..auth_utils import auth_required_enabled

    if not auth_required_enabled():
        return True
    if user is None:
        return False
    from ..access_engine import user_has_permission

    try:
        return bool(user_has_permission(user, permission))
    except Exception:  # noqa: BLE001
        return False


def _can_access(user, cluster_id: str) -> bool:
    from ..auth_utils import auth_required_enabled

    if not auth_required_enabled():
        return True
    if user is None:
        return False
    from ..access_engine import can_access_cluster

    return bool(can_access_cluster(user, cluster_id))


# ---------------------------------------------------------------------------
# Mock mode: a simulated kubeadm cluster
# ---------------------------------------------------------------------------

_mock_lock = threading.Lock()
_MOCK_KUBEADM: Dict[str, Dict[str, Any]] = {}


def _mock_kubeadm_cluster(cluster_id: str) -> Optional[Dict[str, Any]]:
    """The simulated kubeadm cluster's state (``staging-eu-west`` only)."""
    if cluster_id != "staging-eu-west":
        return None
    with _mock_lock:
        if cluster_id not in _MOCK_KUBEADM:
            names = [("cp-1", "control-plane"), ("cp-2", "control-plane"), ("cp-3", "control-plane"),
                     ("w-1", "worker"), ("w-2", "worker"), ("w-3", "worker"), ("w-4", "worker")]
            pods = {"cp-1": 4, "cp-2": 3, "cp-3": 3, "w-1": 24, "w-2": 27, "w-3": 21, "w-4": 25}
            _MOCK_KUBEADM[cluster_id] = {
                "version": "v1.34.4",
                "nodes": [{"name": n, "role": r, "version": "v1.34.4", "ready": True,
                           "unschedulable": False, "pool": None} for n, r in names],
                "pods": pods,
            }
        return _MOCK_KUBEADM[cluster_id]


def _mock_context(cluster_id: str) -> str:
    lowered = cluster_id.lower()
    if "kind" in lowered:
        return "kind-test"
    if "minikube" in lowered:
        return "minikube"
    return "docker-desktop"


# ---------------------------------------------------------------------------
# Cluster snapshot (version, provider, nodes) — cheap and cached
# ---------------------------------------------------------------------------

def _mode_for(provider: str, support: Dict[str, Any]) -> str:
    if provider == "kubeadm":
        return "automatic" if auto_upgrade_enabled() else "guided"
    if provider == "minikube":
        return "automatic" if auto_upgrade_enabled() and support.get("cliAvailable") else "guided"
    if provider in ("eks", "aks", "gke"):
        return "guided"
    return "manual"


def _mode_reason(provider: str, mode: str, support: Dict[str, Any]) -> str:
    if mode == "automatic":
        return "KubeSight runs every step after approval."
    if provider == "kubeadm":
        return "Automatic upgrades are off on this KubeSight (KUBESIGHT_AUTO_UPGRADE), so you run the commands."
    if provider in ("eks", "aks", "gke"):
        cli = {"eks": "AWS CLI and eksctl", "aks": "Azure CLI", "gke": "gcloud"}[provider]
        return f"You run the {cli} commands; KubeSight watches the cluster and ticks each step off."
    if provider == "minikube":
        return "Run the minikube command; KubeSight watches the version."
    return support.get("reason") or "The platform upgrades itself."


def _cluster_names() -> Dict[str, str]:
    try:
        from ..routes.clusters import _list_clusters_payload

        payload = _list_clusters_payload()
        items = (payload.get("items") if isinstance(payload, dict) else None) or []
    except Exception:  # noqa: BLE001
        items = []
    return {str(c.get("id")): c.get("name") or str(c.get("id")) for c in items if c.get("id")}


def _visible_clusters(user) -> List[Dict[str, Any]]:
    try:
        from ..routes.clusters import _list_clusters_payload

        payload = _list_clusters_payload()
        items = (payload.get("items") if isinstance(payload, dict) else None) or []
    except Exception:  # noqa: BLE001
        logger.warning("Upgrade Center: cluster list unavailable", exc_info=True)
        items = []
    return [c for c in items if c.get("id") and _can_access(user, str(c["id"]))]


def _prepare(cluster_id: str) -> Dict[str, Any]:
    """The database part of a snapshot (real or mock? which kubeconfig?) —
    runs on the request thread, so the kubectl part can run on a pool."""
    from ..k8s_provider import resolve_cluster_access, should_use_real_k8s

    if not should_use_real_k8s(cluster_id):
        return {"clusterId": cluster_id, "real": False}
    access = resolve_cluster_access(cluster_id)
    if access is None:
        raise LookupError("Cluster not found")
    return {"clusterId": cluster_id, "real": True, "access": access}


def _real_snapshot(cluster_id: str, access) -> Dict[str, Any]:
    import json

    from ..k8s_provider import _K8S_READ_CACHE, _run_for_access, _server_version_output

    version_data = json.loads(_server_version_output(access))
    node_items = _K8S_READ_CACHE.get_or_compute(
        f"upg-nodes:{cluster_id}",
        _NODES_TTL,
        lambda: json.loads(_run_for_access(access, ["get", "nodes", "-o", "json"])).get("items", []),
    )
    provider = detect_cluster_provider(access, _run_for_access, version_data=version_data, node_items=node_items)
    return {
        "version": version_data.get("serverVersion", {}).get("gitVersion") or "unknown",
        "provider": provider,
        "nodes": describe_nodes(node_items),
        "access": access,
    }


def _mock_snapshot(cluster_id: str) -> Dict[str, Any]:
    sim = _mock_kubeadm_cluster(cluster_id)
    if sim is not None:
        with _mock_lock:
            return {"version": sim["version"], "provider": "kubeadm",
                    "nodes": [dict(n) for n in sim["nodes"]], "access": None, "simulated": True}
    from ..upgrade_provider import mock_upgrade_info

    info = mock_upgrade_info(cluster_id, "v1.31.0", _mock_context(cluster_id))
    return {
        "version": info["clusterInfo"]["controlPlaneVersion"],
        "provider": info["clusterInfo"]["provider"],
        "nodes": [{"name": n["name"], "role": "control-plane", "version": n["version"], "ready": n["ready"],
                   "unschedulable": False, "pool": None} for n in info["clusterInfo"]["nodes"]],
        "access": None,
    }


def cluster_snapshot(cluster_id: str, *, prepared: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    prepared = prepared or _prepare(cluster_id)
    snap = _real_snapshot(cluster_id, prepared["access"]) if prepared["real"] else _mock_snapshot(cluster_id)
    snap["real"] = prepared["real"]
    support = get_provider_support(snap["provider"])
    if snap.get("simulated"):
        mode = "automatic"
        support = {**support, "executionMode": "execute-with-cli", "upgradeSupported": True,
                   "reason": "Simulated kubeadm cluster (mock mode): runs step through every phase without touching anything."}
    else:
        mode = _mode_for(snap["provider"], support)
    nodes = snap["nodes"]
    snap.update(
        providerDisplay=PROVIDER_DISPLAY.get(snap["provider"], "Unknown"),
        providerSupport=support,
        mode=mode,
        modeReason=_mode_reason(snap["provider"], mode, support),
        controlPlanes=sum(1 for n in nodes if n["role"] == "control-plane"),
        workers=sum(1 for n in nodes if n["role"] != "control-plane"),
        notReady=[n["name"] for n in nodes if not n["ready"]],
        pools=sorted({n["pool"] for n in nodes if n.get("pool")}),
    )
    return snap


# ---------------------------------------------------------------------------
# Readiness results
# ---------------------------------------------------------------------------

def _latest_precheck(cluster_id: str) -> Optional[Dict[str, Any]]:
    from ..models_upgrade import UpgradePrecheck

    try:
        row = (UpgradePrecheck.query.filter_by(cluster_id=cluster_id)
               .order_by(UpgradePrecheck.created_at.desc()).first())
    except Exception:  # noqa: BLE001
        db.session.rollback()
        return None
    return _precheck_row(row) if row else None


def _latest_prechecks(cluster_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    from sqlalchemy import func

    from ..models_upgrade import UpgradePrecheck

    if not cluster_ids:
        return {}
    try:
        newest = (
            db.session.query(UpgradePrecheck.cluster_id, func.max(UpgradePrecheck.id))
            .filter(UpgradePrecheck.cluster_id.in_(cluster_ids))
            .group_by(UpgradePrecheck.cluster_id).all()
        )
        ids = [row_id for _cid, row_id in newest]
        rows = UpgradePrecheck.query.filter(UpgradePrecheck.id.in_(ids)).all() if ids else []
    except Exception:  # noqa: BLE001
        db.session.rollback()
        return {}
    return {row.cluster_id: _precheck_row(row, with_checks=False) for row in rows}


def _precheck_row(row, *, with_checks: bool = True) -> Dict[str, Any]:
    created = row.created_at if row.created_at.tzinfo else row.created_at.replace(tzinfo=timezone.utc)
    out = {
        "id": row.id,
        "targetVersion": row.target_version,
        "currentVersion": row.current_version,
        "canUpgrade": bool(row.can_upgrade),
        "summary": row.summary or {},
        "checkedAt": created.isoformat(),
        "checkedBy": row.created_by,
    }
    if with_checks:
        payload = row.payload or {}
        out["checks"] = payload.get("checks") or []
        out["baseline"] = payload.get("baseline")
    return out


def _store_precheck(cluster_id: str, data: Dict[str, Any], user) -> Dict[str, Any]:
    from ..models_upgrade import UpgradePrecheck

    row = UpgradePrecheck(
        cluster_id=cluster_id,
        target_version=data.get("targetVersion"),
        current_version=data.get("currentVersion"),
        can_upgrade=bool(data.get("canUpgrade")),
        summary=data.get("summary") or {},
        payload={"checks": data.get("checks") or [], "baseline": data.get("baseline")},
        created_by=_actor(user)["name"],
    )
    try:
        db.session.add(row)
        db.session.commit()
    except Exception:  # noqa: BLE001 — a result we could not store is still a result
        db.session.rollback()
        logger.warning("Could not store upgrade precheck for %s", cluster_id, exc_info=True)
        return {"checkedAt": _iso(), "checkedBy": _actor(user)["name"]}
    return {"id": row.id, "checkedAt": _iso(), "checkedBy": row.created_by}


def _mock_checks(cluster_id: str, target: str, snap: Dict[str, Any]) -> List[Dict[str, Any]]:
    from ..upgrade_checks import check

    simulated = snap.get("simulated")
    workers = snap["workers"]
    current = snap["version"]
    checks = [
        check("api", "API reachable", "health", "passed", f"API server answered ({current})."),
        check("nodes_ready", "Nodes ready", "health", "passed", f"All {len(snap['nodes'])} node(s) Ready."),
        check("control_plane", "Control plane pods", "health", "passed", "kube-system has no failed pods."),
        check("kube_system", "kube-system pods", "health", "passed", "38 kube-system pods ready."),
        check("node_pressure", "Node pressure", "health", "passed", "No memory, disk or PID pressure."),
        check("pdb", "Pod disruption budgets", "workloads", "passed", "6 PDBs, each allowing at least one disruption."),
    ]
    if simulated and workers:
        checks.append(check(
            "single_replica", "Single-replica workloads", "workloads", "warning",
            "3 workloads run one replica; each is briefly unavailable while its node drains.",
            objects=["Deployment payments/ledger-api", "Deployment kafka/schema-registry", "StatefulSet ops/vault"],
        ))
        checks.append(check(
            "restarts", "Pod restarts", "workloads", "warning", "10 or more restarts: kafka/kafka-connect-0.",
            objects=["kafka/kafka-connect-0"],
        ))
    else:
        checks.append(check("single_replica", "Single-replica workloads", "workloads", "passed",
                            "Single-node cluster: no node is drained."))
        checks.append(check("restarts", "Pod restarts", "workloads", "passed", "No pod restarted 10 or more times."))
    checks += [
        check("crashloop", "CrashLoopBackOff", "workloads", "passed", "No pods crash-looping."),
        check("pending", "Pending pods", "workloads", "passed", "No pods waiting for a node."),
    ]
    if parse_k8s_version(current) >= parse_k8s_version(target):
        checks.append(check("target", "Target version", "versions", "warning",
                            f"The cluster is already at or above {target} ({current})."))
    elif simulated and parse_k8s_version(target)[1] - parse_k8s_version(current)[1] > 1:
        checks.append(check("target", "Target version", "versions", "failed",
                            "kubeadm supports upgrading one minor version at a time.",
                            fix="Upgrade to the next minor first; each hop is its own run."))
    else:
        checks.append(check("target", "Target version", "versions", "passed", f"{current} → {target}."))
    checks += [
        check("skew", "Version skew", "versions", "passed", "All kubelets on the control plane's version."),
        check("apiservices", "API services", "versions", "passed", "All 24 APIServices available."),
        check("metrics", "Metrics server", "versions", "warning" if not simulated else "passed",
              "metrics-server is serving." if simulated else
              "metrics-server is not installed: node and pod usage won't show during the run."),
        check("pvc", "PVC health", "storage", "passed", "31 PVCs, none Lost or Pending."),
        check("default_storageclass", "Default StorageClass", "storage", "warning" if simulated else "passed",
              "2 StorageClasses, none marked default. A claim that names no class stays Pending."
              if simulated else "Default: hostpath."),
    ]
    return checks


def run_readiness(cluster_id: str, target_version: str, *, user=None) -> Result:
    """Run every check now, store the result, return it with its context."""
    from ..k8s_provider import K8sCommandError, _run_for_access, resolve_cluster_access, should_use_real_k8s
    from ..upgrade_checks import GROUPS, summarize
    from ..upgrade_provider import run_extended_prechecks

    if not cluster_id:
        return None, "clusterId is required.", 400
    valid, error = validate_target_version(target_version)
    if not valid:
        return None, error, 400
    target = normalize_version(target_version)
    started = time.monotonic()
    if should_use_real_k8s(cluster_id):
        access = resolve_cluster_access(cluster_id)
        if not access:
            return None, "Cluster not found", 404
        try:
            data = run_extended_prechecks(access, target, _run_for_access)
        except K8sCommandError as exc:
            return None, f"Readiness checks failed to run: {exc}", 503
    else:
        snap = _mock_snapshot(cluster_id)
        snap["workers"] = sum(1 for n in snap["nodes"] if n["role"] != "control-plane")
        checks = _mock_checks(cluster_id, target, snap)
        sim = _mock_kubeadm_cluster(cluster_id)
        data = {
            "clusterId": cluster_id,
            "targetVersion": target,
            "currentVersion": snap["version"],
            "canUpgrade": not any(c["status"] == "failed" for c in checks),
            "checks": checks,
            "summary": summarize(checks),
            "groups": GROUPS,
            "nodes": snap["nodes"],
            "baseline": {"readyPods": 418, "totalPods": 418, "podsByNode": dict(sim["pods"])} if sim else
            {"readyPods": 24, "totalPods": 24, "podsByNode": {}},
        }
    data["durationMs"] = int((time.monotonic() - started) * 1000)
    data.update(_store_precheck(cluster_id, data, user))
    log_audit(
        "upgrade_precheck_run",
        actor_user_id=getattr(user, "id", None),
        target_type="cluster",
        target_id=cluster_id,
        details={"targetVersion": target, "canUpgrade": data.get("canUpgrade"), "summary": data.get("summary")},
    )
    return data, None, 200


# ---------------------------------------------------------------------------
# Fleet + one cluster's workspace
# ---------------------------------------------------------------------------

def _open_jobs_by_cluster(cluster_ids: List[str]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for job in list_jobs(cluster_ids=cluster_ids, statuses=OPEN_STATUSES, limit=200):
        out.setdefault(str(job.get("clusterId")), _job_brief(job))
    return out


def _job_brief(job: Dict[str, Any]) -> Dict[str, Any]:
    nodes = job.get("nodes") or []
    done = sum(1 for n in nodes if n.get("status") in ("done", "skipped"))
    active = next((n.get("name") for n in nodes if n.get("status") == "active"), None)
    return {
        "jobId": job.get("jobId"),
        "status": job.get("status"),
        "mode": job.get("mode"),
        "targetVersion": job.get("targetVersion"),
        "currentVersion": job.get("currentVersion") or job.get("versionBefore"),
        "schedule": job.get("schedule"),
        "approvals": _approval_summary(job),
        "nodesDone": done,
        "nodesTotal": len(nodes),
        "activeNode": active,
        "startedAt": job.get("startedAt"),
        "requestedAt": job.get("requestedAt"),
        "requestedBy": (job.get("requestedBy") or {}).get("name"),
        "message": job.get("message"),
    }


def _row_for(cluster: Dict[str, Any], prepared: Any, latest: str) -> Dict[str, Any]:
    cluster_id = str(cluster["id"])
    row: Dict[str, Any] = {"id": cluster_id, "name": cluster.get("name") or cluster_id}
    try:
        if isinstance(prepared, Exception):
            raise prepared
        snap = cluster_snapshot(cluster_id, prepared=prepared)
    except Exception as exc:  # noqa: BLE001 — one unreachable cluster is one row
        row.update(reachable=False, error=str(exc)[:300], version=cluster.get("k8sVersion") or cluster.get("version"))
        return row
    ctx = release_context_fast(snap["version"], latest, None if snap["real"] else mock_patch)
    row.update(
        reachable=True,
        version=snap["version"],
        provider=snap["provider"],
        providerDisplay=snap["providerDisplay"],
        mode=snap["mode"],
        modeReason=snap["modeReason"],
        controlPlanes=snap["controlPlanes"],
        workers=snap["workers"],
        notReady=snap["notReady"],
        nodeVersions=sorted({n["version"] for n in snap["nodes"]}),
        support=ctx["support"],
        nextHop=(ctx["path"] or [None])[0],
        hops=len(ctx["path"]),
        patchTarget=ctx["patchTarget"],
    )
    return row


def release_context_fast(current_version: str, latest: str, patch=None) -> Dict[str, Any]:
    """``release_context`` that never waits on the network (fleet rows)."""
    from ..upgrade_releases import support_status, upgrade_path

    patch = patch or peek_latest_patch
    current = parse_k8s_version(current_version)
    return {
        "support": support_status(current_version),
        "path": upgrade_path(current_version, latest, patch),
        "patchTarget": patch(current[0], current[1]) if current[1] else None,
    }


# Mock mode never asks dl.k8s.io: fixed newest patches per minor.
MOCK_LATEST = "v1.37.1"
_MOCK_PATCHES = {29: "v1.29.15", 30: "v1.30.14", 31: "v1.31.13", 32: "v1.32.9", 33: "v1.33.5",
                 34: "v1.34.9", 35: "v1.35.7", 36: "v1.36.4", 37: "v1.37.1"}


def mock_patch(major: int, minor: int) -> Optional[str]:
    return _MOCK_PATCHES.get(minor) if major == 1 else None


def fleet(user) -> Result:
    from ..upgrade_releases import calendar

    clusters = _visible_clusters(user)
    prepared: List[Any] = []
    for cluster in clusters:
        try:
            prepared.append(_prepare(str(cluster["id"])))
        except Exception as exc:  # noqa: BLE001
            prepared.append(exc)
    real = any(isinstance(p, dict) and p.get("real") for p in prepared)
    latest = peek_latest_stable() if real else MOCK_LATEST
    with ThreadPoolExecutor(max_workers=max(1, min(_FLEET_WORKERS, len(clusters) or 1)),
                            thread_name_prefix="upgrade-fleet") as pool:
        rows = list(pool.map(lambda pair: _row_for(pair[0], pair[1], latest), zip(clusters, prepared)))
    ids = [r["id"] for r in rows]
    prechecks = _latest_prechecks(ids)
    jobs = _open_jobs_by_cluster(ids)
    minors = []
    for row in rows:
        row["readiness"] = prechecks.get(row["id"])
        row["openJob"] = jobs.get(row["id"])
        parsed = parse_k8s_version(row.get("version") or "")
        if parsed[1]:
            minors.append(parsed[1])
    return {
        "clusters": rows,
        "calendar": calendar(latest, around=minors),
        "latest": latest,
        "autoUpgradeEnabled": auto_upgrade_enabled(),
        "generatedAt": _iso(),
    }, None, 200


def _record_built_version(cluster_id: str, server_version: str) -> None:
    """A cluster upgraded outside a finished run (one that failed after the
    control plane, or by hand) still brings its Cluster Builder build along."""
    from .cluster_build.onboard import record_cluster_version

    try:
        record_cluster_version(cluster_id, server_version)
    except Exception:  # noqa: BLE001 — the page still shows the cluster
        db.session.rollback()
        logger.warning("Could not record %s on the build of %s", server_version, cluster_id, exc_info=True)


def workspace(cluster_id: str, *, user=None) -> Result:
    """Everything the cluster page needs: versions, path, nodes, last readiness, open job."""
    from ..upgrade_provider import _fetch_latest_k8s_version

    try:
        snap = cluster_snapshot(cluster_id)
    except LookupError:
        return None, "Cluster not found", 404
    except Exception as exc:  # noqa: BLE001
        return None, f"Could not read the cluster: {exc}", 503
    if snap["real"]:
        _record_built_version(cluster_id, snap["version"])
    latest = _fetch_latest_k8s_version() if snap["real"] else MOCK_LATEST
    ctx = release_context(snap["version"], latest, None if snap["real"] else mock_patch)
    jobs = list_jobs(cluster_ids=[cluster_id], statuses=OPEN_STATUSES, limit=1)
    names = _cluster_names()
    # A run that upgraded the control plane but not every node: finishing it
    # (same target, already-done nodes skipped) comes before the next minor.
    behind = nodes_behind(snap["version"], snap["nodes"])
    finish = {"version": snap["version"], "nodes": behind} if behind and snap["mode"] == "automatic" else None
    confirm_for = finish["version"] if finish else (ctx["path"][0].get("version") if ctx["path"] else None)
    return {
        "clusterId": cluster_id,
        "name": names.get(cluster_id, cluster_id),
        "version": snap["version"],
        "provider": snap["provider"],
        "providerDisplay": snap["providerDisplay"],
        "providerSupport": snap["providerSupport"],
        "mode": snap["mode"],
        "modeReason": snap["modeReason"],
        "nodes": snap["nodes"],
        "controlPlanes": snap["controlPlanes"],
        "workers": snap["workers"],
        "pools": snap["pools"],
        "latest": latest,
        **ctx,
        "finish": finish,
        "readiness": _latest_precheck(cluster_id),
        "openJob": jobs[0] if jobs else None,
        "requiredApprovals": approvals_for(user, cluster_id),
        "clusterApprovals": _required_approvals(cluster_id),
        "confirmationText": confirmation_text(names.get(cluster_id, cluster_id), confirm_for) if confirm_for else None,
        "canStart": _has(user, "upgrades:start"),
        "isAdmin": _is_admin(user),
        "generatedAt": _iso(),
    }, None, 200


def confirmation_text(cluster_name: str, target_version: str) -> str:
    return f"UPGRADE {cluster_name} TO {normalize_version(target_version)}"


# ---------------------------------------------------------------------------
# Requests: plan → approval → schedule → run
# ---------------------------------------------------------------------------

def approvals_for(user, cluster_id: str) -> int:
    """Approvals an upgrade request from ``user`` needs: none for an admin,
    else the cluster's count."""
    return 0 if _is_admin(user) else _required_approvals(cluster_id)


def _required_approvals(cluster_id: str) -> int:
    try:
        from .deployment_request_service import cluster_required_approvals

        return int(cluster_required_approvals(cluster_id))
    except Exception:  # noqa: BLE001
        db.session.rollback()
        return 1


def _approval_summary(job: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    approvals = job.get("approvals")
    if not approvals:
        return None
    votes = approvals.get("votes") or []
    return {
        "required": approvals.get("required", 0),
        "approved": sum(1 for v in votes if v.get("decision") == "approve"),
        "votes": votes,
    }


def _normalize_schedule(raw: Optional[Dict[str, Any]]) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    raw = raw or {}
    if (raw.get("mode") or "asap") == "asap":
        return {"mode": "asap"}, None
    start = _parse_iso(raw.get("start"))
    end = _parse_iso(raw.get("end"))
    if not start:
        return None, "A maintenance window needs a start time."
    if end and end <= start:
        return None, "The window must end after it starts."
    if end and end <= _now():
        return None, "That window has already ended."
    return {"mode": "window", "start": start.isoformat(), "end": end.isoformat() if end else None,
            "timezone": str(raw.get("timezone") or "")[:64] or None}, None


def create_request(
    user,
    cluster_id: str,
    target_version: str,
    *,
    options: Optional[Dict[str, Any]] = None,
    schedule: Optional[Dict[str, Any]] = None,
    waivers: Optional[List[Dict[str, Any]]] = None,
    confirmation: Optional[str] = None,
    note: Optional[str] = None,
) -> Result:
    valid, error = validate_target_version(target_version)
    if not valid:
        return None, error, 400
    target = normalize_version(target_version)
    try:
        snap = cluster_snapshot(cluster_id)
    except LookupError:
        return None, "Cluster not found", 404
    except Exception as exc:  # noqa: BLE001
        return None, f"Could not read the cluster: {exc}", 503
    if snap["mode"] != "automatic":
        return None, f"{snap['providerDisplay']} clusters upgrade with the runbook: {snap['modeReason']}", 400
    try:
        validate_automated_target(snap["provider"], snap["version"], target)
    except ValueError as exc:
        return None, str(exc), 400
    if list_jobs(cluster_ids=[cluster_id], statuses=OPEN_STATUSES, limit=1):
        return None, "This cluster already has an upgrade in progress or waiting. Finish or withdraw it first.", 409

    names = _cluster_names()
    name = names.get(cluster_id, cluster_id)
    expected = confirmation_text(name, target)
    if (confirmation or "").strip() != expected:
        return {"requiredConfirmation": expected}, f"Type {expected} to confirm.", 400

    sched, sched_error = _normalize_schedule(schedule)
    if sched_error:
        return None, sched_error, 400

    readiness, err, status = run_readiness(cluster_id, target, user=user)
    if err:
        return None, err, status
    failed = [c for c in readiness["checks"] if c.get("status") == "failed"]
    waived_by_key = {str(w.get("key")): str(w.get("reason") or "").strip() for w in (waivers or []) if w.get("key")}
    unwaived = [c for c in failed if c["key"] not in waived_by_key or not waived_by_key[c["key"]]]
    if unwaived:
        return {"readiness": readiness}, (
            "Blocked by readiness checks: " + ", ".join(c["name"] for c in unwaived)
            + ". Fix them and re-check, or have an admin waive each with a reason."
        ), 409
    if failed and not _is_admin(user):
        return None, "Only an admin can waive a blocking check.", 403
    actor = _actor(user)
    waiver_rows = [
        {"key": c["key"], "name": c["name"], "details": c.get("details"), "reason": waived_by_key[c["key"]],
         "by": actor["name"], "at": _iso()}
        for c in failed
    ]

    required = approvals_for(user, cluster_id)
    opts = normalize_options(options)
    nodes = snap["nodes"]
    job = {
        "jobId": new_job_id(),
        "clusterId": cluster_id,
        "clusterName": name,
        "provider": snap["provider"],
        "mode": "automatic",
        "simulated": bool(snap.get("simulated")),
        "currentVersion": snap["version"],
        "targetVersion": target,
        "status": "awaiting_approval" if required > 0 else "scheduled",
        "message": (f"Waiting for {required} approval(s)." if required > 0 else
                    "No approval needed: requested by an admin." if _is_admin(user) else
                    "Approved: no approval needed on this cluster."),
        "options": opts,
        "schedule": sched,
        "note": (note or "").strip()[:1000] or None,
        "requestedBy": actor,
        "requestedAt": _iso(),
        "approvals": {"required": required, "votes": []},
        "waivers": waiver_rows,
        "readiness": {
            "id": readiness.get("id"),
            "checkedAt": readiness.get("checkedAt"),
            "summary": readiness.get("summary"),
            "warnings": [c["name"] for c in readiness["checks"] if c.get("status") == "warning"],
        },
        "planPreview": {
            "controlPlanes": [n["name"] for n in nodes if n["role"] == "control-plane"],
            "workers": [n["name"] for n in nodes if n["role"] != "control-plane"],
            "podsByNode": (readiness.get("baseline") or {}).get("podsByNode") or {},
        },
        "executionSupported": True,
        "steps": [],
        "activeStep": -1,
        "startedAt": None,
        "finishedAt": None,
        "error": None,
    }
    store_request(job)
    log_audit(
        "upgrade_requested",
        actor_user_id=actor["id"],
        target_type="cluster",
        target_id=cluster_id,
        details={"jobId": job["jobId"], "targetVersion": target, "options": opts, "schedule": sched,
                 "requiredApprovals": required, "waived": [w["key"] for w in waiver_rows]},
    )
    _invalidate_inbox()
    if required == 0 and sched["mode"] == "asap":
        started, err, status = start_job(job["jobId"], trigger="request", user=user)
        if err:
            return None, err, status
        return started, None, 201
    return job, None, 201


def _invalidate_inbox() -> None:
    try:
        from .approvals_service import invalidate_waiting_counts

        invalidate_waiting_counts()
    except Exception:  # noqa: BLE001
        pass


def why_cannot_decide(job: Dict[str, Any], user) -> Optional[str]:
    """Why ``user`` may not approve/reject this request now (None: they may)."""
    if job.get("status") != "awaiting_approval":
        return "This request is not waiting for approval."
    if not _has(user, "upgrades:start"):
        return "You need the Start upgrades permission."
    if not _can_access(user, str(job.get("clusterId"))):
        return "You don't have access to this cluster."
    actor = _actor(user)
    if actor["id"] is not None and actor["id"] == (job.get("requestedBy") or {}).get("id"):
        return "You can't approve your own upgrade."
    votes = (job.get("approvals") or {}).get("votes") or []
    if any(v.get("userId") == actor["id"] for v in votes):
        return "You already voted."
    return None


def decide(user, job_id: str, decision: str, note: Optional[str] = None) -> Result:
    job = get_job(job_id)
    if not job:
        return None, "Upgrade request not found", 404
    why = why_cannot_decide(job, user)
    if why:
        return None, why, 403 if "permission" in why or "access" in why or "own" in why else 409
    if decision == "reject" and not (note or "").strip():
        return None, "Say why you reject it.", 400
    actor = _actor(user)
    result: Dict[str, Any] = {}

    def _vote(payload: Dict[str, Any]) -> None:
        if payload.get("status") != "awaiting_approval":
            result["late"] = True
            return
        approvals = payload.setdefault("approvals", {"required": 1, "votes": []})
        approvals.setdefault("votes", []).append({
            "userId": actor["id"], "name": actor["name"], "decision": decision,
            "note": (note or "").strip()[:1000] or None, "at": _iso(),
        })
        if decision == "reject":
            payload.update(status="rejected", message=f"Rejected by {actor['name']}: {(note or '').strip()}",
                           finishedAt=_iso())
            return
        approved = sum(1 for v in approvals["votes"] if v.get("decision") == "approve")
        if approved >= int(approvals.get("required") or 0):
            payload.update(status="scheduled", approvedAt=_iso(),
                           message="Approved. " + ("Starting now." if (payload.get("schedule") or {}).get("mode") != "window"
                                                   else "Waiting for the maintenance window."))
        else:
            payload["message"] = f"{approved} of {approvals.get('required')} approvals."

    stored = change_stored(job_id, _vote)
    if result.get("late") or stored is None:
        return None, "This request is no longer waiting for approval.", 409
    _invalidate_inbox()
    log_audit(
        f"upgrade_{'approved' if decision == 'approve' else 'rejected'}",
        actor_user_id=actor["id"],
        target_type="cluster",
        target_id=str(stored.get("clusterId")),
        details={"jobId": job_id, "note": note},
    )
    if stored.get("status") == "scheduled" and (stored.get("schedule") or {}).get("mode") != "window":
        started, err, status = start_job(job_id, trigger="approval", user=user)
        if err:
            return None, err, status
        return started, None, 200
    return stored, None, 200


def withdraw(user, job_id: str) -> Result:
    job = get_job(job_id)
    if not job:
        return None, "Upgrade request not found", 404
    actor = _actor(user)
    own = actor["id"] is not None and actor["id"] == (job.get("requestedBy") or {}).get("id")
    if not (own or _is_admin(user)):
        return None, "Only the person who asked, or an admin, can withdraw it.", 403
    if job.get("status") not in PENDING_STATUSES:
        return None, "Only a request that has not started can be withdrawn.", 409

    def _w(payload: Dict[str, Any]) -> None:
        if payload.get("status") in PENDING_STATUSES:
            payload.update(status="withdrawn", message=f"Withdrawn by {actor['name']}.", finishedAt=_iso())

    stored = change_stored(job_id, _w)
    _invalidate_inbox()
    log_audit("upgrade_withdrawn", actor_user_id=actor["id"], target_type="cluster",
              target_id=str(job.get("clusterId")), details={"jobId": job_id})
    return stored, None, 200


def control(user, job_id: str, action: str) -> Result:
    job = get_job(job_id)
    if not job:
        return None, "Upgrade not found", 404
    if not (_has(user, "upgrades:start") and _can_access(user, str(job.get("clusterId")))):
        return None, "You need the Start upgrades permission on this cluster.", 403
    status = job.get("status")
    if action == "pause" and status not in ("running", "queued"):
        return None, "Only a running upgrade can be paused.", 409
    if action == "resume" and status != "paused":
        return None, "This upgrade is not paused.", 409
    if action == "stop" and status not in ("running", "paused", "queued"):
        return None, "This upgrade is not running.", 409
    flags = {"pause": {"pause": True}, "resume": {"pause": False}, "stop": {"stop": True}}[action]
    actor = _actor(user)

    def _c(payload: Dict[str, Any]) -> None:
        payload.setdefault("control", {}).update(flags, by=actor["name"], at=_iso())
        note = {"pause": "Pause requested: finishing the current node.",
                "resume": "Resuming.", "stop": "Stop requested: finishing the current node, then stopping."}[action]
        payload["controlMessage"] = note

    change_stored(job_id, _c)
    log_audit(f"upgrade_{action}", actor_user_id=actor["id"], target_type="cluster",
              target_id=str(job.get("clusterId")), details={"jobId": job_id})
    return get_job(job_id), None, 200


def start_job(job_id: str, *, trigger: str, user=None) -> Result:
    """Adopt a scheduled request into this process and run it."""
    from ..k8s_provider import _run_for_access, resolve_cluster_access

    job = get_job(job_id)
    if not job:
        return None, "Upgrade request not found", 404
    if job.get("status") != "scheduled":
        return None, "This request is not ready to start.", 409
    cluster_id = str(job["clusterId"])
    window_end = (job.get("schedule") or {}).get("end")

    if trigger == "schedule":
        # Days may have passed since the plan: check again; a new blocker that
        # nobody waived stops the run before it touches anything.
        readiness, err, _status = run_readiness(cluster_id, job["targetVersion"], user=None)
        waived = {w.get("key") for w in job.get("waivers") or []}
        new_blockers = [] if err else [c["name"] for c in readiness["checks"]
                                       if c.get("status") == "failed" and c["key"] not in waived]
        if err or new_blockers:
            reason = err or ("New blockers since approval: " + ", ".join(new_blockers) + ".")
            change_stored(job_id, lambda p: p.update(status="failed", error=reason,
                                                     message="Did not start: " + reason, finishedAt=_iso()))
            return None, reason, 409

    adopted = adopt_job(job_id, status="queued", message="Starting.", startedAt=_iso(), trigger=trigger)
    if adopted is None:
        return None, "Upgrade request not found", 404
    log_audit("upgrade_start_run", actor_user_id=getattr(user, "id", None), target_type="cluster",
              target_id=cluster_id, details={"jobId": job_id, "trigger": trigger,
                                             "targetVersion": job["targetVersion"]})
    if job.get("simulated"):
        run_job_async(job_id, lambda: _simulate_kubeadm(job_id, cluster_id, job["targetVersion"],
                                                          job.get("options") or {}, window_end))
        return get_job(job_id), None, 200
    access = resolve_cluster_access(cluster_id)
    if access is None:
        update_job(job_id, status="failed", error="Cluster not found", finishedAt=_iso())
        return None, "Cluster not found", 404
    try:
        launch(access, job_id, provider=job["provider"], target_version=job["targetVersion"],
               run_kubectl=_run_for_access, options=job.get("options"), window_end=window_end)
    except ValueError as exc:
        update_job(job_id, status="failed", error=str(exc), finishedAt=_iso())
        return None, str(exc), 400
    return get_job(job_id), None, 200


def start_due_upgrades() -> int:
    """Scheduler tick: start every approved request whose window has opened;
    expire the ones whose window closed without them."""
    started = 0
    for job in list_jobs(statuses=("scheduled",), limit=100):
        sched = job.get("schedule") or {}
        start = _parse_iso(sched.get("start")) if sched.get("mode") == "window" else None
        end = _parse_iso(sched.get("end"))
        now = _now()
        if start and now < start:
            continue
        if end and now >= end:
            change_stored(job["jobId"], lambda p: p.update(
                status="expired", message="The maintenance window ended before the upgrade could start.",
                finishedAt=_iso()))
            continue
        _data, err, _status = start_job(job["jobId"], trigger="schedule")
        if not err:
            started += 1
        else:
            logger.warning("Scheduled upgrade %s did not start: %s", job["jobId"], err)
    return started


# ---------------------------------------------------------------------------
# The simulated kubeadm run (mock mode)
# ---------------------------------------------------------------------------

def _simulate_kubeadm(job_id: str, cluster_id: str, target: str, options: Dict[str, Any],
                      window_end: Optional[str]) -> None:
    sim = _mock_kubeadm_cluster(cluster_id)
    opts = normalize_options(options)
    step = MOCK_STEP_SECONDS
    tracker = RunTracker(job_id, window_end=window_end)
    with _mock_lock:
        before = sim["version"]
        node_rows = [dict(n) for n in sim["nodes"]]
        pods = dict(sim["pods"])
    cps = [n["name"] for n in node_rows if n["role"] == "control-plane"]
    workers = [n["name"] for n in node_rows if n["role"] != "control-plane"]
    fake_items = [{"metadata": {"name": n["name"]}, "status": {"nodeInfo": {"kubeletVersion": n["version"]}}}
                  for n in node_rows]
    baseline = {"readyPods": 418, "totalPods": 418, "podsByNode": pods, "at": _iso()}
    tracker.save(nodes=initial_nodes(fake_items, cps, workers, opts["waveSize"], pods),
                 stages=initial_stages(opts, cps, workers), options=opts, baseline=baseline,
                 versionBefore=before, startedAt=_iso())
    tracker.log(None, f"Upgrading {cluster_id} from {before} to {target} (simulated).")

    def set_version(name: Optional[str], version: str) -> None:
        with _mock_lock:
            if name is None:
                sim["version"] = version
            for n in sim["nodes"]:
                if n["name"] == name:
                    n["version"] = version

    tracker.stage("safety", "active")
    if opts["etcdSnapshot"]:
        tracker.log("cp-1", "Taking an etcd snapshot.")
        time.sleep(step)
        path = f"/var/lib/etcd/kubesight-pre-upgrade-{_now().strftime('%Y%m%dT%H%M%SZ')}.db"
        tracker.save(snapshot={"status": "done", "node": "cp-1", "path": path, "message": f"Saved to {path} on cp-1."})
        tracker.log("cp-1", f"Saved to {path} on cp-1.")
    else:
        tracker.save(snapshot={"status": "skipped", "message": "Switched off in the plan."})
    tracker.log(None, "Baseline: 418 ready pods.")
    tracker.stage("safety", "done", "Snapshot and baseline taken." if opts["etcdSnapshot"] else "Baseline taken.")

    def upgrade(name: str) -> None:
        tracker.node(name, state="active")
        for phase in NODE_PHASES:
            label = {"cordon": "Cordoning", "drain": f"Draining {pods.get(name, 0)} pod(s)",
                     "upgrade": "Upgrading packages and kubeadm", "uncordon": "Uncordoning",
                     "ready": "Waiting for Ready"}[phase]
            tracker.node(name, phase=phase, status="active", message=label)
            time.sleep(step * (2 if phase in ("drain", "upgrade") else 0.6))
            if phase == "upgrade":
                set_version(name, target)
                if name == cps[0]:
                    set_version(None, target)
                    tracker.log(name, f"kubeadm upgrade apply {target} · API server reports {target}")
                else:
                    tracker.log(name, "kubeadm upgrade node · kubelet restarted")
            elif phase == "drain":
                tracker.log(name, f"drain complete · {pods.get(name, 0)} pod(s) evicted")
            elif phase == "cordon":
                tracker.log(name, f"cordon node/{name}")
            tracker.node(name, phase=phase, status="done")
        tracker.node(name, state="done", versionAfter=target, message=f"Ready on {target}", finishedAt=_iso())
        tracker.log(name, f"Ready · kubelet {target}")

    try:
        tracker.stage("control-plane", "active")
        for cp in cps:
            tracker.checkpoint(cp)
            upgrade(cp)
        tracker.stage("control-plane", "done", f"{len(cps)} control plane(s) on {target}.")
        tracker.stage("workers", "active")
        waves = [workers[i:i + opts["waveSize"]] for i in range(0, len(workers), opts["waveSize"])]
        for number, wave in enumerate(waves, start=1):
            tracker.checkpoint(f"wave {number} ({', '.join(wave)})",
                               force_pause=opts["pauseBetweenWaves"] and number > 1)
            tracker.log(None, f"Wave {number} of {len(waves)}: {', '.join(wave)}")
            threads = [threading.Thread(target=upgrade, args=(w,), daemon=True) for w in wave]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        tracker.stage("workers", "done", f"{len(workers)} worker(s) in {len(waves)} wave(s).")
    except RunStopped as stop:
        tracker.log(None, str(stop))
        update_job(job_id, status="stopped", message=f"{stop} Nodes already done keep {target}.", finishedAt=_iso())
        return
    tracker.stage("verify", "active")
    time.sleep(step)
    tracker.stage("verify", "done", "Matches the baseline.")
    tracker.log(None, f"Verify: API server {target}; 418 of 418 ready pods, matches the baseline.")
    update_job(job_id, status="completed", message=f"Cluster upgraded to {target}.", versionAfter=target,
               after={"readyPods": 418, "totalPods": 418, "at": _iso()}, verdict="clean", finishedAt=_iso())


# ---------------------------------------------------------------------------
# Guided runbooks
# ---------------------------------------------------------------------------

_APT_KUBEADM = ("sudo apt-mark unhold kubeadm && sudo apt-get update && "
                "sudo apt-get install -y kubeadm='{pkg}-*' && sudo apt-mark hold kubeadm")
_APT_KUBELET = ("sudo apt-mark unhold kubelet kubectl && sudo apt-get install -y kubelet='{pkg}-*' "
                "kubectl='{pkg}-*' && sudo apt-mark hold kubelet kubectl")
_DNF_ALL = "sudo dnf install -y kubeadm-{pkg} kubelet-{pkg} kubectl-{pkg} --disableexcludes=kubernetes"
_RESTART = "sudo systemctl daemon-reload && sudo systemctl restart kubelet"


def _cmd(text: str, where: str = "workstation", label: Optional[str] = None) -> Dict[str, Any]:
    return {"text": text, "where": where, "label": label}


def _node_state(nodes: List[Dict[str, Any]], names: List[str], target: Tuple[int, int, int], *, minor_only: bool) -> str:
    rows = [n for n in nodes if n["name"] in names]
    if not rows:
        return "waiting"

    def ok(v: str) -> bool:
        parsed = parse_k8s_version(v)
        return parsed[:2] >= target[:2] if minor_only else parsed >= target

    return "done" if all(ok(n["version"]) for n in rows) else "waiting"


def build_runbook(snap: Dict[str, Any], target: str, cluster_name: str) -> Dict[str, Any]:
    provider = snap["provider"]
    nodes = snap["nodes"]
    target_t = parse_k8s_version(target)
    minor = f"{target_t[0]}.{target_t[1]}"
    pkg = normalize_version(target).lstrip("v")
    api_done = parse_k8s_version(snap["version"])[:2] >= target_t[:2]
    steps: List[Dict[str, Any]] = []
    placeholders: Dict[str, Any] = {}

    if provider == "kubeadm":
        cps = [n["name"] for n in nodes if n["role"] == "control-plane"]
        workers = [n["name"] for n in nodes if n["role"] != "control-plane"]
        first = cps[0] if cps else "<control-plane>"
        steps.append({
            "key": "snapshot", "title": "Back up etcd", "observe": "manual",
            "body": f"On {first}: a snapshot is the way back, because kubeadm can't downgrade a control plane.",
            "commands": [_cmd(
                "sudo ETCDCTL_API=3 etcdctl --endpoints=https://127.0.0.1:2379 "
                "--cacert=/etc/kubernetes/pki/etcd/ca.crt --cert=/etc/kubernetes/pki/etcd/server.crt "
                "--key=/etc/kubernetes/pki/etcd/server.key snapshot save /var/lib/etcd/pre-upgrade.db", first)],
        })
        for index, cp in enumerate(cps):
            first_cp = index == 0
            steps.append({
                "key": f"node:{cp}", "title": f"{'First control plane' if first_cp else 'Control plane'} · {cp}",
                "observe": "auto",
                "body": "Upgrades the cluster configuration and the control plane." if first_cp else
                "One control plane at a time keeps the API up.",
                "commands": [
                    _cmd(f"kubectl drain {cp} --ignore-daemonsets --delete-emptydir-data"),
                    _cmd(_APT_KUBEADM.format(pkg=pkg), cp, "Debian/Ubuntu"),
                    _cmd(_DNF_ALL.format(pkg=pkg), cp, "RHEL/Rocky"),
                    _cmd(f"sudo kubeadm upgrade plan {normalize_version(target)}" if first_cp else "sudo kubeadm upgrade node", cp),
                ] + ([_cmd(f"sudo kubeadm upgrade apply -y {normalize_version(target)}", cp)] if first_cp else []) + [
                    _cmd(_APT_KUBELET.format(pkg=pkg), cp, "Debian/Ubuntu"),
                    _cmd(_RESTART, cp),
                    _cmd(f"kubectl uncordon {cp}"),
                ],
                "state": ("done" if api_done and _node_state(nodes, [cp], target_t, minor_only=False) == "done"
                          else "waiting"),
                "seen": "API server and kubelet on the target" if first_cp else "kubelet on the target",
            })
        for worker in workers:
            steps.append({
                "key": f"node:{worker}", "title": f"Worker · {worker}", "observe": "auto",
                "body": "One worker at a time; wait until it is Ready before the next.",
                "commands": [
                    _cmd(f"kubectl drain {worker} --ignore-daemonsets --delete-emptydir-data"),
                    _cmd(_APT_KUBEADM.format(pkg=pkg), worker, "Debian/Ubuntu"),
                    _cmd(_DNF_ALL.format(pkg=pkg), worker, "RHEL/Rocky"),
                    _cmd("sudo kubeadm upgrade node", worker),
                    _cmd(_APT_KUBELET.format(pkg=pkg), worker, "Debian/Ubuntu"),
                    _cmd(_RESTART, worker),
                    _cmd(f"kubectl uncordon {worker}"),
                ],
                "state": _node_state(nodes, [worker], target_t, minor_only=False),
                "seen": "kubelet on the target",
            })
    elif provider in ("eks", "aks", "gke"):
        placeholders = {"clusterName": cluster_name}
        if provider == "aks":
            placeholders["resourceGroup"] = ""
        if provider == "gke":
            placeholders["location"] = ""
        pools = snap["pools"] or ["<node-pool>"]
        if provider == "eks":
            cp_cmd = [_cmd(f"aws eks update-cluster-version --name {{clusterName}} --kubernetes-version {minor}")]
            pool_cmd = lambda p: _cmd(f"eksctl upgrade nodegroup --cluster {{clusterName}} --name {p} --kubernetes-version {minor}")  # noqa: E731
            addons = [
                _cmd("eksctl utils update-kube-proxy --cluster {clusterName} --approve"),
                _cmd("eksctl utils update-coredns --cluster {clusterName} --approve"),
                _cmd("eksctl utils update-aws-node --cluster {clusterName} --approve"),
            ]
        elif provider == "aks":
            cp_cmd = [
                _cmd("az aks get-upgrades --resource-group {resourceGroup} --name {clusterName} -o table"),
                _cmd(f"az aks upgrade --resource-group {{resourceGroup}} --name {{clusterName}} "
                     f"--kubernetes-version {pkg} --control-plane-only --yes"),
            ]
            pool_cmd = lambda p: _cmd(f"az aks nodepool upgrade --resource-group {{resourceGroup}} --cluster-name {{clusterName}} --name {p} --kubernetes-version {pkg} --yes")  # noqa: E731
            addons = []
        else:
            cp_cmd = [_cmd(f"gcloud container clusters upgrade {{clusterName}} --master --cluster-version {pkg} --location {{location}}")]
            pool_cmd = lambda p: _cmd(f"gcloud container clusters upgrade {{clusterName}} --node-pool {p} --cluster-version {pkg} --location {{location}}")  # noqa: E731
            addons = []
        steps.append({
            "key": "control-plane", "title": "Upgrade the control plane", "observe": "auto",
            "body": "The provider replaces the control plane in the background. KubeSight checks the API server version.",
            "commands": cp_cmd, "state": "done" if api_done else "waiting", "seen": f"API server on {minor}",
        })
        for pool in pools:
            members = [n["name"] for n in nodes if n.get("pool") == pool] or [n["name"] for n in nodes]
            steps.append({
                "key": f"pool:{pool}", "title": f"Node pool · {pool}", "observe": "auto",
                "body": f"{len(members)} node(s). The provider replaces them one by one and respects PodDisruptionBudgets.",
                "commands": [pool_cmd(pool)],
                "state": _node_state(nodes, members, target_t, minor_only=True) if api_done else "blocked",
                "seen": f"every node in {pool} on {minor}",
            })
        if addons:
            steps.append({"key": "addons", "title": "Update the core add-ons", "observe": "manual",
                          "body": "kube-proxy, CoreDNS and the VPC CNI should match the new version.",
                          "commands": addons})
    elif provider == "minikube":
        steps.append({"key": "control-plane", "title": "Restart minikube on the new version", "observe": "auto",
                      "body": "minikube recreates its node on the requested version.",
                      "commands": [_cmd(f"minikube start --kubernetes-version={normalize_version(target)}")],
                      "state": "done" if api_done else "waiting", "seen": f"API server on {normalize_version(target)}"})
    else:
        instructions = snap["providerSupport"].get("instructions") or {}
        steps.append({"key": "manual", "title": instructions.get("title") or "Upgrade from the platform",
                      "observe": "auto", "body": instructions.get("summary") or snap["modeReason"],
                      "commands": [_cmd(s, "note") for s in instructions.get("steps") or []],
                      "state": "done" if api_done else "waiting", "seen": f"API server on {minor}"})
    auto = [s for s in steps if s.get("observe") == "auto"]
    return {
        "steps": steps,
        "placeholders": placeholders,
        "observedDone": bool(auto) and all(s.get("state") == "done" for s in auto),
    }


def runbook(cluster_id: str, target_version: Optional[str], *, user=None) -> Result:
    try:
        snap = cluster_snapshot(cluster_id)
    except LookupError:
        return None, "Cluster not found", 404
    except Exception as exc:  # noqa: BLE001
        return None, f"Could not read the cluster: {exc}", 503
    names = _cluster_names()
    open_guided = next(iter(list_jobs(cluster_ids=[cluster_id], statuses=("guided",), limit=1)), None)
    target = target_version or (open_guided or {}).get("targetVersion")
    if not target:
        ctx = release_context_fast(snap["version"], peek_latest_stable() if snap["real"] else MOCK_LATEST,
                                   None if snap["real"] else mock_patch)
        hop = (ctx["path"] or [None])[0]
        target = (hop or {}).get("version") or (f"v{hop['minor']}.0" if hop else snap["version"])
    valid, error = validate_target_version(target)
    if not valid:
        return None, error, 400
    target = normalize_version(target)
    book = build_runbook(snap, target, names.get(cluster_id, cluster_id))
    job = None
    if open_guided and open_guided.get("targetVersion") == target:
        job = open_guided
        if book["observedDone"]:
            def _done(payload: Dict[str, Any]) -> None:
                if payload.get("status") == "guided":
                    payload.update(status="completed", message=f"KubeSight saw the cluster reach {target}.",
                                   versionAfter=snap["version"], finishedAt=_iso(), verdict="clean")

            job = change_stored(open_guided["jobId"], _done) or open_guided
    return {
        "clusterId": cluster_id,
        "name": names.get(cluster_id, cluster_id),
        "provider": snap["provider"],
        "providerDisplay": snap["providerDisplay"],
        "mode": snap["mode"],
        "modeReason": snap["modeReason"],
        "version": snap["version"],
        "targetVersion": target,
        "nodes": snap["nodes"],
        "job": job,
        "checkedAt": _iso(),
        **book,
    }, None, 200


def start_guided(user, cluster_id: str, target_version: str) -> Result:
    valid, error = validate_target_version(target_version)
    if not valid:
        return None, error, 400
    target = normalize_version(target_version)
    try:
        snap = cluster_snapshot(cluster_id)
    except LookupError:
        return None, "Cluster not found", 404
    if list_jobs(cluster_ids=[cluster_id], statuses=OPEN_STATUSES, limit=1):
        return None, "This cluster already has an upgrade in progress or waiting.", 409
    actor = _actor(user)
    job = {
        "jobId": new_job_id(),
        "clusterId": cluster_id,
        "clusterName": _cluster_names().get(cluster_id, cluster_id),
        "provider": snap["provider"],
        "mode": "guided",
        "currentVersion": snap["version"],
        "versionBefore": snap["version"],
        "targetVersion": target,
        "status": "guided",
        "message": "Follow the runbook; KubeSight ticks steps off as the cluster changes.",
        "requestedBy": actor,
        "requestedAt": _iso(),
        "startedAt": _iso(),
        "finishedAt": None,
        "executionSupported": False,
    }
    store_request(job)
    log_audit("upgrade_guided_started", actor_user_id=actor["id"], target_type="cluster", target_id=cluster_id,
              details={"jobId": job["jobId"], "targetVersion": target})
    return job, None, 201


def cancel_guided(user, job_id: str) -> Result:
    job = get_job(job_id)
    if not job or job.get("mode") != "guided":
        return None, "Guided upgrade not found", 404
    if job.get("status") != "guided":
        return None, "This guided upgrade is already finished.", 409
    actor = _actor(user)
    stored = change_stored(job_id, lambda p: p.update(status="stopped", message=f"Cancelled by {actor['name']}.",
                                                       finishedAt=_iso()))
    return stored, None, 200


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

def history(user, cluster_id: Optional[str] = None, limit: int = 50) -> Result:
    visible = [str(c["id"]) for c in _visible_clusters(user)]
    if cluster_id:
        if cluster_id not in visible and not _can_access(user, cluster_id):
            return None, "Forbidden", 403
        ids = [cluster_id]
    else:
        ids = visible
    jobs = list_jobs(cluster_ids=ids, limit=limit)
    names = _cluster_names()
    for job in jobs:
        job.setdefault("clusterName", names.get(str(job.get("clusterId")), job.get("clusterId")))
        if not job.get("mode"):
            job["mode"] = "automatic"
    return {"items": jobs}, None, 200


def job_detail(user, job_id: str) -> Result:
    job = get_job(job_id)
    if not job:
        return None, "Upgrade job not found", 404
    if not _can_access(user, str(job.get("clusterId"))):
        return None, "Forbidden", 403
    job = dict(job)
    job["canDecide"] = job.get("status") == "awaiting_approval" and why_cannot_decide(job, user) is None
    job["whyNot"] = why_cannot_decide(job, user) if job.get("status") == "awaiting_approval" else None
    actor = _actor(user)
    job["canWithdraw"] = job.get("status") in PENDING_STATUSES and (
        _is_admin(user) or (actor["id"] is not None and actor["id"] == (job.get("requestedBy") or {}).get("id")))
    job["canControl"] = _has(user, "upgrades:start")
    return job, None, 200
