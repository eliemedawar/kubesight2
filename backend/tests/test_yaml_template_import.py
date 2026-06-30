"""Tests for YAML import → template drafts (parser + route gate)."""

from api.services.yaml_template_parser import parse_yaml_to_template_drafts
from tests.conftest import auth_headers

MULTI_YAML = """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: web
  labels:
    app: web
spec:
  replicas: 3
  selector:
    matchLabels:
      app: web
  template:
    metadata:
      labels:
        app: web
    spec:
      containers:
        - name: web
          image: nginx:1.25
          imagePullPolicy: Always
          ports:
            - containerPort: 8080
          resources:
            requests:
              cpu: 250m
              memory: 256Mi
            limits:
              cpu: "1"
              memory: 512Mi
          env:
            - name: LOG_LEVEL
              value: info
            - name: API_KEY
              valueFrom:
                secretKeyRef:
                  name: web-secrets
                  key: api-key
            - name: FEATURE_FLAGS
              valueFrom:
                configMapKeyRef:
                  name: web-config
                  key: flags
          readinessProbe:
            httpGet:
              path: /healthz
              port: 8080
            initialDelaySeconds: 5
            periodSeconds: 10
          volumeMounts:
            - name: data
              mountPath: /var/lib/web
      volumes:
        - name: data
          persistentVolumeClaim:
            claimName: web-data
---
apiVersion: v1
kind: Service
metadata:
  name: web
spec:
  type: NodePort
  selector:
    app: web
  ports:
    - port: 80
      targetPort: 8080
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: web-data
spec:
  resources:
    requests:
      storage: 5Gi
---
apiVersion: apps/v1
kind: StatefulSet
metadata:
  name: cache
spec:
  replicas: 1
  template:
    metadata:
      labels:
        app: cache
    spec:
      containers:
        - name: redis
          image: redis
        - name: sidecar
          image: busybox
"""


def test_parser_maps_deployment_fields():
    drafts, error = parse_yaml_to_template_drafts(MULTI_YAML)
    assert error is None
    assert {d["name"] for d in drafts} == {"web", "cache"}

    web = next(d for d in drafts if d["name"] == "web")
    container = web["containers"][0]
    assert container["image"] == "nginx"
    assert container["tag"] == "1.25"
    assert container["pullPolicy"] == "Always"
    assert container["ports"] == [8080]
    assert web["resources"] == {
        "cpuRequest": "250m", "cpuLimit": "1",
        "memoryRequest": "256Mi", "memoryLimit": "512Mi",
    }
    assert web["scaling"]["replicas"] == 3

    # Default override surface.
    assert web["schema"]["overrides"] == {"tag": True, "replicas": True}

    # Env vars become configurable schema rows by kind.
    by_key = {e["key"]: e for e in web["schema"]["env"]}
    assert by_key["LOG_LEVEL"] == {"key": "LOG_LEVEL", "required": False,
                                   "sensitive": False, "kind": "value", "default": "info"}
    assert by_key["API_KEY"]["kind"] == "secret" and by_key["API_KEY"]["sensitive"] is True
    assert by_key["FEATURE_FLAGS"]["kind"] == "configMap"

    # Readiness probe.
    assert web["healthChecks"]["readiness"] == {
        "initialDelaySeconds": 5, "periodSeconds": 10, "type": "http",
        "path": "/healthz", "port": 8080,
    }

    # Storage size resolved from the PVC in the same file.
    assert web["storage"]["newPvc"]["size"] == "5Gi"
    assert web["storage"]["volumeMounts"][0]["mountPath"] == "/var/lib/web"

    # Service matched by selector.
    assert web["networking"]["service"]["type"] == "NodePort"
    assert web["networking"]["service"]["port"] == 80
    assert web["networking"]["service"]["targetPort"] == 8080


SENSITIVE_YAML = """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: svc
spec:
  replicas: 1
  template:
    metadata:
      labels:
        app: svc
    spec:
      containers:
        - name: svc
          image: registry.example.com/svc:1.0
          env:
            - name: db_username
              value: processing
            - name: db_password
              value: PassPrdV2
            - name: API_TOKEN
              value: abc123
"""


def test_parser_auto_flags_sensitive_plain_values():
    drafts, error = parse_yaml_to_template_drafts(SENSITIVE_YAML)
    assert error is None
    by_key = {e["key"]: e for e in drafts[0]["schema"]["env"]}

    # Non-sensitive name keeps its plain value as a default.
    assert by_key["db_username"] == {"key": "db_username", "required": False,
                                     "sensitive": False, "kind": "value", "default": "processing"}
    # Credential-looking names become required Secrets with no plaintext default.
    for key in ("db_password", "API_TOKEN"):
        assert by_key[key]["sensitive"] is True
        assert by_key[key]["kind"] == "secret"
        assert by_key[key]["default"] == ""
    assert any("db_password" in w and "Secret" in w for w in drafts[0]["warnings"])


VOLUME_YAML = """
apiVersion: apps/v1
kind: Deployment
metadata:
  name: certs-app
spec:
  replicas: 1
  template:
    metadata:
      labels:
        app: certs-app
    spec:
      containers:
        - name: certs-app
          image: registry.example.com/certs-app:1.0
          volumeMounts:
            - mountPath: /etc/config
              name: app-config
            - mountPath: /opt/keys/wso2carbon.jks
              name: wso2carbon
              subPath: wso2carbon.jks
              readOnly: true
            - mountPath: /var/lib/data
              name: data
      volumes:
        - name: app-config
          configMap:
            name: app-config-cm
        - name: wso2carbon
          secret:
            secretName: wso2-keys
        - name: data
          persistentVolumeClaim:
            claimName: data-claim
"""


def test_parser_imports_config_secret_volume_mounts():
    drafts, error = parse_yaml_to_template_drafts(VOLUME_YAML)
    assert error is None
    mounts = {m["mountPath"]: m for m in drafts[0]["schema"]["volumeMounts"]}

    # ConfigMap and Secret volumes become volume-mount schema rows; the PVC volume
    # is handled by storage, not here.
    assert "/var/lib/data" not in mounts
    assert mounts["/etc/config"]["kind"] == "configMap"
    assert mounts["/etc/config"]["allowedSources"] == ["existingConfigMap", "createConfigMap"]
    assert mounts["/opt/keys/wso2carbon.jks"]["kind"] == "secret"
    assert mounts["/opt/keys/wso2carbon.jks"]["readOnly"] is True

    # subPath is preserved so the mount behaves like the source manifest.
    assert mounts["/opt/keys/wso2carbon.jks"]["subPath"] == "wso2carbon.jks"
    assert "subPath" not in mounts["/etc/config"]
    # The PVC volume still drives storage.
    assert drafts[0]["storage"]["volumeMounts"][0]["mountPath"] == "/var/lib/data"


def test_parser_warns_on_extra_containers():
    drafts, error = parse_yaml_to_template_drafts(MULTI_YAML)
    assert error is None
    cache = next(d for d in drafts if d["name"] == "cache")
    # StatefulSet, first container only, tag defaults to latest.
    assert cache["workloadType"] == "StatefulSet"
    assert cache["containers"][0]["image"] == "redis"
    assert cache["containers"][0]["tag"] == "latest"
    assert any("2 containers" in w for w in cache["warnings"])


def test_parser_rejects_invalid_and_empty():
    drafts, error = parse_yaml_to_template_drafts("")
    assert drafts is None and "No YAML" in error

    drafts, error = parse_yaml_to_template_drafts("kind: ConfigMap\nmetadata:\n  name: x")
    assert drafts is None and "No Deployment" in error

    drafts, error = parse_yaml_to_template_drafts("foo: [bar:")
    assert drafts is None and "Invalid YAML" in error


def test_import_route_requires_manage_permission(client, admin_token, viewer_token):
    # Admin can manage templates → parse succeeds.
    resp = client.post(
        "/api/inventory/deploy/wizard/templates/import",
        json={"yaml": MULTI_YAML},
        headers=auth_headers(admin_token),
    )
    assert resp.status_code == 200, resp.get_json()
    drafts = resp.get_json()["data"]["drafts"]
    assert len(drafts) == 2

    # Viewer lacks inventory:update → forbidden.
    resp = client.post(
        "/api/inventory/deploy/wizard/templates/import",
        json={"yaml": MULTI_YAML},
        headers=auth_headers(viewer_token),
    )
    assert resp.status_code == 403


def test_imported_draft_round_trips_to_template(client, admin_token):
    """A parsed draft can be saved through the existing create endpoint and
    re-fetched as a schema-bearing template (so it deploys via the schema wizard)."""
    drafts = client.post(
        "/api/inventory/deploy/wizard/templates/import",
        json={"yaml": MULTI_YAML},
        headers=auth_headers(admin_token),
    ).get_json()["data"]["drafts"]
    web = next(d for d in drafts if d["name"] == "web")

    created = client.post(
        "/api/inventory/deploy/wizard/templates",
        json=web,
        headers=auth_headers(admin_token),
    )
    assert created.status_code == 201, created.get_json()
    template_id = created.get_json()["data"]["id"]

    detail = client.get(
        f"/api/inventory/deploy/wizard/templates/{template_id}",
        headers=auth_headers(admin_token),
    ).get_json()["data"]
    assert detail["containers"][0]["image"] == "nginx"
    assert detail["schema"]["env"]
