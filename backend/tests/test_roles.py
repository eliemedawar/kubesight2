from tests.conftest import auth_headers


def test_list_roles_admin(client, admin_token):
    response = client.get("/api/roles", headers=auth_headers(admin_token))
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["data"]["count"] >= 1


def test_viewer_cannot_update_role_permissions(client, viewer_token):
    response = client.put(
        "/api/roles/2/permissions",
        headers=auth_headers(viewer_token),
        json={"permissions": ["clusters:view"]},
    )
    assert response.status_code == 403
