"""Demo users should gain access to discovered clusters when only mock IDs are seeded."""

from unittest.mock import patch

from api.access_rules import apply_access_rules
from api.models import AccessRule, User
from api.rbac_data import VIEWER_PERMISSIONS
from api.seed import MOCK_CLUSTER_IDS, _sync_demo_users_to_discovered_clusters


def _mock_only_rules(cluster_ids):
    rules = []
    for cluster_id in cluster_ids:
        rules.append(
            {
                "clusterId": cluster_id,
                "resourceType": "cluster",
                "permissionKey": "clusters:view",
                "effect": "allow",
            }
        )
    return rules


def test_sync_replaces_mock_only_demo_user_access(app):
    with app.app_context():
        viewer = User.query.filter_by(username="viewer").first()
        apply_access_rules(viewer, _mock_only_rules(MOCK_CLUSTER_IDS))
        from api.db import db

        db.session.commit()

        with patch("api.k8s_provider.should_use_real_k8s", return_value=True), patch(
            "api.k8s_provider.list_clusters_from_k8s",
            return_value={"items": [{"id": "docker-desktop", "name": "Docker Desktop"}]},
        ):
            _sync_demo_users_to_discovered_clusters()
            db.session.commit()

        cluster_ids = {
            rule.cluster_id for rule in AccessRule.query.filter_by(user_id=viewer.id).all()
        }
        assert cluster_ids == {"docker-desktop"}
        assert AccessRule.query.filter_by(user_id=viewer.id).count() == len(VIEWER_PERMISSIONS)


def test_sync_skips_when_user_already_has_discovered_cluster(app):
    with app.app_context():
        viewer = User.query.filter_by(username="viewer").first()
        apply_access_rules(
            viewer,
            _full_cluster_rules := [
                {
                    "clusterId": "docker-desktop",
                    "resourceType": "cluster",
                    "permissionKey": "clusters:view",
                    "effect": "allow",
                }
            ],
        )
        from api.db import db

        db.session.commit()
        before = AccessRule.query.filter_by(user_id=viewer.id).count()

        with patch("api.k8s_provider.should_use_real_k8s", return_value=True), patch(
            "api.k8s_provider.list_clusters_from_k8s",
            return_value={"items": [{"id": "docker-desktop"}]},
        ):
            _sync_demo_users_to_discovered_clusters()
            db.session.commit()

        after = AccessRule.query.filter_by(user_id=viewer.id).count()
        assert after == before


def test_sync_skips_customized_non_mock_access(app):
    with app.app_context():
        viewer = User.query.filter_by(username="viewer").first()
        apply_access_rules(
            viewer,
            [
                {
                    "clusterId": "custom-cluster",
                    "resourceType": "cluster",
                    "permissionKey": "clusters:view",
                    "effect": "allow",
                }
            ],
        )
        from api.db import db

        db.session.commit()

        with patch("api.k8s_provider.should_use_real_k8s", return_value=True), patch(
            "api.k8s_provider.list_clusters_from_k8s",
            return_value={"items": [{"id": "docker-desktop"}]},
        ):
            _sync_demo_users_to_discovered_clusters()
            db.session.commit()

        cluster_ids = {
            rule.cluster_id for rule in AccessRule.query.filter_by(user_id=viewer.id).all()
        }
        assert cluster_ids == {"custom-cluster"}
