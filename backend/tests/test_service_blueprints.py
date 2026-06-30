"""Tests for the Service Catalog (service blueprints + deploy-from-blueprint)."""

from tests.conftest import auth_headers


def _blueprint_payload():
    return {
        "name": "QR Code Service",
        "description": "Generates and resolves QR codes.",
        "category": "Platform",
        "ownerTeam": "Payments",
        "criticality": "high",
        "status": "ready",
        "components": [
            {"tempId": "fe", "name": "Frontend", "role": "frontend", "componentType": "deployment",
             "required": True, "defaultPort": 80, "position": 0},
            {"tempId": "be", "name": "Backend API", "role": "backend", "componentType": "deployment",
             "required": True, "defaultTemplateId": "flask-backend", "defaultPort": 8000, "position": 1},
            {"tempId": "db", "name": "Database", "role": "database", "componentType": "database",
             "required": True, "supportsExternal": True, "position": 2},
            {"tempId": "cache", "name": "Redis", "role": "cache", "componentType": "redis",
             "required": False, "position": 3},
        ],
        "connections": [
            {"sourceTempId": "fe", "targetTempId": "be", "protocol": "HTTP", "port": 8000},
            {"sourceTempId": "be", "targetTempId": "db", "protocol": "TCP", "port": 5432},
        ],
        "requirements": [
            {"key": "INGRESS_HOST", "requirementType": "ingress_host", "required": True,
             "valueSource": "manual", "componentTempId": "fe"},
            {"key": "DB_PASSWORD", "requirementType": "secret", "required": True,
             "valueSource": "generated", "secret": True, "autoGenerate": True, "componentTempId": "db"},
        ],
    }


def _create_blueprint(client, admin_token):
    resp = client.post("/api/service-blueprints", json=_blueprint_payload(), headers=auth_headers(admin_token))
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()["data"]


def test_blueprint_crud(client, admin_token):
    created = _create_blueprint(client, admin_token)
    assert created["name"] == "QR Code Service"
    assert created["componentCount"] == 4
    assert created["dependencyCount"] == 2
    assert created["requirementCount"] == 2
    assert len(created["components"]) == 4

    # Connections resolved temp ids to real component ids.
    component_ids = {c["id"] for c in created["components"]}
    for conn in created["connections"]:
        assert conn["sourceComponentId"] in component_ids
        assert conn["targetComponentId"] in component_ids

    # List + get.
    listed = client.get("/api/service-blueprints", headers=auth_headers(admin_token)).get_json()["data"]
    assert listed["count"] == 1

    bp_id = created["id"]
    fetched = client.get(f"/api/service-blueprints/{bp_id}", headers=auth_headers(admin_token)).get_json()["data"]
    assert fetched["id"] == bp_id

    # Update: rename + drop the optional cache component.
    payload = _blueprint_payload()
    payload["name"] = "QR Service v2"
    payload["components"] = payload["components"][:3]
    payload["requirements"] = payload["requirements"][:1]
    updated = client.put(f"/api/service-blueprints/{bp_id}", json=payload, headers=auth_headers(admin_token))
    assert updated.status_code == 200
    body = updated.get_json()["data"]
    assert body["name"] == "QR Service v2"
    assert body["componentCount"] == 3


def test_blueprint_duplicate_name_rejected(client, admin_token):
    _create_blueprint(client, admin_token)
    dup = client.post("/api/service-blueprints", json=_blueprint_payload(), headers=auth_headers(admin_token))
    assert dup.status_code == 409


def test_deploy_plan_smart_defaults(client, admin_token):
    bp = _create_blueprint(client, admin_token)
    plan_resp = client.post(
        f"/api/service-blueprints/{bp['id']}/deploy-plan",
        json={"environment": "production", "clusterId": "prod-cluster-1"},
        headers=auth_headers(admin_token),
    )
    assert plan_resp.status_code == 200, plan_resp.get_json()
    plan = plan_resp.get_json()["data"]
    assert plan["appServiceName"] == "QR Code Service - Production"
    assert plan["namespaceSuggested"] is True
    names = {c["name"]: c for c in plan["components"]}
    assert names["Backend API"]["generatedName"] == "qr-code-service-backend-prod"
    assert names["Backend API"]["labels"]["kubesight.io/component"] == "backend"
    assert names["Backend API"]["labels"]["kubesight.io/environment"] == "production"
    # Required INGRESS_HOST is the only missing value (DB_PASSWORD auto-generates).
    missing_keys = {m["key"] for m in plan["missingValues"]}
    assert "INGRESS_HOST" in missing_keys
    assert "DB_PASSWORD" not in missing_keys
    # Optional cache component can be skipped; database can be external.
    assert "skip" in names["Redis"]["options"]
    assert "external_dependency" in names["Database"]["options"]


def test_deploy_from_blueprint_creates_app_service(client, admin_token):
    bp = _create_blueprint(client, admin_token)
    deploy_resp = client.post(
        f"/api/service-blueprints/{bp['id']}/deploy",
        json={
            "environment": "production",
            "clusterId": "prod-cluster-1",
            "namespace": "qr-prod",
            "mappings": [
                {"componentId": [c["id"] for c in bp["components"] if c["name"] == "Backend API"][0],
                 "mappingType": "existing_resource", "kind": "Deployment", "name": "qr-code-1"},
                {"componentId": [c["id"] for c in bp["components"] if c["name"] == "Database"][0],
                 "mappingType": "external_dependency", "externalEndpoint": "db.company.com:5432"},
                {"componentId": [c["id"] for c in bp["components"] if c["name"] == "Redis"][0],
                 "mappingType": "skip"},
            ],
        },
        headers=auth_headers(admin_token),
    )
    assert deploy_resp.status_code == 201, deploy_resp.get_json()
    app_service = deploy_resp.get_json()["data"]
    assert app_service["status"] == "active"  # bridged into the App Services tab
    assert app_service["namespace"] == "qr-prod"
    assert app_service["blueprintName"] == "QR Code Service"

    mappings = {m["componentName"]: m for m in app_service["mappings"]}
    assert mappings["Backend API"]["mappingType"] == "existing_resource"
    assert mappings["Backend API"]["name"] == "qr-code-1"
    assert mappings["Database"]["mappingType"] == "external_dependency"
    assert mappings["Database"]["externalEndpoint"] == "db.company.com:5432"
    assert mappings["Redis"]["status"] == "skipped"
    # Frontend was omitted from mappings -> falls back to create_new with a generated name.
    assert mappings["Frontend"]["mappingType"] == "create_new"
    assert mappings["Frontend"]["generatedName"] == "qr-code-service-frontend-prod"

    # Runtime topology: skipped Redis node present but its edges dropped; the
    # Frontend->Backend->Database chain survives.
    edges = app_service["topology"]["edges"]
    assert len(edges) == 2

    # Bridged into the operational App Services tab (ApplicationService) so it is
    # visible there with workloads + topology.
    assert app_service["applicationServiceId"] is not None
    from api.models import ApplicationService
    mirror = ApplicationService.query.get(app_service["applicationServiceId"])
    assert mirror is not None
    assert mirror.name == app_service["name"]
    # Backend (qr-code-1) is a workload -> one deployment row; database is external
    # and redis was skipped, so they are topology-only.
    workload_names = {d.deployment_name for d in mirror.deployments}
    assert "qr-code-1" in workload_names

    # Listing app services + filtering by client/blueprint.
    listed = client.get("/api/app-services", headers=auth_headers(admin_token)).get_json()["data"]
    assert listed["count"] == 1
    by_bp = client.get(
        f"/api/service-blueprints/{bp['id']}/app-services", headers=auth_headers(admin_token)
    ).get_json()["data"]
    assert by_bp["count"] == 1


def test_deploy_links_client_to_app_service(client, admin_token):
    # A blueprint deploy with a client should link that client to the mirrored
    # ApplicationService so it appears on the client's services list.
    create_resp = client.post("/api/clients", json={"name": "Areeba"}, headers=auth_headers(admin_token))
    assert create_resp.status_code == 201
    client_id = create_resp.get_json()["data"]["id"]

    bp = _create_blueprint(client, admin_token)
    deploy_resp = client.post(
        f"/api/service-blueprints/{bp['id']}/deploy",
        json={"clientId": client_id, "environment": "production", "clusterId": "c1", "namespace": "qr-prod"},
        headers=auth_headers(admin_token),
    )
    assert deploy_resp.status_code == 201
    app_service = deploy_resp.get_json()["data"]
    assert app_service["clientId"] == client_id

    from api.models import AppService, ClientApplicationService
    mirror_id = AppService.query.get(app_service["id"]).application_service_id
    assert mirror_id is not None
    link = ClientApplicationService.query.filter_by(client_id=client_id, service_id=mirror_id).first()
    assert link is not None


def _deploy(client, token, bp, **target):
    body = {"environment": "production", "clusterId": "c1", "namespace": "qr-prod", **target}
    resp = client.post(f"/api/service-blueprints/{bp['id']}/deploy", json=body, headers=auth_headers(token))
    assert resp.status_code == 201, resp.get_json()
    return resp.get_json()["data"]


def test_deleting_app_service_mirror_prunes_blueprint_instance(client, admin_token):
    bp = _create_blueprint(client, admin_token)
    app_service = _deploy(client, admin_token, bp)
    mirror_id = app_service["applicationServiceId"]
    assert mirror_id is not None

    listed = client.get("/api/service-blueprints", headers=auth_headers(admin_token)).get_json()["data"]
    card = next(b for b in listed["items"] if b["id"] == bp["id"])
    assert card["appServiceCount"] == 1

    # Deleting the App Services tab entry must also remove the blueprint instance.
    del_resp = client.delete(f"/api/application-services/{mirror_id}", headers=auth_headers(admin_token))
    assert del_resp.status_code == 200

    listed2 = client.get("/api/service-blueprints", headers=auth_headers(admin_token)).get_json()["data"]
    card2 = next(b for b in listed2["items"] if b["id"] == bp["id"])
    assert card2["appServiceCount"] == 0
    assert client.get("/api/app-services", headers=auth_headers(admin_token)).get_json()["data"]["count"] == 0


def test_deleting_blueprint_instance_removes_mirror(client, admin_token):
    bp = _create_blueprint(client, admin_token)
    app_service = _deploy(client, admin_token, bp)
    mirror_id = app_service["applicationServiceId"]

    del_resp = client.delete(f"/api/app-services/{app_service['id']}", headers=auth_headers(admin_token))
    assert del_resp.status_code == 200

    from api.models import ApplicationService
    assert ApplicationService.query.get(mirror_id) is None


def test_blueprint_deletable_after_orphaned_instance(client, admin_token):
    # Reproduces the "stuck deployment" case: an App Service mirror was deleted
    # out-of-band (pre-cascade), leaving an orphaned blueprint instance that
    # blocked blueprint deletion. delete_blueprint must prune it and succeed.
    bp = _create_blueprint(client, admin_token)
    app_service = _deploy(client, admin_token, bp)
    mirror_id = app_service["applicationServiceId"]

    from api.db import db
    from api.models import ApplicationService
    db.session.delete(ApplicationService.query.get(mirror_id))
    db.session.commit()

    resp = client.delete(f"/api/service-blueprints/{bp['id']}", headers=auth_headers(admin_token))
    assert resp.status_code == 200


def test_required_component_cannot_be_skipped(client, admin_token):
    bp = _create_blueprint(client, admin_token)
    backend_id = [c["id"] for c in bp["components"] if c["name"] == "Backend API"][0]
    deploy_resp = client.post(
        f"/api/service-blueprints/{bp['id']}/deploy",
        json={
            "environment": "dev",
            "clusterId": "c1",
            "mappings": [{"componentId": backend_id, "mappingType": "skip"}],
        },
        headers=auth_headers(admin_token),
    )
    assert deploy_resp.status_code == 201
    mappings = {m["componentName"]: m for m in deploy_resp.get_json()["data"]["mappings"]}
    assert mappings["Backend API"]["mappingType"] == "create_new"


def test_viewer_cannot_create_blueprint(client, viewer_token):
    resp = client.post("/api/service-blueprints", json=_blueprint_payload(), headers=auth_headers(viewer_token))
    assert resp.status_code == 403


def test_viewer_can_list_blueprints(client, viewer_token):
    resp = client.get("/api/service-blueprints", headers=auth_headers(viewer_token))
    assert resp.status_code == 200


def test_pickers_degrade_off_cluster(client, admin_token):
    # Off real-k8s mode the pickers return an empty list with live=False (HTTP 200)
    # so the wizard can fall back to generated names / manual entry.
    ns = client.get(
        "/api/service-blueprints/pickers/namespaces?clusterId=c1",
        headers=auth_headers(admin_token),
    )
    assert ns.status_code == 200
    body = ns.get_json()["data"]
    assert body["items"] == []
    assert body["live"] is False

    res = client.get(
        "/api/service-blueprints/pickers/resources?clusterId=c1&namespace=ns&kind=deployments",
        headers=auth_headers(admin_token),
    )
    assert res.status_code == 200
    assert res.get_json()["data"]["live"] is False


def test_picker_rejects_unknown_kind(client, admin_token):
    res = client.get(
        "/api/service-blueprints/pickers/resources?clusterId=c1&namespace=ns&kind=bogus",
        headers=auth_headers(admin_token),
    )
    assert res.status_code == 400


def test_viewer_cannot_use_pickers(client, viewer_token):
    # Pickers require service_blueprints:deploy, which viewers lack.
    res = client.get(
        "/api/service-blueprints/pickers/namespaces?clusterId=c1",
        headers=auth_headers(viewer_token),
    )
    assert res.status_code == 403
