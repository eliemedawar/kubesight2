from __future__ import annotations

import json
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml
from flask import current_app

from .cluster_access import custom_cluster_public_id, parse_custom_cluster_db_id
from .db import db
from .models import Cluster

_HOST_PATTERN = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9.\-]*$")
_ALLOWED_PROTOCOLS = {"http", "https"}


class ClusterValidationError(ValueError):
    pass


def get_kubeconfig_storage_dir() -> Path:
    configured = os.getenv("KUBESIGHT_KUBECONFIG_DIR", "").strip()
    if configured:
        base = Path(configured)
    else:
        base = Path(current_app.root_path).parent / "data" / "kubeconfigs"
    base.mkdir(parents=True, exist_ok=True)
    return base.resolve()


def _kubeconfig_file_path(cluster_db_id: int) -> Path:
    storage_dir = get_kubeconfig_storage_dir()
    filename = f"cluster-{cluster_db_id}.yaml"
    target = (storage_dir / filename).resolve()
    if not str(target).startswith(str(storage_dir)):
        raise ClusterValidationError("Invalid kubeconfig storage path.")
    return target


def validate_host(host: str) -> str:
    value = (host or "").strip()
    if not value or len(value) > 253 or not _HOST_PATTERN.match(value):
        raise ClusterValidationError("host must be a valid DNS name or IP-style hostname.")
    return value


def validate_protocol(protocol: str) -> str:
    value = (protocol or "").strip().lower()
    if value not in _ALLOWED_PROTOCOLS:
        raise ClusterValidationError("protocol must be http or https.")
    return value


def validate_port(port: Any) -> int:
    try:
        parsed = int(port)
    except (TypeError, ValueError) as exc:
        raise ClusterValidationError("port must be an integer between 1 and 65535.") from exc
    if parsed < 1 or parsed > 65535:
        raise ClusterValidationError("port must be an integer between 1 and 65535.")
    return parsed


def validate_name(name: str) -> str:
    value = (name or "").strip()
    if not value or len(value) > 120:
        raise ClusterValidationError("name is required (max 120 characters).")
    return value


def validate_kubeconfig_content(content: str) -> Dict[str, Any]:
    raw = (content or "").strip()
    if not raw:
        raise ClusterValidationError("kubeconfig content is required.")
    if len(raw) > 512_000:
        raise ClusterValidationError("kubeconfig content is too large.")
    try:
        document = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise ClusterValidationError("kubeconfig is not valid YAML.") from exc
    if not isinstance(document, dict):
        raise ClusterValidationError("kubeconfig must be a YAML mapping.")
    if document.get("kind") not in (None, "Config") and "clusters" not in document:
        raise ClusterValidationError("kubeconfig does not look like a Kubernetes config file.")
    if not document.get("clusters") or not document.get("contexts"):
        raise ClusterValidationError("kubeconfig must include clusters and contexts.")
    return document


def resolve_context_name(document: Dict[str, Any], requested: Optional[str]) -> Optional[str]:
    if requested:
        context_names = {
            ctx.get("name")
            for ctx in document.get("contexts", [])
            if isinstance(ctx, dict) and ctx.get("name")
        }
        if requested not in context_names:
            raise ClusterValidationError(f"context '{requested}' was not found in kubeconfig.")
        return requested
    current = document.get("current-context")
    if current:
        return str(current)
    contexts = document.get("contexts") or []
    if len(contexts) == 1 and isinstance(contexts[0], dict):
        return contexts[0].get("name")
    if contexts:
        first = contexts[0]
        if isinstance(first, dict) and first.get("name"):
            return str(first["name"])
    return None


def _patch_server_url(document: Dict[str, Any], context_name: Optional[str], server_url: str) -> None:
    contexts = document.get("contexts") or []
    clusters = {item.get("name"): item for item in document.get("clusters") or [] if isinstance(item, dict)}
    cluster_ref = None
    for ctx in contexts:
        if not isinstance(ctx, dict):
            continue
        if context_name and ctx.get("name") != context_name:
            continue
        if not context_name or ctx.get("name") == context_name:
            cluster_ref = (ctx.get("context") or {}).get("cluster")
            break
    if not cluster_ref and contexts and isinstance(contexts[0], dict):
        cluster_ref = (contexts[0].get("context") or {}).get("cluster")
    if not cluster_ref:
        return
    cluster_entry = clusters.get(cluster_ref)
    if not cluster_entry:
        return
    cluster_entry.setdefault("cluster", {})["server"] = server_url


def build_cluster_kubeconfig(
    *,
    kubeconfig_content: str,
    host: str,
    port: int,
    protocol: str,
    context_name: Optional[str],
) -> Tuple[str, Optional[str]]:
    document = validate_kubeconfig_content(kubeconfig_content)
    resolved_context = resolve_context_name(document, (context_name or "").strip() or None)
    server_url = f"{protocol}://{host}:{port}"
    _patch_server_url(document, resolved_context, server_url)
    rendered = yaml.safe_dump(document, default_flow_style=False)
    return rendered, resolved_context


def write_kubeconfig_file(cluster_db_id: int, content: str) -> str:
    path = _kubeconfig_file_path(cluster_db_id)
    path.write_text(content, encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return str(path)


def read_kubeconfig_file(cluster_db_id: int) -> str:
    path = _kubeconfig_file_path(cluster_db_id)
    if not path.is_file():
        raise ClusterValidationError("Kubeconfig file is missing for this cluster.")
    return path.read_text(encoding="utf-8")


def delete_kubeconfig_file(cluster_db_id: int) -> None:
    path = _kubeconfig_file_path(cluster_db_id)
    if path.exists():
        path.unlink()


def cluster_to_management_dict(cluster: Cluster) -> Dict[str, Any]:
    return {
        "id": cluster.id,
        "publicId": custom_cluster_public_id(cluster.id),
        "name": cluster.name,
        "host": cluster.host,
        "port": cluster.port,
        "protocol": cluster.protocol,
        "connectionMethod": cluster.connection_method or "kubeconfig",
        "authenticationType": cluster.authentication_type,
        "skipTlsVerify": bool(cluster.skip_tls_verify),
        "connectionTimeoutSeconds": cluster.connection_timeout_seconds,
        "contextName": cluster.context_name,
        "isActive": cluster.is_active,
        "lastConnectionStatus": cluster.last_connection_status,
        "lastConnectionError": cluster.last_connection_error,
        "lastTestedAt": cluster.last_tested_at.isoformat() if cluster.last_tested_at else None,
        "createdAt": cluster.created_at.isoformat() if cluster.created_at else None,
        "updatedAt": cluster.updated_at.isoformat() if cluster.updated_at else None,
    }


def list_active_custom_clusters() -> List[Cluster]:
    return (
        Cluster.query.filter_by(is_active=True)
        .order_by(Cluster.id.asc())
        .all()
    )


def get_active_cluster_by_public_id(public_id: str) -> Optional[Cluster]:
    db_id = parse_custom_cluster_db_id(public_id)
    if db_id is None:
        return None
    cluster = Cluster.query.get(db_id)
    if not cluster or not cluster.is_active:
        return None
    return cluster


def _node_summary(nodes_json: Dict[str, Any]) -> List[Dict[str, str]]:
    from .k8s_provider import summarize_node_items

    return summarize_node_items(nodes_json.get("items", []))


def test_cluster_connection(cluster: Cluster) -> Dict[str, Any]:
    from .k8s_provider import K8sCommandError, _run_kubectl

    kubeconfig_path = cluster.kubeconfig_path
    if not kubeconfig_path or not Path(kubeconfig_path).is_file():
        return {
            "success": False,
            "reachable": False,
            "error": "Kubeconfig file is missing for this cluster.",
        }

    context = cluster.context_name or None
    started = time.perf_counter()
    try:
        _run_kubectl(["cluster-info"], context=context, kubeconfig_path=kubeconfig_path)
        version_output = _run_kubectl(
            ["version", "-o", "json"],
            context=context,
            kubeconfig_path=kubeconfig_path,
        )
        version_data = json.loads(version_output)
        server_version = (
            version_data.get("serverVersion", {}).get("gitVersion")
            or version_data.get("serverVersion", {}).get("major")
            or "unknown"
        )
        nodes_output = _run_kubectl(
            ["get", "nodes", "-o", "json"],
            context=context,
            kubeconfig_path=kubeconfig_path,
        )
        nodes_data = json.loads(nodes_output)
        latency_ms = int((time.perf_counter() - started) * 1000)
        return {
            "success": True,
            "reachable": True,
            "nodes": _node_summary(nodes_data),
            "serverVersion": server_version,
            "latencyMs": latency_ms,
        }
    except K8sCommandError as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        return {
            "success": False,
            "reachable": False,
            "error": str(exc),
            "latencyMs": latency_ms,
        }


def record_connection_test(cluster: Cluster, result: Dict[str, Any]) -> None:
    now = datetime.now(timezone.utc)
    cluster.last_tested_at = now
    cluster.updated_at = now
    if result.get("success") and result.get("reachable"):
        cluster.last_connection_status = "connected"
        cluster.last_connection_error = None
    else:
        cluster.last_connection_status = "error"
        cluster.last_connection_error = str(result.get("error") or "Connection failed.")
    db.session.commit()
