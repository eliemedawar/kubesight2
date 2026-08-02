import pytest
from api.models import AuditLog, Permission, Role, User
from tests.conftest import auth_headers


def test_seed_preserves_custom_role_permissions(app):
    """A role an operator created is never touched by seeding.

    This previously asserted the same thing about `viewer`, which is a system
    role (`rbac_data.py:412`), so it was testing the opposite of the contract
    its name describes -- and failing. The distinction is the point: system
    roles are product-managed, custom roles belong to the operator.
    """
    from api.db import db
    from api.seed import seed_defaults

    with app.app_context():
        permissions = Permission.query.limit(3).all()
        assert len(permissions) == 3

        role = Role(name="custom_ops_seed", description="operator owned", is_system_role=False)
        role.permissions = list(permissions)
        db.session.add(role)
        db.session.commit()

        dropped = permissions[0].key
        role.permissions = [p for p in role.permissions if p.key != dropped]
        db.session.commit()

        seed_defaults()

        role = Role.query.filter_by(name="custom_ops_seed").first()
        assert role is not None
        assert all(perm.key != dropped for perm in role.permissions), (
            "seeding must not re-grant a permission removed from a custom role"
        )


def test_seed_reconciles_system_role_permissions(app):
    """System roles snap back to their definition on every seed. Deliberate.

    It is how a release that introduces a permission grants it to existing
    installations. The consequence is that narrowing a built-in role does not
    survive a restart -- to narrow permissions durably, create a custom role.
    Asserted here so the behaviour is a decision on record rather than
    something discovered in production.
    """
    from api.db import db
    from api.seed import seed_defaults

    with app.app_context():
        viewer = Role.query.filter_by(name="viewer").first()
        assert viewer is not None and viewer.is_system_role
        viewer.permissions = [perm for perm in viewer.permissions if perm.key != "logs:view"]
        db.session.commit()

        seed_defaults()

        viewer = Role.query.filter_by(name="viewer").first()
        assert any(perm.key == "logs:view" for perm in viewer.permissions)


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


def _role_assigned_to_viewer(client, admin_token, name="in_use_role"):
    created = client.post(
        "/api/roles",
        headers=auth_headers(admin_token),
        json={
            "name": name,
            "description": "Assigned to a user",
            "permissions": ["clusters:view"],
        },
    ).get_json()["data"]
    viewer = User.query.filter_by(username="viewer").first()
    viewer.role_id = created["id"]
    from api.db import db

    db.session.commit()
    return created


def test_delete_role_with_users_blocked(client, admin_token):
    """Refused while users are assigned, and the error names them.

    Deleting a role strips every assigned user's permissions. That is a large
    authorization change disguised as a tidy-up, so it does not happen as a
    side effect -- the caller has to see who is affected first.
    """
    created = _role_assigned_to_viewer(client, admin_token)

    response = client.delete(f"/api/roles/{created['id']}", headers=auth_headers(admin_token))
    assert response.status_code == 409
    error = response.get_json()["error"].lower()
    assert "assigned" in error
    assert "viewer" in error, "the operator needs to know who is affected"

    assert Role.query.get(created["id"]) is not None
    assert User.query.filter_by(username="viewer").first().role_id == created["id"]


def test_delete_role_with_users_succeeds_when_forced(client, admin_token):
    """`force=true` keeps the capability, unassigns, and records the count."""
    created = _role_assigned_to_viewer(client, admin_token, name="in_use_role_forced")

    response = client.delete(
        f"/api/roles/{created['id']}?force=true", headers=auth_headers(admin_token)
    )
    assert response.status_code == 200
    assert response.get_json()["data"]["deleted"] is True

    assert Role.query.get(created["id"]) is None
    # Unassigned rather than left pointing at a deleted role: no role means no
    # permissions, so the user fails closed until someone reassigns them.
    assert User.query.filter_by(username="viewer").first().role_id is None

    audit = AuditLog.query.filter_by(action="role_deleted").order_by(AuditLog.id.desc()).first()
    assert audit is not None
    assert audit.details["users_unassigned"] == 1
    assert audit.details["forced"] is True


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
