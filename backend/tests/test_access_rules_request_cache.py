"""Request-level AccessRule cache: one DB load per user per HTTP request."""

from __future__ import annotations

from sqlalchemy import event

import api.access_engine as access_engine
from api.access_engine import (
    _load_rules,
    filter_alerts_for_user,
    filter_namespaces_for_user,
    invalidate_access_rules_cache,
)
from api.access_rules import apply_access_rules
from api.db import db
from api.models import User

CLUSTER_ID = "prod-us-east"
NAMESPACE_COUNT = 25
ALERT_COUNT = 25


def _seed_viewer_namespace_rules(viewer: User) -> None:
    rules = [
        {
            "clusterId": CLUSTER_ID,
            "resourceType": "cluster",
            "permissionKey": "clusters:view",
            "effect": "allow",
        },
        {
            "clusterId": CLUSTER_ID,
            "resourceType": "cluster",
            "permissionKey": "alerts:view",
            "effect": "allow",
        },
    ]
    for index in range(NAMESPACE_COUNT):
        ns = f"ns-{index}"
        rules.append(
            {
                "clusterId": CLUSTER_ID,
                "namespace": ns,
                "resourceType": "namespace",
                "permissionKey": "namespaces:view",
                "effect": "allow",
            }
        )
        rules.append(
            {
                "clusterId": CLUSTER_ID,
                "namespace": ns,
                "resourceType": "namespace",
                "permissionKey": "alerts:view",
                "effect": "allow",
            }
        )
    apply_access_rules(viewer, rules)
    db.session.commit()


def _sample_namespaces() -> list[dict]:
    return [{"name": f"ns-{index}"} for index in range(NAMESPACE_COUNT)]


def _sample_alerts() -> list[dict]:
    return [
        {
            "clusterId": CLUSTER_ID,
            "namespace": f"ns-{index}",
            "severity": "warning",
        }
        for index in range(ALERT_COUNT)
    ]


def _count_access_rules_sql(engine, callback) -> int:
    """Count explicit SELECTs against access_rules (not User JOIN eager loads)."""
    statements: list[str] = []

    def before_cursor_execute(conn, cursor, statement, parameters, context, executemany):
        normalized = statement.lower().replace("\n", " ")
        if "select" in normalized and "from access_rules" in normalized:
            statements.append(statement)

    event.listen(engine, "before_cursor_execute", before_cursor_execute)
    try:
        callback()
    finally:
        event.remove(engine, "before_cursor_execute", before_cursor_execute)
    return len(statements)


def test_access_rules_loaded_once_per_request(app):
    from api.access_engine import is_admin

    namespaces = _sample_namespaces()
    alerts = _sample_alerts()

    with app.app_context():
        viewer = User.query.filter_by(username="viewer").first()
        assert viewer is not None
        assert is_admin(viewer) is False
        _seed_viewer_namespace_rules(viewer)

        load_calls = 0
        real_load_rules = access_engine._load_rules

        def counting_load_rules(user: User):
            nonlocal load_calls
            load_calls += 1
            return real_load_rules(user)

        with app.test_request_context("/"):
            access_engine._load_rules = counting_load_rules
            try:

                def run_filters():
                    filter_namespaces_for_user(viewer, CLUSTER_ID, namespaces)
                    filter_alerts_for_user(viewer, alerts)

                query_count = _count_access_rules_sql(db.engine, run_filters)
            finally:
                access_engine._load_rules = real_load_rules

    # _load_rules() is invoked per permission check; request cache reuses the first load.
    assert load_calls >= NAMESPACE_COUNT
    assert query_count == 0


def test_uncached_load_rules_baseline_query_count(app):
    """Documents pre-cache behavior: every permission check hits the database."""

    namespaces = _sample_namespaces()
    alerts = _sample_alerts()

    def uncached_load_rules(user: User):
        from api.models import AccessRule

        return AccessRule.query.filter_by(user_id=user.id).all()

    with app.app_context():
        viewer = User.query.filter_by(username="viewer").first()
        assert viewer is not None
        _seed_viewer_namespace_rules(viewer)

        with app.test_request_context("/"):
            original_load_rules = access_engine._load_rules
            access_engine._load_rules = uncached_load_rules
            try:

                def run_filters():
                    filter_namespaces_for_user(viewer, CLUSTER_ID, namespaces)
                    filter_alerts_for_user(viewer, alerts)

                query_count = _count_access_rules_sql(db.engine, run_filters)
            finally:
                access_engine._load_rules = original_load_rules

    # Namespace filter: one rules load to build the allowed namespace set.
    # Alert filter: 2 SELECTs per alert (can_access_cluster + namespace-scoped evaluate_access).
    expected_minimum = 2 + (ALERT_COUNT * 2)
    assert query_count >= expected_minimum


def test_access_rules_cache_invalidated_after_mutation(app):
    with app.app_context():
        viewer = User.query.filter_by(username="viewer").first()
        assert viewer is not None
        _seed_viewer_namespace_rules(viewer)

        with app.test_request_context("/"):
            _load_rules(viewer)
            db.session.expire(viewer, ["access_rules"])
            invalidate_access_rules_cache(viewer.id)

            def reload_once():
                _load_rules(viewer)

            query_count = _count_access_rules_sql(db.engine, reload_once)
            assert query_count == 1
