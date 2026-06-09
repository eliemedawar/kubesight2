from __future__ import annotations

from functools import wraps
from typing import Callable, Optional

from flask import request

from .access_engine import can_access_cluster, can_access_namespace, is_admin, user_has_permission
from .audit import log_audit
from .auth_utils import auth_required_enabled, get_current_user
from .response import error_response


def require_auth(fn: Callable):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not auth_required_enabled():
            return fn(*args, **kwargs)
        user = get_current_user()
        if not user:
            return error_response("Unauthorized", 401)
        return fn(*args, **kwargs)

    return wrapper


def require_admin(fn: Callable):
    @wraps(fn)
    @require_auth
    def wrapper(*args, **kwargs):
        if not auth_required_enabled():
            return fn(*args, **kwargs)
        user = get_current_user()
        if not user or not is_admin(user):
            log_audit(
                "forbidden_access_attempt",
                actor=user,
                target_type="admin",
                target_id="admin_only",
                details={"path": request.path, "method": request.method},
            )
            return error_response("Forbidden", 403)
        return fn(*args, **kwargs)

    return wrapper


def require_permission(permission_key: str):
    def decorator(fn: Callable):
        @wraps(fn)
        @require_auth
        def wrapper(*args, **kwargs):
            if not auth_required_enabled():
                return fn(*args, **kwargs)
            user = get_current_user()
            if not user or not user_has_permission(user, permission_key):
                log_audit(
                    "forbidden_access_attempt",
                    actor=user,
                    target_type="permission",
                    target_id=permission_key,
                    details={"path": request.path, "method": request.method},
                )
                return error_response("Forbidden", 403)
            return fn(*args, **kwargs)

        return wrapper

    return decorator


def require_any_permission(*permission_keys: str):
    def decorator(fn: Callable):
        @wraps(fn)
        @require_auth
        def wrapper(*args, **kwargs):
            if not auth_required_enabled():
                return fn(*args, **kwargs)
            user = get_current_user()
            if not user:
                return error_response("Unauthorized", 401)
            if not any(user_has_permission(user, key) for key in permission_keys):
                log_audit(
                    "forbidden_access_attempt",
                    actor=user,
                    target_type="permission",
                    target_id=",".join(permission_keys),
                    details={"path": request.path, "method": request.method},
                )
                return error_response("Forbidden", 403)
            return fn(*args, **kwargs)

        return wrapper

    return decorator


def _cluster_id_from_request() -> Optional[str]:
    if "cluster_id" in request.view_args:
        return request.view_args.get("cluster_id")
    payload = request.get_json(silent=True) or {}
    return (
        request.args.get("cluster")
        or request.args.get("clusterId")
        or payload.get("clusterId")
        or payload.get("cluster_id")
    )


def _namespace_from_request() -> Optional[str]:
    if "namespace" in request.view_args:
        return request.view_args.get("namespace")
    return request.args.get("namespace")


def require_cluster_access(fn: Callable):
    @wraps(fn)
    @require_auth
    def wrapper(*args, **kwargs):
        if not auth_required_enabled():
            return fn(*args, **kwargs)
        user = get_current_user()
        cluster_id = _cluster_id_from_request()
        if cluster_id and user and not can_access_cluster(user, cluster_id):
            log_audit(
                "forbidden_access_attempt",
                actor=user,
                target_type="cluster",
                target_id=cluster_id,
                details={"path": request.path},
            )
            return error_response("Forbidden", 403)
        return fn(*args, **kwargs)

    return wrapper


def require_namespace_access(fn: Callable):
    @wraps(fn)
    @require_auth
    def wrapper(*args, **kwargs):
        if not auth_required_enabled():
            return fn(*args, **kwargs)
        user = get_current_user()
        cluster_id = _cluster_id_from_request()
        namespace = _namespace_from_request()
        if cluster_id and namespace and user:
            if not can_access_namespace(user, cluster_id, namespace):
                log_audit(
                    "forbidden_access_attempt",
                    actor=user,
                    target_type="namespace",
                    target_id=f"{cluster_id}/{namespace}",
                    details={"path": request.path},
                )
                return error_response("Forbidden", 403)
        return fn(*args, **kwargs)

    return wrapper
