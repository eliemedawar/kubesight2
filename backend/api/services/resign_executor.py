"""Drives an external signing job for a mobile binary.

KubeSight cannot sign anything itself: Android needs the upload keystore and
iOS needs macOS with a keychain, and neither belongs in a Flask pod. What it
can do is run the signer somewhere the key already lives and collect the
result — which is all this module does.

The Android executor is a Kubernetes Job. KubeSight already drives clusters by
shelling out to ``kubectl``, so launching a Job is the same plumbing it uses
everywhere else, and the keystore stays in a Kubernetes Secret the Job mounts —
it never reaches KubeSight's database or image.

Transfer is over HTTP in both directions: the Job pulls the unsigned binary
from KubeSight's existing build-download route and POSTs the signed one back to
the resign-result route, both with a short-lived token scoped to that single
run. That keeps the executor independent of where the artifact store lives, so
no shared volume has to exist between the API pod and the Job.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import yaml

from ..k8s_provider import K8sCommandError, _run_for_access, resolve_cluster_access

logger = logging.getLogger(__name__)

# Job/Secret name prefix. The resign id makes it unique and greppable.
_NAME_PREFIX = "kubesight-resign"

# Signing is CPU-light and quick; anything past this is wedged, not slow.
JOB_DEADLINE_SECONDS = 900
# Leave finished Jobs around briefly so their logs remain fetchable for
# diagnosis, then let the cluster reap them.
JOB_TTL_SECONDS = 3600


class ResignExecutorError(Exception):
    """The signing job could not be launched, read, or cleaned up."""


@dataclass
class ResignJobSpec:
    """Everything needed to run one signing job."""

    resign_id: int
    build_id: int
    artifact_type: str  # apk | aab
    cluster: str
    namespace: str
    image: str
    callback_url: str
    token: str
    keystore_secret: str
    keystore_key: str = "upload.jks"
    key_alias: str = "upload"
    store_pass_key: str = "store-password"
    key_pass_key: str = "key-password"
    service_account: str = ""
    image_pull_secret: str = ""


def job_name(resign_id: int) -> str:
    return f"{_NAME_PREFIX}-{int(resign_id)}"


def _env(spec: ResignJobSpec) -> List[Dict[str, Any]]:
    """Job environment. The keystore passwords are pulled straight from the
    Secret by reference so they never pass through KubeSight."""
    return [
        {"name": "KUBESIGHT_URL", "value": spec.callback_url},
        {"name": "BUILD_ID", "value": str(spec.build_id)},
        {"name": "RESIGN_ID", "value": str(spec.resign_id)},
        {"name": "ARTIFACT_TYPE", "value": spec.artifact_type},
        {"name": "KEY_ALIAS", "value": spec.key_alias},
        {"name": "KEYSTORE_PATH", "value": f"/keys/{spec.keystore_key}"},
        {
            "name": "RESIGN_TOKEN",
            "valueFrom": {
                # Held in its own Secret rather than inlined, so the token does
                # not show up in `kubectl get job -o yaml`.
                "secretKeyRef": {"name": job_name(spec.resign_id), "key": "token"}
            },
        },
        {
            "name": "STORE_PASS",
            "valueFrom": {
                "secretKeyRef": {"name": spec.keystore_secret, "key": spec.store_pass_key}
            },
        },
        {
            "name": "KEY_PASS",
            "valueFrom": {
                "secretKeyRef": {"name": spec.keystore_secret, "key": spec.key_pass_key}
            },
        },
    ]


def render_manifests(spec: ResignJobSpec) -> str:
    """The token Secret + the Job, as one multi-document YAML string."""
    name = job_name(spec.resign_id)
    labels = {
        "app.kubernetes.io/managed-by": "kubesight",
        "app.kubernetes.io/component": "mobile-resign",
        "kubesight.io/resign-id": str(spec.resign_id),
    }

    token_secret = {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": {"name": name, "namespace": spec.namespace, "labels": labels},
        "type": "Opaque",
        "stringData": {"token": spec.token},
    }

    pod_spec: Dict[str, Any] = {
        "restartPolicy": "Never",
        # fsGroup makes the emptyDir volumes group-writable, without which a
        # non-root signer cannot write the binary it just downloaded.
        "securityContext": {
            "runAsNonRoot": True,
            "runAsUser": 65532,
            "runAsGroup": 65532,
            "fsGroup": 65532,
        },
        "containers": [
            {
                "name": "signer",
                "image": spec.image,
                "imagePullPolicy": "IfNotPresent",
                "env": _env(spec),
                "volumeMounts": [
                    {"name": "keystore", "mountPath": "/keys", "readOnly": True},
                    {"name": "work", "mountPath": "/work"},
                    # The JDK wants a writable temp dir, which a read-only root
                    # filesystem otherwise denies it.
                    {"name": "tmp", "mountPath": "/tmp"},
                ],
                "securityContext": {
                    "allowPrivilegeEscalation": False,
                    "readOnlyRootFilesystem": True,
                    "capabilities": {"drop": ["ALL"]},
                },
            }
        ],
        "volumes": [
            {"name": "keystore", "secret": {"secretName": spec.keystore_secret}},
            {"name": "work", "emptyDir": {}},
            {"name": "tmp", "emptyDir": {}},
        ],
    }
    if spec.service_account:
        pod_spec["serviceAccountName"] = spec.service_account
    if spec.image_pull_secret:
        pod_spec["imagePullSecrets"] = [{"name": spec.image_pull_secret}]

    job = {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": name, "namespace": spec.namespace, "labels": labels},
        "spec": {
            # A signing failure is deterministic — retrying just repeats it and
            # muddies which pod's logs explain the failure.
            "backoffLimit": 0,
            "ttlSecondsAfterFinished": JOB_TTL_SECONDS,
            "activeDeadlineSeconds": JOB_DEADLINE_SECONDS,
            "template": {"metadata": {"labels": labels}, "spec": pod_spec},
        },
    }
    return yaml.safe_dump_all([token_secret, job], sort_keys=False)


def _kubectl(spec_or_cluster, args: List[str], timeout: Optional[int] = None) -> str:
    cluster = (
        spec_or_cluster.cluster
        if isinstance(spec_or_cluster, ResignJobSpec)
        else str(spec_or_cluster)
    )
    access = resolve_cluster_access(cluster)
    if not access:
        raise ResignExecutorError(f"Cluster not found: {cluster}")
    try:
        return _run_for_access(access, args, timeout=timeout)
    except K8sCommandError as exc:
        raise ResignExecutorError(str(exc)) from exc


def launch(spec: ResignJobSpec) -> Dict[str, Any]:
    """Create the token Secret and the Job. Returns the job reference."""
    manifests = render_manifests(spec)
    fd, path = tempfile.mkstemp(suffix=".yaml", prefix="kubesight-resign-")
    os.close(fd)
    try:
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(manifests)
        _kubectl(spec, ["apply", "-f", path, "-n", spec.namespace])
    finally:
        try:
            os.remove(path)
        except OSError:
            pass
    return {
        "kind": "k8s_job",
        "name": job_name(spec.resign_id),
        "namespace": spec.namespace,
        "cluster": spec.cluster,
    }


def poll(job_ref: Dict[str, Any]) -> Dict[str, Any]:
    """Current job phase: ``running`` | ``succeeded`` | ``failed``.

    A Job that has vanished reads as failed rather than running forever —
    someone deleted it, or its TTL reaped it before we looked.
    """
    name = job_ref.get("name") or ""
    namespace = job_ref.get("namespace") or "default"
    cluster = job_ref.get("cluster") or ""
    try:
        raw = _kubectl(cluster, ["get", "job", name, "-n", namespace, "-o", "json"])
    except ResignExecutorError as exc:
        if "not found" in str(exc).lower():
            return {"phase": "failed", "detail": "the signing job is gone from the cluster"}
        raise

    try:
        status = (json.loads(raw) or {}).get("status") or {}
    except (ValueError, TypeError):
        return {"phase": "running", "detail": "job status unreadable"}

    if int(status.get("succeeded") or 0) > 0:
        return {"phase": "succeeded", "detail": "signing job completed"}
    if int(status.get("failed") or 0) > 0:
        reason = ""
        for cond in status.get("conditions") or []:
            if cond.get("type") == "Failed" and cond.get("status") == "True":
                reason = cond.get("message") or cond.get("reason") or ""
                break
        return {"phase": "failed", "detail": reason or "the signing job failed"}
    return {"phase": "running", "detail": "signing in progress"}


def logs(job_ref: Dict[str, Any], tail: int = 40) -> str:
    """Job pod logs, best-effort — used to explain a failure, never to drive it."""
    name = job_ref.get("name") or ""
    namespace = job_ref.get("namespace") or "default"
    cluster = job_ref.get("cluster") or ""
    try:
        return _kubectl(
            cluster,
            ["logs", f"job/{name}", "-n", namespace, f"--tail={int(tail)}"],
        ).strip()
    except Exception:
        return ""


def cleanup(job_ref: Dict[str, Any]) -> None:
    """Drop the Job and its token Secret. Best-effort: a leftover object must
    never turn a successful signing into a failed one."""
    name = job_ref.get("name") or ""
    namespace = job_ref.get("namespace") or "default"
    cluster = job_ref.get("cluster") or ""
    if not name:
        return
    for target in (f"job/{name}", f"secret/{name}"):
        try:
            _kubectl(cluster, ["delete", target, "-n", namespace, "--ignore-not-found"])
        except Exception:
            logger.warning("Resign cleanup failed for %s in %s", target, namespace)
