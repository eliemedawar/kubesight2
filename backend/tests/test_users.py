from tests.conftest import auth_headers


def test_viewer_cannot_list_users(client, viewer_token):
    response = client.get("/api/users", headers=auth_headers(viewer_token))
    assert response.status_code == 403


def test_admin_lists_users(client, admin_token):
    response = client.get("/api/users", headers=auth_headers(admin_token))
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["data"]["count"] >= 2


def test_seed_preserves_custom_user_full_name(app):
    from api.db import db
    from api.models import User
    from api.seed import seed_defaults

    with app.app_context():
        admin = User.query.filter_by(username="admin").first()
        assert admin is not None
        admin.full_name = "Custom Admin Name"
        db.session.commit()

        seed_defaults()

        admin = User.query.filter_by(username="admin").first()
        assert admin.full_name == "Custom Admin Name"


def test_soft_disable_user(client, admin_token):
    roles = client.get("/api/roles", headers=auth_headers(admin_token)).get_json()
    viewer_role = next(r for r in roles["data"]["items"] if r["name"] == "viewer")
    create = client.post(
        "/api/users",
        headers=auth_headers(admin_token),
        json={
            "username": "tempuser",
            "password": "temp12345",
            "roleId": viewer_role["id"],
            "fullName": "Temp",
            "email": "temp@test.local",
        },
    )
    assert create.status_code == 201
    user_id = create.get_json()["data"]["id"]

    disable = client.delete(f"/api/users/{user_id}", headers=auth_headers(admin_token))
    assert disable.status_code == 200
    assert disable.get_json()["data"]["isActive"] is False
