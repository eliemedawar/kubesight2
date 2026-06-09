from api.access_rules import apply_access_rules
from api.db import db
from api.models import User
from api.access_summary import build_effective_access_summary


def test_effective_access_from_namespace_rules(app):
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
                    "namespace": "default",
                    "resourceType": "namespace",
                    "permissionKey": "namespaces:view",
                    "effect": "allow",
                },
                {
                    "clusterId": "docker-desktop",
                    "namespace": "default",
                    "resourceType": "namespace",
                    "permissionKey": "resources:view",
                    "effect": "allow",
                },
                {
                    "clusterId": "docker-desktop",
                    "namespace": "default",
                    "resourceType": "namespace",
                    "permissionKey": "logs:view",
                    "effect": "allow",
                },
            ],
        )
        db.session.commit()

        summary = build_effective_access_summary(
            viewer,
            cluster_labels={"docker-desktop": "Docker Desktop"},
            focus_cluster_id="docker-desktop",
        )

        assert summary["hasAccessibleScope"] is True
        assert summary["clusters"] == ["Docker Desktop"]
        assert summary["namespaces"] == ["default"]
        assert any(item["id"] == "view_resources" for item in summary["permissions"])
        assert any(item["id"] == "view_logs" for item in summary["permissions"])
        assert "upgrade_precheck" not in {item["id"] for item in summary["permissions"]}


def test_effective_access_full_cluster_legacy(app):
    with app.app_context():
        viewer = User.query.filter_by(username="viewer").first()
        summary = build_effective_access_summary(
            viewer,
            cluster_labels={
                "prod-us-east": "Production US-East",
                "staging-eu-west": "Staging EU-West",
            },
        )

        assert summary["hasAccessibleScope"] is True
        assert any("(all namespaces)" in line for line in summary["clusters"])
        assert any(item["id"] == "view_alerts" for item in summary["permissions"])


def test_effective_access_resource_rules(app):
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
                    "namespace": "default",
                    "resourceType": "namespace",
                    "permissionKey": "namespaces:view",
                    "effect": "allow",
                },
                {
                    "clusterId": "docker-desktop",
                    "namespace": "default",
                    "resourceType": "pod",
                    "resourceName": "api-pod",
                    "permissionKey": "pods:view",
                    "effect": "allow",
                },
                {
                    "clusterId": "docker-desktop",
                    "namespace": "default",
                    "resourceType": "pod",
                    "resourceName": "api-pod",
                    "permissionKey": "logs:view",
                    "effect": "allow",
                },
            ],
        )
        db.session.commit()

        summary = build_effective_access_summary(
            viewer,
            cluster_labels={"docker-desktop": "Docker Desktop"},
        )

        assert "api-pod" in summary["resources"]
        assert summary["namespaces"] == ["default"]
        assert summary["counts"]["pods"] == 1
