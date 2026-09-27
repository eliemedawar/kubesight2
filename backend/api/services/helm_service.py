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

from ..access_engine import can_access_cluster, can_access_namespace, is_admin, user_has_permission
from ..audit import log_audit
from ..cluster_access import ClusterAccess
from ..k8s_provider import resolve_cluster_access
from ..models import User
from .deployment_service import analyze_resources, check_registry_images, sanitize_yaml_preview

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


def _prepare_chart_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Resolve a reusable chart id to an ephemeral local archive and values file.

    The catalog service owns stored chart content; the existing Helm command
    path continues to operate on the same local archive shape as uploaded tgz
    charts.
    """
    try:
        from .helm_chart_template_service import (
            ChartTemplateError,
            prepare_chart_template_payload,
        )

        return prepare_chart_template_payload(payload)
    except ChartTemplateError as exc:
        raise HelmCommandError(str(exc)) from exc


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


def _helm_env(access: ClusterAccess, kubeconfig_path: Optional[str] = None) -> Dict[str, str]:
    """Subprocess env for helm. ``kubeconfig_path`` is the materialized
    (decrypted) path; it defaults to the access path for pass-through configs."""
    env = os.environ.copy()
    path = kubeconfig_path if kubeconfig_path is not None else access.kubeconfig_path
    if path:
        env["KUBECONFIG"] = path
    return env


def run_helm(
    access: ClusterAccess,
    args: List[str],
    *,
    extra_env: Optional[Dict[str, str]] = None,
) -> str:
    from ..kubeconfig_vault import KubeconfigDecryptError, materialized_kubeconfig

    ensure_helm_installed()
    try:
        with materialized_kubeconfig(access.kubeconfig_path) as kubeconfig_path:
            command = [_helm_binary()]
            if kubeconfig_path:
                command += ["--kubeconfig", kubeconfig_path]
            if access.context_name:
                command += ["--kube-context", access.context_name]
            command += args

            env = _helm_env(access, kubeconfig_path or "")
            if extra_env:
                env.update(extra_env)

            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                check=False,
                env=env,
            )
    except KubeconfigDecryptError as exc:
        raise HelmCommandError(str(exc)) from exc
    if completed.returncode != 0:
        stderr = (completed.stderr or completed.stdout or "").strip()
        raise HelmCommandError(stderr or f"helm command failed: helm {' '.join(args)}")

    # Helm installs/upgrades/rollbacks change workloads — drop cached namespace
    # resource reads for this cluster so the UI reflects the release right away.
    verb = args[0] if args else ""
    if verb in {"install", "upgrade", "uninstall", "rollback", "delete"} and "--dry-run" not in args:
        from ..k8s_provider import invalidate_namespace_resources_cache
        from .inventory_service import invalidate_inventory_discovery_cache

        invalidate_namespace_resources_cache(access.cluster_id)
        invalidate_inventory_discovery_cache(access.cluster_id)

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
        payload = _prepare_chart_payload(payload)
    except HelmCommandError as exc:
        return None, str(exc), 400
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
    try:
        payload = _prepare_chart_payload(payload)
    except HelmCommandError as exc:
        return None, str(exc), 400
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
    try:
        payload = _prepare_chart_payload(payload)
    except HelmCommandError:
        return False
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


def _queue_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """What a queued Helm change keeps: the request as the caller sent it.

    A chart template stays a template id + version (re-resolved when it runs)
    rather than the resolved archive; an uploaded chart keeps its archive. The
    typed confirmation is left out — the approval replaces it.
    """
    return {k: v for k, v in (payload or {}).items() if k != "confirmation"}


def _render_for_checks(
    runner: RunHelmFn,
    access: ClusterAccess,
    release_name: str,
    chart_target: str,
    namespace: str,
    values_path: str,
    version_args: List[str],
) -> Tuple[Optional[str], Optional[str]]:
    """``helm template`` the release so its images can be checked.

    Returns ``(rendered, error)``. Best-effort: a chart that cannot be rendered
    offline is not refused here (the install itself is the authority); the
    caller reports that the image check could not run.
    """
    args = ["template", release_name, chart_target, "--namespace", namespace, "-f", values_path]
    args.extend(version_args)
    try:
        return runner(access, args), None
    except HelmNotInstalledError:
        raise
    except HelmCommandError as exc:
        return None, str(exc)


def _helm_preview(header: str, rendered: Optional[str], values_yaml: str) -> str:
    """Bundle preview for a Helm change: the rendered manifest (secrets redacted)
    or, when it could not be rendered, the values it will be installed with."""
    lines = [f"# {header}"]
    if rendered and rendered.strip():
        try:
            body = sanitize_yaml_preview(rendered)
        except Exception:  # noqa: BLE001 — preview is informational
            body = ""
        if body.strip():
            lines.append("# Rendered manifest (helm template; Secret values redacted):")
            return "\n".join(lines) + "\n" + body
    lines.append("# The chart could not be rendered for a preview. Values:")
    return "\n".join(lines) + "\n" + (values_yaml or "# (chart defaults)\n")


def install_or_upgrade_release(
    user: Optional[User],
    payload: Dict[str, Any],
    confirmation: str,
    *,
    run_helm_fn: Optional[RunHelmFn] = None,
    approval_context: Optional[str] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    """Install or upgrade a release, under the cluster's approval rule.

    On a cluster that needs approval (and no live approved request) the change
    is not refused: it is queued as a single-item change bundle with its full
    parameters and carried out by the bundle executor once approved (202,
    ``pendingApproval``). The executor calls back in here with
    ``approval_context`` so the same namespace, chart and image checks run
    again at execution time; the typed confirmation is not asked again then.
    """
    original = _queue_payload(payload)
    try:
        payload = _prepare_chart_payload(payload)
    except HelmCommandError as exc:
        return None, str(exc), 400
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

    if not approval_context:
        expected = expected_confirmation(release_name, namespace, is_upgrade)
        if (confirmation or "").strip() != expected:
            return None, f"Confirmation must be exactly: {expected}", 400

    try:
        ensure_helm_installed()
    except HelmNotInstalledError as exc:
        return None, str(exc), 503

    values_yaml = payload.get("valuesYaml") or payload.get("values_yaml") or ""
    try:
        release_name, namespace, cluster_id, access, chart_ref, local_path, _ = _base_helm_args(payload)
    except HelmCommandError as exc:
        return None, str(exc), 400
    values_path = _write_values_file(values_yaml)
    runner = run_helm_fn or run_helm
    action = "helm_upgrade_attempted" if is_upgrade else "helm_install_attempted"
    verb = "upgrade" if is_upgrade else "install"

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

        # Registry image gate — the same rule as a YAML apply: render the
        # release and refuse when an image is missing from a registry whose
        # enforcement is "block" (warn/off registries never block).
        rendered, render_err = _render_for_checks(
            runner, access, release_name, chart_target, namespace, values_path, version_args
        )
        image_checks: List[Dict[str, Any]] = []
        if rendered:
            image_checks, image_blocking, image_err = check_registry_images(rendered)
            if image_blocking:
                log_audit(
                    "helm_upgrade_failed" if is_upgrade else "helm_install_failed",
                    actor=user,
                    target_type="helm_release",
                    target_id=f"{cluster_id}/{namespace}/{release_name}",
                    details={"error": image_err, "reason": "image_not_in_registry"},
                )
                return None, image_err, 422

        # The cluster's approval rule — checked after validation so only a
        # change this user could make is ever queued.
        chart_label = f"{payload.get('chartName') or chart_target} {payload.get('chartVersion') or ''}".strip()
        queued = _approval_gate(
            user,
            cluster_id,
            namespace,
            release_name,
            verb,
            approval_context=approval_context,
            bundle_payload={
                "helm": {**original, "isUpgrade": is_upgrade},
                "helmPreview": _helm_preview(
                    f"helm {verb} {release_name} ({chart_label}) in namespace {namespace}",
                    rendered,
                    values_yaml,
                ),
            },
            what=f"helm {verb} {release_name} in {namespace}",
        )
        if queued is not None:
            return queued

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
                **({"approval": approval_context} if approval_context else {}),
            },
        )
        log_audit(
            success_action,
            actor=user,
            target_type="helm_release",
            target_id=f"{cluster_id}/{namespace}/{release_name}",
            details={"output": output[:500] if output else ""},
        )
        result: Dict[str, Any] = {
            "installed": not is_upgrade,
            "upgraded": is_upgrade,
            "releaseName": release_name,
            "namespace": namespace,
            "output": output,
            "imageChecks": image_checks,
        }
        if render_err:
            result["imageCheckWarning"] = (
                "The chart could not be rendered to check its images against the "
                f"linked registries: {render_err}"
            )
        return result, None, 200
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


def _current_manifest_preview(cluster_id: str, namespace: str, release_name: str) -> str:
    """Best-effort ``helm get manifest`` (secrets redacted) for a queued preview."""
    from ..k8s_provider import should_use_real_k8s

    try:
        if not should_use_real_k8s(cluster_id) or not is_helm_installed():
            return ""
        manifest = run_helm(_resolve_access(cluster_id), ["get", "manifest", release_name, "-n", namespace])
        return sanitize_yaml_preview(manifest) if manifest else ""
    except Exception:  # noqa: BLE001 — preview is informational
        return ""


def rollback_release(
    user: Optional[User],
    cluster_id: str,
    namespace: str,
    release_name: str,
    revision: Optional[int] = None,
    *,
    run_helm_fn: Optional[RunHelmFn] = None,
    approval_context: Optional[str] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    if user and not user_has_permission(user, "helm:rollback"):
        _audit_unauthorized(user, {"clusterId": cluster_id, "namespace": namespace, "releaseName": release_name}, "rollback")
        return None, "Forbidden", 403
    if user and not can_access_namespace(user, cluster_id, namespace):
        return None, "Forbidden", 403

    ok, err = validate_release_name(release_name)
    if not ok:
        return None, err, 400
    if revision not in (None, ""):
        try:
            revision = int(revision)
        except (TypeError, ValueError):
            return None, "revision must be a whole number.", 400
        if revision < 1:
            return None, "revision must be 1 or greater.", 400
    else:
        revision = None

    target = f"revision {revision}" if revision else "the previous revision"
    queued = _approval_gate(
        user,
        cluster_id,
        namespace,
        release_name,
        "rollback",
        approval_context=approval_context,
        bundle_payload={
            "revision": revision,
            "helmPreview": f"# helm rollback {release_name} in namespace {namespace} to {target}\n",
        },
        what=f"helm rollback {release_name} to {target}",
    )
    if queued is not None:
        return queued

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
            details={
                "revision": revision,
                "result": "success",
                **({"approval": approval_context} if approval_context else {}),
            },
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
    approval_context: Optional[str] = None,
) -> Tuple[Optional[Dict[str, Any]], Optional[str], int]:
    if user and not user_has_permission(user, "helm:uninstall"):
        _audit_unauthorized(user, {"clusterId": cluster_id, "namespace": namespace, "releaseName": release_name}, "uninstall")
        return None, "Forbidden", 403
    if user and not can_access_namespace(user, cluster_id, namespace):
        return None, "Forbidden", 403

    ok, err = validate_release_name(release_name)
    if not ok:
        return None, err, 400

    queued = _approval_gate(
        user,
        cluster_id,
        namespace,
        release_name,
        "uninstall",
        approval_context=approval_context,
        bundle_payload={
            "helmPreview": (
                f"# helm uninstall {release_name} in namespace {namespace}\n"
                "# Every resource of the release (below, when readable) is deleted.\n"
                + (_current_manifest_preview(cluster_id, namespace, release_name) if not approval_context else "")
            ),
        },
        what=f"helm uninstall {release_name}",
    )
    if queued is not None:
        return queued

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
            details={
                "result": "success",
                **({"approval": approval_context} if approval_context else {}),
            },
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


# Change-bundle action types for Helm (see change_bundle_service.ACTION_TYPES).
HELM_ACTION_TYPES = ("helm_install", "helm_upgrade", "helm_rollback", "helm_uninstall")


def run_approved_helm_change(
    item_input: Dict[str, Any], action_type: str, *, approval_context: str
) -> Tuple[str, Dict[str, Any]]:
    """Carry out a queued Helm change once its bundle is approved.

    Called by the bundle executor. Runs through the same functions as a direct
    call — with no user (the approval is the authorization; the requester's
    Helm permission and namespace access were checked when it was queued) and
    an explicit ``approval_context`` so the gate knows it is already approved.
    Returns ``(output, data)``; raises HelmCommandError on failure.
    """
    item_input = item_input or {}
    cluster_id = str(item_input.get("clusterId") or "")
    namespace = str(item_input.get("namespace") or "")
    release_name = str(item_input.get("resourceName") or "")
    if action_type in ("helm_install", "helm_upgrade"):
        helm_payload = dict(item_input.get("helm") or {})
        helm_payload.setdefault("clusterId", cluster_id)
        helm_payload.setdefault("namespace", namespace)
        helm_payload.setdefault("releaseName", release_name)
        data, err, status = install_or_upgrade_release(
            None, helm_payload, "", approval_context=approval_context
        )
    elif action_type == "helm_rollback":
        data, err, status = rollback_release(
            None, cluster_id, namespace, release_name, item_input.get("revision"),
            approval_context=approval_context,
        )
    elif action_type == "helm_uninstall":
        data, err, status = uninstall_release(
            None, cluster_id, namespace, release_name, approval_context=approval_context
        )
    else:
        raise HelmCommandError(f"Unsupported Helm action: {action_type}")
    if err or status >= 300:
        raise HelmCommandError(err or f"{action_type} did not run (status {status})")
    return str((data or {}).get("output") or "").strip(), data or {}


def record_helm_catalog_entry(user: Optional[User], body: Dict[str, Any]) -> None:
    """Register/refresh the App Catalog entry for an installed/upgraded release.

    Shared by the direct install/upgrade routes and the bundle executor (for a
    change that was queued for approval and has now run).
    """
    from .app_catalog_service import create_or_update_from_helm
    from .helm_chart_template_service import get_chart_template

    template_id = body.get("chartTemplateId") or body.get("chart_template_id")
    template = get_chart_template(str(template_id)) if template_id else None
    create_or_update_from_helm(
        user,
        cluster_id=body.get("clusterId") or body.get("cluster"),
        namespace=body.get("namespace"),
        release_name=body.get("releaseName") or body.get("release_name"),
        chart_name=body.get("chartName") or body.get("chart_name") or (template or {}).get("name"),
        chart_version=(
            body.get("chartVersion") or body.get("chart_version") or (template or {}).get("version")
        ),
        owner_team=body.get("ownerTeam") or body.get("owner_team"),
        environment=body.get("environment"),
        criticality=body.get("criticality"),
        description=body.get("description"),
    )


def filter_releases_for_user(
    user: Optional[User], cluster_id: str, releases: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    """Keep only the releases in namespaces ``user`` may see on ``cluster_id``.

    ``user=None`` is an internal caller and gets everything; admins see all; a
    user without access to the cluster sees nothing.
    """
    if user is None or is_admin(user):
        return releases
    if not can_access_cluster(user, cluster_id):
        return []
    verdicts: Dict[str, bool] = {}
    kept: List[Dict[str, Any]] = []
    for release in releases:
        namespace = str((release or {}).get("namespace") or "")
        if namespace not in verdicts:
            verdicts[namespace] = bool(namespace) and can_access_namespace(user, cluster_id, namespace)
        if verdicts[namespace]:
            kept.append(release)
    return kept


def list_releases(
    cluster_id: str,
    namespace: Optional[str] = None,
    *,
    user: Optional[User] = None,
    run_helm_fn: Optional[RunHelmFn] = None,
) -> List[Dict[str, Any]]:
    """Helm releases on a cluster, in one namespace or across all (``-A``).

    With a ``user`` the result is scoped to what that user may see: a namespace
    they cannot access yields nothing, and the all-namespaces listing is
    filtered to their namespaces. ``user=None`` is for internal callers only
    (e.g. inventory discovery, which filters its own rows per user).
    """
    if user is not None and not is_admin(user):
        if not can_access_cluster(user, cluster_id):
            return []
        if namespace and not can_access_namespace(user, cluster_id, namespace):
            return []
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
        releases = json.loads(output or "[]")
    except (HelmCommandError, json.JSONDecodeError):
        return []
    if not isinstance(releases, list):
        return []
    return filter_releases_for_user(user, cluster_id, releases)


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


# Values keys that commonly hold credentials. Chart values are free-form, so
# this is a name heuristic: it hides `postgresql.auth.password`, `apiKey`,
# `tls.privateKey`, `smtp.secret` and the like from anyone without
# secrets:reveal, the same bar the Secret YAML view applies.
_SENSITIVE_VALUE_WORD = re.compile(
    r"pass(word|wd|phrase)?|secret|token|api[-_]?key|access[-_]?key|private[-_]?key"
    r"|credential|dsn|connection[-_]?string|^key$|[-_]key$",
    re.IGNORECASE,
)
# camelCase `...Key` (apiKey, encryptionKey) without catching "monkey".
_SENSITIVE_VALUE_CAMEL = re.compile(r"[a-z0-9]Key$")
HIDDEN_HELM_VALUE = "<hidden: secrets:reveal required>"


def _is_sensitive_value_key(key: str) -> bool:
    return bool(_SENSITIVE_VALUE_WORD.search(key) or _SENSITIVE_VALUE_CAMEL.search(key))


def _mask_sensitive_values(value: Any) -> Any:
    if isinstance(value, dict):
        masked: Dict[str, Any] = {}
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                masked[key] = _mask_sensitive_values(item)
            elif item not in (None, "") and _is_sensitive_value_key(str(key)):
                masked[key] = HIDDEN_HELM_VALUE
            else:
                masked[key] = item
        return masked
    if isinstance(value, list):
        return [_mask_sensitive_values(item) for item in value]
    return value


def get_release_detail(
    cluster_id: str,
    namespace: str,
    release_name: str,
    *,
    user: Optional[User] = None,
    run_helm_fn: Optional[RunHelmFn] = None,
) -> Optional[Dict[str, Any]]:
    """Release status, manifest and values, redacted to what ``user`` may see.

    ``helm get manifest`` carries every Secret the chart renders and ``helm get
    values`` the chart's credentials, so neither goes out raw by default:
    without ``secrets:reveal`` Secret data is stripped from the manifest and
    credential-looking values are masked; without ``helm:values:view`` the
    values are not returned at all. ``user=None`` (internal callers) gets the
    fully redacted form.
    """
    if user is not None and not is_admin(user):
        if not can_access_cluster(user, cluster_id) or not can_access_namespace(user, cluster_id, namespace):
            return None
    reveal = bool(user) and user_has_permission(user, "secrets:reveal")
    show_values = bool(user) and user_has_permission(user, "helm:values:view")
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
    rendered = sanitize_yaml_preview(manifest) if manifest else ""
    if not show_values:
        values_summary = {}
    elif not reveal:
        values_summary = _mask_sensitive_values(values_summary)
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
        "valuesHidden": not show_values,
        "secretValuesHidden": not reveal,
        "renderedManifest": rendered,
        "manifest": manifest if reveal else rendered,
    }


def _approval_gate(
    user: Optional[User],
    cluster_id: str,
    namespace: str,
    release_name: str,
    action: str,
    *,
    approval_context: Optional[str] = None,
    bundle_payload: Optional[Dict[str, Any]] = None,
    what: str = "",
) -> Optional[Tuple[Optional[Dict[str, Any]], Optional[str], int]]:
    """The cluster's deployment-approval rule, same as a YAML apply.

    ``None`` → go ahead. Otherwise the ``(data, error, status)`` to return: 202
    with a pending-approval payload when the change was queued as a change
    bundle (applied by the executor once approved), or the refusal.
    """
    from .change_bundle_service import gate_or_queue

    return gate_or_queue(
        user,
        cluster_id,
        bundle_payload={
            "actionType": f"helm_{action}",
            "namespace": namespace,
            "resourceKind": "HelmRelease",
            "resourceName": release_name,
            **(bundle_payload or {}),
        },
        what=what or f"helm {action} {release_name}",
        action=f"helm_{action}",
        target_type="helm_release",
        target_id=f"{cluster_id}/{namespace}/{release_name}",
        approval_context=approval_context,
    )


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
