from tests.conftest import auth_headers


def test_viewer_can_list_alerts(client, viewer_token):
    response = client.get("/api/alerts", headers=auth_headers(viewer_token))
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True


def test_viewer_cannot_send_test_email(client, viewer_token):
    response = client.post(
        "/api/alerts/notifications/email/test",
        headers=auth_headers(viewer_token),
    )
    assert response.status_code == 403
