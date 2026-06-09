"""Safe YAML deployment: validate, dry-run, diff, and apply."""

from __future__ import annotations

import ctypes
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml

from ..access_engine import can_access_namespace, is_admin, user_has_permission
from ..audit import log_audit
from ..k8s_provider import K8sCommandError, resolve_cluster_access
from ..models import User

RunKubectlFn = Callable[..., str]

ALLOWED_NAMESPACED_KINDS = {
    "Deployment",
    "Service",
    "ConfigMap",
    "Secret",
    "StatefulSet",
    "DaemonSet",
    "Job",
    "CronJob",
    "Ingress",
    "PersistentVolumeClaim",
    "HorizontalPodAutoscaler",
}

BLOCKED_CLUSTER_KINDS = {
    "ClusterRole",
    "ClusterRoleBinding",
    "RoleBinding",
    "MutatingWebhookConfiguration",
    "ValidatingWebhookConfiguration",
    "PodSecurityPolicy",
    "Namespace",
    "Node",
    "PersistentVolume",
}

SECRET_DATA_KEYS = ("data", "stringData")


def _run_kubectl_for_cluster(cluster_id: str, args: List[str]) -> str:
    from ..k8s_provider import _run_for_access

    access = resolve_cluster_access(cluster_id)
    if not access:
        raise K8sCommandError(f"Cluster not found: {cluster_id}")
    return _run_for_access(access, args)


def parse_yaml_documents(yaml_content: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    if not yaml_content or not yaml_content.strip():
        return [], "YAML content is empty"
    try:
        docs = list(yaml.safe_load_all(yaml_content))
    except yaml.YAMLError as exc:
        return [], f"Invalid YAML syntax: {exc}"
    documents = [doc for doc in docs if doc]
    if not documents:
        return [], "No Kubernetes resources found in YAML"
    return documents, None


def _resource_key(doc: Dict[str, Any]) -> Tuple[str, str, str]:
    kind = doc.get("kind") or "Unknown"
    meta = doc.get("metadata") or {}
    name = meta.get("name") or "unknown"
    namespace = meta.get("namespace") or ""
    return kind, namespace, name


def analyze_resources(
    documents: List[Dict[str, Any]],
    target_namespace: str,
    *,
    user: Optional[User] = None,
    preview_mode: bool = False,
) -> Dict[str, Any]:
    resources: List[Dict[str, str]] = []
    warnings: List[str] = []
    blocked: List[str] = []
    has_secrets = False

    for doc in documents:
        kind = doc.get("kind") or "Unknown"
        meta = doc.get("metadata") or {}
        name = meta.get("name") or "unknown"
        ns = meta.get("namespace") or target_namespace

        if kind in BLOCKED_CLUSTER_KINDS:
            if preview_mode or (user and is_admin(user)):
                warnings.append(f"{kind}/{name} is a cluster-scoped or sensitive resource — proceed with caution")
            else:
                blocked.append(f"{kind}/{name} is not allowed for non-admin deployments")
                continue

        if kind not in ALLOWED_NAMESPACED_KINDS and kind not in BLOCKED_CLUSTER_KINDS:
            warnings.append(f"{kind}/{name} is not in the default allowlist")

        if kind == "Secret":
            has_secrets = True
            if any(key in doc for key in SECRET_DATA_KEYS):
                warnings.append(f"Secret/{name} contains secret values — only metadata will be shown in preview")

        resources.append({"kind": kind, "name": name, "namespace": ns, "action": "create/update"})

    return {
        "resources": resources,
        "warnings": warnings,
        "blocked": blocked,
        "hasSecrets": has_secrets,
    }


def validate_yaml(yaml_content: str, namespace: str, user: Optional[User] = None) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    documents, err = parse_yaml_documents(yaml_content)
    if err:
        return None, err, 400

    analysis = analyze_resources(documents, namespace, user=user)
    if analysis["blocked"]:
        return None, f"Blocked resources: {', '.join(analysis['blocked'])}", 403

    return {
        "valid": True,
        "resourceCount": len(documents),
        **analysis,
    }, None, 200


def _write_temp_yaml(yaml_content: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".yaml", prefix="kubesight-deploy-")
    os.close(fd)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(yaml_content)
    return path


def _cleanup_temp(path: str) -> None:
    try:
        os.remove(path)
    except OSError:
        pass


def dry_run_yaml(
    user: Optional[User],
    cluster_id: str,
    namespace: str,
    yaml_content: str,
    run_kubectl: Optional[RunKubectlFn] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    if user and not user_has_permission(user, "apps:dryrun"):
        log_audit(
            "unauthorized_deployment_attempt",
            actor=user,
            target_type="deployment",
            details={"action": "dry-run", "cluster": cluster_id, "namespace": namespace},
        )
        return None, "Forbidden", 403

    if user and not can_access_namespace(user, cluster_id, namespace):
        log_audit(
            "unauthorized_deployment_attempt",
            actor=user,
            target_type="namespace",
            target_id=f"{cluster_id}/{namespace}",
            details={"action": "dry-run"},
        )
        return None, "Forbidden", 403

    validation, err, code = validate_yaml(yaml_content, namespace, user=user)
    if err:
        return None, err, code

    path = _write_temp_yaml(yaml_content)
    try:
        runner = run_kubectl or _run_kubectl_for_cluster
        output = runner(
            cluster_id,
            ["apply", "--dry-run=server", "-f", path, "-n", namespace],
        )
        log_audit(
            "deployment_dry_run",
            actor=user,
            target_type="deployment",
            target_id=f"{cluster_id}/{namespace}",
            details={
                "cluster": cluster_id,
                "namespace": namespace,
                "resources": validation.get("resources") if validation else [],
                "result": "success",
            },
        )
        return {
            "dryRun": True,
            "output": output,
            **(validation or {}),
        }, None, 200
    except K8sCommandError as exc:
        log_audit(
            "deployment_failed",
            actor=user,
            target_type="deployment",
            target_id=f"{cluster_id}/{namespace}",
            details={"action": "dry-run", "error": str(exc), "result": "failed"},
        )
        return None, str(exc), 503
    finally:
        _cleanup_temp(path)


def diff_yaml(
    user: Optional[User],
    cluster_id: str,
    namespace: str,
    yaml_content: str,
    run_kubectl: Optional[RunKubectlFn] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    if user and not user_has_permission(user, "apps:diff"):
        log_audit(
            "unauthorized_deployment_attempt",
            actor=user,
            target_type="deployment",
            details={"action": "diff", "cluster": cluster_id, "namespace": namespace},
        )
        return None, "Forbidden", 403

    if user and not can_access_namespace(user, cluster_id, namespace):
        return None, "Forbidden", 403

    validation, err, code = validate_yaml(yaml_content, namespace, user=user)
    if err:
        return None, err, code

    path = _write_temp_yaml(yaml_content)
    try:
        if run_kubectl:
            try:
                diff_output = run_kubectl(cluster_id, ["diff", "-f", path, "-n", namespace])
            except K8sCommandError as exc:
                diff_output = str(exc)
        else:
            diff_output = _run_kubectl_diff(cluster_id, path, namespace)

        log_audit(
            "deployment_diff",
            actor=user,
            target_type="deployment",
            target_id=f"{cluster_id}/{namespace}",
            details={"cluster": cluster_id, "namespace": namespace, "result": "success"},
        )
        return {"diff": diff_output, **(validation or {})}, None, 200
    except K8sCommandError as exc:
        log_audit(
            "deployment_failed",
            actor=user,
            target_type="deployment",
            target_id=f"{cluster_id}/{namespace}",
            details={"action": "diff", "error": str(exc), "result": "failed"},
        )
        return None, str(exc), 503
    finally:
        _cleanup_temp(path)


def _windows_diff_candidates() -> List[Path]:
    local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
    candidates = [
        Path(r"C:\Program Files\Git\usr\bin\diff.exe"),
        Path(r"C:\Program Files (x86)\Git\usr\bin\diff.exe"),
        Path(r"C:\Program Files\GnuWin32\bin\diff.exe"),
        Path(r"C:\Program Files (x86)\GnuWin32\bin\diff.exe"),
    ]
    if local_app_data:
        candidates.append(
            Path(local_app_data) / "Programs" / "Git" / "usr" / "bin" / "diff.exe"
        )
    return candidates


def _windows_short_path(path: str) -> str:
    """Return the 8.3 short path so kubectl can run executables with spaces."""
    if os.name != "nt" or " " not in path:
        return path

    get_short_path_name = ctypes.windll.kernel32.GetShortPathNameW
    buffer = ctypes.create_unicode_buffer(512)
    if get_short_path_name(path, buffer, len(buffer)) == 0:
        return path
    return buffer.value or path


def kubectl_external_diff_env_value(diff_executable: str) -> str:
    """Format a diff executable path for KUBECTL_EXTERNAL_DIFF."""
    return _windows_short_path(diff_executable)


def resolve_kubectl_external_diff() -> Optional[str]:
    """Find a diff executable for kubectl diff (required on Windows)."""
    configured = os.environ.get("KUBECTL_EXTERNAL_DIFF", "").strip()
    if configured:
        return configured

    found = shutil.which("diff")
    if found:
        return found

    if os.name == "nt":
        for candidate in _windows_diff_candidates():
            if candidate.is_file():
                return str(candidate)
    return None


def friendly_kubectl_diff_error(stderr: str) -> str:
    text = (stderr or "").strip()
    lowered = text.lower()
    if "executable file not found" in lowered or 'failed to run "diff"' in lowered:
        return (
            "Diff preview needs a diff utility on the machine running the backend. "
            "On Windows, install Git for Windows or run: choco install diffutils — "
            "then add diff.exe to PATH and restart the backend (python app.py). "
            "Alternatively set KUBECTL_EXTERNAL_DIFF to the full path of diff.exe."
        )
    return text or "kubectl diff failed"


def _run_kubectl_diff(cluster_id: str, path: str, namespace: str) -> str:
    """Run kubectl diff; exit code 1 means differences were found (not an error)."""
    import subprocess

    from ..k8s_provider import resolve_cluster_access

    access = resolve_cluster_access(cluster_id)
    if not access:
        raise K8sCommandError(f"Cluster not found: {cluster_id}")

    command = ["kubectl"]
    if access.kubeconfig_path:
        command += ["--kubeconfig", access.kubeconfig_path]
    if access.context_name:
        command += ["--context", access.context_name]
    command += ["diff", "-f", path, "-n", namespace]

    env = os.environ.copy()
    if access.kubeconfig_path:
        env["KUBECONFIG"] = access.kubeconfig_path

    diff_executable = resolve_kubectl_external_diff()
    if diff_executable:
        env["KUBECTL_EXTERNAL_DIFF"] = kubectl_external_diff_env_value(diff_executable)

    completed = subprocess.run(command, capture_output=True, text=True, check=False, env=env)
    if completed.returncode == 0:
        return completed.stdout or "No differences found."
    if completed.returncode == 1:
        return completed.stdout or completed.stderr or "Differences detected."
    stderr = (completed.stderr or "").strip()
    raise K8sCommandError(friendly_kubectl_diff_error(stderr))


def apply_yaml(
    user: Optional[User],
    cluster_id: str,
    namespace: str,
    yaml_content: str,
    confirmation: str,
    run_kubectl: Optional[RunKubectlFn] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    if user and not user_has_permission(user, "apps:deploy"):
        log_audit(
            "unauthorized_deployment_attempt",
            actor=user,
            target_type="deployment",
            details={"action": "apply", "cluster": cluster_id, "namespace": namespace},
        )
        return None, "Forbidden", 403

    if user and not can_access_namespace(user, cluster_id, namespace):
        log_audit(
            "unauthorized_deployment_attempt",
            actor=user,
            target_type="namespace",
            target_id=f"{cluster_id}/{namespace}",
            details={"action": "apply"},
        )
        return None, "Forbidden", 403

    expected = f"APPLY {namespace}"
    if (confirmation or "").strip() != expected:
        return None, f"Confirmation must be exactly: {expected}", 400

    validation, err, code = validate_yaml(yaml_content, namespace, user=user)
    if err:
        return None, err, code

    path = _write_temp_yaml(yaml_content)
    try:
        runner = run_kubectl or _run_kubectl_for_cluster
        output = runner(cluster_id, ["apply", "-f", path, "-n", namespace])
        log_audit(
            "deployment_applied",
            actor=user,
            target_type="deployment",
            target_id=f"{cluster_id}/{namespace}",
            details={
                "cluster": cluster_id,
                "namespace": namespace,
                "resources": validation.get("resources") if validation else [],
                "result": "success",
            },
        )
        return {"applied": True, "output": output, **(validation or {})}, None, 200
    except K8sCommandError as exc:
        log_audit(
            "deployment_failed",
            actor=user,
            target_type="deployment",
            target_id=f"{cluster_id}/{namespace}",
            details={"action": "apply", "error": str(exc), "result": "failed"},
        )
        return None, str(exc), 503
    finally:
        _cleanup_temp(path)


def sanitize_yaml_preview(yaml_content: str) -> str:
    """Strip secret values from YAML preview."""
    documents, _ = parse_yaml_documents(yaml_content)
    sanitized = []
    for doc in documents:
        if doc.get("kind") == "Secret":
            copy = dict(doc)
            meta = dict(copy.get("metadata") or {})
            copy["metadata"] = meta
            copy.pop("data", None)
            copy.pop("stringData", None)
            copy["__warning"] = "Secret values redacted from preview"
            sanitized.append(copy)
        else:
            sanitized.append(doc)
    return "\n---\n".join(yaml.dump(d, default_flow_style=False, sort_keys=False) for d in sanitized)
