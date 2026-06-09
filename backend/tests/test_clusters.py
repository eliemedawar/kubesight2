from unittest.mock import patch

from tests.conftest import auth_headers


def test_list_clusters_mock_mode(client, admin_token):
    response = client.get("/api/clusters", headers=auth_headers(admin_token))
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert len(payload["data"]["items"]) > 0


def test_custom_cluster_crud(client, admin_token):
    kubeconfig = """
apiVersion: v1
kind: Config
clusters:
  - name: test
    cluster:
      server: https://127.0.0.1:6443
contexts:
  - name: test
    context:
      cluster: test
      user: test
current-context: test
users:
  - name: test
    user: {}
"""
    main_before = client.get("/api/clusters", headers=auth_headers(admin_token))
    assert main_before.status_code == 200
    ids_before = {item["id"] for item in main_before.get_json()["data"]["items"]}

    create = client.post(
        "/api/clusters/custom",
        headers=auth_headers(admin_token),
        json={
            "name": "Test Cluster",
            "connectionMethod": "kubeconfig",
            "kubeconfigContent": kubeconfig,
        },
    )
    assert create.status_code in (200, 201), create.get_json()
    cluster_id = create.get_json()["data"]["cluster"]["publicId"]

    listing = client.get("/api/clusters/custom", headers=auth_headers(admin_token))
    assert listing.status_code == 200
    public_ids = [item["publicId"] for item in listing.get_json()["data"]["items"]]
    assert cluster_id in public_ids

    with patch("api.cluster_store.test_cluster_connection") as mock_test:
        mock_test.return_value = {
            "status": "connected",
            "message": "Connection test passed (mocked)",
            "kubernetesVersion": "v1.29.0",
        }
        test = client.post(
            f"/api/clusters/custom/{cluster_id}/test",
            headers=auth_headers(admin_token),
        )
        assert test.status_code == 200

    main_with_custom = client.get("/api/clusters", headers=auth_headers(admin_token))
    assert main_with_custom.status_code == 200
    assert cluster_id in {item["id"] for item in main_with_custom.get_json()["data"]["items"]}

    delete = client.delete(
        f"/api/clusters/custom/{cluster_id}",
        headers=auth_headers(admin_token),
    )
    assert delete.status_code == 200

    main_after = client.get("/api/clusters", headers=auth_headers(admin_token))
    assert main_after.status_code == 200
    ids_after = {item["id"] for item in main_after.get_json()["data"]["items"]}
    assert cluster_id not in ids_after
    assert ids_after == ids_before


def test_cluster_list_cache_invalidated_on_delete(app):
    from api.k8s_provider import invalidate_cluster_list_cache, list_clusters_from_k8s

    with patch("api.k8s_provider._cluster_list_cache_disabled", return_value=False):
        with patch("api.k8s_provider._discovered_clusters_from_k8s", return_value=[]):
            with patch("api.k8s_provider._custom_clusters_as_items") as mock_custom:
                mock_custom.return_value = [{"id": "custom-1", "name": "Cached Cluster"}]
                cached = list_clusters_from_k8s()
                assert cached["count"] == 1

                mock_custom.return_value = []
                still_cached = list_clusters_from_k8s()
                assert still_cached["count"] == 1

                invalidate_cluster_list_cache()
                refreshed = list_clusters_from_k8s()
                assert refreshed["count"] == 0

    invalidate_cluster_list_cache()


def test_manual_cluster_create_generates_kubeconfig(client, admin_token):
    with __import__("unittest.mock").mock.patch(
        "api.cluster_store.test_cluster_connection"
    ) as mock_test:
        mock_test.return_value = {
            "success": True,
            "reachable": True,
            "serverVersion": "v1.29.0",
            "nodes": [],
            "latencyMs": 12,
        }
        response = client.post(
            "/api/clusters/custom",
            headers=auth_headers(admin_token),
            json={
                "name": "Manual Cluster",
                "connectionMethod": "manual",
                "authenticationType": "token",
                "host": "api.example.com",
                "port": 6443,
                "protocol": "https",
                "bearerToken": "test-token-value",
            },
        )
    assert response.status_code in (200, 201), response.get_json()
    data = response.get_json()["data"]["cluster"]
    assert data["connectionMethod"] == "manual"
    assert data["authenticationType"] == "token"
    assert "test-token" not in str(response.get_json())


def test_kubeconfig_builder_manual_token():
    from api.kubeconfig_builder import generate_manual_kubeconfig

    content, context = generate_manual_kubeconfig(
        name="demo",
        host="kubernetes.example.com",
        port=6443,
        protocol="https",
        authentication_type="token",
        bearer_token="secret-token",
    )
    assert "secret-token" in content
    assert context
    assert "kubernetes.example.com:6443" in content


def test_kubeconfig_builder_extract_server():
    from api.kubeconfig_builder import extract_server_target
    import yaml

    document = yaml.safe_load(
        """
apiVersion: v1
kind: Config
clusters:
  - name: c1
    cluster:
      server: https://k8s.local:6443
contexts:
  - name: ctx1
    context:
      cluster: c1
      user: u1
current-context: ctx1
users:
  - name: u1
    user: {}
"""
    )
    host, port, protocol = extract_server_target(document, "ctx1")
    assert host == "k8s.local"
    assert port == 6443
    assert protocol == "https"
