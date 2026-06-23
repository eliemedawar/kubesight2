"""Tests for the template schema -> deployment payload resolver."""

from api.services.template_resolver import resolve_template
from api.services.wizard_manifest_generator import generate_wizard_manifests


def _base_template(**schema):
    return {
        "id": "orders-api",
        "name": "Orders API",
        "workloadType": "Deployment",
        "containers": [{"name": "app", "image": "acme/orders", "tag": "1.0.0", "ports": [8080]}],
        "resources": {"cpuRequest": "100m", "cpuLimit": "500m", "memoryRequest": "128Mi", "memoryLimit": "256Mi"},
        "networking": {"service": {"enabled": True, "type": "ClusterIP", "port": 8080, "targetPort": 8080}},
        "scaling": {"replicas": 1},
        "schema": schema,
    }


def _answers(**kwargs):
    answers = {"basics": {"appName": "orders", "namespace": "shop", "clusterId": "c1"}}
    answers.update(kwargs)
    return answers


def test_resolves_minimal_template_to_generatable_payload():
    payload, err = resolve_template(_base_template(), _answers())
    assert err is None
    yaml_text, summary, gen_err = generate_wizard_manifests(payload)
    assert gen_err is None
    assert "kind: Deployment" in yaml_text
    assert "image: acme/orders:1.0.0" in yaml_text
    assert summary["appName"] == "orders"


def test_required_env_without_value_errors():
    template = _base_template(env=[{"key": "DB_HOST", "required": True, "allowedSources": ["value"]}])
    payload, err = resolve_template(template, _answers())
    assert payload is None
    assert "DB_HOST" in err and "required" in err


def test_optional_env_uses_template_default():
    template = _base_template(env=[{"key": "APP_ENV", "required": False, "default": "production"}])
    payload, err = resolve_template(template, _answers())
    assert err is None
    env = payload["environment"]["envVars"]
    assert {"name": "APP_ENV", "value": "production"} in env


def test_sensitive_var_rejects_plaintext_source():
    template = _base_template(
        env=[{"key": "DB_PASSWORD", "required": True, "sensitive": True,
              "allowedSources": ["value", "createSecret"]}],
    )
    answers = _answers(env={"DB_PASSWORD": {"source": "value", "value": "hunter2"}})
    payload, err = resolve_template(template, answers)
    assert payload is None
    assert "sensitive" in err


def test_disallowed_source_rejected():
    template = _base_template(env=[{"key": "API_KEY", "allowedSources": ["existingSecret"]}])
    answers = _answers(env={"API_KEY": {"source": "value", "value": "x"}})
    payload, err = resolve_template(template, answers)
    assert payload is None
    assert "not allowed" in err


def test_create_secret_emits_secret_and_valuefrom():
    template = _base_template(
        env=[{"key": "DB_PASSWORD", "required": True, "sensitive": True, "allowedSources": ["createSecret"]}],
    )
    answers = _answers(
        env={"DB_PASSWORD": {"source": "createSecret", "secretName": "orders-secret",
                              "key": "password", "value": "s3cr3t"}},
    )
    payload, err = resolve_template(template, answers)
    assert err is None
    assert payload["environment"]["provisionedSecrets"] == [
        {"name": "orders-secret", "stringData": {"password": "s3cr3t"}}
    ]
    yaml_text, _, gen_err = generate_wizard_manifests(payload)
    assert gen_err is None
    assert "kind: Secret" in yaml_text
    assert "name: orders-secret" in yaml_text
    assert "secretKeyRef" in yaml_text
    # The Secret must be emitted before the Deployment that references it.
    assert yaml_text.index("kind: Secret") < yaml_text.index("kind: Deployment")


def test_existing_configmap_produces_reference_only():
    template = _base_template(env=[{"key": "APP_ENV", "allowedSources": ["existingConfigMap"]}])
    answers = _answers(
        env={"APP_ENV": {"source": "existingConfigMap", "configMapName": "shared-config", "key": "env"}},
    )
    payload, err = resolve_template(template, answers)
    assert err is None
    assert payload["environment"]["provisionedConfigMaps"] == []
    yaml_text, _, gen_err = generate_wizard_manifests(payload)
    assert gen_err is None
    assert "kind: ConfigMap" not in yaml_text
    assert "configMapKeyRef" in yaml_text


def test_override_applied_only_when_schema_allows():
    template = _base_template(overrides={"tag": True, "replicas": True})
    answers = _answers(overrides={"tag": "2.5.0", "replicas": 4})
    payload, err = resolve_template(template, answers)
    assert err is None
    assert payload["containers"][0]["tag"] == "2.5.0"
    assert payload["scaling"]["replicas"] == 4


def test_override_ignored_when_not_permitted():
    template = _base_template(overrides={"tag": False})
    answers = _answers(overrides={"tag": "2.5.0"})
    payload, err = resolve_template(template, answers)
    assert err is None
    assert payload["containers"][0]["tag"] == "1.0.0"


def test_disallowed_service_type_rejected():
    template = _base_template(overrides={"serviceType": ["ClusterIP"]})
    answers = _answers(overrides={"serviceType": "LoadBalancer"})
    payload, err = resolve_template(template, answers)
    assert payload is None
    assert "not permitted" in err


def test_dependency_wiring_populates_env():
    template = _base_template(
        env=[{"key": "DB_HOST", "required": False}],
        dependencies=[{
            "kind": "postgresql", "name": "primary-db", "required": True,
            "provisioning": ["create", "existing"],
            "wiring": [
                {"from": "host", "into": "DB_HOST", "as": "value"},
                {"from": "password", "into": "DB_PASSWORD", "as": "secret"},
            ],
        }],
    )
    answers = _answers(dependencies={"primary-db": {"mode": "create", "password": "pgpass"}})
    payload, err = resolve_template(template, answers)
    assert err is None
    env = payload["environment"]["envVars"]
    assert {"name": "DB_HOST", "value": "primary-db"} in env
    assert any(e["name"] == "DB_PASSWORD" and "valueFrom" in e for e in env)
    assert payload["environment"]["provisionedSecrets"][0]["stringData"] == {"password": "pgpass"}


def test_informational_dependency_without_wiring_is_ignored():
    # A dependency with no wiring is documentation only and must not block deploy.
    template = _base_template(
        dependencies=[{"kind": "redis", "name": "cache", "required": True}],
    )
    payload, err = resolve_template(template, _answers())
    assert err is None
    assert payload is not None


def test_existing_image_pull_secret_referenced_on_pod():
    template = _base_template(imagePullSecret={"mode": "existing", "name": "regcred"})
    payload, err = resolve_template(template, _answers())
    assert err is None
    assert payload["containers"][0]["imagePullSecret"] == "regcred"
    yaml_text, _, gen_err = generate_wizard_manifests(payload)
    assert gen_err is None
    assert "imagePullSecrets" in yaml_text
    assert "name: regcred" in yaml_text


def test_create_image_pull_secret_provisions_dockerconfig():
    template = _base_template(imagePullSecret={"mode": "create", "name": "orders-registry"})
    answers = _answers(imagePullSecret={"username": "bot", "password": "tok", "registry": "ghcr.io"})
    payload, err = resolve_template(template, answers)
    assert err is None
    assert payload["environment"]["provisionedDockerSecrets"][0]["username"] == "bot"
    yaml_text, _, gen_err = generate_wizard_manifests(payload)
    assert gen_err is None
    assert "kubernetes.io/dockerconfigjson" in yaml_text
    assert ".dockerconfigjson" in yaml_text
    assert yaml_text.index("dockerconfigjson") < yaml_text.index("kind: Deployment")


def test_create_image_pull_secret_requires_credentials():
    template = _base_template(imagePullSecret={"mode": "create", "name": "regcred"})
    payload, err = resolve_template(template, _answers())
    assert payload is None
    assert "username and password" in err


def test_deploy_time_pull_secret_without_template_schema():
    # Template declares no imagePullSecret; the deployer adds one at deploy time.
    answers = _answers(imagePullSecret={"mode": "existing", "name": "regcred"})
    payload, err = resolve_template(_base_template(), answers)
    assert err is None
    assert payload["containers"][0]["imagePullSecret"] == "regcred"


def test_pull_secret_mode_locked_unless_overridable():
    template = _base_template(imagePullSecret={"mode": "none", "overridable": False})
    answers = _answers(imagePullSecret={"mode": "existing", "name": "sneaky"})
    payload, err = resolve_template(template, answers)
    assert err is None
    assert "imagePullSecret" not in payload["containers"][0]


def test_extra_env_added_by_deployer():
    answers = _answers(extraEnv=[
        {"name": "FEATURE_FLAG", "source": "value", "value": "on"},
        {"name": "SHARED", "source": "existingConfigMap", "configMapName": "cfg", "key": "shared"},
    ])
    payload, err = resolve_template(_base_template(), answers)
    assert err is None
    env = payload["environment"]["envVars"]
    assert {"name": "FEATURE_FLAG", "value": "on"} in env
    shared = next(e for e in env if e["name"] == "SHARED")
    assert shared["valueFrom"] == {"kind": "configMap", "name": "cfg", "key": "shared"}


def test_extra_env_create_secret_provisions_and_skips_duplicates():
    template = _base_template(env=[{"key": "DUP", "allowedSources": ["value"]}])
    answers = _answers(
        env={"DUP": {"source": "value", "value": "from-schema"}},
        extraEnv=[
            {"name": "DUP", "source": "value", "value": "ignored"},
            {"name": "EXTRA_TOKEN", "source": "createSecret", "secretName": "extra-sec", "key": "token", "value": "t0k"},
        ],
    )
    payload, err = resolve_template(template, answers)
    assert err is None
    env = payload["environment"]["envVars"]
    dup = [e for e in env if e["name"] == "DUP"]
    assert len(dup) == 1 and dup[0]["value"] == "from-schema"
    assert payload["environment"]["provisionedSecrets"][0]["stringData"] == {"token": "t0k"}


def test_storage_new_pvc_from_deployer():
    answers = _answers(storage={
        "enabled": True,
        "mode": "new",
        "mountPath": "/var/lib/data",
        "newPvc": {"name": "orders-data", "size": "10Gi", "accessMode": "ReadWriteOnce", "storageClass": "fast"},
    })
    payload, err = resolve_template(_base_template(), answers)
    assert err is None
    yaml_text, _, gen_err = generate_wizard_manifests(payload)
    assert gen_err is None
    assert "kind: PersistentVolumeClaim" in yaml_text
    assert "claimName: orders-data" in yaml_text
    assert "mountPath: /var/lib/data" in yaml_text
    assert "storageClassName: fast" in yaml_text


def test_storage_existing_pvc_no_pvc_document():
    answers = _answers(storage={"enabled": True, "mode": "existing", "existingPvc": "shared-data", "mountPath": "/data"})
    payload, err = resolve_template(_base_template(), answers)
    assert err is None
    yaml_text, _, gen_err = generate_wizard_manifests(payload)
    assert gen_err is None
    assert "kind: PersistentVolumeClaim" not in yaml_text
    assert "claimName: shared-data" in yaml_text


def test_storage_with_manual_pv():
    answers = _answers(storage={
        "enabled": True,
        "mode": "new",
        "mountPath": "/data",
        "newPvc": {"name": "pv-data", "size": "5Gi"},
        "pv": {"enabled": True, "name": "pv-data-pv", "storageType": "hostPath", "hostPath": "/mnt/data", "reclaimPolicy": "Retain"},
    })
    payload, err = resolve_template(_base_template(), answers)
    assert err is None
    yaml_text, _, gen_err = generate_wizard_manifests(payload)
    assert gen_err is None
    assert "kind: PersistentVolume" in yaml_text
    assert "hostPath:" in yaml_text


def test_ingress_host_path_and_existing_tls():
    payload, err = resolve_template(
        _base_template(),
        _answers(ingress={
            "host": "orders.example.com",
            "path": "/api",
            "tls": {"mode": "existing", "secret": "tls-cert"},
        }),
    )
    assert err is None
    yaml_text, _, gen_err = generate_wizard_manifests(payload)
    assert gen_err is None
    assert "kind: Ingress" in yaml_text
    assert "orders.example.com" in yaml_text
    assert "path: /api" in yaml_text
    assert "secretName: tls-cert" in yaml_text


def test_ingress_create_tls_provisions_secret():
    payload, err = resolve_template(
        _base_template(),
        _answers(ingress={
            "host": "orders.example.com",
            "tls": {"mode": "create", "secret": "orders-tls", "cert": "CERTDATA", "key": "KEYDATA"},
        }),
    )
    assert err is None
    assert payload["environment"]["provisionedTlsSecrets"][0]["name"] == "orders-tls"
    yaml_text, _, gen_err = generate_wizard_manifests(payload)
    assert gen_err is None
    assert "kubernetes.io/tls" in yaml_text
    assert "tls.crt" in yaml_text
    assert "secretName: orders-tls" in yaml_text
    assert yaml_text.index("kubernetes.io/tls") < yaml_text.index("kind: Ingress")


def test_ingress_create_tls_requires_cert_and_key():
    payload, err = resolve_template(
        _base_template(),
        _answers(ingress={"host": "x.example.com", "tls": {"mode": "create", "secret": "x-tls"}}),
    )
    assert payload is None
    assert "certificate and private key" in err
