from datetime import datetime, timezone
from typing import Optional

import yaml
from flask import Blueprint, request

from ..cluster_access import custom_cluster_public_id, parse_custom_cluster_db_id
from ..cluster_store import (
    ClusterValidationError,
    build_cluster_kubeconfig,
    cluster_to_management_dict,
    list_active_custom_clusters,
    record_connection_test,
    test_cluster_connection,
    validate_name,
    write_kubeconfig_file,
)
from ..kubeconfig_builder import extract_server_target
from ..services.cluster_connection_service import (
    _advanced_from_payload,
    _parse_timeout,
    build_kubeconfig_from_create_payload,
    build_kubeconfig_from_update_payload,
)
from ..db import db
from ..k8s_provider import (
    K8sCommandError,
    cluster_overview_from_k8s,
    invalidate_cluster_list_cache,
    list_clusters_from_k8s,
    list_namespaces_from_k8s,
    list_configmaps_secrets_from_k8s,
    list_nodes_from_k8s,
    list_storage_classes_from_k8s,
    namespace_events_from_k8s,
    namespace_resource_list_from_k8s,
    namespace_resources_from_k8s,
    NAMESPACE_RESOURCE_LIST_KEYS,
    resolve_cluster_access,
    should_use_real_k8s,
)
from ..access_engine import (
    can_access_namespace,
    can_access_resource,
    filter_clusters_for_user,
    filter_namespace_events,
    filter_namespace_resources,
    filter_namespaces_for_user,
    is_admin,
)
from ..auth_utils import auth_required_enabled, get_current_user
from ..decorators import require_cluster_access, require_namespace_access, require_permission
from ..mock_data import (
    CLUSTERS,
    CLUSTER_NODES,
    CLUSTER_OVERVIEWS,
    NAMESPACES,
    NAMESPACE_EVENTS,
    NAMESPACE_RESOURCES,
    STORAGE_CLASSES,
)
from ..models import Cluster
from ..response import error_response, success_response
from ..services.logs_service import fetch_pod_logs, parse_logs_query
from ..services.resource_actions_service import (
    get_deployment_rollout_history,
    get_resource_describe,
    get_resource_yaml,
    restart_resource,
)

clusters_bp = Blueprint("clusters", __name__, url_prefix="/api/clusters")


def _list_clusters_payload():
    if should_use_real_k8s():
        try:
            return list_clusters_from_k8s()
        except K8sCommandError as exc:
            custom_only = {
                "items": [],
                "count": 0,
            }
            try:
                from ..k8s_provider import _custom_clusters_as_items

                custom_only["items"] = _custom_clusters_as_items()
                custom_only["count"] = len(custom_only["items"])
                if custom_only["items"]:
                    return custom_only
            except Exception:
                pass
            return error_response(f"Failed to query kubernetes clusters: {exc}", 503)

    discovered = {"items": CLUSTERS, "count": len(CLUSTERS)}
    try:
        from ..k8s_provider import _custom_clusters_as_items

        custom_items = _custom_clusters_as_items()
        if custom_items:
            discovered["items"] = list(discovered["items"]) + custom_items
            discovered["count"] = len(discovered["items"])
    except Exception:
        pass
    return discovered


def _resolve_cluster_access_or_error(cluster_id: str):
    if should_use_real_k8s(cluster_id):
        access = resolve_cluster_access(cluster_id)
        if not access:
            return None, error_response("Cluster not found", 404)
        return access, None
    return None, None


@clusters_bp.route("", methods=["GET"])
@require_permission("clusters:view")
def list_clusters():
    payload = _list_clusters_payload()
    if not isinstance(payload, dict):
        return payload
    user = get_current_user()
    if user and payload.get("items"):
        payload["items"] = filter_clusters_for_user(user, payload["items"])
        payload["count"] = len(payload["items"])
    return success_response(payload)


@clusters_bp.route("/custom", methods=["GET"])
@require_permission("clusters:view")
def list_custom_clusters():
    clusters = list_active_custom_clusters()
    items = [cluster_to_management_dict(cluster) for cluster in clusters]
    user = get_current_user()
    if user and items:
        items = filter_clusters_for_user(user, items)
    return success_response({"items": items, "count": len(items)})


@clusters_bp.route("/custom", methods=["POST"])
@require_permission("clusters:add")
def create_custom_cluster():
    payload = request.get_json(silent=True) or {}
    try:
        fields = build_kubeconfig_from_create_payload(payload)
        rendered = fields["kubeconfig_content"]
    except ClusterValidationError as exc:
        return error_response(str(exc), 400)

    now = datetime.now(timezone.utc)
    cluster = Cluster(
        name=fields["name"],
        host=fields["host"],
        port=fields["port"],
        protocol=fields["protocol"],
        connection_method=fields["connection_method"],
        authentication_type=fields["authentication_type"],
        skip_tls_verify=fields["skip_tls_verify"],
        connection_timeout_seconds=fields["connection_timeout_seconds"],
        context_name=fields["context_name"],
        is_active=True,
        created_at=now,
        updated_at=now,
    )
    db.session.add(cluster)
    db.session.flush()

    try:
        kubeconfig_path = write_kubeconfig_file(cluster.id, rendered)
    except ClusterValidationError as exc:
        db.session.rollback()
        return error_response(str(exc), 400)

    cluster.kubeconfig_path = kubeconfig_path
    db.session.commit()
    invalidate_cluster_list_cache()

    test_result = test_cluster_connection(cluster)
    record_connection_test(cluster, test_result)

    return success_response(
        {
            "cluster": cluster_to_management_dict(cluster),
            "test": test_result,
        },
        status_code=201,
    )


def _get_custom_cluster_or_404(cluster_ref: str):
    db_id = parse_custom_cluster_db_id(cluster_ref)
    if db_id is None:
        return None, error_response("Cluster not found", 404)
    cluster = Cluster.query.get(db_id)
    if not cluster:
        return None, error_response("Cluster not found", 404)
    return cluster, None


@clusters_bp.route("/custom/<cluster_ref>", methods=["PUT"])
@require_permission("clusters:update")
def update_custom_cluster(cluster_ref: str):
    cluster, err = _get_custom_cluster_or_404(cluster_ref)
    if err:
        return err

    payload = request.get_json(silent=True) or {}
    try:
        advanced = _advanced_from_payload(payload)
        if "name" in payload:
            cluster.name = validate_name(str(payload.get("name", "")))
        if "host" in payload:
            from ..cluster_store import validate_host

            cluster.host = validate_host(str(payload.get("host", "")))
        if "port" in payload:
            from ..cluster_store import validate_port

            cluster.port = validate_port(payload.get("port"))
        if "protocol" in payload:
            from ..cluster_store import validate_protocol

            cluster.protocol = validate_protocol(str(payload.get("protocol", "")))
        if payload.get("connectionMethod") or payload.get("connection_method"):
            from ..kubeconfig_builder import validate_connection_method

            cluster.connection_method = validate_connection_method(
                str(payload.get("connectionMethod") or payload.get("connection_method"))
            )
        if payload.get("authenticationType") or payload.get("authentication_type"):
            cluster.authentication_type = str(
                payload.get("authenticationType") or payload.get("authentication_type")
            ).strip().lower()
        if "skipTlsVerify" in payload or "skip_tls_verify" in payload:
            cluster.skip_tls_verify = advanced["skip_tls_verify"]
        if "connectionTimeoutSeconds" in payload or advanced.get("connection_timeout") is not None:
            cluster.connection_timeout_seconds = _parse_timeout(advanced.get("connection_timeout"))

        rendered = build_kubeconfig_from_update_payload(cluster, payload)
        if rendered:
            cluster.kubeconfig_path = write_kubeconfig_file(cluster.id, rendered)
            if cluster.connection_method == "kubeconfig":
                document = yaml.safe_load(rendered)
                host, port, protocol = extract_server_target(document, cluster.context_name)
                cluster.host = host
                cluster.port = port
                cluster.protocol = protocol
                cluster.authentication_type = None
        elif advanced["context_name"]:
            cluster.context_name = advanced["context_name"]

        cluster.updated_at = datetime.now(timezone.utc)
        db.session.commit()
        invalidate_cluster_list_cache()
    except ClusterValidationError as exc:
        return error_response(str(exc), 400)

    return success_response({"cluster": cluster_to_management_dict(cluster)})


@clusters_bp.route("/custom/<cluster_ref>", methods=["DELETE"])
@require_permission("clusters:remove")
def delete_custom_cluster(cluster_ref: str):
    cluster, err = _get_custom_cluster_or_404(cluster_ref)
    if err:
        return err

    cluster.is_active = False
    cluster.updated_at = datetime.now(timezone.utc)
    db.session.commit()
    invalidate_cluster_list_cache()
    return success_response(
        {
            "publicId": custom_cluster_public_id(cluster.id),
            "isActive": False,
        }
    )


@clusters_bp.route("/custom/<cluster_ref>/test", methods=["POST"])
@require_permission("clusters:test")
def test_custom_cluster(cluster_ref: str):
    cluster, err = _get_custom_cluster_or_404(cluster_ref)
    if err:
        return err
    if not cluster.is_active:
        return error_response("Cluster is not active.", 400)

    result = test_cluster_connection(cluster)
    record_connection_test(cluster, result)
    return success_response(result)


@clusters_bp.route("/<cluster_id>/overview", methods=["GET"])
@require_permission("overview:view")
@require_cluster_access
def cluster_overview(cluster_id: str):
    access, err = _resolve_cluster_access_or_error(cluster_id)
    if err:
        return err
    if access:
        try:
            return success_response(cluster_overview_from_k8s(access))
        except K8sCommandError as exc:
            return error_response(f"Failed to load cluster overview: {exc}", 503)

    overview = CLUSTER_OVERVIEWS.get(cluster_id)
    if not overview:
        return error_response("Cluster not found", 404)
    return success_response(overview)


@clusters_bp.route("/<cluster_id>/nodes", methods=["GET"])
@require_permission("clusters:view")
@require_cluster_access
def cluster_nodes(cluster_id: str):
    access, err = _resolve_cluster_access_or_error(cluster_id)
    if err:
        return err
    if access:
        try:
            return success_response(list_nodes_from_k8s(access))
        except K8sCommandError as exc:
            return error_response(f"Failed to load nodes: {exc}", 503)

    items = CLUSTER_NODES.get(cluster_id)
    if items is None:
        return error_response("Cluster not found", 404)
    return success_response(items)


@clusters_bp.route("/<cluster_id>/storageclasses", methods=["GET"])
@require_permission("clusters:view")
@require_cluster_access
def cluster_storage_classes(cluster_id: str):
    access, err = _resolve_cluster_access_or_error(cluster_id)
    if err:
        return err
    if access:
        try:
            return success_response(list_storage_classes_from_k8s(access))
        except K8sCommandError as exc:
            return error_response(f"Failed to load storage classes: {exc}", 503)

    items = STORAGE_CLASSES.get(cluster_id)
    if items is None:
        return error_response("Cluster not found", 404)
    return success_response(items)


@clusters_bp.route("/<cluster_id>/namespaces/<namespace>/config-resources", methods=["GET"])
@require_permission("resources:view")
@require_cluster_access
@require_namespace_access
def namespace_config_resources(cluster_id: str, namespace: str):
    """ConfigMap and Secret names (and their keys) for the deployment wizard."""
    access, err = _resolve_cluster_access_or_error(cluster_id)
    if err:
        return err
    if access:
        try:
            return success_response(list_configmaps_secrets_from_k8s(access, namespace))
        except K8sCommandError as exc:
            return error_response(f"Failed to load config resources: {exc}", 503)

    # Mock mode: surface names from the canned namespace resources (no real keys).
    cluster_resources = NAMESPACE_RESOURCES.get(cluster_id) or {}
    ns_resources = cluster_resources.get(namespace) or {}
    config_maps = [
        {"name": cm.get("name", ""), "keys": []}
        for cm in ns_resources.get("configMaps", [])
        if cm.get("name")
    ]
    secrets = [
        {"name": sec.get("name", ""), "type": sec.get("type") or "Opaque", "keys": []}
        for sec in ns_resources.get("secrets", [])
        if sec.get("name")
    ]
    return success_response({"configMaps": config_maps, "secrets": secrets})


@clusters_bp.route("/<cluster_id>/namespaces", methods=["GET"])
@require_permission("namespaces:view")
@require_cluster_access
def cluster_namespaces(cluster_id: str):
    user = get_current_user()
    access, err = _resolve_cluster_access_or_error(cluster_id)
    if err:
        return err
    if access:
        try:
            namespaces = list_namespaces_from_k8s(access)
            items = namespaces["items"]
            if user:
                items = filter_namespaces_for_user(user, cluster_id, items)
            return success_response(
                {
                    "clusterId": cluster_id,
                    "items": items,
                    "count": len(items),
                }
            )
        except K8sCommandError as exc:
            return error_response(f"Failed to load namespaces: {exc}", 503)

    namespaces = NAMESPACES.get(cluster_id)
    if namespaces is None:
        return error_response("Cluster not found", 404)
    items = namespaces
    if user:
        items = filter_namespaces_for_user(user, cluster_id, items)
    return success_response({"clusterId": cluster_id, "items": items, "count": len(items)})


@clusters_bp.route("/<cluster_id>/namespaces/<namespace>/resources", methods=["GET"])
@require_permission("resources:view")
@require_cluster_access
@require_namespace_access
def namespace_resources(cluster_id: str, namespace: str):
    user = get_current_user()
    if user and not is_admin(user):
        from ..access_engine import can_access_namespace

        if not can_access_namespace(user, cluster_id, namespace):
            from ..audit import log_audit

            log_audit(
                "forbidden_access_attempt",
                actor=user,
                target_type="namespace",
                target_id=f"{cluster_id}/{namespace}",
            )
            return error_response("Forbidden", 403)

    access, err = _resolve_cluster_access_or_error(cluster_id)
    if err:
        return err
    if access:
        try:
            resources = namespace_resources_from_k8s(access, namespace)
            if user:
                resources = filter_namespace_resources(user, cluster_id, resources)
            return success_response(resources)
        except K8sCommandError as exc:
            return error_response(f"Failed to load namespace resources: {exc}", 503)

    namespaces = NAMESPACES.get(cluster_id)
    if namespaces is None:
        return error_response("Cluster not found", 404)
    namespace_names = {item.get("name") for item in namespaces}
    if namespace not in namespace_names:
        return error_response("Namespace not found", 404)

    cluster_resources = NAMESPACE_RESOURCES.get(cluster_id)
    if cluster_resources is None:
        from ..access_engine import NAMESPACE_RESOURCE_LIST_KEYS

        payload = {"namespace": namespace, **{key: [] for key in NAMESPACE_RESOURCE_LIST_KEYS}}
        if user:
            payload = filter_namespace_resources(user, cluster_id, payload)
        return success_response(payload)

    resources = cluster_resources.get(namespace)
    if resources is None:
        from ..access_engine import NAMESPACE_RESOURCE_LIST_KEYS

        payload = {"namespace": namespace, **{key: [] for key in NAMESPACE_RESOURCE_LIST_KEYS}}
        if user:
            payload = filter_namespace_resources(user, cluster_id, payload)
        return success_response(payload)

    if user:
        resources = filter_namespace_resources(user, cluster_id, resources)
    return success_response(resources)


@clusters_bp.route("/<cluster_id>/namespaces/<namespace>/resources/<resource_type>", methods=["GET"])
@require_permission("resources:view")
@require_cluster_access
@require_namespace_access
def namespace_resource_list(cluster_id: str, namespace: str, resource_type: str):
    if resource_type not in NAMESPACE_RESOURCE_LIST_KEYS:
        return error_response(f"Unsupported resource type: {resource_type}", 400)

    user = get_current_user()
    if user and not is_admin(user):
        if not can_access_namespace(user, cluster_id, namespace):
            from ..audit import log_audit

            log_audit(
                "forbidden_access_attempt",
                actor=user,
                target_type="namespace",
                target_id=f"{cluster_id}/{namespace}",
            )
            return error_response("Forbidden", 403)

    access, err = _resolve_cluster_access_or_error(cluster_id)
    if err:
        return err
    if access:
        try:
            resources = namespace_resource_list_from_k8s(access, namespace, resource_type)
            if user:
                resources = filter_namespace_resources(user, cluster_id, resources)
            return success_response(resources)
        except K8sCommandError as exc:
            return error_response(f"Failed to load {resource_type}: {exc}", 503)
        except ValueError as exc:
            return error_response(str(exc), 400)

    namespaces = NAMESPACES.get(cluster_id)
    if namespaces is None:
        return error_response("Cluster not found", 404)
    namespace_names = {item.get("name") for item in namespaces}
    if namespace not in namespace_names:
        return error_response("Namespace not found", 404)

    cluster_resources = NAMESPACE_RESOURCES.get(cluster_id)
    if cluster_resources is None:
        payload = {"namespace": namespace, resource_type: []}
        if user:
            payload = filter_namespace_resources(user, cluster_id, payload)
        return success_response(payload)

    resources = cluster_resources.get(namespace)
    if resources is None:
        payload = {"namespace": namespace, resource_type: []}
        if user:
            payload = filter_namespace_resources(user, cluster_id, payload)
        return success_response(payload)

    payload = {"namespace": namespace, resource_type: resources.get(resource_type) or []}
    if user:
        payload = filter_namespace_resources(user, cluster_id, payload)
    return success_response(payload)


@clusters_bp.route(
    "/<cluster_id>/namespaces/<namespace>/resources/<resource_kind>/<resource_name>/describe",
    methods=["GET"],
)
@require_permission("resources:view")
@require_cluster_access
@require_namespace_access
def resource_describe(cluster_id: str, namespace: str, resource_kind: str, resource_name: str):
    user = get_current_user()
    data, error, status = get_resource_describe(
        user, cluster_id, namespace, resource_kind, resource_name
    )
    if error:
        return error_response(error, status)
    return success_response(data)


@clusters_bp.route(
    "/<cluster_id>/namespaces/<namespace>/resources/<resource_kind>/<resource_name>/yaml",
    methods=["GET"],
)
@require_permission("resources:view")
@require_cluster_access
@require_namespace_access
def resource_yaml(cluster_id: str, namespace: str, resource_kind: str, resource_name: str):
    user = get_current_user()
    data, error, status = get_resource_yaml(
        user, cluster_id, namespace, resource_kind, resource_name
    )
    if error:
        return error_response(error, status)
    return success_response(data)


@clusters_bp.route(
    "/<cluster_id>/namespaces/<namespace>/resources/<resource_kind>/<resource_name>/restart",
    methods=["POST"],
)
@require_permission("apps:deploy")
@require_cluster_access
@require_namespace_access
def resource_restart(cluster_id: str, namespace: str, resource_kind: str, resource_name: str):
    user = get_current_user()
    data, error, status = restart_resource(
        user, cluster_id, namespace, resource_kind, resource_name
    )
    if error:
        return error_response(error, status)
    return success_response(data)


@clusters_bp.route(
    "/<cluster_id>/namespaces/<namespace>/deployments/<deployment_name>/rollout-history",
    methods=["GET"],
)
@require_permission("resources:view")
@require_cluster_access
@require_namespace_access
def deployment_rollout_history(cluster_id: str, namespace: str, deployment_name: str):
    user = get_current_user()
    data, error, status = get_deployment_rollout_history(
        user, cluster_id, namespace, deployment_name
    )
    if error:
        return error_response(error, status)
    return success_response(data)


def _involved_kind_resource_type(involved_kind: str) -> Optional[str]:
    mapping = {
        "Pod": "pod",
        "Deployment": "deployment",
        "Service": "service",
    }
    return mapping.get(involved_kind.strip()) if involved_kind else None


@clusters_bp.route("/<cluster_id>/namespaces/<namespace>/events", methods=["GET"])
@require_permission("resources:view")
@require_cluster_access
@require_namespace_access
def namespace_events(cluster_id: str, namespace: str):
    involved_kind = (request.args.get("involvedKind") or "").strip() or None
    involved_name = (request.args.get("involvedName") or "").strip() or None
    limit = request.args.get("limit", type=int)

    user = get_current_user()
    if auth_required_enabled():
        if not user:
            return error_response("Unauthorized", 401)
        if not is_admin(user) and not can_access_namespace(user, cluster_id, namespace):
            from ..audit import log_audit

            log_audit(
                "forbidden_access_attempt",
                actor=user,
                target_type="namespace",
                target_id=f"{cluster_id}/{namespace}",
            )
            return error_response("Forbidden", 403)

    if user and involved_kind and involved_name and not is_admin(user):
        resource_type = _involved_kind_resource_type(involved_kind)
        if resource_type and not can_access_resource(
            user, cluster_id, namespace, resource_type, involved_name
        ):
            from ..audit import log_audit

            log_audit(
                "forbidden_access_attempt",
                actor=user,
                target_type=resource_type,
                target_id=f"{cluster_id}/{namespace}/{involved_name}",
            )
            return error_response("Forbidden", 403)

    access, err = _resolve_cluster_access_or_error(cluster_id)
    if err:
        return err
    if access:
        try:
            payload = namespace_events_from_k8s(
                access,
                namespace,
                involved_kind=involved_kind,
                involved_name=involved_name,
                limit=limit,
            )
            if user:
                payload = filter_namespace_events(user, cluster_id, namespace, payload)
            return success_response(payload)
        except K8sCommandError as exc:
            return error_response(f"Failed to load namespace events: {exc}", 503)

    namespaces = NAMESPACES.get(cluster_id)
    if namespaces is None:
        return error_response("Cluster not found", 404)
    namespace_names = {item.get("name") for item in namespaces}
    if namespace not in namespace_names:
        return error_response("Namespace not found", 404)

    cluster_events = NAMESPACE_EVENTS.get(cluster_id, {})
    items = list(cluster_events.get(namespace, []))

    if involved_kind:
        items = [event for event in items if event.get("involvedKind") == involved_kind]
    if involved_name:
        items = [event for event in items if event.get("involvedName") == involved_name]
    items.sort(
        key=lambda event: event.get("lastTimestamp") or event.get("firstTimestamp") or "",
        reverse=True,
    )
    if limit is not None and limit > 0:
        items = items[:limit]

    payload = {
        "clusterId": cluster_id,
        "namespace": namespace,
        "items": items,
        "count": len(items),
    }
    if user:
        payload = filter_namespace_events(user, cluster_id, namespace, payload)
    return success_response(payload)


@clusters_bp.route("/<cluster_id>/namespaces/<namespace>/pods", methods=["GET"])
@require_permission("logs:view")
@require_cluster_access
@require_namespace_access
def list_namespace_pods_for_logs_route(cluster_id: str, namespace: str):
    from ..services.logs_service import list_pods_for_logs

    payload, error = list_pods_for_logs(cluster_id, namespace)
    if error:
        return error
    return success_response(payload)


@clusters_bp.route(
    "/<cluster_id>/namespaces/<namespace>/pods/<pod_name>/containers", methods=["GET"]
)
@require_permission("logs:view")
@require_cluster_access
@require_namespace_access
def list_pod_containers_route(cluster_id: str, namespace: str, pod_name: str):
    from ..services.logs_service import list_containers_for_pod

    payload, error = list_containers_for_pod(cluster_id, namespace, pod_name)
    if error:
        return error
    return success_response(payload)


@clusters_bp.route(
    "/<cluster_id>/namespaces/<namespace>/pods/<pod_name>/containers/<container_name>/logs",
    methods=["GET"],
)
@require_permission("logs:view")
@require_cluster_access
@require_namespace_access
def get_container_logs_route(
    cluster_id: str, namespace: str, pod_name: str, container_name: str
):
    params, param_error = parse_logs_query(request)
    if param_error:
        return param_error

    # Map live=true query param for incremental polling (same as legacy endpoint).
    if request.args.get("live", "").lower() == "true":
        params = {**params, "live": True}

    data, error = fetch_pod_logs(
        cluster_id=cluster_id,
        namespace=namespace,
        pod_name=pod_name,
        container_name=container_name,
        params=params,
    )
    if error:
        return error
    return success_response(data)
