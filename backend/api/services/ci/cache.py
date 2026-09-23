"""Build cache — the operator's half of it.

The runner half (mounting /cache, pointing every build tool at it) is in
``runners/kubernetes.py``. This module is what somebody can *do* about the
cache from the UI: see whether one exists, create the volume behind it, turn
caching on and off, measure it, and empty it.

Where the setting lives: on the Kubernetes runner's own metadata row. The cache
is part of WHERE builds run, and the alternative — the ``CI_CACHE_*``
environment variables — cannot be changed by a backend that is already running,
so a UI switch built on them would not switch anything. The variables still
work and are still the way to configure this without a UI: they are the
fallback for as long as nothing has been saved here.

Everything that touches the cluster goes through the runner's own kubectl
transport, so it is injectable in tests and honours K8S_KUBECONFIG in
production exactly like a build does.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

from ...db import db
from ...models_ci import CiBuild, CiRunner, CiService
from . import cache_layout

logger = logging.getLogger(__name__)

# The builtin row seeded by migrate_rbac._seed_builtin_ci_runners.
RUNNER_NAME = "kubesight-kubernetes"

DEFAULT_CLAIM = "ci-cache"
DEFAULT_SIZE = "20Gi"
# The same mount path the runner uses, from the same module — a maintenance
# Job that measured or emptied a different directory than builds write to
# would be worse than useless, so neither side gets its own copy.
MOUNT_PATH = cache_layout.CACHE_MOUNT_PATH
# uid/gid stage containers run as; the cache is handed to them by group.
BUILD_UID = cache_layout.CACHE_FS_GROUP
MAINTENANCE_PURPOSE = "ci-cache-maintenance"
ACTIVE_BUILD_STATUSES = ("queued", "running")

_SIZE_RE = re.compile(r"^[1-9][0-9]{0,5}(Mi|Gi|Ti)$")
_DNS_RE = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")


class CacheError(RuntimeError):
    """Something an operator can read and act on."""


# ---------------------------------------------------------------------------
# The kubectl transport and small helpers, imported late to keep this module
# free of an import cycle with the runner that imports it back.
# ---------------------------------------------------------------------------

# kubectl itself could not run: no binary, no kubeconfig, a timeout. Distinct
# from "the cluster said no", because the answer for an operator is different.
UNAVAILABLE = 127


def _kubectl(args: List[str], input_text: Optional[str] = None, timeout: int = 30):
    from .runners import kubernetes as k8s

    try:
        return k8s.kubectl(args, input_text=input_text, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - any failure here is "no cluster"
        # A backend running outside a cluster (a developer's laptop, mock mode)
        # has no kubectl at all. The cache card must still render and say so
        # rather than turning the Runners page into a 500.
        logger.warning("cache kubectl could not run: %s", exc)
        return UNAVAILABLE, "", f"kubectl could not run: {exc}"


def _worker_image() -> str:
    from .runners import kubernetes as k8s

    return k8s.worker_image()


def _dns(value: str, limit: int = 63) -> str:
    safe = re.sub(r"[^a-z0-9-]+", "-", str(value or "").lower()).strip("-")
    return safe[:limit].rstrip("-")


# kubectl ran but never got an answer. "Not there" and "could not look" must
# not be reported as the same thing: one is a missing volume, the other is a
# backend with no cluster access at all.
_UNREACHABLE = (
    "unable to connect to the server",
    "connection refused",
    "no configuration has been provided",
    "the server could not find the requested resource",
    "couldn't get current server api group list",
    "i/o timeout",
)


def _get_json(args: List[str]):
    """(rc, parsed) — the rc matters: UNAVAILABLE means we never reached the
    cluster, which is not the same answer as "it is not there"."""
    rc, out, stderr = _kubectl(args + ["-o", "json"], timeout=20)
    if rc != 0:
        lowered = (stderr or "").lower()
        if any(marker in lowered for marker in _UNREACHABLE):
            return UNAVAILABLE, None
        return rc, None
    try:
        return rc, json.loads(out or "{}")
    except ValueError:
        return rc, None


def _explain(stderr: str) -> str:
    """Turn a kubectl failure into the sentence that fixes it."""
    text = (stderr or "").strip()
    lowered = text.lower()
    if "kubectl could not run" in lowered:
        return (
            "This backend cannot run kubectl, so it cannot manage the cache volume. "
            "It needs to run in the cluster, or K8S_KUBECONFIG has to point at a kubeconfig."
        )
    if "forbidden" in lowered and "persistentvolume" in lowered:
        return (
            "This backend may not create PersistentVolumes. They are cluster-scoped, "
            "so the namespaced CI role cannot grant it — apply k8s/ci-cache-rbac.yaml "
            "(or create the volume yourself with k8s/ci-cache.sh)."
        )
    if "forbidden" in lowered:
        return (
            "The cluster refused this: " + (text.splitlines()[-1] if text else "forbidden")
            + " — check the backend's RBAC in that namespace."
        )
    if not text:
        return "kubectl failed without saying why. The backend log has the full output."
    return text.splitlines()[-1][:400]


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def _runner_row() -> Optional[CiRunner]:
    # Called from manifest building too, which tests exercise with no app
    # context at all — a missing database is "nothing saved", not an error.
    try:
        return CiRunner.query.filter(CiRunner.name == RUNNER_NAME).first()
    except Exception:  # pragma: no cover - depends on app/db state
        return None


def stored_settings() -> Dict[str, Any]:
    """What the UI has saved, or {} when it has never been used."""
    row = _runner_row()
    if row is None:
        return {}
    value = (row.runner_metadata or {}).get("cache")
    return dict(value) if isinstance(value, dict) else {}


def runtime_config() -> Dict[str, str]:
    """The claim to mount and the storage class to provision with — either or
    both empty when caching is off.

    A saved setting wins over the environment in BOTH directions: a switch that
    cannot turn something off is not a switch. With nothing saved, the
    CI_CACHE_* variables still decide, so every install that predates this
    keeps the behaviour it was configured with.
    """
    saved = stored_settings()
    if saved:
        if not saved.get("enabled"):
            return {"claimName": "", "storageClass": ""}
        return {
            "claimName": str(saved.get("claimName") or "").strip(),
            "storageClass": str(saved.get("storageClass") or "").strip(),
        }
    return {
        "claimName": os.getenv("CI_CACHE_CLAIM_NAME", "").strip(),
        "storageClass": os.getenv("CI_CACHE_STORAGE_CLASS", "").strip(),
    }


def shared_tools() -> tuple:
    """The tools that cache into the shared subtree: saved here, else
    ``CI_CACHE_SHARED``, else every shareable tool. Whether sharing applies at
    all (only on the one hand-made claim) is the runner's decision."""
    saved = stored_settings()
    if "shared" in saved:
        return cache_layout.parse_shared(saved.get("shared") or [])
    return cache_layout.parse_shared(os.getenv("CI_CACHE_SHARED"))


def set_shared(tools) -> Dict[str, Any]:
    """Choose which tools share one cache across services.

    Takes effect on the next build. Nothing is moved or deleted: a tool taken
    out of the shared set starts cold once in each service's own subtree, and
    one put in starts cold once in the shared one.
    """
    if not isinstance(tools, (list, tuple)):
        raise CacheError("Send shared as a list of tool names.")
    unknown = [str(t) for t in tools if str(t).strip().lower() not in cache_layout.SHAREABLE_KEYS]
    if unknown:
        raise CacheError(
            "These cannot be shared: " + ", ".join(unknown) + ". Shareable: "
            + ", ".join(cache_layout.SHAREABLE_KEYS) + "."
        )
    save_settings({"shared": list(cache_layout.parse_shared(list(tools)))})
    return status()


def save_settings(patch: Dict[str, Any]) -> Dict[str, Any]:
    row = _runner_row()
    if row is None:
        raise CacheError(
            "The Kubernetes runner row is missing, so there is nowhere to save this. "
            "Restart the backend to finish its migrations."
        )
    merged = {**stored_settings(), **patch}
    metadata = dict(row.runner_metadata or {})
    metadata["cache"] = merged
    row.runner_metadata = metadata
    db.session.add(row)
    db.session.commit()
    return merged


def build_namespace() -> str:
    return os.getenv("CI_KUBERNETES_NAMESPACE", "").strip() or "kubesight-ci"


def cache_namespace() -> str:
    return str(stored_settings().get("namespace") or "").strip() or build_namespace()


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def _claim_state(namespace: str, name: str) -> Dict[str, Any]:
    rc, doc = _get_json(["get", "pvc", name, "-n", namespace])
    if not doc:
        return {
            "exists": False,
            "phase": "",
            "capacity": "",
            "volumeName": "",
            "unknown": rc == UNAVAILABLE,
        }
    spec, status_block = doc.get("spec") or {}, doc.get("status") or {}
    return {
        "exists": True,
        "phase": status_block.get("phase") or "",
        "capacity": (status_block.get("capacity") or {}).get("storage")
        or ((spec.get("resources") or {}).get("requests") or {}).get("storage")
        or "",
        "accessModes": list(spec.get("accessModes") or []),
        "volumeName": spec.get("volumeName") or "",
    }


def _volume_state(name: str) -> Dict[str, Any]:
    if not name:
        return {"exists": False}
    _, doc = _get_json(["get", "pv", name])
    if not doc:
        return {"exists": False}
    spec = doc.get("spec") or {}
    backing: Dict[str, Any] = {"type": "unknown"}
    if spec.get("nfs"):
        backing = {
            "type": "nfs",
            "server": (spec["nfs"] or {}).get("server") or "",
            "path": (spec["nfs"] or {}).get("path") or "",
        }
    elif spec.get("local"):
        backing = {"type": "local", "path": (spec["local"] or {}).get("path") or ""}
        # The node is in the affinity, not the volume source.
        terms = (((spec.get("nodeAffinity") or {}).get("required") or {})
                 .get("nodeSelectorTerms") or [])
        for term in terms:
            for expression in term.get("matchExpressions") or []:
                if expression.get("key") == "kubernetes.io/hostname":
                    backing["node"] = (expression.get("values") or [""])[0]
    elif spec.get("hostPath"):
        backing = {"type": "hostPath", "path": (spec["hostPath"] or {}).get("path") or ""}
    return {
        "exists": True,
        "name": name,
        "capacity": (spec.get("capacity") or {}).get("storage") or "",
        "reclaimPolicy": spec.get("persistentVolumeReclaimPolicy") or "",
        "accessModes": list(spec.get("accessModes") or []),
        "backing": backing,
    }


# The tools KubeSight points at the cache. Shown in the UI so "caching is on"
# is a statement about something concrete rather than a promise.
CACHED_TOOLS = [
    {"tool": "Maven", "path": "maven"},
    {"tool": "Gradle", "path": "gradle"},
    {"tool": "Gradle build cache", "path": "gradle-build-cache"},
    {"tool": "Dependency-Check (NVD)", "path": "dependency-check-data"},
    {"tool": "Semgrep", "path": "semgrep"},
    {"tool": "BuildKit layers", "path": "buildkit"},
    {"tool": "npm", "path": "npm"},
    {"tool": "yarn", "path": "yarn"},
    {"tool": "pnpm", "path": "pnpm"},
    {"tool": "pip", "path": "pip"},
    {"tool": "Go", "path": "go"},
    {"tool": "Cargo", "path": "cargo"},
    {"tool": "Composer", "path": "composer"},
    {"tool": "NuGet", "path": "nuget"},
    {"tool": "Other (XDG)", "path": "xdg"},
]


def status() -> Dict[str, Any]:
    """Everything the cache card needs in one request."""
    saved = stored_settings()
    runtime = runtime_config()
    namespace = cache_namespace()
    builds_ns = build_namespace()
    claim_name = runtime["claimName"] or str(saved.get("claimName") or "") or DEFAULT_CLAIM

    claim = _claim_state(namespace, claim_name)
    volume = _volume_state(claim.get("volumeName") or "")

    warnings: List[str] = []
    if claim.get("unknown"):
        warnings.append(
            "This backend cannot run kubectl, so the volume's state is unknown. The switch "
            "below still works; creating, measuring and cleaning need cluster access."
        )
    elif runtime["claimName"] and not claim["exists"]:
        warnings.append(
            f"Caching is on but no claim named {claim_name} exists in {namespace} — "
            "every build will fail at its first stage until the volume is created."
        )
    elif claim["exists"] and claim["phase"] and claim["phase"] != "Bound":
        warnings.append(
            f"The claim is {claim['phase']}, not Bound. Builds cannot start until it binds; "
            "kubectl describe pvc says why."
        )
    if namespace != builds_ns:
        warnings.append(
            f"This claim is in {namespace} but builds run in {builds_ns}. A claim can only be "
            "mounted from its own namespace, so the cache would be ignored."
        )
    if runtime["storageClass"]:
        warnings.append(
            f"A storage class ({runtime['storageClass']}) is configured, so KubeSight creates "
            "one claim per service itself. Create and Clean here manage the single shared "
            "volume instead and do not apply."
        )

    return {
        "enabled": bool(runtime["claimName"] or runtime["storageClass"]),
        "mode": "claim" if runtime["claimName"] else ("class" if runtime["storageClass"] else "off"),
        # Where the current answer comes from, so nobody hunts for a UI toggle
        # that is being overridden by a ConfigMap, or the reverse.
        "source": "settings" if saved else "environment",
        "claimName": claim_name,
        "namespace": namespace,
        "buildNamespace": builds_ns,
        "storageClass": runtime["storageClass"],
        "mountPath": MOUNT_PATH,
        "size": str(saved.get("size") or (claim.get("capacity") or DEFAULT_SIZE)),
        "clusterReachable": not claim.get("unknown"),
        "claim": claim,
        "volume": volume,
        "tools": CACHED_TOOLS,
        # Which tools every service shares one copy of, and where. Only real on
        # the hand-made claim; storage-class mode keeps everything per service.
        "shared": {
            "applies": not runtime["storageClass"] or bool(runtime["claimName"]),
            "path": cache_layout.shared_cache_dir(MOUNT_PATH),
            "dirName": cache_layout.SHARED_DIR_NAME,
            "tools": list(shared_tools()),
            "options": [
                {"key": key, "label": label, "path": subdir}
                for key, label, subdir, _ in cache_layout.SHAREABLE_TOOLS
            ],
        },
        "maintenance": maintenance_status(namespace),
        "warnings": warnings,
        # Prefill for the create form: this cluster's other volumes are NFS.
        "suggestions": {
            "backing": "nfs",
            "nfsServer": str((saved.get("backing") or {}).get("server") or ""),
            "nfsPath": str((saved.get("backing") or {}).get("path") or ""),
            "size": DEFAULT_SIZE,
            "buildUid": BUILD_UID,
        },
    }


# ---------------------------------------------------------------------------
# Creating the volume
# ---------------------------------------------------------------------------

def _require_dns(value: str, field: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned or len(cleaned) > 63 or not _DNS_RE.match(cleaned):
        raise CacheError(
            f"{field} must be a Kubernetes name: lower-case letters, digits and dashes."
        )
    return cleaned


def _require_path(value: str, field: str) -> str:
    cleaned = str(value or "").strip()
    if not cleaned.startswith("/") or ".." in cleaned or len(cleaned) > 512:
        raise CacheError(f"{field} must be an absolute path.")
    return cleaned


def create_volume(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Create the PersistentVolume and its claim, then remember them.

    Deliberately create-only. Resizing or re-pathing a bound volume is not a
    thing Kubernetes allows in place, and deleting one from a web page is not a
    button this needs.
    """
    namespace = _require_dns(payload.get("namespace") or build_namespace(), "The namespace")
    claim_name = _require_dns(payload.get("claimName") or DEFAULT_CLAIM, "The claim name")
    size = str(payload.get("size") or DEFAULT_SIZE).strip()
    if not _SIZE_RE.match(size):
        raise CacheError("The size must look like 20Gi, 500Mi or 1Ti.")

    backing_type = str(payload.get("backing") or "nfs").strip().lower()
    if backing_type == "nfs":
        server = str(payload.get("nfsServer") or "").strip()
        if not server or len(server) > 253:
            raise CacheError("An NFS server address is required.")
        path = _require_path(payload.get("nfsPath"), "The NFS export path")
        source: Dict[str, Any] = {"nfs": {"server": server, "path": path}}
        access_modes = ["ReadWriteMany"]
        remembered = {"type": "nfs", "server": server, "path": path}
    elif backing_type == "local":
        node = _require_dns(payload.get("node"), "The node name")
        path = _require_path(payload.get("path"), "The directory on the node")
        source = {
            "local": {"path": path},
            "nodeAffinity": {
                "required": {
                    "nodeSelectorTerms": [
                        {
                            "matchExpressions": [
                                {
                                    "key": "kubernetes.io/hostname",
                                    "operator": "In",
                                    "values": [node],
                                }
                            ]
                        }
                    ]
                }
            },
        }
        # One node holds the directory, so RWO is the honest access mode and
        # every build pod ends up pinned to that node.
        access_modes = ["ReadWriteOnce"]
        remembered = {"type": "local", "node": node, "path": path}
    else:
        raise CacheError("The backing store must be nfs or local.")

    existing = _claim_state(namespace, claim_name)
    if existing["exists"]:
        raise CacheError(
            f"A claim named {claim_name} already exists in {namespace} ({existing['phase']}). "
            "Use it, or delete it and its volume first — deleting them leaves the cached "
            "files on the server untouched."
        )

    pv_name = _dns(f"kubesight-{namespace}-{claim_name}")
    volume = {
        "apiVersion": "v1",
        "kind": "PersistentVolume",
        "metadata": {
            "name": pv_name,
            "labels": {"kubesight.io/purpose": "ci-dependency-cache"},
        },
        "spec": {
            "capacity": {"storage": size},
            "volumeMode": "Filesystem",
            "accessModes": access_modes,
            # Retain: the cache must outlive the claim, or "delete the claim"
            # silently becomes "throw away every dependency".
            "persistentVolumeReclaimPolicy": "Retain",
            # Not a class name: "do not involve dynamic provisioning".
            "storageClassName": "",
            # Pre-bound, so no other pending claim in the cluster can take it.
            "claimRef": {"namespace": namespace, "name": claim_name},
            **source,
        },
    }
    claim = {
        "apiVersion": "v1",
        "kind": "PersistentVolumeClaim",
        "metadata": {
            "name": claim_name,
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/name": "kubesight-ci",
                "kubesight.io/purpose": "ci-dependency-cache",
            },
        },
        "spec": {
            "accessModes": access_modes,
            "storageClassName": "",
            "volumeName": pv_name,
            "resources": {"requests": {"storage": size}},
        },
    }

    rc, _, stderr = _kubectl(
        ["apply", "-f", "-"],
        input_text=json.dumps({"apiVersion": "v1", "kind": "List", "items": [volume, claim]}),
        timeout=60,
    )
    if rc != 0:
        logger.error("CI cache volume apply failed: %s", (stderr or "")[-2000:])
        raise CacheError(_explain(stderr))

    save_settings(
        {
            "namespace": namespace,
            "claimName": claim_name,
            "size": size,
            "backing": remembered,
            # Creating a volume is not the same as switching caching on: the
            # export still has to be writable by the build uid, and `verify`
            # (or the first build) is what proves it. Left to the operator.
            "enabled": bool(stored_settings().get("enabled")),
        }
    )
    return status()


# ---------------------------------------------------------------------------
# On / off
# ---------------------------------------------------------------------------

def set_enabled(enabled: bool) -> Dict[str, Any]:
    saved = stored_settings()
    if enabled:
        namespace = cache_namespace()
        claim_name = str(saved.get("claimName") or os.getenv("CI_CACHE_CLAIM_NAME", "").strip()
                         or DEFAULT_CLAIM)
        state = _claim_state(namespace, claim_name)
        # Turning it on with nothing behind it would fail every build at its
        # first stage — worse than the cold builds it is meant to replace.
        if not state["exists"]:
            raise CacheError(
                f"There is no claim named {claim_name} in {namespace} to cache into. "
                "Create the volume first."
            )
        if state["phase"] and state["phase"] != "Bound":
            raise CacheError(
                f"The claim {claim_name} is {state['phase']}, not Bound. "
                "Builds cannot mount it yet."
            )
        save_settings({"enabled": True, "claimName": claim_name, "namespace": namespace})
    else:
        # Nothing is deleted: re-enabling picks the same warm cache back up.
        save_settings({"enabled": False})
    return status()


# ---------------------------------------------------------------------------
# Maintenance: measure and empty, from inside the cluster
# ---------------------------------------------------------------------------

def _maintenance_jobs(namespace: str) -> List[Dict[str, Any]]:
    _, doc = _get_json(
        ["get", "jobs", "-n", namespace, "-l", f"kubesight.io/purpose={MAINTENANCE_PURPOSE}"]
    )
    items = (doc or {}).get("items") or []
    return sorted(items, key=lambda job: (job.get("metadata") or {}).get("creationTimestamp") or "")


def _parse_usage(output: str) -> List[Dict[str, str]]:
    """`du -sh` lines into rows. Anything unexpected is skipped rather than
    guessed at — a wrong size is worse than no size."""
    rows: List[Dict[str, str]] = []
    for line in (output or "").splitlines():
        parts = line.split("\t")
        if len(parts) != 2:
            continue
        size, path = parts[0].strip(), parts[1].strip()
        if not size or not path.startswith(MOUNT_PATH):
            continue
        name = path[len(MOUNT_PATH):].strip("/")
        rows.append({"service": name or "(total)", "size": size})
    return rows


def maintenance_status(namespace: Optional[str] = None) -> Dict[str, Any]:
    """The newest measure/clean job: whether it is running, and what it said."""
    namespace = namespace or cache_namespace()
    jobs = _maintenance_jobs(namespace)
    if not jobs:
        return {"job": "", "kind": "", "phase": "idle"}
    job = jobs[-1]
    meta, state = job.get("metadata") or {}, job.get("status") or {}
    kind = (meta.get("labels") or {}).get("kubesight.io/cache-op") or ""
    name = meta.get("name") or ""
    if state.get("active"):
        return {"job": name, "kind": kind, "phase": "running"}
    phase = "succeeded" if state.get("succeeded") else ("failed" if state.get("failed") else "unknown")

    output = ""
    rc, logs, _ = _kubectl(["logs", f"job/{name}", "-n", namespace, "--tail", "200"], timeout=20)
    if rc == 0:
        output = logs or ""
    result: Dict[str, Any] = {
        "job": name,
        "kind": kind,
        "phase": phase,
        "finishedAt": state.get("completionTime") or "",
        "output": output[-4000:],
    }
    if kind == "measure" and phase == "succeeded":
        result["usage"] = _parse_usage(output)
    return result


def _start_job(kind: str, script: str, namespace: str) -> Dict[str, Any]:
    running = maintenance_status(namespace)
    if running.get("phase") == "running":
        raise CacheError(
            f"A cache {running.get('kind') or 'maintenance'} job is still running. "
            "Wait for it to finish."
        )

    name = f"ci-cache-{_dns(kind)}-{int(time.time())}"
    claim_name = status_claim_name()
    job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "labels": {
                "app.kubernetes.io/name": "kubesight-ci",
                "kubesight.io/purpose": MAINTENANCE_PURPOSE,
                "kubesight.io/cache-op": kind,
            },
        },
        "spec": {
            "backoffLimit": 0,
            "activeDeadlineSeconds": 1800,
            # Long enough for the UI to read the result, short enough not to
            # accumulate: the log IS the result here.
            "ttlSecondsAfterFinished": 900,
            "template": {
                "metadata": {
                    "labels": {
                        "app.kubernetes.io/name": "kubesight-ci",
                        "kubesight.io/purpose": MAINTENANCE_PURPOSE,
                    }
                },
                "spec": {
                    "restartPolicy": "Never",
                    "automountServiceAccountToken": False,
                    "enableServiceLinks": False,
                    "securityContext": {
                        "runAsNonRoot": True,
                        # Same reason the build Job does this: the volume
                        # arrives owned by root and this pod cannot chown it.
                        "fsGroup": BUILD_UID,
                        "fsGroupChangePolicy": "OnRootMismatch",
                        "seccompProfile": {"type": "RuntimeDefault"},
                    },
                    "containers": [
                        {
                            "name": "cache",
                            "image": _worker_image(),
                            "imagePullPolicy": os.getenv("CI_IMAGE_PULL_POLICY", "IfNotPresent"),
                            "command": ["/bin/sh", "-c", script],
                            # Identical to a stage container, so anything that
                            # works here works in a build — and the namespace's
                            # restricted Pod Security Standard admits it.
                            "securityContext": {
                                "allowPrivilegeEscalation": False,
                                "readOnlyRootFilesystem": True,
                                "runAsNonRoot": True,
                                "runAsUser": BUILD_UID,
                                "runAsGroup": BUILD_UID,
                                "capabilities": {"drop": ["ALL"]},
                            },
                            "resources": {
                                "requests": {"cpu": "50m", "memory": "64Mi"},
                                "limits": {"cpu": "500m", "memory": "512Mi"},
                            },
                            "volumeMounts": [{"name": "cache", "mountPath": MOUNT_PATH}],
                        }
                    ],
                    "volumes": [
                        {
                            "name": "cache",
                            "persistentVolumeClaim": {"claimName": claim_name},
                        }
                    ],
                },
            },
        },
    }
    rc, _, stderr = _kubectl(["apply", "-f", "-"], input_text=json.dumps(job), timeout=45)
    if rc != 0:
        logger.error("CI cache %s job failed to start: %s", kind, (stderr or "")[-2000:])
        raise CacheError(_explain(stderr))
    return {"job": name, "kind": kind, "phase": "running"}


def status_claim_name() -> str:
    saved = stored_settings()
    return (
        runtime_config()["claimName"]
        or str(saved.get("claimName") or "")
        or os.getenv("CI_CACHE_CLAIM_NAME", "").strip()
        or DEFAULT_CLAIM
    )


def _require_claim_mode() -> str:
    """Measure and clean act on the one shared claim. Per-service claims made
    by a storage class are a different shape and are left to the operator."""
    namespace = cache_namespace()
    claim_name = status_claim_name()
    state = _claim_state(namespace, claim_name)
    if not state["exists"]:
        raise CacheError(
            f"There is no claim named {claim_name} in {namespace}. "
            "Create the cache volume first."
        )
    return namespace


def measure() -> Dict[str, Any]:
    namespace = _require_claim_mode()
    # Read-only. The trailing total is what the UI shows against the capacity.
    return _start_job(
        "measure",
        f"du -sh {MOUNT_PATH}/* 2>/dev/null; du -sh {MOUNT_PATH} 2>/dev/null; echo done",
        namespace,
    )


def _active_builds(service_id: Optional[int]) -> List[str]:
    query = CiBuild.query.filter(CiBuild.status.in_(ACTIVE_BUILD_STATUSES))
    if service_id is not None:
        query = query.filter(CiBuild.service_id == service_id)
    return [f"#{row.number}" for row in query.limit(10).all()]


def clean(
    service_id: Optional[int] = None, all_services: bool = False, shared: bool = False
) -> Dict[str, Any]:
    """Empty one service's cache, the shared one, or all of them.

    Refuses while a build that would be reading those files is running:
    deleting a Gradle cache underneath a build fails it with errors that look
    nothing like the cause.
    """
    namespace = _require_claim_mode()

    if shared and not all_services:
        # Any running build may be reading it, whichever service it belongs to.
        busy = _active_builds(None)
        if busy:
            raise CacheError(
                "Builds are running (" + ", ".join(busy) + "). The shared cache is read by "
                "every service, so emptying it now could fail any of them. Try again when "
                "they finish."
            )
        target_dir = cache_layout.shared_cache_dir(MOUNT_PATH)
        script = f"rm -rf {target_dir}; echo cleaned {target_dir}"
        target = "the shared cache"
    elif all_services:
        busy = _active_builds(None)
        if busy:
            raise CacheError(
                "Builds are running (" + ", ".join(busy) + "). Emptying the cache underneath "
                "them would fail them with errors that look unrelated. Try again when they finish."
            )
        # -mindepth 1 empties the directory without removing the mount point.
        script = (
            f"find {MOUNT_PATH} -mindepth 1 -maxdepth 1 -exec rm -rf {{}} + ; "
            "echo cleaned everything"
        )
        target = "every service"
    else:
        service = db.session.get(CiService, int(service_id)) if service_id else None
        if service is None:
            raise CacheError("That service does not exist.")
        # The runner's own rule for the directory name, so this deletes the
        # subtree that service's builds actually wrote — including for a
        # service whose slug sanitises to nothing, which still HAS a cache.
        slug = cache_layout.slug_dir(service.slug or "")
        busy = _active_builds(service.id)
        if busy:
            raise CacheError(
                f"{service.name} has a build running (" + ", ".join(busy) + "). "
                "Emptying its cache now would fail that build. Try again when it finishes."
            )
        target_dir = f"{MOUNT_PATH}/{slug}"
        script = f"rm -rf {target_dir}; echo cleaned {target_dir}"
        target = service.name

    result = _start_job("clean", script, namespace)
    result["target"] = target
    return result
