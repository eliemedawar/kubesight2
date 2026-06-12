"""Namespace-scoped grants must include workload view permissions."""

from api.access_engine import can_access_resource, filter_namespace_resources
from api.access_rules import apply_access_rules
from api.models import User
from api.seed import _repair_incomplete_namespace_resource_grants


def _namespace_only_rules(cluster_id: str, namespace: str, permission_keys: list[str]) -> list[dict]:
    return [
        {
            "clusterId": cluster_id,
            "namespace": namespace,
            "resourceType": "namespace",
            "permissionKey": key,
            "effect": "allow",
        }
        for key in permission_keys
    ]


def test_namespace_view_resources_rules_allow_workloads(app):
    with app.app_context():
        viewer = User.query.filter_by(username="viewer").first()
        apply_access_rules(
            viewer,
            _namespace_only_rules(
                "docker-desktop",
                "kubesight",
                [
                    "clusters:view",
                    "namespaces:view",
                    "resources:view",
                    "pods:view",
                    "deployments:view",
                    "services:view",
                    "logs:view",
                ],
            ),
        )
        from api.db import db

        db.session.commit()

        assert can_access_resource(viewer, "docker-desktop", "kubesight", "pod", "api-1")
        assert can_access_resource(viewer, "docker-desktop", "kubesight", "deployment", "api")
        assert can_access_resource(viewer, "docker-desktop", "kubesight", "service", "api")

        resources = {
            "namespace": "kubesight",
            "pods": [{"name": "api-1"}],
            "deployments": [{"name": "api"}],
            "replicasets": [],
            "statefulsets": [],
            "daemonsets": [],
            "jobs": [],
            "cronjobs": [],
            "services": [{"name": "api"}],
        }
        filtered = filter_namespace_resources(viewer, "docker-desktop", resources)
        assert len(filtered["pods"]) == 1
        assert len(filtered["deployments"]) == 1
        assert len(filtered["services"]) == 1


def test_cluster_level_resources_view_grants_workload_access(app):
    with app.app_context():
        viewer = User.query.filter_by(username="viewer").first()
        apply_access_rules(
            viewer,
            [
                {
                    "clusterId": "docker-desktop",
                    "resourceType": "cluster",
                    "permissionKey": key,
                    "effect": "allow",
                }
                for key in (
                    "clusters:view",
                    "resources:view",
                    "logs:view",
                    "overview:view",
                    "services:ports:view",
                    "services:view",
                )
            ],
        )
        from api.access_engine import can_access_namespace
        from api.db import db

        db.session.commit()

        assert can_access_namespace(viewer, "docker-desktop", "kubesight")
        assert can_access_resource(viewer, "docker-desktop", "kubesight", "pod", "api-1")
        assert can_access_resource(viewer, "docker-desktop", "kubesight", "deployment", "api")


def test_named_pod_grant_requires_pods_view_rule(app):
    with app.app_context():
        viewer = User.query.filter_by(username="viewer").first()
        pod_name = "api-pod-1"
        apply_access_rules(
            viewer,
            [
                {
                    "clusterId": "docker-desktop",
                    "resourceType": "cluster",
                    "permissionKey": "clusters:view",
                    "effect": "allow",
                },
                {
                    "clusterId": "docker-desktop",
                    "namespace": "kubesight",
                    "resourceType": "namespace",
                    "permissionKey": "namespaces:view",
                    "effect": "allow",
                },
                {
                    "clusterId": "docker-desktop",
                    "namespace": "kubesight",
                    "resourceType": "pod",
                    "resourceName": pod_name,
                    "permissionKey": "logs:view",
                    "effect": "allow",
                },
            ],
        )
        from api.db import db

        db.session.commit()

        assert not can_access_resource(viewer, "docker-desktop", "kubesight", "pod", pod_name)
        assert not can_access_resource(viewer, "docker-desktop", "kubesight", "pod", "other-pod")

        from api.seed import _repair_named_resource_view_permissions

        _repair_named_resource_view_permissions()
        db.session.commit()

        assert can_access_resource(viewer, "docker-desktop", "kubesight", "pod", pod_name)
        assert not can_access_resource(viewer, "docker-desktop", "kubesight", "pod", "other-pod")


def test_deployment_grant_allows_current_pod_instances(app):
    with app.app_context():
        viewer = User.query.filter_by(username="viewer").first()
        apply_access_rules(
            viewer,
            [
                {
                    "clusterId": "docker-desktop",
                    "resourceType": "cluster",
                    "permissionKey": "clusters:view",
                    "effect": "allow",
                },
                {
                    "clusterId": "docker-desktop",
                    "namespace": "kubesight",
                    "resourceType": "namespace",
                    "permissionKey": "namespaces:view",
                    "effect": "allow",
                },
                {
                    "clusterId": "docker-desktop",
                    "namespace": "kubesight",
                    "resourceType": "deployment",
                    "resourceName": "kubesight-backend",
                    "permissionKey": "deployments:view",
                    "effect": "allow",
                },
            ],
        )
        from api.db import db

        db.session.commit()

        assert can_access_resource(
            viewer, "docker-desktop", "kubesight", "pod", "kubesight-backend-68474796d5-zmlq2"
        )
        assert not can_access_resource(
            viewer, "docker-desktop", "kubesight", "pod", "postgres-94fc44b96-xxnc6"
        )


def test_repair_adds_missing_namespace_resource_permissions(app):
    with app.app_context():
        viewer = User.query.filter_by(username="viewer").first()
        apply_access_rules(
            viewer,
            _namespace_only_rules(
                "docker-desktop",
                "kubesight",
                [
                    "clusters:view",
                    "namespaces:view",
                    "logs:view",
                    "overview:view",
                    "services:ports:view",
                ],
            ),
        )
        from api.db import db

        db.session.commit()

        assert not can_access_resource(viewer, "docker-desktop", "kubesight", "pod", "api-1")

        _repair_incomplete_namespace_resource_grants()
        db.session.commit()

        assert can_access_resource(viewer, "docker-desktop", "kubesight", "pod", "api-1")
        assert can_access_resource(viewer, "docker-desktop", "kubesight", "deployment", "api")
