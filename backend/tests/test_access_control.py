from tests.conftest import auth_headers


def test_viewer_can_list_clusters(client, viewer_token):
    response = client.get("/api/clusters", headers=auth_headers(viewer_token))
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    items = payload["data"]["items"]
    assert isinstance(items, list)


def test_viewer_cannot_access_user_management_api(client, viewer_token):
    response = client.get("/api/audit-logs", headers=auth_headers(viewer_token))
    assert response.status_code == 403


def test_viewer_cannot_update_settings(client, viewer_token):
    response = client.put(
        "/api/settings",
        headers=auth_headers(viewer_token),
        json={"theme": "dark"},
    )
    assert response.status_code == 403
