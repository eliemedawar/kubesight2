"""Namespace events API — RBAC and mock payload structure."""

from api.access_engine import can_access_namespace
from api.auth_utils import auth_required_enabled
from api.models import AccessRule, User
from tests.conftest import auth_headers

EVENT_URL = "/api/clusters/prod-us-east/namespaces/{namespace}/events"

REQUIRED_EVENT_KEYS = {
    "type",
    "reason",
    "message",
    "involvedKind",
    "involvedName",
    "count",
    "firstTimestamp",
    "lastTimestamp",
    "age",
    "source",
}


def _viewer_id(client, admin_token):
    users = client.get("/api/users", headers=auth_headers(admin_token)).get_json()["data"]["items"]
    viewer = next(u for u in users if u["username"] == "viewer")
    return viewer["id"]


def _restrict_viewer_namespaces(client, admin_token, namespaces):
    user_id = _viewer_id(client, admin_token)
    response = client.put(
        f"/api/users/{user_id}",
        headers=auth_headers(admin_token),
        json={
            "clusterAccess": ["prod-us-east"],
            "namespaceAccess": [
                {"clusterId": "prod-us-east", "namespace": ns} for ns in namespaces
            ],
        },
    )
    assert response.status_code == 200
    from api.db import db
    from api.models import AccessRule

    db.session.expire_all()
    assert AccessRule.query.filter_by(user_id=user_id).count() >= len(namespaces) * 2


def test_events_endpoint_requires_auth(client):
    assert auth_required_enabled()
    response = client.get(EVENT_URL.format(namespace="payments"))
    assert response.status_code == 401


def test_namespace_access_rules_enforced(app, client, admin_token):
    _restrict_viewer_namespaces(client, admin_token, ["payments"])
    with app.app_context():
        viewer = User.query.filter_by(username="viewer").first()
        assert AccessRule.query.filter_by(user_id=viewer.id).count() >= 2
        assert can_access_namespace(viewer, "prod-us-east", "payments") is True
        assert can_access_namespace(viewer, "prod-us-east", "checkout") is False
        assert can_access_namespace(viewer, "prod-us-east", "kube-system") is False


def test_admin_can_retrieve_namespace_events(client, admin_token):
    response = client.get(
        EVENT_URL.format(namespace="payments"),
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["clusterId"] == "prod-us-east"
    assert data["namespace"] == "payments"
    assert data["count"] == len(data["items"])
    assert data["count"] >= 6
    reasons = {item["reason"] for item in data["items"]}
    assert "Scheduled" in reasons
    assert "BackOff" in reasons
    assert "OOMKilled" in reasons


def test_namespace_events_handler_denies_checkout(app, client, admin_token, viewer_token):
    _restrict_viewer_namespaces(client, admin_token, ["payments"])
    with app.test_request_context(
        EVENT_URL.format(namespace="checkout"),
        method="GET",
        headers=auth_headers(viewer_token),
    ):
        from api.auth_utils import get_current_user
        from api.routes.clusters import namespace_events

        user = get_current_user()
        assert user is not None
        assert user.username == "viewer"
        assert can_access_namespace(user, "prod-us-east", "checkout") is False
        response = namespace_events("prod-us-east", "checkout")
        status_code = response[1] if isinstance(response, tuple) else response.status_code
        assert status_code == 403


def test_viewer_allowed_namespace_events(app, client, admin_token, viewer_token):
    _restrict_viewer_namespaces(client, admin_token, ["payments"])
    with app.app_context():
        viewer = User.query.filter_by(username="viewer").first()
        assert can_access_namespace(viewer, "prod-us-east", "checkout") is False

    allowed = client.get(
        EVENT_URL.format(namespace="payments"),
        headers=auth_headers(viewer_token),
    )
    assert allowed.status_code == 200
    assert allowed.get_json()["data"]["count"] > 0

    denied = client.get(
        EVENT_URL.format(namespace="checkout"),
        headers=auth_headers(viewer_token),
    )
    assert denied.status_code == 403


def test_viewer_without_namespace_access_gets_403(client, admin_token, viewer_token):
    _restrict_viewer_namespaces(client, admin_token, ["payments"])
    response = client.get(
        EVENT_URL.format(namespace="kube-system"),
        headers=auth_headers(viewer_token),
    )
    assert response.status_code == 403


def test_mock_mode_returns_stable_event_structure(client, admin_token):
    response = client.get(
        EVENT_URL.format(namespace="payments"),
        headers=auth_headers(admin_token),
        query_string={"limit": 3},
    )
    assert response.status_code == 200
    items = response.get_json()["data"]["items"]
    assert len(items) == 3
    for item in items:
        assert REQUIRED_EVENT_KEYS <= set(item.keys())
        assert isinstance(item["source"], dict)
        assert "component" in item["source"]
    timestamps = [item["lastTimestamp"] for item in items]
    assert timestamps == sorted(timestamps, reverse=True)


def test_involved_filter_query_params(client, admin_token):
    response = client.get(
        EVENT_URL.format(namespace="payments"),
        headers=auth_headers(admin_token),
        query_string={"involvedKind": "Pod", "involvedName": "payments-api-84b5d5"},
    )
    assert response.status_code == 200
    items = response.get_json()["data"]["items"]
    assert items
    assert all(item["involvedKind"] == "Pod" for item in items)
    assert all(item["involvedName"] == "payments-api-84b5d5" for item in items)
