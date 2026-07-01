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
