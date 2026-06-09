"""User accessRules persist and reload correctly."""

from tests.conftest import auth_headers


def _viewer_id(client, admin_token):
    users = client.get("/api/users", headers=auth_headers(admin_token)).get_json()["data"]["items"]
    viewer = next(u for u in users if u["username"] == "viewer")
    return viewer["id"]


def test_viewer_full_cluster_access_rules_persist(client, admin_token):
    user_id = _viewer_id(client, admin_token)
    rules = [
        {
            "clusterId": "prod-us-east",
            "resourceType": "cluster",
            "permissionKey": "clusters:view",
            "effect": "allow",
        },
        {
            "clusterId": "prod-us-east",
            "resourceType": "cluster",
            "permissionKey": "namespaces:view",
            "effect": "allow",
        },
        {
            "clusterId": "prod-us-east",
            "resourceType": "cluster",
            "permissionKey": "resources:view",
            "effect": "allow",
        },
        {
            "clusterId": "prod-us-east",
            "resourceType": "cluster",
            "permissionKey": "logs:view",
            "effect": "allow",
        },
        {
            "clusterId": "prod-us-east",
            "resourceType": "cluster",
            "permissionKey": "alerts:view",
            "effect": "allow",
        },
    ]

    put = client.put(
        f"/api/users/{user_id}",
        headers=auth_headers(admin_token),
        json={"accessRules": rules},
    )
    assert put.status_code == 200

    profile = client.get(f"/api/users/{user_id}", headers=auth_headers(admin_token)).get_json()["data"]
    saved = profile.get("accessRules") or []
    assert len(saved) >= 5
    assert any(
        r["clusterId"] == "prod-us-east"
        and r["permissionKey"] == "clusters:view"
        and r["resourceType"] == "cluster"
        for r in saved
    )
    assert "prod-us-east" in (profile.get("clusterAccess") or [])
