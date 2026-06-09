from tests.conftest import auth_headers


def test_admin_can_read_settings(client, admin_token):
    response = client.get("/api/settings", headers=auth_headers(admin_token))
    assert response.status_code == 200
    assert response.get_json()["success"] is True


def test_viewer_cannot_read_settings(client, viewer_token):
    response = client.get("/api/settings", headers=auth_headers(viewer_token))
    assert response.status_code == 403
