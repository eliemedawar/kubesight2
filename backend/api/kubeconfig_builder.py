"""Build and parse Kubernetes kubeconfig documents for custom clusters."""

from __future__ import annotations

import base64
import re
from typing import Any, Dict, Optional, Tuple
from urllib.parse import urlparse

import yaml

from .cluster_store import ClusterValidationError

_CONNECTION_METHODS = {"kubeconfig", "manual"}
_AUTH_TYPES = {"token", "certificate", "anonymous"}


def _pem_to_b64(pem: str) -> str:
    value = (pem or "").strip()
    if not value:
        raise ClusterValidationError("Certificate material is required.")
    return base64.b64encode(value.encode("utf-8")).decode("ascii")


def _validate_auth_type(value: str) -> str:
    auth = (value or "").strip().lower()
    if auth not in _AUTH_TYPES:
        raise ClusterValidationError("authenticationType must be token, certificate, or anonymous.")
    return auth


def validate_connection_method(value: str) -> str:
    method = (value or "kubeconfig").strip().lower()
    if method not in _CONNECTION_METHODS:
        raise ClusterValidationError("connectionMethod must be kubeconfig or manual.")
    return method


def _resolve_cluster_entry(document: Dict[str, Any], context_name: Optional[str]) -> Dict[str, Any]:
    contexts = document.get("contexts") or []
    clusters = {
        item.get("name"): item
        for item in document.get("clusters") or []
        if isinstance(item, dict) and item.get("name")
    }
    cluster_ref = None
    for ctx in contexts:
        if not isinstance(ctx, dict):
            continue
        if context_name and ctx.get("name") != context_name:
            continue
        cluster_ref = (ctx.get("context") or {}).get("cluster")
        break
    if not cluster_ref and contexts and isinstance(contexts[0], dict):
        cluster_ref = (contexts[0].get("context") or {}).get("cluster")
    if not cluster_ref:
        raise ClusterValidationError("Could not resolve cluster entry from kubeconfig.")
    entry = clusters.get(cluster_ref)
    if not entry or not isinstance(entry.get("cluster"), dict):
        raise ClusterValidationError("Kubeconfig cluster entry is missing.")
    return entry["cluster"]


def extract_server_target(document: Dict[str, Any], context_name: Optional[str]) -> Tuple[str, int, str]:
    cluster_block = _resolve_cluster_entry(document, context_name)
    server = str(cluster_block.get("server") or "").strip()
    if not server:
        raise ClusterValidationError("Kubeconfig cluster server URL is missing.")
    parsed = urlparse(server)
    if not parsed.hostname:
        raise ClusterValidationError("Kubeconfig server URL is invalid.")
    protocol = (parsed.scheme or "https").lower()
    if protocol not in ("http", "https"):
        raise ClusterValidationError("Kubeconfig server URL must use http or https.")
    port = parsed.port or (443 if protocol == "https" else 80)
    return parsed.hostname, int(port), protocol


def apply_kubeconfig_advanced_options(
    document: Dict[str, Any],
    *,
    context_name: Optional[str],
    skip_tls_verify: bool = False,
    custom_ca: Optional[str] = None,
) -> Dict[str, Any]:
    cluster_block = _resolve_cluster_entry(document, context_name)
    if skip_tls_verify:
        cluster_block["insecure-skip-tls-verify"] = True
    if custom_ca and custom_ca.strip():
        cluster_block["certificate-authority-data"] = _pem_to_b64(custom_ca)
    return document


def generate_manual_kubeconfig(
    *,
    name: str,
    host: str,
    port: int,
    protocol: str,
    authentication_type: str,
    bearer_token: Optional[str] = None,
    client_certificate: Optional[str] = None,
    client_key: Optional[str] = None,
    ca_certificate: Optional[str] = None,
    context_name: Optional[str] = None,
    skip_tls_verify: bool = False,
) -> Tuple[str, str]:
    auth = _validate_auth_type(authentication_type)
    server = f"{protocol}://{host}:{port}"
    safe_name = re.sub(r"[^a-zA-Z0-9\-]+", "-", (name or "cluster").strip())[:40] or "cluster"
    cluster_ref = f"{safe_name}-cluster"
    user_ref = f"{safe_name}-user"
    context = (context_name or "").strip() or f"{safe_name}-context"

    if auth == "token":
        token = (bearer_token or "").strip()
        if not token:
            raise ClusterValidationError("Bearer token is required for token authentication.")
        user_block: Dict[str, Any] = {"token": token}
    elif auth == "certificate":
        if not (client_certificate or "").strip() or not (client_key or "").strip():
            raise ClusterValidationError("Client certificate and client key are required.")
        user_block = {
            "client-certificate-data": _pem_to_b64(client_certificate or ""),
            "client-key-data": _pem_to_b64(client_key or ""),
        }
    else:
        user_block = {}

    cluster_block: Dict[str, Any] = {"server": server}
    if ca_certificate and ca_certificate.strip():
        cluster_block["certificate-authority-data"] = _pem_to_b64(ca_certificate)
    if skip_tls_verify:
        cluster_block["insecure-skip-tls-verify"] = True

    document = {
        "apiVersion": "v1",
        "kind": "Config",
        "clusters": [{"name": cluster_ref, "cluster": cluster_block}],
        "contexts": [{"name": context, "context": {"cluster": cluster_ref, "user": user_ref}}],
        "current-context": context,
        "users": [{"name": user_ref, "user": user_block}],
    }
    return yaml.safe_dump(document, default_flow_style=False), context
