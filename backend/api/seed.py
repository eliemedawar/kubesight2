from __future__ import annotations

from .db import db
from .mock_data import SETTINGS
from .models import AppSettings, Permission, Role, User, UserClusterAccess
from .passwords import hash_password
from .rbac_data import DEFAULT_USERS, PERMISSIONS, ROLE_DEFINITIONS


def _seed_permissions() -> dict:
    by_key = {}
    for key, description in PERMISSIONS:
        perm = Permission.query.filter_by(key=key).first()
        if not perm:
            perm = Permission(key=key, description=description)
            db.session.add(perm)
        else:
            perm.description = description
        by_key[key] = perm
    db.session.flush()
    return by_key


def _seed_roles(permissions_by_key: dict) -> dict:
    roles_by_name = {}
    for name, definition in ROLE_DEFINITIONS.items():
        role = Role.query.filter_by(name=name).first()
        default_permissions = [
            permissions_by_key[key]
            for key in definition["permissions"]
            if key in permissions_by_key
        ]
        if not role:
            role = Role(
                name=name,
                description=definition["description"],
                is_system_role=definition["is_system_role"],
            )
            db.session.add(role)
            role.permissions = default_permissions
        elif not role.permissions:
            role.permissions = default_permissions
        roles_by_name[name] = role
    db.session.flush()
    return roles_by_name


def _seed_users(roles_by_name: dict) -> None:
    for spec in DEFAULT_USERS:
        user = User.query.filter_by(username=spec["username"]).first()
        role = roles_by_name.get(spec["role"])
        if not user:
            user = User(
                username=spec["username"],
                email=spec["email"],
                password_hash=hash_password(spec["password"]),
                full_name=spec["full_name"],
                role=role,
                is_active=True,
            )
            db.session.add(user)
            continue
        # Existing users are left as-is so UI edits survive restarts.
        if role and user.role_id is None:
            user.role = role
        if not user.password_hash:
            user.password_hash = hash_password(spec["password"])


def _seed_role_cluster_access(username: str, cluster_ids: tuple[str, ...]) -> None:
    user = User.query.filter_by(username=username).first()
    if not user or user.role is None or user.role.name != username:
        return
    if UserClusterAccess.query.filter_by(user_id=user.id).count():
        return
    for cluster_id in cluster_ids:
        db.session.add(UserClusterAccess(user_id=user.id, cluster_id=cluster_id, can_view=True))


def seed_defaults() -> None:
    permissions_by_key = _seed_permissions()
    roles_by_name = _seed_roles(permissions_by_key)
    _seed_users(roles_by_name)
    cluster_ids = ("prod-us-east", "staging-eu-west")
    _seed_role_cluster_access("viewer", cluster_ids)
    _seed_role_cluster_access("operator", cluster_ids)

    existing_settings = AppSettings.query.first()
    if not existing_settings:
        db.session.add(
            AppSettings(
                theme=SETTINGS.get("theme", "system"),
                refresh_interval_seconds=SETTINGS.get("refreshIntervalSeconds", 30),
                default_cluster=SETTINGS.get("defaultCluster", "prod-us-east"),
                notifications=SETTINGS.get("notifications", {"alerts": True, "upgrades": True}),
            )
        )

    db.session.commit()
