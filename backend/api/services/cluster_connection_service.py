"""Parse cluster connection payloads and produce kubeconfig files."""

from __future__ import annotations

from typing import Any, Dict, Optional

import yaml

from ..cluster_store import (
    ClusterValidationError,
    build_cluster_kubeconfig,
    read_kubeconfig_file,
    resolve_context_name,
    validate_host,
    validate_kubeconfig_content,
    validate_name,
    validate_port,
    validate_protocol,
)
from ..kubeconfig_builder import (
    apply_kubeconfig_advanced_options,
    extract_server_target,
    generate_manual_kubeconfig,
    validate_connection_method,
)
from ..models import Cluster


def _advanced_from_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    advanced = payload.get("advanced") or {}
    return {
        "context_name": str(
            payload.get("contextName")
            or payload.get("context_name")
            or advanced.get("contextOverride")
            or ""
        ).strip()
        or None,
        "skip_tls_verify": bool(
            payload.get("skipTlsVerify")
            or payload.get("skip_tls_verify")
            or advanced.get("skipTlsVerify")
        ),
        "custom_ca": str(
            payload.get("customCa")
            or payload.get("custom_ca")
            or advanced.get("customCa")
            or ""
        ).strip()
        or None,
        "connection_timeout": payload.get("connectionTimeoutSeconds")
        or payload.get("connection_timeout_seconds")
        or advanced.get("connectionTimeout"),
    }


def _parse_timeout(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ClusterValidationError("connectionTimeout must be a positive integer.") from exc
    if parsed < 1 or parsed > 300:
        raise ClusterValidationError("connectionTimeout must be between 1 and 300 seconds.")
    return parsed


def _has_manual_credentials(payload: Dict[str, Any]) -> bool:
    return bool(
        (payload.get("bearerToken") or payload.get("bearer_token") or "").strip()
        or (payload.get("clientCertificate") or payload.get("client_certificate") or "").strip()
        or (payload.get("clientKey") or payload.get("client_key") or "").strip()
        or str(payload.get("authenticationType") or payload.get("authentication_type") or "").strip()
    )


def build_kubeconfig_from_create_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Return kubeconfig text and cluster metadata fields for a new cluster."""
    name = validate_name(str(payload.get("name", "")))
    connection_method = validate_connection_method(
        str(payload.get("connectionMethod") or payload.get("connection_method") or "kubeconfig")
    )
    advanced = _advanced_from_payload(payload)
    timeout = _parse_timeout(advanced.get("connection_timeout"))

    if connection_method == "kubeconfig":
        kubeconfig_content = str(
            payload.get("kubeconfigContent") or payload.get("kubeconfig_content") or ""
        ).strip()
        if not kubeconfig_content:
            raise ClusterValidationError("Kubeconfig YAML or file upload is required.")

        document = validate_kubeconfig_content(kubeconfig_content)
        resolved_context = resolve_context_name(document, advanced["context_name"])
        document = apply_kubeconfig_advanced_options(
            document,
            context_name=resolved_context,
            skip_tls_verify=advanced["skip_tls_verify"],
            custom_ca=advanced["custom_ca"],
        )
        host, port, protocol = extract_server_target(document, resolved_context)
        rendered = yaml.safe_dump(document, default_flow_style=False)
        return {
            "name": name,
            "host": host,
            "port": port,
            "protocol": protocol,
            "connection_method": "kubeconfig",
            "authentication_type": None,
            "skip_tls_verify": advanced["skip_tls_verify"],
            "connection_timeout_seconds": timeout,
            "context_name": resolved_context,
            "kubeconfig_content": rendered,
        }

    host = validate_host(str(payload.get("host", "")))
    port = validate_port(payload.get("port"))
    protocol = validate_protocol(str(payload.get("protocol", "https")))
    auth_type = str(
        payload.get("authenticationType") or payload.get("authentication_type") or ""
    ).strip().lower()
    if not auth_type:
        raise ClusterValidationError("authenticationType is required for manual connection.")

    rendered, resolved_context = generate_manual_kubeconfig(
        name=name,
        host=host,
        port=port,
        protocol=protocol,
        authentication_type=auth_type,
        bearer_token=str(payload.get("bearerToken") or payload.get("bearer_token") or ""),
        client_certificate=str(
            payload.get("clientCertificate") or payload.get("client_certificate") or ""
        ),
        client_key=str(payload.get("clientKey") or payload.get("client_key") or ""),
        ca_certificate=str(payload.get("caCertificate") or payload.get("ca_certificate") or ""),
        context_name=advanced["context_name"],
        skip_tls_verify=advanced["skip_tls_verify"],
    )

    if advanced["custom_ca"]:
        document = yaml.safe_load(rendered)
        document = apply_kubeconfig_advanced_options(
            document,
            context_name=resolved_context,
            skip_tls_verify=advanced["skip_tls_verify"],
            custom_ca=advanced["custom_ca"],
        )
        rendered = yaml.safe_dump(document, default_flow_style=False)

    return {
        "name": name,
        "host": host,
        "port": port,
        "protocol": protocol,
        "connection_method": "manual",
        "authentication_type": auth_type,
        "skip_tls_verify": advanced["skip_tls_verify"],
        "connection_timeout_seconds": timeout,
        "context_name": resolved_context,
        "kubeconfig_content": rendered,
    }


def build_kubeconfig_from_update_payload(cluster: Cluster, payload: Dict[str, Any]) -> Optional[str]:
    """Build updated kubeconfig content, or None if kubeconfig unchanged."""
    advanced = _advanced_from_payload(payload)
    connection_method = cluster.connection_method or "kubeconfig"
    if payload.get("connectionMethod") or payload.get("connection_method"):
        connection_method = validate_connection_method(
            str(payload.get("connectionMethod") or payload.get("connection_method"))
        )

    kubeconfig_upload = payload.get("kubeconfigContent") or payload.get("kubeconfig_content")

    if connection_method == "kubeconfig" and kubeconfig_upload:
        document = validate_kubeconfig_content(str(kubeconfig_upload))
        resolved = resolve_context_name(document, advanced["context_name"] or cluster.context_name)
        document = apply_kubeconfig_advanced_options(
            document,
            context_name=resolved,
            skip_tls_verify=advanced["skip_tls_verify"],
            custom_ca=advanced["custom_ca"],
        )
        return yaml.safe_dump(document, default_flow_style=False)

    if connection_method == "kubeconfig" and not kubeconfig_upload:
        if not any(
            [
                advanced["context_name"],
                advanced["custom_ca"],
                advanced["skip_tls_verify"] != cluster.skip_tls_verify,
            ]
        ):
            return None
        existing = read_kubeconfig_file(cluster.id)
        document = validate_kubeconfig_content(existing)
        resolved = resolve_context_name(
            document, advanced["context_name"] or cluster.context_name
        )
        document = apply_kubeconfig_advanced_options(
            document,
            context_name=resolved,
            skip_tls_verify=advanced["skip_tls_verify"],
            custom_ca=advanced["custom_ca"],
        )
        return yaml.safe_dump(document, default_flow_style=False)

    host = validate_host(str(payload.get("host", cluster.host)))
    port = validate_port(payload.get("port", cluster.port))
    protocol = validate_protocol(str(payload.get("protocol", cluster.protocol)))
    auth_type = str(
        payload.get("authenticationType")
        or payload.get("authentication_type")
        or cluster.authentication_type
        or "token"
    ).strip().lower()

    if _has_manual_credentials(payload):
        rendered, _ = generate_manual_kubeconfig(
            name=cluster.name,
            host=host,
            port=port,
            protocol=protocol,
            authentication_type=auth_type,
            bearer_token=str(payload.get("bearerToken") or payload.get("bearer_token") or ""),
            client_certificate=str(
                payload.get("clientCertificate") or payload.get("client_certificate") or ""
            ),
            client_key=str(payload.get("clientKey") or payload.get("client_key") or ""),
            ca_certificate=str(payload.get("caCertificate") or payload.get("ca_certificate") or ""),
            context_name=advanced["context_name"] or cluster.context_name,
            skip_tls_verify=advanced["skip_tls_verify"],
        )
        return rendered

    existing = read_kubeconfig_file(cluster.id)
    rendered, _ = build_cluster_kubeconfig(
        kubeconfig_content=existing,
        host=host,
        port=port,
        protocol=protocol,
        context_name=advanced["context_name"] or cluster.context_name,
    )
    return rendered
