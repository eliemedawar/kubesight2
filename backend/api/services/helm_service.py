"""Helm chart operations: template, dry-run, install, upgrade, rollback, uninstall."""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import yaml

from ..access_engine import can_access_namespace, is_admin, user_has_permission
from ..audit import log_audit
from ..cluster_access import ClusterAccess
from ..k8s_provider import resolve_cluster_access
from ..models import User
from .deployment_service import analyze_resources, sanitize_yaml_preview

RunHelmFn = Callable[[ClusterAccess, List[str], Optional[Dict[str, str]]], str]

HELM_MISSING_MESSAGE = "Helm is not installed on the backend server."

RELEASE_NAME_PATTERN = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
NAMESPACE_PATTERN = re.compile(r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$")
CHART_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")
REPO_NAME_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9._-]*$")


class HelmCommandError(RuntimeError):
    pass


class HelmNotInstalledError(HelmCommandError):
    pass


def _helm_binary() -> str:
    return os.getenv("HELM_BINARY", "helm")


def is_helm_installed(run_version: Optional[Callable[[], str]] = None) -> bool:
    if run_version:
        try:
            run_version()
            return True
        except (HelmCommandError, HelmNotInstalledError, OSError):
            return False
    try:
        completed = subprocess.run(
            [_helm_binary(), "version", "--short"],
            capture_output=True,
            text=True,
            check=False,
        )
        return completed.returncode == 0 and bool((completed.stdout or completed.stderr or "").strip())
    except OSError:
        return False


def ensure_helm_installed() -> None:
    if not is_helm_installed():
        raise HelmNotInstalledError(HELM_MISSING_MESSAGE)


def validate_release_name(name: str) -> Tuple[bool, Optional[str]]:
    cleaned = (name or "").strip().lower()
    if not cleaned or len(cleaned) > 53:
        return False, "Release name must be 1-53 characters."
    if not RELEASE_NAME_PATTERN.match(cleaned):
        return False, "Release name must be a valid DNS subdomain (lowercase alphanumeric and hyphens)."
    return True, None


def validate_namespace_name(namespace: str) -> Tuple[bool, Optional[str]]:
    cleaned = (namespace or "").strip()
    if not cleaned or len(cleaned) > 63:
        return False, "Namespace must be 1-63 characters."
    if not NAMESPACE_PATTERN.match(cleaned):
        return False, "Invalid namespace name."
    return True, None


def validate_repo_url(url: str) -> Tuple[bool, Optional[str]]:
    cleaned = (url or "").strip()
    if not cleaned:
        return False, "Repository URL is required."
    parsed = urlparse(cleaned)
    if parsed.scheme not in {"http", "https"}:
        return False, "Repository URL must use http or https."
    if not parsed.netloc:
        return False, "Repository URL is invalid."
    return True, None


def validate_repo_name(name: str) -> Tuple[bool, Optional[str]]:
    cleaned = (name or "").strip()
    if not cleaned or not REPO_NAME_PATTERN.match(cleaned):
        return False, "Repository name is invalid."
    return True, None


def validate_chart_name(name: str) -> Tuple[bool, Optional[str]]:
    cleaned = (name or "").strip()
    if not cleaned or not CHART_NAME_PATTERN.match(cleaned):
        return False, "Chart name is invalid."
    return True, None


def validate_chart_version(version: str) -> Tuple[bool, Optional[str]]:
    cleaned = (version or "").strip()
    if not cleaned:
        return False, "Chart version is required."
    if len(cleaned) > 128:
        return False, "Chart version is too long."
    return True, None


def _helm_env(access: ClusterAccess) -> Dict[str, str]:
    env = os.environ.copy()
    if access.kubeconfig_path:
        env["KUBECONFIG"] = access.kubeconfig_path
    return env


def run_helm(
    access: ClusterAccess,
    args: List[str],
    *,
    extra_env: Optional[Dict[str, str]] = None,
) -> str:
    ensure_helm_installed()
    command = [_helm_binary()]
    if access.kubeconfig_path:
        command += ["--kubeconfig", access.kubeconfig_path]
    if access.context_name:
        command += ["--kube-context", access.context_name]
    command += args

    env = _helm_env(access)
    if extra_env:
        env.update(extra_env)

    completed = subprocess.run(
        command,
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    if completed.returncode != 0:
        stderr = (completed.stderr or completed.stdout or "").strip()
        raise HelmCommandError(stderr or f"helm command failed: {' '.join(command)}")
    return completed.stdout


def _resolve_access(cluster_id: str) -> ClusterAccess:
    access = resolve_cluster_access(cluster_id)
    if access:
        return access
    from ..k8s_provider import should_use_real_k8s
    from ..mock_data import CLUSTERS

    if not should_use_real_k8s(cluster_id):
        known = {c["id"] for c in CLUSTERS if c.get("id")}
        if cluster_id in known:
            return ClusterAccess(cluster_id=cluster_id, context_name=None, kubeconfig_path=None)
    raise HelmCommandError(f"Cluster not found: {cluster_id}")


def _write_values_file(values_yaml: str) -> str:
    fd, path = tempfile.mkstemp(suffix=".yaml", prefix="kubesight-helm-values-")
    os.close(fd)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(values_yaml or "")
    return path


def _write_chart_archive(chart_b64: str) -> str:
    raw = base64.b64decode(chart_b64)
    fd, path = tempfile.mkstemp(suffix=".tgz", prefix="kubesight-helm-chart-")
    os.close(fd)
    with open(path, "wb") as handle:
        handle.write(raw)
    return path


def _cleanup_path(path: Optional[str]) -> None:
    if not path:
        return
    try:
        os.remove(path)
    except OSError:
        pass


def build_chart_ref(payload: Dict[str, Any]) -> Tuple[str, Optional[str], Optional[str]]:
    """Return (chart_ref, local_chart_path, error)."""
    source = (payload.get("chartSource") or payload.get("chart_source") or "repository").strip().lower()
    if source == "local":
        chart_b64 = payload.get("chartArchiveBase64") or payload.get("chart_archive_base64")
        if not chart_b64:
            return "", None, "Local chart .tgz upload is required."
        return "", _write_chart_archive(chart_b64), None

    repo_name = (payload.get("repositoryName") or payload.get("repoName") or payload.get("repository_name") or "").strip()
    chart_name = (payload.get("chartName") or payload.get("chart_name") or "").strip()
    chart_version = (payload.get("chartVersion") or payload.get("chart_version") or "").strip()

    ok, err = validate_chart_name(chart_name)
    if not ok:
        return "", None, err
    ok, err = validate_chart_version(chart_version)
    if not ok:
        return "", None, err

    if repo_name:
        chart_ref = f"{repo_name}/{chart_name}"
    else:
        chart_ref = chart_name
    if chart_version and chart_version.lower() != "latest":
        chart_ref = f"{chart_ref} --version {chart_version}"
    return chart_ref, None, None


def _split_chart_ref(chart_ref: str) -> Tuple[str, List[str]]:
    parts = chart_ref.split(" --version ", 1)
    ref = parts[0]
    extra = ["--version", parts[1]] if len(parts) == 2 else []
    return ref, extra


def _base_helm_args(payload: Dict[str, Any]) -> Tuple[str, str, str, ClusterAccess, Optional[str], Optional[str], Optional[str]]:
    cluster_id = (payload.get("clusterId") or payload.get("cluster") or "").strip()
    namespace = (payload.get("namespace") or "").strip()
    release_name = (payload.get("releaseName") or payload.get("release_name") or "").strip().lower()

    ok, err = validate_release_name(release_name)
    if not ok:
        raise HelmCommandError(err or "Invalid release name")
    ok, err = validate_namespace_name(namespace)
    if not ok:
        raise HelmCommandError(err or "Invalid namespace")
    if not cluster_id:
        raise HelmCommandError("clusterId is required")

    access = _resolve_access(cluster_id)
    chart_ref, local_path, chart_err = build_chart_ref(payload)
    if chart_err:
        raise HelmCommandError(chart_err)

    return release_name, namespace, cluster_id, access, chart_ref, local_path, None


def _helm_chart_args(chart_ref: str, local_path: Optional[str]) -> Tuple[str, List[str]]:
    if local_path:
        return local_path, []
    ref, version_args = _split_chart_ref(chart_ref)
    return ref, version_args


def add_repository(repo_name: str, repo_url: str, access: ClusterAccess) -> str:
    ok, err = validate_repo_name(repo_name)
    if not ok:
        raise HelmCommandError(err or "Invalid repo name")
    ok, err = validate_repo_url(repo_url)
    if not ok:
        raise HelmCommandError(err or "Invalid repo URL")
    return run_helm(access, ["repo", "add", repo_name, repo_url])


def update_repositories(access: ClusterAccess) -> str:
    return run_helm(access, ["repo", "update"])


def list_repositories(access: ClusterAccess) -> List[Dict[str, Any]]:
    output = run_helm(access, ["repo", "list", "-o", "json"])
    try:
        return json.loads(output or "[]")
    except json.JSONDecodeError:
        return []


def search_charts(access: ClusterAccess, repo_name: str, chart_query: str = "") -> List[Dict[str, Any]]:
    ok, err = validate_repo_name(repo_name)
    if not ok:
        raise HelmCommandError(err or "Invalid repo name")
    query = f"{repo_name}/{chart_query}".strip("/")
    output = run_helm(access, ["search", "repo", query, "-o", "json"])
    try:
        return json.loads(output or "[]")
    except json.JSONDecodeError:
        return []


def release_exists(access: ClusterAccess, release_name: str, namespace: str) -> bool:
    try:
        output = run_helm(
            access,
            ["list", "-n", namespace, "-o", "json", "-f", f"^{release_name}$"],
        )
        items = json.loads(output or "[]")
        return any(item.get("name") == release_name for item in items)
    except HelmCommandError:
        return False


def render_template(
    payload: Dict[str, Any],
    *,
    run_helm_fn: Optional[RunHelmFn] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    try:
        ensure_helm_installed()
    except HelmNotInstalledError as exc:
        return None, str(exc), 503

    values_yaml = payload.get("valuesYaml") or payload.get("values_yaml") or ""
    release_name, namespace, cluster_id, access, chart_ref, local_path, _ = _base_helm_args(payload)
    values_path = _write_values_file(values_yaml)
    runner = run_helm_fn or run_helm

    try:
        if payload.get("chartSource", payload.get("chart_source", "repository")) == "repository":
            repo_name = (payload.get("repositoryName") or payload.get("repoName") or "").strip()
            repo_url = (payload.get("repositoryUrl") or payload.get("repoUrl") or "").strip()
            if repo_name and repo_url:
                add_repository(repo_name, repo_url, access)
                update_repositories(access)

        chart_target, version_args = _helm_chart_args(chart_ref, local_path)
        args = ["template", release_name, chart_target, "--namespace", namespace, "-f", values_path]
        args.extend(version_args)
        rendered = runner(access, args)
        analysis = analyze_resources(list(yaml.safe_load_all(rendered)), namespace, preview_mode=True)
        preview = sanitize_yaml_preview(rendered)

        log_audit(
            "helm_template_rendered",
            target_type="helm_release",
            target_id=f"{cluster_id}/{namespace}/{release_name}",
            details={
                "cluster": cluster_id,
                "namespace": namespace,
                "release": release_name,
                "chart": payload.get("chartName"),
                "version": payload.get("chartVersion"),
                "result": "success",
            },
        )
        return {
            "rendered": rendered,
            "preview": preview,
            **analysis,
        }, None, 200
    except HelmNotInstalledError as exc:
        return None, str(exc), 503
    except HelmCommandError as exc:
        return None, str(exc), 400
    finally:
        _cleanup_path(values_path)
        _cleanup_path(local_path)


def dry_run_release(
    user: Optional[User],
    payload: Dict[str, Any],
    *,
    run_helm_fn: Optional[RunHelmFn] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    cluster_id = (payload.get("clusterId") or payload.get("cluster") or "").strip()
    namespace = (payload.get("namespace") or "").strip()
    if user and not can_access_namespace(user, cluster_id, namespace):
        _audit_unauthorized(user, payload, "dry-run")
        return None, "Forbidden", 403

    perm = "helm:upgrade" if release_exists_from_payload(payload, run_helm_fn) else "helm:install"
    if user and not user_has_permission(user, perm):
        if not (perm == "helm:upgrade" and user_has_permission(user, "helm:install")):
            if not (perm == "helm:install" and user_has_permission(user, "helm:upgrade")):
                _audit_unauthorized(user, payload, "dry-run")
                return None, "Forbidden", 403

    try:
        ensure_helm_installed()
    except HelmNotInstalledError as exc:
        return None, str(exc), 503

    values_yaml = payload.get("valuesYaml") or payload.get("values_yaml") or ""
    release_name, namespace, cluster_id, access, chart_ref, local_path, _ = _base_helm_args(payload)
    values_path = _write_values_file(values_yaml)
    runner = run_helm_fn or run_helm

    try:
        if (payload.get("chartSource") or payload.get("chart_source") or "repository") == "repository":
            repo_name = (payload.get("repositoryName") or payload.get("repoName") or "").strip()
            repo_url = (payload.get("repositoryUrl") or payload.get("repoUrl") or "").strip()
            if repo_name and repo_url:
                add_repository(repo_name, repo_url, access)
                update_repositories(access)

        chart_target, version_args = _helm_chart_args(chart_ref, local_path)
        args = [
            "upgrade", "--install", release_name, chart_target,
            "--namespace", namespace,
            "-f", values_path,
            "--dry-run",
        ]
        args.extend(version_args)
        output = runner(access, args)

        template_data, _, _ = render_template(payload, run_helm_fn=run_helm_fn)
        log_audit(
            "helm_dry_run",
            actor=user,
            target_type="helm_release",
            target_id=f"{cluster_id}/{namespace}/{release_name}",
            details={
                "cluster": cluster_id,
                "namespace": namespace,
                "release": release_name,
                "result": "success",
            },
        )
        return {
            "dryRun": True,
            "output": output,
            "preview": template_data.get("preview") if template_data else "",
            "warnings": template_data.get("warnings") if template_data else [],
            "resources": template_data.get("resources") if template_data else [],
        }, None, 200
    except HelmNotInstalledError as exc:
        return None, str(exc), 503
    except HelmCommandError as exc:
        log_audit(
            "helm_install_failed",
            actor=user,
            target_type="helm_release",
            target_id=f"{cluster_id}/{namespace}/{release_name}",
            details={"action": "dry-run", "error": str(exc), "result": "failed"},
        )
        return None, str(exc), 400
    finally:
        _cleanup_path(values_path)
        _cleanup_path(local_path)


def release_exists_from_payload(
    payload: Dict[str, Any],
    run_helm_fn: Optional[RunHelmFn] = None,
) -> bool:
    if payload.get("isUpgrade"):
        return True
    if payload.get("isInstall"):
        return False
    try:
        release_name, namespace, cluster_id, access, _, _, _ = _base_helm_args(payload)
        if run_helm_fn:
            return False
        return release_exists(access, release_name, namespace)
    except HelmCommandError:
        return False


def expected_confirmation(release_name: str, namespace: str, is_upgrade: bool) -> str:
    if is_upgrade:
        return f"UPGRADE {release_name} IN {namespace}"
    return f"INSTALL {release_name} IN {namespace}"


def install_or_upgrade_release(
    user: Optional[User],
    payload: Dict[str, Any],
    confirmation: str,
    *,
    run_helm_fn: Optional[RunHelmFn] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    cluster_id = (payload.get("clusterId") or payload.get("cluster") or "").strip()
    namespace = (payload.get("namespace") or "").strip()
    release_name = (payload.get("releaseName") or payload.get("release_name") or "").strip().lower()

    if user and not can_access_namespace(user, cluster_id, namespace):
        _audit_unauthorized(user, payload, "install")
        return None, "Forbidden", 403

    is_upgrade = bool(payload.get("isUpgrade")) or release_exists_from_payload(payload, run_helm_fn)
    perm = "helm:upgrade" if is_upgrade else "helm:install"
    if user and not user_has_permission(user, perm):
        _audit_unauthorized(user, payload, perm)
        return None, "Forbidden", 403

    expected = expected_confirmation(release_name, namespace, is_upgrade)
    if (confirmation or "").strip() != expected:
        return None, f"Confirmation must be exactly: {expected}", 400

    try:
        ensure_helm_installed()
    except HelmNotInstalledError as exc:
        return None, str(exc), 503

    values_yaml = payload.get("valuesYaml") or payload.get("values_yaml") or ""
    release_name, namespace, cluster_id, access, chart_ref, local_path, _ = _base_helm_args(payload)
    values_path = _write_values_file(values_yaml)
    runner = run_helm_fn or run_helm
    action = "helm_upgrade_attempted" if is_upgrade else "helm_install_attempted"

    try:
        if (payload.get("chartSource") or payload.get("chart_source") or "repository") == "repository":
            repo_name = (payload.get("repositoryName") or payload.get("repoName") or "").strip()
            repo_url = (payload.get("repositoryUrl") or payload.get("repoUrl") or "").strip()
            if repo_name and repo_url:
                add_repository(repo_name, repo_url, access)
                log_audit(
                    "helm_repo_added",
                    actor=user,
                    target_type="helm_repo",
                    target_id=repo_name,
                    details={"url": repo_url, "cluster": cluster_id},
                )
                update_repositories(access)

        chart_target, version_args = _helm_chart_args(chart_ref, local_path)
        args = [
            "upgrade", "--install", release_name, chart_target,
            "--namespace", namespace,
            "-f", values_path,
            "--create-namespace",
        ]
        args.extend(version_args)
        output = runner(access, args)

        success_action = "helm_upgrade_succeeded" if is_upgrade else "helm_install_succeeded"
        log_audit(
            action,
            actor=user,
            target_type="helm_release",
            target_id=f"{cluster_id}/{namespace}/{release_name}",
            details={
                "cluster": cluster_id,
                "namespace": namespace,
                "release": release_name,
                "chart": payload.get("chartName"),
                "version": payload.get("chartVersion"),
                "result": "success",
            },
        )
        log_audit(
            success_action,
            actor=user,
            target_type="helm_release",
            target_id=f"{cluster_id}/{namespace}/{release_name}",
            details={"output": output[:500] if output else ""},
        )
        return {
            "installed": not is_upgrade,
            "upgraded": is_upgrade,
            "releaseName": release_name,
            "namespace": namespace,
            "output": output,
        }, None, 200
    except HelmNotInstalledError as exc:
        return None, str(exc), 503
    except HelmCommandError as exc:
        fail_action = "helm_upgrade_failed" if is_upgrade else "helm_install_failed"
        log_audit(
            fail_action,
            actor=user,
            target_type="helm_release",
            target_id=f"{cluster_id}/{namespace}/{release_name}",
            details={"error": str(exc), "result": "failed"},
        )
        return None, str(exc), 400
    finally:
        _cleanup_path(values_path)
        _cleanup_path(local_path)


def rollback_release(
    user: Optional[User],
    cluster_id: str,
    namespace: str,
    release_name: str,
    revision: Optional[int] = None,
    *,
    run_helm_fn: Optional[RunHelmFn] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    if user and not user_has_permission(user, "helm:rollback"):
        _audit_unauthorized(user, {"clusterId": cluster_id, "namespace": namespace, "releaseName": release_name}, "rollback")
        return None, "Forbidden", 403
    if user and not can_access_namespace(user, cluster_id, namespace):
        return None, "Forbidden", 403

    ok, err = validate_release_name(release_name)
    if not ok:
        return None, err, 400

    ensure_helm_installed()
    access = _resolve_access(cluster_id)
    runner = run_helm_fn or run_helm
    args = ["rollback", release_name, "--namespace", namespace]
    if revision is not None:
        args.append(str(revision))

    try:
        output = runner(access, args)
        log_audit(
            "helm_rollback_attempted",
            actor=user,
            target_type="helm_release",
            target_id=f"{cluster_id}/{namespace}/{release_name}",
            details={"revision": revision, "result": "success"},
        )
        return {"rolledBack": True, "output": output}, None, 200
    except HelmNotInstalledError as exc:
        return None, str(exc), 503
    except HelmCommandError as exc:
        log_audit(
            "helm_rollback_attempted",
            actor=user,
            target_type="helm_release",
            target_id=f"{cluster_id}/{namespace}/{release_name}",
            details={"error": str(exc), "result": "failed"},
        )
        return None, str(exc), 400


def uninstall_release(
    user: Optional[User],
    cluster_id: str,
    namespace: str,
    release_name: str,
    *,
    run_helm_fn: Optional[RunHelmFn] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    if user and not user_has_permission(user, "helm:uninstall"):
        _audit_unauthorized(user, {"clusterId": cluster_id, "namespace": namespace, "releaseName": release_name}, "uninstall")
        return None, "Forbidden", 403
    if user and not can_access_namespace(user, cluster_id, namespace):
        return None, "Forbidden", 403

    ok, err = validate_release_name(release_name)
    if not ok:
        return None, err, 400

    ensure_helm_installed()
    access = _resolve_access(cluster_id)
    runner = run_helm_fn or run_helm

    try:
        output = runner(access, ["uninstall", release_name, "--namespace", namespace])
        log_audit(
            "helm_uninstall_attempted",
            actor=user,
            target_type="helm_release",
            target_id=f"{cluster_id}/{namespace}/{release_name}",
            details={"result": "success"},
        )
        return {"uninstalled": True, "output": output}, None, 200
    except HelmNotInstalledError as exc:
        return None, str(exc), 503
    except HelmCommandError as exc:
        log_audit(
            "helm_uninstall_attempted",
            actor=user,
            target_type="helm_release",
            target_id=f"{cluster_id}/{namespace}/{release_name}",
            details={"error": str(exc), "result": "failed"},
        )
        return None, str(exc), 400


def list_releases(
    cluster_id: str,
    namespace: Optional[str] = None,
    *,
    run_helm_fn: Optional[RunHelmFn] = None,
) -> List[Dict[str, Any]]:
    if not is_helm_installed():
        return []
    access = _resolve_access(cluster_id)
    runner = run_helm_fn or run_helm
    args = ["list", "-o", "json"]
    if namespace:
        args.extend(["-n", namespace])
    else:
        args.append("-A")
    try:
        output = runner(access, args)
        return json.loads(output or "[]")
    except (HelmCommandError, json.JSONDecodeError):
        return []


def _parse_chart_label(chart_label: str) -> Tuple[str, str]:
    if not chart_label:
        return "-", "-"
    if "-" in chart_label:
        name, version = chart_label.rsplit("-", 1)
        return name, version
    return chart_label, "-"


def helm_release_to_inventory_row(cluster_id: str, release: Dict[str, Any]) -> Dict[str, Any]:
    release_name = release.get("name") or "unknown"
    namespace = release.get("namespace") or "default"
    chart_label = release.get("chart") or ""
    chart_name, chart_version = _parse_chart_label(chart_label)
    app_version = release.get("app_version") or release.get("appVersion") or "-"
    revision = release.get("revision") or 0
    status = release.get("status") or "unknown"
    updated = release.get("updated") or release.get("lastDeployed") or datetime.now(timezone.utc).isoformat()

    return {
        "id": make_inventory_id(cluster_id, namespace, release_name),
        "name": release_name,
        "cluster": cluster_id,
        "clusterId": cluster_id,
        "namespace": namespace,
        "workloadType": "Helm Release",
        "workloadNames": [release_name],
        "status": "Healthy" if status == "deployed" else "Warning" if status == "pending-upgrade" else "Unknown",
        "replicas": 0,
        "readyReplicas": 0,
        "image": "-",
        "versionTag": chart_version,
        "service": "-",
        "ports": [],
        "cpuUsage": "-",
        "memoryUsage": "-",
        "lastUpdated": updated,
        "ownerTeam": "Unassigned",
        "environment": "Not set",
        "criticality": "Not set",
        "documentationUrl": None,
        "contactEmail": "Not set",
        "tags": [],
        "source": "Helm",
        "catalogEntryId": None,
        "releaseName": release_name,
        "chartName": chart_name,
        "chartVersion": chart_version,
        "appVersion": app_version,
        "helmRevision": revision,
        "helmStatus": status,
        "helm": {
            "releaseName": release_name,
            "chartName": chart_name,
            "chartVersion": chart_version,
            "appVersion": app_version,
            "revision": revision,
            "status": status,
            "lastDeployed": updated,
            "namespace": namespace,
        },
    }


def make_inventory_id(cluster_id: str, namespace: str, name: str) -> str:
    from urllib.parse import quote
    return quote(f"{cluster_id}|{namespace}|{name}", safe="")


def get_release_detail(
    cluster_id: str,
    namespace: str,
    release_name: str,
    *,
    run_helm_fn: Optional[RunHelmFn] = None,
) -> Optional[Dict[str, Any]]:
    if not is_helm_installed():
        return None
    access = _resolve_access(cluster_id)
    runner = run_helm_fn or run_helm

    try:
        status_output = runner(access, ["status", release_name, "-n", namespace, "-o", "json"])
        status_data = json.loads(status_output)
    except (HelmCommandError, json.JSONDecodeError):
        status_data = {}

    manifest = ""
    values_summary: Dict[str, Any] = {}
    try:
        manifest = runner(access, ["get", "manifest", release_name, "-n", namespace])
    except HelmCommandError:
        pass
    try:
        values_raw = runner(access, ["get", "values", release_name, "-n", namespace, "-o", "json"])
        values_summary = json.loads(values_raw or "{}")
    except (HelmCommandError, json.JSONDecodeError):
        values_summary = {}

    chart_label = status_data.get("chart", {}).get("metadata", {}).get("name") or status_data.get("chart") or ""
    if isinstance(chart_label, dict):
        chart_name = chart_label.get("metadata", {}).get("name", "-")
        chart_version = chart_label.get("metadata", {}).get("version", "-")
    else:
        chart_name, chart_version = _parse_chart_label(str(chart_label))

    info = status_data.get("info") or {}
    return {
        "releaseName": release_name,
        "namespace": namespace,
        "clusterId": cluster_id,
        "chartName": chart_name,
        "chartVersion": chart_version,
        "appVersion": status_data.get("chart", {}).get("metadata", {}).get("appVersion") if isinstance(status_data.get("chart"), dict) else "-",
        "revision": status_data.get("version") or status_data.get("revision"),
        "status": info.get("status") or status_data.get("status") or "unknown",
        "lastDeployed": info.get("last_deployed") or info.get("lastDeployed"),
        "valuesSummary": values_summary,
        "renderedManifest": sanitize_yaml_preview(manifest) if manifest else "",
        "manifest": manifest,
    }


def _audit_unauthorized(user: Optional[User], payload: Dict[str, Any], action: str) -> None:
    log_audit(
        "unauthorized_helm_action",
        actor=user,
        target_type="helm_release",
        target_id=f"{payload.get('clusterId')}/{payload.get('namespace')}/{payload.get('releaseName')}",
        details={"action": action, "result": "forbidden"},
    )


def check_helm_available() -> Dict[str, Any]:
    installed = is_helm_installed()
    version = ""
    if installed:
        try:
            completed = subprocess.run(
                [_helm_binary(), "version", "--short"],
                capture_output=True,
                text=True,
                check=False,
            )
            version = (completed.stdout or completed.stderr or "").strip()
        except OSError:
            installed = False
    return {"installed": installed, "version": version, "message": HELM_MISSING_MESSAGE if not installed else None}
