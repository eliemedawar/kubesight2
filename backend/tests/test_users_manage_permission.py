"""users:manage should satisfy granular user and role permission checks."""

from api.models import Permission, Role, User
from api.passwords import hash_password
from tests.conftest import auth_headers


def _create_user_manager_role(app):
    with app.app_context():
        perms = Permission.query.filter(
            Permission.key.in_(["users:manage", "clusters:view"])
        ).all()
        role = Role(
            name="user_manager",
            description="User manager test role",
            is_system_role=False,
        )
        role.permissions = perms
        from api.db import db

        db.session.add(role)
        db.session.flush()
        user = User(
            username="usermgr",
            email="usermgr@test.local",
            password_hash=hash_password("usermgr123"),
            full_name="User Manager",
            role=role,
            is_active=True,
        )
        db.session.add(user)
        db.session.commit()
        return role.id, user.id


def test_users_manage_role_can_list_and_create_users(client, app):
    _create_user_manager_role(app)
    login = client.post(
        "/api/auth/login",
        json={"username": "usermgr", "password": "usermgr123"},
    )
    assert login.status_code == 200
    token = login.get_json()["data"]["token"]
    headers = auth_headers(token)

    users = client.get("/api/users", headers=headers)
    assert users.status_code == 200

    roles = client.get("/api/roles", headers=headers)
    assert roles.status_code == 200

    viewer_role = next(r for r in roles.get_json()["data"]["items"] if r["name"] == "viewer")
    create = client.post(
        "/api/users",
        headers=headers,
        json={
            "username": "managed_user",
            "password": "managed123",
            "roleId": viewer_role["id"],
            "fullName": "Managed",
            "email": "managed@test.local",
        },
    )
    assert create.status_code == 201


def test_users_manage_implies_granular_permission_checks(app):
    from api.access_engine import user_has_permission
    from api.models import User

    _create_user_manager_role(app)
    with app.app_context():
        user = User.query.filter_by(username="usermgr").first()
        assert user is not None
        assert user_has_permission(user, "users:view")
        assert user_has_permission(user, "users:create")
        assert user_has_permission(user, "users:update")
        assert user_has_permission(user, "roles:view")
