"""Shared pod log fetch logic for legacy and REST log endpoints."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

from flask import Request

from ..access_engine import can_view_logs
from ..audit import log_audit
from ..auth_utils import get_current_user
from ..k8s_provider import (
    K8sCommandError,
    list_pod_containers_from_k8s,
    list_namespace_pods_for_logs,
    pod_logs_from_k8s,
    resolve_cluster_access,
    should_use_real_k8s,
)
from ..log_noise import filter_health_probe_log_lines, filter_live_log_noise
from ..log_time_filters import (
    filter_log_lines_after,
    filter_log_lines_until,
    format_rfc3339_z,
    parse_log_time_filters,
)
from ..mock_data import NAMESPACE_RESOURCES, NAMESPACES
from ..response import error_response


def _parse_bool(value: str, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return str(value).lower() in {"1", "true", "yes", "on"}


def _parse_tail_lines(value: str, *, live: bool, incremental: bool) -> Optional[int]:
    if value is not None and str(value).strip():
        try:
            parsed = int(value)
        except ValueError:
            return None
        if parsed < 1 or parsed > 10000:
            return None
        return parsed
    if incremental:
        return 50
    if live:
        return 500
    return 200


def parse_logs_query(request: Request) -> Tuple[Optional[Dict[str, Any]], Optional[Any]]:
    """Parse shared log query parameters. Returns (params dict, error_response)."""
    live = _parse_bool(request.args.get("live", "false"))
    previous = _parse_bool(request.args.get("previous", "false"))
    follow = _parse_bool(request.args.get("follow", "false"))
    timestamps = _parse_bool(request.args.get("timestamps", "true"), default=True)

    if follow:
        return None, error_response(
            "follow=true is not supported; use live polling with sinceTime instead.",
            400,
        )

    time_filters, time_filter_error = parse_log_time_filters(
        request.args.get("sinceSeconds", ""),
        request.args.get("sinceTime", ""),
        request.args.get("untilTime", ""),
    )
    if time_filter_error:
        return None, error_response(time_filter_error, 400)

    incremental = time_filters.since_time is not None and time_filters.since_seconds is None
    tail_lines = _parse_tail_lines(
        request.args.get("tailLines", ""),
        live=live,
        incremental=incremental,
    )
    if request.args.get("tailLines", "").strip() and tail_lines is None:
        return None, error_response("tailLines must be an integer between 1 and 10000.", 400)

    return {
        "live": live,
        "previous": previous,
        "timestamps": timestamps,
        "time_filters": time_filters,
        "incremental": incremental,
        "tail_lines": tail_lines,
    }, None


def _mock_log_lines(
    *,
    cluster_id: str,
    namespace: str,
    pod: str,
    container: str,
    previous: bool,
    live: bool,
    time_filters,
) -> list[str]:
    now = datetime.now(timezone.utc)
    lines = [
        f"{format_rfc3339_z(now - timedelta(seconds=2))} INFO [{container}] Starting request processor",
        f"{format_rfc3339_z(now - timedelta(seconds=1))} INFO [{container}] Connected to upstream service",
        f"{format_rfc3339_z(now)} WARN [{container}] Retry on transient network timeout",
        f'{format_rfc3339_z(now)} INFO [{container}] 127.0.0.1 - "GET /health HTTP/1.1" 200 -',
    ]
    if previous:
        lines.append(
            f"{format_rfc3339_z(now)} ERROR [{container}] Previous instance exited with code 137"
        )
    if live:
        lines.append(f"{format_rfc3339_z(datetime.now(timezone.utc))} INFO [{container}] Live stream tick")
    if time_filters.since_seconds is not None:
        since_cutoff = now - timedelta(seconds=time_filters.since_seconds)
        lines = filter_log_lines_after(lines, since_cutoff)
    if time_filters.since_time is not None:
        lines = filter_log_lines_after(lines, time_filters.since_time)
    if time_filters.until_time is not None:
        lines = filter_log_lines_until(lines, time_filters.until_time)
    if live:
        lines = filter_live_log_noise(lines)
    else:
        lines = filter_health_probe_log_lines(lines)
    return lines


def _build_query_payload(
    *,
    cluster_id: str,
    namespace: str,
    pod: str,
    container: Optional[str],
    params: Dict[str, Any],
) -> Dict[str, Any]:
    time_filters = params["time_filters"]
    query: Dict[str, Any] = {
        "cluster": cluster_id,
        "namespace": namespace,
        "pod": pod,
        "container": container or "",
        "live": params["live"],
        "previous": params["previous"],
        "timestamps": params["timestamps"],
    }
    if params["tail_lines"] is not None:
        query["tailLines"] = params["tail_lines"]
    if time_filters.since_seconds is not None:
        query["sinceSeconds"] = time_filters.since_seconds
    if time_filters.since_time is not None:
        query["sinceTime"] = format_rfc3339_z(time_filters.since_time)
    if time_filters.until_time is not None:
        query["untilTime"] = format_rfc3339_z(time_filters.until_time)
    return query


def fetch_pod_logs(
    *,
    cluster_id: str,
    namespace: str,
    pod_name: str,
    container_name: Optional[str],
    params: Dict[str, Any],
) -> Tuple[Optional[Dict[str, Any]], Optional[Any]]:
    user = get_current_user()
    if user and not can_view_logs(user, cluster_id, namespace, pod_name, container_name):
        log_audit(
            "forbidden_access_attempt",
            actor=user,
            target_type="logs",
            target_id=f"{cluster_id}/{namespace}/{pod_name}",
        )
        return None, error_response("Forbidden", 403)

    time_filters = params["time_filters"]
    container = (container_name or "").strip() or None

    if not should_use_real_k8s(cluster_id):
        mock_items = _mock_namespace_pods(cluster_id, namespace)
        if mock_items is None:
            return None, error_response("Cluster not found", 404)
        mock_pod = next((item for item in mock_items if item.get("name") == pod_name), None)
        if not mock_pod:
            return None, error_response("Pod not found", 404)
        if container and container not in (mock_pod.get("containers") or []):
            return None, error_response("Container not found", 404)

    if should_use_real_k8s(cluster_id):
        access = resolve_cluster_access(cluster_id)
        if not access:
            return None, error_response("Cluster not found", 404)
        try:
            data = pod_logs_from_k8s(
                access=access,
                namespace=namespace,
                pod=pod_name,
                container=container,
                live=params["live"],
                previous=params["previous"],
                since_seconds=time_filters.since_seconds,
                since_time=time_filters.since_time,
                until_time=time_filters.until_time,
                tail_lines=params["tail_lines"],
                timestamps=params["timestamps"],
            )
            return data, None
        except K8sCommandError as exc:
            message = str(exc).lower()
            if "not found" in message or "does not exist" in message:
                return None, error_response(f"Pod or container not found: {exc}", 404)
            return None, error_response(f"Failed to fetch kubernetes logs: {exc}", 500)

    mock_container = container or "app"
    lines = _mock_log_lines(
        cluster_id=cluster_id,
        namespace=namespace,
        pod=pod_name,
        container=mock_container,
        previous=params["previous"],
        live=params["live"],
        time_filters=time_filters,
    )
    query = _build_query_payload(
        cluster_id=cluster_id,
        namespace=namespace,
        pod=pod_name,
        container=mock_container,
        params=params,
    )
    return {
        "query": query,
        "stream": "live" if params["live"] else "snapshot",
        "lines": lines,
    }, None


def _mock_namespace_pods(cluster_id: str, namespace: str) -> Optional[list]:
    namespaces = NAMESPACES.get(cluster_id)
    if namespaces is None:
        return None
    namespace_names = {item.get("name") for item in namespaces}
    if namespace not in namespace_names:
        return None
    resources = NAMESPACE_RESOURCES.get(cluster_id, {}).get(namespace, {})
    pods = resources.get("pods") or []
    items = []
    for pod in pods:
        name = pod.get("name")
        if not name:
            continue
        containers = pod.get("containers")
        if not containers:
            image = pod.get("image", "app:latest")
            containers = [image.split("/")[-1].split(":")[0] or "app"]
        items.append(
            {
                "name": name,
                "namespace": namespace,
                "status": pod.get("status", "Unknown"),
                "containers": containers,
            }
        )
    return items


def list_pods_for_logs(cluster_id: str, namespace: str) -> Tuple[Optional[Dict[str, Any]], Optional[Any]]:
    user = get_current_user()

    if should_use_real_k8s(cluster_id):
        access = resolve_cluster_access(cluster_id)
        if not access:
            return None, error_response("Cluster not found", 404)
        try:
            payload = list_namespace_pods_for_logs(access, namespace)
        except K8sCommandError as exc:
            return None, error_response(f"Failed to list pods: {exc}", 500)
    else:
        items = _mock_namespace_pods(cluster_id, namespace)
        if items is None:
            return None, error_response("Cluster not found", 404)
        payload = {"clusterId": cluster_id, "namespace": namespace, "items": items, "count": len(items)}

    if user:
        filtered = []
        for pod in payload.get("items") or []:
            name = pod.get("name")
            if not name:
                continue
            allowed = can_view_logs(user, cluster_id, namespace, name)
            pod_copy = dict(pod)
            pod_copy["canViewLogs"] = allowed
            if allowed:
                filtered.append(pod_copy)
        payload = {**payload, "items": filtered, "count": len(filtered)}
    return payload, None


def list_containers_for_pod(
    cluster_id: str, namespace: str, pod_name: str
) -> Tuple[Optional[Dict[str, Any]], Optional[Any]]:
    user = get_current_user()
    if user and not can_view_logs(user, cluster_id, namespace, pod_name):
        log_audit(
            "forbidden_access_attempt",
            actor=user,
            target_type="logs",
            target_id=f"{cluster_id}/{namespace}/{pod_name}",
        )
        return None, error_response("Forbidden", 403)

    if should_use_real_k8s(cluster_id):
        access = resolve_cluster_access(cluster_id)
        if not access:
            return None, error_response("Cluster not found", 404)
        try:
            payload = list_pod_containers_from_k8s(access, namespace, pod_name)
        except K8sCommandError as exc:
            message = str(exc).lower()
            if "not found" in message:
                return None, error_response(f"Pod not found: {exc}", 404)
            return None, error_response(f"Failed to list containers: {exc}", 500)
    else:
        items = _mock_namespace_pods(cluster_id, namespace)
        if items is None:
            return None, error_response("Cluster not found", 404)
        pod = next((item for item in items if item.get("name") == pod_name), None)
        if not pod:
            return None, error_response("Pod not found", 404)
        payload = {
            "clusterId": cluster_id,
            "namespace": namespace,
            "pod": pod_name,
            "items": [{"name": name} for name in pod.get("containers") or ["app"]],
            "count": len(pod.get("containers") or ["app"]),
        }

    if user:
        allowed_items = []
        for item in payload.get("items") or []:
            container_name = item.get("name")
            if can_view_logs(user, cluster_id, namespace, pod_name, container_name):
                allowed_items.append(item)
        payload = {**payload, "items": allowed_items, "count": len(allowed_items)}
    return payload, None
