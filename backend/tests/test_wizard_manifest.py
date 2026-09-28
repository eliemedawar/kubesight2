"""Tests for Application Builder wizard manifest generation."""

from api.services.wizard_manifest_generator import generate_wizard_manifests, validate_k8s_name


def test_validate_k8s_name():
    assert validate_k8s_name("my-app") is None
    assert validate_k8s_name("My_App") is not None


def test_generate_pvc_with_manual_pv():
    payload = {
        "basics": {"appName": "data-store", "namespace": "default"},
        "workloadType": "Deployment",
        "containers": [{"name": "app", "image": "nginx", "tag": "latest", "ports": [80]}],
        "resources": {"cpuRequest": "100m", "cpuLimit": "500m", "memoryRequest": "128Mi", "memoryLimit": "256Mi"},
        "storage": {
            "pvcMode": "new",
            "newPvc": {
                "name": "data-pvc",
                "size": "5Gi",
                "storageClass": "",
                "accessMode": "ReadWriteOnce",
            },
            "advanced": {
                "createManualPv": True,
                "pvName": "data-pv",
                "capacity": "5Gi",
                "storageType": "hostPath",
                "reclaimPolicy": "Retain",
                "hostPath": "/mnt/data",
            },
        },
        "scaling": {"replicas": 1},
    }
    yaml_text, summary, error = generate_wizard_manifests(payload)
    assert error is None
    assert "kind: PersistentVolume" in yaml_text
    assert "kind: PersistentVolumeClaim" in yaml_text
    assert "hostPath:" in yaml_text
    assert "volumeName: data-pv" in yaml_text
    assert yaml_text.count("storageClassName: ''") == 2
    kinds = [resource["kind"] for resource in summary["resources"]]
    assert "PersistentVolume" in kinds
    assert "PersistentVolumeClaim" in kinds


def test_generate_pvc_with_manual_pv_ignores_storage_class_in_payload():
    payload = {
        "basics": {"appName": "data-store", "namespace": "default"},
        "workloadType": "Deployment",
        "containers": [{"name": "app", "image": "nginx", "tag": "latest", "ports": [80]}],
        "resources": {"cpuRequest": "100m", "cpuLimit": "500m", "memoryRequest": "128Mi", "memoryLimit": "256Mi"},
        "storage": {
            "pvcMode": "new",
            "newPvc": {
                "name": "data-pvc",
                "size": "1Gi",
                "storageClass": "should-be-ignored",
                "accessMode": "ReadWriteOnce",
            },
            "advanced": {
                "createManualPv": True,
                "pvName": "data-pv",
                "storageType": "hostPath",
                "hostPath": "/mnt/data",
            },
        },
        "scaling": {"replicas": 1},
    }
    yaml_text, _, error = generate_wizard_manifests(payload)
    assert error is None
    assert "storageClassName: should-be-ignored" not in yaml_text
    assert yaml_text.count("storageClassName: ''") == 2


def test_generate_pvc_with_manual_pv_empty_storage_class():
    payload = {
        "basics": {"appName": "data-store", "namespace": "default"},
        "workloadType": "Deployment",
        "containers": [{"name": "app", "image": "nginx", "tag": "latest", "ports": [80]}],
        "resources": {"cpuRequest": "100m", "cpuLimit": "500m", "memoryRequest": "128Mi", "memoryLimit": "256Mi"},
        "storage": {
            "pvcMode": "new",
            "newPvc": {
                "name": "data-pvc",
                "size": "1Gi",
                "storageClass": "",
                "accessMode": "ReadWriteOnce",
            },
            "advanced": {
                "createManualPv": True,
                "pvName": "data-pv",
                "storageType": "hostPath",
                "hostPath": "/mnt/data",
            },
        },
        "scaling": {"replicas": 1},
    }
    yaml_text, _, error = generate_wizard_manifests(payload)
    assert error is None
    assert yaml_text.count("storageClassName: ''") == 2


def test_generate_local_pv_with_node_affinity():
    payload = {
        "basics": {"appName": "local-data", "namespace": "default"},
        "workloadType": "Deployment",
        "containers": [{"name": "app", "image": "nginx", "tag": "latest", "ports": [80]}],
        "resources": {"cpuRequest": "100m", "cpuLimit": "500m", "memoryRequest": "128Mi", "memoryLimit": "256Mi"},
        "storage": {
            "pvcMode": "new",
            "newPvc": {
                "name": "local-pvc",
                "size": "2Gi",
                "storageClass": "",
                "accessMode": "ReadWriteOnce",
            },
            "advanced": {
                "createManualPv": True,
                "pvName": "local-pv",
                "storageType": "local",
                "localPath": "/mnt/disks/ssd1",
                "nodeName": "kind-worker",
            },
        },
        "scaling": {"replicas": 1},
    }
    yaml_text, _, error = generate_wizard_manifests(payload)
    assert error is None
    assert "nodeAffinity:" in yaml_text
    assert "kubernetes.io/hostname" in yaml_text
    assert "kind-worker" in yaml_text


def test_generate_deployment_with_pvc_volume_mount():
    payload = {
        "basics": {"appName": "data-app", "namespace": "default"},
        "workloadType": "Deployment",
        "containers": [{"name": "app", "image": "nginx", "tag": "latest", "ports": [80]}],
        "resources": {"cpuRequest": "100m", "cpuLimit": "500m", "memoryRequest": "128Mi", "memoryLimit": "256Mi"},
        "storage": {
            "pvcMode": "new",
            "newPvc": {
                "name": "data-pvc",
                "size": "1Gi",
                "storageClass": "standard",
                "accessMode": "ReadWriteOnce",
            },
            "volumeMounts": [{"name": "data", "mountPath": "/data", "readOnly": False}],
        },
        "scaling": {"replicas": 1},
    }
    yaml_text, _, error = generate_wizard_manifests(payload)
    assert error is None
    assert "claimName: data-pvc" in yaml_text
    assert "mountPath: /data" in yaml_text
    assert "name: data" in yaml_text


def test_pvc_and_pv_emitted_before_workload():
    # kubectl applies a multi-doc file in order; the claim (and its bound volume)
    # must exist before the pod that mounts it, or the pod churns on
    # "persistentvolumeclaim not found" / "unbound immediate PVC" until it catches up.
    payload = {
        "basics": {"appName": "data-app", "namespace": "default"},
        "workloadType": "Deployment",
        "containers": [{"name": "app", "image": "nginx", "tag": "latest", "ports": [80]}],
        "resources": {"cpuRequest": "100m", "cpuLimit": "500m", "memoryRequest": "128Mi", "memoryLimit": "256Mi"},
        "storage": {
            "pvcMode": "new",
            "advanced": {"createManualPv": True, "storageType": "hostPath", "hostPath": "/data"},
            "newPvc": {"name": "data-pvc", "size": "1Gi", "accessMode": "ReadWriteOnce"},
            "volumeMounts": [{"name": "data", "mountPath": "/data"}],
        },
        "scaling": {"replicas": 1},
    }
    yaml_text, _, error = generate_wizard_manifests(payload)
    assert error is None
    assert yaml_text.index("kind: PersistentVolume\n") < yaml_text.index("kind: PersistentVolumeClaim")
    assert yaml_text.index("kind: PersistentVolumeClaim") < yaml_text.index("kind: Deployment")


def test_generate_deployment_volume_mount_uses_updated_pvc_name():
    payload = {
        "basics": {"appName": "data-app", "namespace": "default"},
        "workloadType": "Deployment",
        "containers": [{"name": "app", "image": "nginx", "tag": "latest", "ports": [80]}],
        "resources": {"cpuRequest": "100m", "cpuLimit": "500m", "memoryRequest": "128Mi", "memoryLimit": "256Mi"},
        "storage": {
            "pvcMode": "new",
            "newPvc": {
                "name": "custom-pvc",
                "size": "1Gi",
                "storageClass": "standard",
                "accessMode": "ReadWriteOnce",
            },
            "volumeMounts": [{"name": "data", "mountPath": "/data", "readOnly": False}],
        },
        "scaling": {"replicas": 1},
    }
    yaml_text, _, error = generate_wizard_manifests(payload)
    assert error is None
    assert "claimName: custom-pvc" in yaml_text
    assert "claimName: data-pvc" not in yaml_text


def test_generate_deployment_with_multiple_pvcs():
    # The multi-volume storage shape: one PVC per newPvcs entry, mounts linked
    # to their claim via pvcName (e.g. /app/pin + /app/logs).
    payload = {
        "basics": {"appName": "data-app", "namespace": "default"},
        "workloadType": "Deployment",
        "containers": [{"name": "app", "image": "nginx", "tag": "latest", "ports": [80]}],
        "resources": {"cpuRequest": "100m", "cpuLimit": "500m", "memoryRequest": "128Mi", "memoryLimit": "256Mi"},
        "storage": {
            "pvcMode": "new",
            "newPvc": {"name": "pin-pvc", "size": "1Gi", "accessMode": "ReadWriteOnce"},
            "newPvcs": [
                {"name": "pin-pvc", "size": "1Gi", "accessMode": "ReadWriteOnce"},
                {"name": "logs-pvc", "size": "5Gi", "accessMode": "ReadWriteOnce"},
            ],
            "volumeMounts": [
                {"name": "data", "mountPath": "/app/pin", "readOnly": False, "pvcName": "pin-pvc"},
                {"name": "data-2", "mountPath": "/app/logs", "readOnly": False, "pvcName": "logs-pvc"},
            ],
        },
        "scaling": {"replicas": 1},
    }
    yaml_text, summary, error = generate_wizard_manifests(payload)
    assert error is None
    assert yaml_text.count("kind: PersistentVolumeClaim") == 2
    assert "claimName: pin-pvc" in yaml_text
    assert "claimName: logs-pvc" in yaml_text
    assert "mountPath: /app/pin" in yaml_text
    assert "mountPath: /app/logs" in yaml_text
    assert "storage: 1Gi" in yaml_text
    assert "storage: 5Gi" in yaml_text
    kinds = [r["kind"] for r in summary["resources"]]
    assert kinds.count("PersistentVolumeClaim") == 2


def test_generate_deployment_with_service():
    payload = {
        "basics": {"appName": "nginx-demo", "namespace": "default"},
        "workloadType": "Deployment",
        "containers": [{"name": "nginx", "image": "nginx", "tag": "latest", "ports": [80]}],
        "resources": {"cpuRequest": "100m", "cpuLimit": "500m", "memoryRequest": "128Mi", "memoryLimit": "256Mi"},
        "networking": {"service": {"enabled": True, "port": 80, "targetPort": 80}},
        "scaling": {"replicas": 2},
    }
    yaml_text, summary, error = generate_wizard_manifests(payload)
    assert error is None
    assert "kind: Deployment" in yaml_text
    assert "kind: Service" in yaml_text
    assert summary["appName"] == "nginx-demo"
    assert len(summary["resources"]) >= 2


def test_generate_multi_port_nodeport_service():
    payload = {
        "basics": {"appName": "api", "namespace": "default"},
        "workloadType": "Deployment",
        "containers": [{"name": "api", "image": "acme/api", "tag": "1.0", "ports": [8080, 9090]}],
        "resources": {"cpuRequest": "100m", "cpuLimit": "500m", "memoryRequest": "128Mi", "memoryLimit": "256Mi"},
        "networking": {"service": {
            "enabled": True,
            "name": "api-svc",
            "type": "NodePort",
            "ports": [
                {"name": "http", "protocol": "TCP", "port": 8080, "targetPort": 8080, "nodePort": 30080},
                {"name": "metrics", "protocol": "TCP", "port": 9090, "targetPort": 9090},
            ],
        }},
        "scaling": {"replicas": 1},
    }
    yaml_text, summary, error = generate_wizard_manifests(payload)
    assert error is None
    assert "name: api-svc" in yaml_text
    assert "type: NodePort" in yaml_text
    assert "nodePort: 30080" in yaml_text
    assert "name: http" in yaml_text
    assert "name: metrics" in yaml_text
    # Both ports surface on the Service.
    assert "port: 8080" in yaml_text and "port: 9090" in yaml_text


def _docs(yaml_text):
    import yaml as _yaml
    return [d for d in _yaml.safe_load_all(yaml_text) if d]


def _statefulset_payload(**networking):
    return {
        "basics": {"appName": "db", "namespace": "data"},
        "workloadType": "StatefulSet",
        "containers": [{"name": "db", "image": "postgres", "tag": "16", "ports": [5432]}],
        "networking": networking,
        "scaling": {"replicas": 3},
    }


def test_statefulset_emits_headless_service_matching_selector():
    yaml_text, summary, error = generate_wizard_manifests(
        _statefulset_payload(service={"enabled": True, "type": "NodePort", "ports": [
            {"name": "pg", "port": 5432, "targetPort": 5432, "nodePort": 30432},
        ]})
    )
    assert error is None
    docs = _docs(yaml_text)
    sts = next(d for d in docs if d["kind"] == "StatefulSet")
    headless = next(d for d in docs if d["kind"] == "Service" and d["metadata"]["name"] == sts["spec"]["serviceName"])
    assert headless["metadata"]["name"] == "db-headless"
    assert headless["metadata"]["namespace"] == "data"
    assert headless["metadata"]["labels"] == sts["metadata"]["labels"]
    assert headless["spec"]["clusterIP"] == "None"
    assert headless["spec"]["selector"] == sts["spec"]["selector"]["matchLabels"]
    assert headless["spec"]["ports"] == [{"name": "pg", "protocol": "TCP", "port": 5432, "targetPort": 5432}]
    assert "type" not in headless["spec"]
    assert {"kind": "Service", "name": "db-headless", "namespace": "data"} in summary["resources"]


def test_statefulset_headless_service_uses_container_ports_without_service():
    yaml_text, _summary, error = generate_wizard_manifests(_statefulset_payload())
    assert error is None
    headless = next(d for d in _docs(yaml_text) if d["kind"] == "Service")
    assert headless["metadata"]["name"] == "db-headless"
    assert headless["spec"]["ports"][0]["port"] == 5432


def test_statefulset_without_ports_emits_portless_headless_service():
    payload = _statefulset_payload()
    payload["containers"][0]["ports"] = []
    yaml_text, _summary, error = generate_wizard_manifests(payload)
    assert error is None
    headless = next(d for d in _docs(yaml_text) if d["kind"] == "Service")
    assert headless["spec"]["clusterIP"] == "None"
    assert "ports" not in headless["spec"]


def test_deployment_emits_no_headless_service():
    yaml_text, _summary, error = generate_wizard_manifests({
        "basics": {"appName": "web"},
        "workloadType": "Deployment",
        "containers": [{"image": "nginx", "ports": [80]}],
    })
    assert error is None
    assert "headless" not in yaml_text


def _ingress_payload(**ingress):
    return {
        "basics": {"appName": "web", "namespace": "default"},
        "workloadType": "Deployment",
        "containers": [{"image": "nginx", "ports": [80]}],
        "networking": {
            "service": {"enabled": True, "port": 80},
            "ingress": {"enabled": True, "host": "web.example.com", "path": "/", **ingress},
        },
    }


def test_ingress_with_class_emits_ingress_class_name():
    yaml_text, _summary, error = generate_wizard_manifests(_ingress_payload(ingressClassName="nginx"))
    assert error is None
    ing = next(d for d in _docs(yaml_text) if d["kind"] == "Ingress")
    assert ing["spec"]["ingressClassName"] == "nginx"


def test_ingress_accepts_class_name_alias():
    yaml_text, _summary, error = generate_wizard_manifests(_ingress_payload(className="internal.nginx"))
    assert error is None
    ing = next(d for d in _docs(yaml_text) if d["kind"] == "Ingress")
    assert ing["spec"]["ingressClassName"] == "internal.nginx"


def test_ingress_without_class_omits_ingress_class_name():
    for extra in ({}, {"ingressClassName": ""}, {"ingressClassName": "  "}):
        yaml_text, _summary, error = generate_wizard_manifests(_ingress_payload(**extra))
        assert error is None
        ing = next(d for d in _docs(yaml_text) if d["kind"] == "Ingress")
        assert "ingressClassName" not in ing["spec"]


def test_ingress_invalid_class_errors():
    for bad in ("Nginx", "nginx_ingress", "-nginx", "nginx.", "a b"):
        yaml_text, summary, error = generate_wizard_manifests(_ingress_payload(ingressClassName=bad))
        assert yaml_text == "" and summary == {}
        assert error and "ingress class" in error.lower()
