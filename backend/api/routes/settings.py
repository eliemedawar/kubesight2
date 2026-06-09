from flask import Blueprint, request

from ..k8s_provider import list_clusters_from_k8s, should_use_real_k8s
from ..models import AppSettings
from ..notification_routing import merge_notifications, serialize_notifications
from ..decorators import require_permission
from ..response import error_response, success_response

settings_bp = Blueprint("settings", __name__, url_prefix="/api")

ALLOWED_KEYS = {"theme", "refreshIntervalSeconds", "defaultCluster", "notifications"}
ALLOWED_THEMES = {"system", "light", "dark"}


def _resolve_default_cluster(stored: str) -> str:
    stored = (stored or "").strip()
    if not should_use_real_k8s():
        return stored or "prod-us-east"
    try:
        clusters = list_clusters_from_k8s().get("items", [])
        cluster_ids = {cluster.get("id") for cluster in clusters if cluster.get("id")}
        if stored and stored in cluster_ids:
            return stored
        if clusters:
            return clusters[0].get("id") or stored
    except Exception:
        pass
    return stored


def _serialize_settings(settings: AppSettings):
    notifications = settings.notifications if isinstance(settings.notifications, dict) else {}
    return {
        "theme": settings.theme or "system",
        "refreshIntervalSeconds": int(settings.refresh_interval_seconds or 30),
        "defaultCluster": _resolve_default_cluster(settings.default_cluster or ""),
        "notifications": serialize_notifications(notifications),
    }


@settings_bp.route("/settings", methods=["GET"])
@require_permission("settings:view")
def get_settings():
    settings = AppSettings.query.first()
    if not settings:
        payload = {
            "theme": "system",
            "refreshIntervalSeconds": 30,
            "defaultCluster": "prod-us-east",
            "notifications": serialize_notifications({}),
        }
        return success_response(payload)

    return success_response(_serialize_settings(settings))


@settings_bp.route("/settings", methods=["PUT"])
@require_permission("settings:manage")
def update_settings():
    payload = request.get_json(silent=True) or {}
    updates = {k: v for k, v in payload.items() if k in ALLOWED_KEYS}
    if not updates:
        return error_response("No valid settings fields provided.", 400)

    if "theme" in updates:
        theme = str(updates["theme"]).strip().lower()
        if theme not in ALLOWED_THEMES:
            return error_response("Invalid theme value. Expected one of: system, light, dark.", 400)
        updates["theme"] = theme

    if "refreshIntervalSeconds" in updates:
        try:
            refresh = int(updates["refreshIntervalSeconds"])
        except (TypeError, ValueError):
            return error_response("refreshIntervalSeconds must be an integer.", 400)
        if refresh < 5 or refresh > 3600:
            return error_response("refreshIntervalSeconds must be between 5 and 3600.", 400)
        updates["refreshIntervalSeconds"] = refresh

    if "defaultCluster" in updates:
        cluster_id = str(updates["defaultCluster"]).strip()
        if not cluster_id:
            return error_response("defaultCluster cannot be empty.", 400)
        updates["defaultCluster"] = cluster_id

    settings = AppSettings.query.first()
    if not settings:
        settings = AppSettings()

    if "notifications" in updates:
        notifications = updates["notifications"]
        if not isinstance(notifications, dict):
            return error_response("notifications must be an object.", 400)
        merged_notifications, routing_errors = merge_notifications(settings.notifications, notifications)
        if routing_errors:
            return error_response(" ".join(routing_errors), 400)
        updates["notifications"] = merged_notifications

    if "theme" in updates:
        settings.theme = updates["theme"]
    if "refreshIntervalSeconds" in updates:
        settings.refresh_interval_seconds = updates["refreshIntervalSeconds"]
    if "defaultCluster" in updates:
        settings.default_cluster = updates["defaultCluster"]
    if "notifications" in updates:
        settings.notifications = updates["notifications"]

    from ..db import db  # local import to avoid circulars

    db.session.add(settings)
    db.session.commit()

    return success_response({"updated": list(updates.keys()), "settings": _serialize_settings(settings)})
