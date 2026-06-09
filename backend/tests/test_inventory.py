from urllib.parse import quote

from tests.conftest import auth_headers


def test_admin_inventory_list(client, admin_token):
    response = client.get("/api/inventory", headers=auth_headers(admin_token))
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    items = payload["data"]
    assert isinstance(items, list)
    assert len(items) >= 1
    row = items[0]
    assert "id" in row
    assert "name" in row
    assert "cluster" in row
    assert "namespace" in row
    assert "status" in row
    assert "workloadType" in row


def test_inventory_search_filter(client, admin_token):
    response = client.get(
        "/api/inventory?search=payments",
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 200
    items = response.get_json()["data"]
    assert all("payments" in (item.get("name") or "").lower() or item.get("namespace") == "payments" for item in items)


def test_inventory_detail(client, admin_token):
    listing = client.get("/api/inventory", headers=auth_headers(admin_token)).get_json()["data"]
    assert listing
    inv_id = listing[0]["id"]
    response = client.get(f"/api/inventory/{inv_id}", headers=auth_headers(admin_token))
    assert response.status_code == 200
    detail = response.get_json()["data"]
    assert detail["summary"]["applicationName"]
    assert "pods" in detail
    assert "services" in detail
    assert "secrets" in detail
    for secret in detail.get("secrets") or []:
        assert "value" not in secret
        assert "data" not in secret


def test_dashboard_includes_inventory_summary(client, admin_token):
    clusters = client.get("/api/clusters", headers=auth_headers(admin_token)).get_json()
    cluster_id = clusters["data"]["items"][0]["id"]
    response = client.get(
        f"/api/dashboard/summary?clusterId={cluster_id}",
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 200
    inventory = response.get_json()["data"].get("inventory")
    assert inventory is not None
    assert "applications" in inventory
    assert "healthy" in inventory


def test_viewer_inventory_list(client, viewer_token):
    response = client.get("/api/inventory", headers=auth_headers(viewer_token))
    assert response.status_code == 200
    items = response.get_json()["data"]
    assert isinstance(items, list)


def test_inventory_status_filter(client, admin_token):
    response = client.get(
        "/api/inventory?status=Healthy",
        headers=auth_headers(admin_token),
    )
    assert response.status_code == 200
    for item in response.get_json()["data"]:
        assert item.get("status") == "Healthy"


def test_inventory_id_roundtrip():
    from api.services.inventory_service import make_inventory_id, parse_inventory_id

    inv_id = make_inventory_id("prod-us-east", "payments", "payments-api")
    parsed = parse_inventory_id(inv_id)
    assert parsed == ("prod-us-east", "payments", "payments-api")


def test_inventory_ids_equal_accepts_encoded_and_decoded():
    from api.services.inventory_service import _inventory_ids_equal, make_inventory_id

    stored = make_inventory_id("docker-desktop", "default", "frontend-app")
    assert _inventory_ids_equal(stored, "docker-desktop|default|frontend-app")
    assert not _inventory_ids_equal(stored, make_inventory_id("docker-desktop", "default", "backend-api"))


def test_filter_resources_for_app_scopes_pods():
    from api.services.inventory_service import _filter_resources_for_app

    resources = {
        "namespace": "default",
        "pods": [
            {"name": "backend-api-6bd6dc586d-9cdt7"},
            {"name": "frontend-app-5fdcd8b575-6xjbg"},
            {"name": "nginx-demo-5b7dc86b48-95927"},
        ],
        "deployments": [
            {"name": "backend-api"},
            {"name": "frontend-app"},
        ],
        "services": [
            {"name": "backend-api"},
            {"name": "frontend-app"},
        ],
    }
    filtered = _filter_resources_for_app(resources, "backend-api", ["backend-api"])
    assert [p["name"] for p in filtered["pods"]] == ["backend-api-6bd6dc586d-9cdt7"]
    assert [d["name"] for d in filtered["deployments"]] == ["backend-api"]
    assert [s["name"] for s in filtered["services"]] == ["backend-api"]


def test_inventory_usage_formatting():
    from api.services.inventory_service import (
        _format_inventory_cpu_usage,
        _format_inventory_memory_usage,
        _usage_fields_for_app,
    )

    assert _format_inventory_cpu_usage(0) == "-"
    assert _format_inventory_cpu_usage(0.045) == "45m"
    assert _format_inventory_cpu_usage(1.25) == "1.250 cores"
    assert _format_inventory_memory_usage(512) == "512 MiB"
    assert _format_inventory_memory_usage(2048) == "2.00 GiB"

    index = {("default", "backend-api"): {"cpu": 0.12, "memory_mib": 256.0}}
    cpu, mem, _, _ = _usage_fields_for_app("default", "backend-api", True, index)
    assert cpu == "120m"
    assert mem == "256 MiB"

    cpu, mem, _, _ = _usage_fields_for_app("default", "missing", True, index)
    assert cpu == "-"
    assert mem == "-"

    cpu, mem, _, _ = _usage_fields_for_app("default", "backend-api", False, index)
    assert cpu == "Metrics unavailable"
    assert mem == "Metrics unavailable"
