from flask import Blueprint, request

from ..alert_notifier import dispatch_firing_alert_emails, send_test_alert_email
from ..services.alert_policy_evaluator import list_active_policy_alerts
from ..email_delivery import EmailDeliveryError
from ..k8s_provider import K8sCommandError, list_alerts_from_k8s, should_use_real_k8s
from ..mock_data import ALERTS
from ..access import get_user_cluster_ids, is_admin
from ..auth_utils import get_current_user
from ..decorators import require_permission
from ..response import error_response, success_response

alerts_bp = Blueprint("alerts", __name__, url_prefix="/api")


def _filter_mock_alerts(cluster_id):
    if not cluster_id:
        return ALERTS
    return [alert for alert in ALERTS if alert.get("clusterId") == cluster_id]


def _merge_policy_alerts(items: list, user, cluster_id) -> list:
    if cluster_id:
        policy_alerts = list_active_policy_alerts(cluster_id=cluster_id, user=user)
    else:
        policy_alerts = list_active_policy_alerts(user=user)
    if not policy_alerts:
        return items
    existing_ids = {item.get("id") for item in items}
    merged = list(items)
    for alert in policy_alerts:
        if alert.get("id") not in existing_ids:
            merged.append(alert)
    return merged


def _attach_email_delivery(payload: dict) -> dict:
    items = payload.get("items") or []
    metadata = dict(payload.get("metadata") or {})
    metadata["emailDelivery"] = dispatch_firing_alert_emails(items)
    payload["metadata"] = metadata
    return payload


@alerts_bp.route("/alerts", methods=["GET"])
@require_permission("alerts:view")
def list_alerts():
    cluster_id = request.args.get("cluster", "").strip() or None
    user = get_current_user()

    if user and not is_admin(user):
        allowed = set(get_user_cluster_ids(user))
        if cluster_id and cluster_id not in allowed:
            return error_response("Forbidden", 403)
        if not cluster_id and allowed:
            combined_items = []
            for cid in allowed:
                if should_use_real_k8s(cid):
                    try:
                        payload = list_alerts_from_k8s(cluster_id=cid)
                        combined_items.extend(payload.get("items") or [])
                    except Exception:
                        pass
                else:
                    combined_items.extend(_filter_mock_alerts(cid))
            combined_items = _merge_policy_alerts(combined_items, user, None)
            return success_response(
                _attach_email_delivery(
                    {
                        "items": combined_items,
                        "count": len(combined_items),
                        "metadata": {"mode": "filtered", "source": "rbac"},
                    }
                )
            )

    if should_use_real_k8s(cluster_id):
        try:
            payload = list_alerts_from_k8s(cluster_id=cluster_id)
            payload["items"] = _merge_policy_alerts(payload.get("items") or [], user, cluster_id)
            payload["count"] = len(payload["items"])
            return success_response(_attach_email_delivery(payload))
        except K8sCommandError as exc:
            return success_response(
                {
                    "items": [],
                    "count": 0,
                    "metadata": {
                        "mode": "real",
                        "source": "none",
                        "hasLiveAlertsSource": False,
                        "reason": "k8s_alert_derivation_unavailable",
                        "detail": str(exc),
                    },
                }
            )
        except Exception:
            # Real mode must not fall back to fake alerts.
            return success_response(
                {
                    "items": [],
                    "count": 0,
                    "metadata": {
                        "mode": "real",
                        "source": "none",
                        "hasLiveAlertsSource": False,
                        "reason": "no_alert_source_available",
                    },
                }
            )

    items = _merge_policy_alerts(_filter_mock_alerts(cluster_id), user, cluster_id)
    return success_response(
        _attach_email_delivery(
            {
                "items": items,
                "count": len(items),
                "metadata": {
                    "mode": "mock",
                    "source": "mock_data",
                    "clusterId": cluster_id,
                },
            }
        )
    )


@alerts_bp.route("/alerts/notifications/email/test", methods=["POST"])
@require_permission("alerts:manage")
def test_alert_email():
    try:
        return success_response(send_test_alert_email())
    except EmailDeliveryError as exc:
        return error_response(str(exc), 400)
