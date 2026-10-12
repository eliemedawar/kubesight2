"""Per-request fixed cost: the role's permissions come from a short process cache.

An authenticated request used to cost five statements before doing any work:
the user, three eager access collections and the role joined to every
permission. Now it is the user alone (plus the rules, for a non-admin that
needs them); the role cache is dropped by any ORM write to a role.
"""

from __future__ import annotations

from sqlalchemy import event

from api import access_engine
from api.db import db
from api.models import Permission, Role, User


def _statements(client, token, path):
    seen = []

    def hook(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(db.engine, "before_cursor_execute", hook)
    try:
        response = client.get(path, headers={"Authorization": f"Bearer {token}"})
    finally:
        event.remove(db.engine, "before_cursor_execute", hook)
    return response, seen


def test_an_admin_request_loads_the_user_and_nothing_else_for_auth(client, admin_token):
    _statements(client, admin_token, "/api/clusters")
    db.session.expunge_all()
    response, seen = _statements(client, admin_token, "/api/clusters")
    assert response.status_code == 200
    auth = [s for s in seen if "FROM users" in s or "FROM roles" in s or "access" in s]
    assert len(auth) == 1, auth
    assert "FROM users" in auth[0]


def test_role_edits_reach_the_next_request(app, client, admin_token, viewer_token):
    # Warm the cache with the viewer's current role.
    assert client.get("/api/clusters", headers={"Authorization": f"Bearer {viewer_token}"}).status_code == 200
    viewer_role = Role.query.filter_by(name="viewer").first()
    assert any(p.key == "clusters:view" for p in viewer_role.permissions)

    keys = [p.key for p in viewer_role.permissions if p.key != "clusters:view"]
    response = client.put(
        f"/api/roles/{viewer_role.id}/permissions",
        json={"permissions": keys, "permissionKeys": keys},
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert response.status_code == 200, response.get_json()
    db.session.expunge_all()

    viewer = User.query.filter_by(username="viewer").first()
    assert "clusters:view" not in access_engine.get_user_permission_keys(viewer)


def test_orm_write_to_a_role_drops_the_cache(app):
    with app.app_context():
        viewer_id = User.query.filter_by(username="viewer").first().id
        db.session.expunge_all()
        viewer = db.session.get(User, viewer_id)
        before = access_engine.get_user_permission_keys(viewer)
        assert access_engine._role_cache

        role = db.session.get(Role, viewer.role_id)
        extra = Permission.query.filter(~Permission.key.in_(before)).first()
        extra_key = extra.key
        role.permissions.append(extra)
        db.session.commit()
        assert not access_engine._role_cache

        db.session.expunge_all()
        viewer = db.session.get(User, viewer_id)
        assert extra_key in access_engine.get_user_permission_keys(viewer)


def test_a_loaded_role_is_read_in_memory(app):
    with app.app_context():
        viewer = User.query.filter_by(username="viewer").first()
        _ = viewer.role.permissions  # loaded: unflushed edits must count
        viewer.role.permissions = []
        assert access_engine.get_user_permission_keys(viewer) == set()
        db.session.rollback()


def test_cache_can_be_switched_off(app, monkeypatch):
    monkeypatch.setenv("ROLE_PERMISSION_CACHE_SECONDS", "0")
    with app.app_context():
        access_engine.invalidate_role_permission_cache()
        viewer_id = User.query.filter_by(username="viewer").first().id
        db.session.expunge_all()
        viewer = db.session.get(User, viewer_id)
        assert access_engine.get_user_permission_keys(viewer)
        assert not access_engine._role_cache
