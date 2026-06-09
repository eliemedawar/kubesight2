from api.models import AuditLog, Role, User
from tests.conftest import auth_headers


def test_seed_preserves_custom_role_permissions(app):
    from api.db import db
    from api.seed import seed_defaults

    with app.app_context():
        viewer = Role.query.filter_by(name="viewer").first()
        assert viewer is not None
        viewer.permissions = [perm for perm in viewer.permissions if perm.key != "logs:view"]
        db.session.commit()

        seed_defaults()

        viewer = Role.query.filter_by(name="viewer").first()
        assert all(perm.key != "logs:view" for perm in viewer.permissions)


def test_create_role_admin(client, admin_token):
    response = client.post(
        "/api/roles",
        headers=auth_headers(admin_token),
        json={
            "name": "custom_ops",
            "description": "Custom operations role",
            "permissions": ["clusters:view", "logs:view", "alerts:view"],
        },
    )
    assert response.status_code == 201
    payload = response.get_json()
    role = payload["data"]
    assert role["name"] == "custom_ops"
    assert role["userCount"] == 0
    assert "clusters:view" in role["permissions"]

    audit = AuditLog.query.filter_by(action="role_created").order_by(AuditLog.id.desc()).first()
    assert audit is not None
    assert audit.details["name"] == "custom_ops"


def test_create_role_requires_permissions(client, admin_token):
    response = client.post(
        "/api/roles",
        headers=auth_headers(admin_token),
        json={"name": "empty_role", "description": "No permissions", "permissions": []},
    )
    assert response.status_code == 400


def test_create_role_unique_name(client, admin_token):
    first = client.post(
        "/api/roles",
        headers=auth_headers(admin_token),
        json={
            "name": "duplicate_role",
            "description": "First",
            "permissions": ["clusters:view"],
        },
    )
    assert first.status_code == 201
    second = client.post(
        "/api/roles",
        headers=auth_headers(admin_token),
        json={
            "name": "duplicate_role",
            "description": "Second",
            "permissions": ["clusters:view"],
        },
    )
    assert second.status_code == 409


def test_get_role(client, admin_token):
    created = client.post(
        "/api/roles",
        headers=auth_headers(admin_token),
        json={
            "name": "inspectable_role",
            "description": "Inspect me",
            "permissions": ["audit:view"],
        },
    ).get_json()["data"]
    response = client.get(f"/api/roles/{created['id']}", headers=auth_headers(admin_token))
    assert response.status_code == 200
    assert response.get_json()["data"]["name"] == "inspectable_role"


def test_update_role(client, admin_token):
    created = client.post(
        "/api/roles",
        headers=auth_headers(admin_token),
        json={
            "name": "editable_role",
            "description": "Before",
            "permissions": ["clusters:view"],
        },
    ).get_json()["data"]
    response = client.put(
        f"/api/roles/{created['id']}",
        headers=auth_headers(admin_token),
        json={
            "description": "After",
            "permissions": ["clusters:view", "logs:view"],
        },
    )
    assert response.status_code == 200
    role = response.get_json()["data"]
    assert role["description"] == "After"
    assert "logs:view" in role["permissions"]

    audit = AuditLog.query.filter_by(action="role_updated").order_by(AuditLog.id.desc()).first()
    assert audit is not None


def test_delete_role_with_users_blocked(client, admin_token):
    created = client.post(
        "/api/roles",
        headers=auth_headers(admin_token),
        json={
            "name": "in_use_role",
            "description": "Assigned to a user",
            "permissions": ["clusters:view"],
        },
    ).get_json()["data"]
    viewer = User.query.filter_by(username="viewer").first()
    viewer.role_id = created["id"]
    from api.db import db

    db.session.commit()

    response = client.delete(f"/api/roles/{created['id']}", headers=auth_headers(admin_token))
    assert response.status_code == 400
    assert "assigned" in response.get_json()["error"].lower()


def test_delete_system_role_blocked(client, admin_token):
    role = Role.query.filter_by(name="admin").first()
    response = client.delete(f"/api/roles/{role.id}", headers=auth_headers(admin_token))
    assert response.status_code == 400
    assert "system" in response.get_json()["error"].lower()


def test_delete_custom_role(client, admin_token):
    created = client.post(
        "/api/roles",
        headers=auth_headers(admin_token),
        json={
            "name": "deletable_role",
            "description": "Temporary",
            "permissions": ["clusters:view"],
        },
    ).get_json()["data"]
    response = client.delete(f"/api/roles/{created['id']}", headers=auth_headers(admin_token))
    assert response.status_code == 200
    assert Role.query.get(created["id"]) is None

    audit = AuditLog.query.filter_by(action="role_deleted").order_by(AuditLog.id.desc()).first()
    assert audit is not None


def test_viewer_cannot_create_role(client, viewer_token):
    response = client.post(
        "/api/roles",
        headers=auth_headers(viewer_token),
        json={
            "name": "viewer_role",
            "description": "Nope",
            "permissions": ["clusters:view"],
        },
    )
    assert response.status_code == 403


def test_list_roles_includes_user_count(client, admin_token):
    response = client.get("/api/roles", headers=auth_headers(admin_token))
    assert response.status_code == 200
    roles = response.get_json()["data"]["items"]
    admin_role = next(role for role in roles if role["name"] == "admin")
    assert admin_role["userCount"] >= 1


def test_user_role_change_audit(client, admin_token):
    custom = client.post(
        "/api/roles",
        headers=auth_headers(admin_token),
        json={
            "name": "assignable_role",
            "description": "Assignable",
            "permissions": ["clusters:view", "logs:view"],
        },
    ).get_json()["data"]
    viewer = User.query.filter_by(username="viewer").first()
    response = client.put(
        f"/api/users/{viewer.id}",
        headers=auth_headers(admin_token),
        json={"roleId": custom["id"]},
    )
    assert response.status_code == 200

    audit = AuditLog.query.filter_by(action="user_role_changed").order_by(AuditLog.id.desc()).first()
    assert audit is not None
    assert audit.details["newRole"] == "assignable_role"
