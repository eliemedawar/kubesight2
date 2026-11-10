"""OKE clusters by OKE workload identity: the platform's requirements K1 to K23.

The real job engine and the simulated OpenTofu engine, with the provisioner
Jobs run in-process (``remote.SimulatedLauncher``) through the same receivers a
real Job posts to. No cloud API, no Kubernetes API.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from api.auth_utils import create_access_token
from api.db import db
from api.models import ClusterBuild, ClusterProvisionJob, CloudAccount, Role, User
from api.services.cluster_build.provisioning import (
    cloud_build, jobs, oke_onboard, remote, state_store, tofu_runner,
)
from api.services.cluster_build.provisioning.clouds import oci, oci_wi

from tests.test_cluster_builds import auth_headers  # noqa: F401

REPO = Path(__file__).resolve().parents[2]
LOCK = "ocid1.compartment.oc1..aaaaaaaauatcompartment"
DIGEST_IMAGE = "registry.areeba.com/kubesight-provisioner@sha256:" + "a" * 64


def _load_script(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


provision = _load_script("kubesight_provision", REPO / "provisioner" / "kubesight_provision.py")
oke_token = _load_script("kubesight_oke_token", REPO / "backend" / "kubesight_oke_token.py")


@pytest.fixture(autouse=True)
def _cluster_needs_no_approval(no_cluster_approvals):
    yield


@pytest.fixture()
def audit_lines():
    lines = []
    remote.set_audit_sink(lines.append)
    yield lines
    remote.set_audit_sink(None)


@pytest.fixture()
def wi_install(tmp_path, monkeypatch, audit_lines):
    monkeypatch.setenv("KUBESIGHT_PROVISIONING_SIMULATE", "1")
    monkeypatch.setenv("KUBESIGHT_TOFU_WORKDIR", str(tmp_path / "tofu"))
    monkeypatch.setenv("KUBESIGHT_OCI_AUTH_MODES", "workload_identity")
    monkeypatch.setenv("KUBESIGHT_OCI_VMS", "false")
    monkeypatch.setenv("KUBESIGHT_OCI_COMPARTMENT_LOCK", LOCK)
    monkeypatch.setenv("KUBESIGHT_PROVISIONER_IMAGE", DIGEST_IMAGE)
    monkeypatch.setenv("KUBESIGHT_PROVISIONER_NAMESPACE", "kubesight")
    monkeypatch.setenv("OCI_RESOURCE_PRINCIPAL_REGION", "me-dubai-1")
    monkeypatch.delenv("KUBESIGHT_PROVISION_NOTIFY_EMAILS", raising=False)
    # The simulated engine keeps no OpenBao; production refuses plain state (K15).
    monkeypatch.setenv("KUBESIGHT_ALLOW_PLAIN_STATE", "1")
    tofu_runner.set_engine(tofu_runner.SimulatedEngine())
    remote.set_launcher(remote.SimulatedLauncher())
    yield
    tofu_runner.set_engine(None)
    remote.set_launcher(None)
    remote.set_token_reviewer(None)
    oke_onboard.set_api(None)
    oke_onboard.set_ca_reader(None)
    oci_wi.set_signer(None)


def wi_account_payload(**overrides):
    settings = {
        "tenancyOcid": "ocid1.tenancy.oc1..aaaaaaaatenancy",
        "compartmentOcid": LOCK,
        "vcnOcid": "ocid1.vcn.oc1.me-dubai-1.aaaaaaaavcn",
        "endpointSubnetOcid": "ocid1.subnet.oc1.me-dubai-1.aaaaaaaamaster",
        "workerSubnetOcid": "ocid1.subnet.oc1.me-dubai-1.aaaaaaaaworkers",
        "serviceLbSubnetOcid": "ocid1.subnet.oc1.me-dubai-1.aaaaaaaalb",
        "sourceCidr": "10.43.11.64/26",
        "availabilityDomain": "wchx:ME-DUBAI-1-AD-1",
        "registryHost": "registry.areeba.com",
        "registryAddress": "10.43.17.16",
    }
    settings.update(overrides.pop("settings", {}))
    payload = {"name": "oci-uat", "provider": "oci", "region": "me-dubai-1", "settings": settings, "secrets": {}}
    payload.update(overrides)
    return payload


def create_wi_account(client, token):
    response = client.post("/api/cloud-accounts", json=wi_account_payload(), headers=auth_headers(token))
    assert response.status_code == 201, response.get_json()
    return response.get_json()["data"]["account"]


def build_payload(account, *, name="oke-01", **spec_overrides):
    spec = {
        "cloudAccountId": account["id"], "region": "me-dubai-1", "kind": "managed",
        "counts": {"loadbalancer": 0, "controlPlane": 0, "worker": 2},
        "sizes": {"worker": {"cpu": 4, "memoryGb": 16, "diskGb": 50}},
        "managed": {"kubernetesVersion": "v1.36.4"},
        "ownerEmail": "emedawer@areeba.com",
        "expiresOn": (date.today() + timedelta(days=7)).isoformat(),
    }
    spec.update(spec_overrides)
    return {
        "name": name, "k8sVersion": "1.36.4", "machineSource": "oci", "templateId": "small",
        "vmsOnly": False, "cniPlugin": "calico", "podCidr": "10.244.0.0/16", "serviceCidr": "10.96.0.0/12",
        "provisioning": spec,
    }


def create_build(client, token, account, **kwargs):
    response = client.post("/api/cluster-builds", json=build_payload(account, **kwargs), headers=auth_headers(token))
    assert response.status_code == 201, response.get_json()
    return response.get_json()["data"]


def second_person(app, *, mfa=True, username="approver"):
    role = Role.query.filter_by(name="admin").first()
    user = User(username=username, email=f"{username}@areeba.com", full_name=username, role_id=role.id,
                is_active=True, mfa_enabled=mfa, first_login_completed=True)
    user.password_hash = "x"
    db.session.add(user)
    db.session.commit()
    return create_access_token(user)


def plan(client, token, build_id):
    response = client.post(f"/api/cluster-builds/{build_id}/provision/plan", headers=auth_headers(token))
    assert response.status_code == 202, response.get_json()
    return response.get_json()["data"]["provisioning"]["job"]


def approve(client, token, build_id, job_id, sha):
    return client.post(f"/api/cluster-builds/{build_id}/provision/jobs/{job_id}/approve",
                       json={"planSha256": sha, "note": "ok"}, headers=auth_headers(token))


# ---------------------------------------------------------------------------
# K1, K5, K12: the account
# ---------------------------------------------------------------------------

class TestAccount:
    def test_catalog_offers_no_key_fields(self, client, admin_token, wi_install):
        data = client.get("/api/cloud-accounts/catalog", headers=auth_headers(admin_token))
        if data.status_code == 404:
            data = client.get("/api/cloud-providers", headers=auth_headers(admin_token))
        payload = json.dumps(data.get_json())
        assert "privateKey" not in payload and "fingerprint" not in payload
        keys = [f["key"] for f in oci.fields()]
        assert "privateKey" not in keys and "userOcid" not in keys and "vcnOcid" in keys

    def test_saved_without_a_key(self, client, admin_token, wi_install):
        account = create_wi_account(client, admin_token)
        assert account["settings"]["authMode"] == "workload_identity"
        assert account["secretsSet"] == {}
        assert "workload identity" in account["identity"]
        row = db.session.get(CloudAccount, account["id"])
        assert row.secrets_cipher is None

    @pytest.mark.parametrize("field", ["userOcid", "fingerprint"])
    def test_api_key_fields_are_refused(self, client, admin_token, wi_install, field):
        payload = wi_account_payload(settings={field: "x"})
        response = client.post("/api/cloud-accounts", json=payload, headers=auth_headers(admin_token))
        assert response.status_code == 400 and "workload identity only" in response.get_json()["error"]

    def test_a_private_key_is_refused(self, client, admin_token, wi_install):
        payload = wi_account_payload(secrets={"privateKey": "-----BEGIN PRIVATE KEY-----"})
        response = client.post("/api/cloud-accounts", json=payload, headers=auth_headers(admin_token))
        assert response.status_code == 400

    def test_another_compartment_is_refused(self, client, admin_token, wi_install):
        payload = wi_account_payload(settings={"compartmentOcid": "ocid1.compartment.oc1..aaaaaaaaother"})
        response = client.post("/api/cloud-accounts", json=payload, headers=auth_headers(admin_token))
        assert response.status_code == 400 and LOCK in response.get_json()["error"]

    def test_an_old_api_key_account_is_never_used(self, app, wi_install):
        account = oci.Account(provider="oci", name="old", region="me-dubai-1",
                              settings={"tenancyOcid": "t", "userOcid": "u", "fingerprint": "f"},
                              secrets={"privateKey": "k"})
        with pytest.raises(oci.CloudError, match="switched off"):
            oci._call(account, "GET", "https://identity.me-dubai-1.oci.oraclecloud.com/20160918/x")

    def test_calls_are_signed_by_workload_identity(self, app, wi_install, monkeypatch):
        seen = {}

        def signer(prepared):
            seen["url"] = prepared.url
            prepared.headers["authorization"] = 'Signature keyId="ST$rpst-token"'
            prepared.headers["date"] = "Tue, 10 Nov 2026 10:00:00 GMT"

        oci_wi.set_signer(signer)
        monkeypatch.setattr(oci.base, "http", lambda method, url, **kw: (200, {}, b'{"items": []}') if
                            seen.setdefault("headers", kw["headers"]) else None)
        account = oci.Account(provider="oci", name="wi", region="me-dubai-1",
                              settings={"authMode": "workload_identity"}, secrets={})
        oci._call(account, "GET", "https://iaas.me-dubai-1.oraclecloud.com/20160918/vcns/x")
        assert seen["headers"]["authorization"].startswith('Signature keyId="ST$')
        assert "privateKey" not in json.dumps(seen["headers"])


# ---------------------------------------------------------------------------
# K6 to K12, K17, K18: what a build may be
# ---------------------------------------------------------------------------

class TestBuildSpec:
    def test_vms_are_refused(self, client, admin_token, wi_install):
        account = create_wi_account(client, admin_token)
        payload = build_payload(account, kind="vms")
        response = client.post("/api/cluster-builds", json=payload, headers=auth_headers(admin_token))
        assert response.status_code == 400 and "OKE clusters only" in response.get_json()["error"]

    def test_landing_zone_is_fixed(self, client, admin_token, wi_install):
        account = create_wi_account(client, admin_token)
        build = create_build(client, admin_token, account)
        spec = db.session.get(ClusterBuild, build["id"]).provisioning_json
        assert spec["compartmentId"] == LOCK
        assert spec["networkId"].endswith("vcn") and spec["subnetId"].endswith("workers")
        assert spec["endpointSubnetId"].endswith("master") and spec["serviceLbSubnetId"].endswith("lb")
        assert spec["shape"] == oci_wi.SHAPE and spec["allowedCidrs"] == ["10.43.11.64/26"]

    def test_another_compartment_is_refused(self, client, admin_token, wi_install):
        account = create_wi_account(client, admin_token)
        payload = build_payload(account, compartmentId="ocid1.compartment.oc1..aaaaaaaaother")
        response = client.post("/api/cluster-builds", json=payload, headers=auth_headers(admin_token))
        assert response.status_code == 400

    def test_another_shape_is_refused(self, client, admin_token, wi_install):
        account = create_wi_account(client, admin_token)
        payload = build_payload(account, shape="VM.Standard.E5.Flex")
        response = client.post("/api/cluster-builds", json=payload, headers=auth_headers(admin_token))
        assert response.status_code == 400 and "E6" in response.get_json()["error"]

    def test_a_pod_subnet_is_refused(self, client, admin_token, wi_install):
        account = create_wi_account(client, admin_token)
        payload = build_payload(account, podSubnetId="ocid1.subnet.oc1.me-dubai-1.aaaaaaaapods")
        response = client.post("/api/cluster-builds", json=payload, headers=auth_headers(admin_token))
        assert response.status_code == 400

    @pytest.mark.parametrize("field,value", [("ownerEmail", ""), ("expiresOn", ""),
                                             ("expiresOn", "2000-01-01")])
    def test_owner_and_expiry_are_required(self, client, admin_token, wi_install, field, value):
        account = create_wi_account(client, admin_token)
        payload = build_payload(account, **{field: value})
        response = client.post("/api/cluster-builds", json=payload, headers=auth_headers(admin_token))
        assert response.status_code == 400


class TestRender:
    def _config(self, client, admin_token):
        account = create_wi_account(client, admin_token)
        build = create_build(client, admin_token, account)
        row = db.session.get(ClusterBuild, build["id"])
        job = ClusterProvisionJob(build_id=row.id, operation="create", status="planning")
        db.session.add(job)
        db.session.commit()
        return cloud_build.render(row, job)

    def test_workload_identity_provider_and_no_keys(self, client, admin_token, wi_install):
        config = self._config(client, admin_token)
        assert config["provider"]["oci"] == {"auth": "OKEWorkloadIdentity", "region": "me-dubai-1"}
        text = json.dumps(config)
        for word in ("OCI_TENANCY_OCID", "OCI_USER_OCID", "fingerprint", "private_key"):
            assert word not in text

    def test_enhanced_private_three_subnets_flannel(self, client, admin_token, wi_install):
        resources = self._config(client, admin_token)["resource"]
        cluster = resources["oci_containerengine_cluster"]["cluster"]
        assert cluster["type"] == "ENHANCED_CLUSTER"
        assert cluster["endpoint_config"][0]["is_public_ip_enabled"] is False
        assert cluster["endpoint_config"][0]["subnet_id"].endswith("master")
        assert cluster["options"][0]["service_lb_subnet_ids"] == ["ocid1.subnet.oc1.me-dubai-1.aaaaaaaalb"]
        assert cluster["options"][0]["kubernetes_network_config"] == [
            {"pods_cidr": "10.244.0.0/16", "services_cidr": "10.96.0.0/16"}]
        assert cluster["cluster_pod_network_options"] == [{"cni_type": "FLANNEL_OVERLAY"}]
        pool = resources["oci_containerengine_node_pool"]["workers"]
        assert pool["node_shape"] == "VM.Standard.E6.Flex"
        assert pool["node_config_details"][0]["placement_configs"][0]["subnet_id"].endswith("workers")
        assert "ssh_public_key" not in pool

    def test_two_nsgs_no_ssh_no_internet(self, client, admin_token, wi_install):
        resources = self._config(client, admin_token)["resource"]
        assert set(resources["oci_core_network_security_group"]) == {"endpoint", "workers"}
        rules = resources["oci_core_network_security_group_security_rule"]
        for rule in rules.values():
            assert rule.get("source") != "0.0.0.0/0" and rule.get("destination") != "0.0.0.0/0"
            ports = (rule.get("tcp_options") or [{}])[0].get("destination_port_range") or [{}]
            assert ports[0].get("min") != 22
        assert rules["endpoint_kubesight_api"]["source"] == "10.43.11.64/26"
        assert rules["workers_lb_nodeports"]["tcp_options"][0]["destination_port_range"] == [{"min": 30000, "max": 32767}]
        assert rules["workers_nexus"]["destination"] == "10.43.17.16/32"
        assert rules["workers_services"]["destination_type"] == "SERVICE_CIDR_BLOCK"

    def test_tags_are_left_alone_and_owner_expiry_added(self, client, admin_token, wi_install):
        resources = self._config(client, admin_token)["resource"]
        cluster = resources["oci_containerengine_cluster"]["cluster"]
        assert "defined_tags" in cluster["lifecycle"][0]["ignore_changes"]
        assert "options[0].service_lb_config[0].defined_tags" in cluster["lifecycle"][0]["ignore_changes"]
        pool = resources["oci_containerengine_node_pool"]["workers"]
        assert "node_config_details[0].defined_tags" in pool["lifecycle"][0]["ignore_changes"]
        for nsg in resources["oci_core_network_security_group"].values():
            assert nsg["lifecycle"][0]["ignore_changes"] == ["defined_tags"]
        assert cluster["freeform_tags"]["owner"] == "emedawer@areeba.com" and cluster["freeform_tags"]["expiry"]

    def test_cloud_init_bootstraps_then_pins_the_registry(self, client, admin_token, wi_install):
        pool = self._config(client, admin_token)["resource"]["oci_containerengine_node_pool"]["workers"]
        text = base64.b64decode(pool["node_metadata"]["user_data"]).decode()
        lines = text.splitlines()
        assert "oke_init_script" in lines[1] and lines[2] == "bash /var/run/oke-init.sh"
        assert lines[3] == "echo '10.43.17.16      registry.areeba.com' >> /etc/hosts"


# ---------------------------------------------------------------------------
# K3, K13, K19, K22: plan in a Job, a second person approves the hash
# ---------------------------------------------------------------------------

class TestFlow:
    def test_plan_waits_for_a_hash_approval(self, client, admin_token, wi_install, audit_lines):
        account = create_wi_account(client, admin_token)
        build = create_build(client, admin_token, account)
        job = plan(client, admin_token, build["id"])
        assert job["status"] == "awaiting_approval", job.get("error")
        assert job["runner"] == "kubernetes" and len(job["planSha256"]) == 64
        refused = client.post(f"/api/cluster-builds/{build['id']}/provision/jobs/{job['id']}/apply",
                              headers=auth_headers(admin_token))
        assert refused.status_code == 400
        assert any(line["event"] == "plan" and line["planSha256"] == job["planSha256"] for line in audit_lines)

    def test_self_approval_is_refused_even_for_an_admin(self, client, admin_token, wi_install):
        account = create_wi_account(client, admin_token)
        build = create_build(client, admin_token, account)
        job = plan(client, admin_token, build["id"])
        response = approve(client, admin_token, build["id"], job["id"], job["planSha256"])
        assert response.status_code == 403

    def test_approver_needs_mfa(self, app, client, admin_token, wi_install):
        account = create_wi_account(client, admin_token)
        build = create_build(client, admin_token, account)
        job = plan(client, admin_token, build["id"])
        token = second_person(app, mfa=False)
        response = approve(client, token, build["id"], job["id"], job["planSha256"])
        assert response.status_code == 403 and "MFA" in response.get_json()["error"]

    def test_a_changed_plan_is_refused(self, app, client, admin_token, wi_install):
        account = create_wi_account(client, admin_token)
        build = create_build(client, admin_token, account)
        job = plan(client, admin_token, build["id"])
        token = second_person(app)
        response = approve(client, token, build["id"], job["id"], "b" * 64)
        assert response.status_code == 400 and "hash" in response.get_json()["error"]

    def test_an_expired_plan_cannot_be_approved(self, app, client, admin_token, wi_install):
        account = create_wi_account(client, admin_token)
        build = create_build(client, admin_token, account)
        job = plan(client, admin_token, build["id"])
        row = db.session.get(ClusterProvisionJob, job["id"])
        config = dict(row.config_json)
        remote_results = dict(config["remote"])
        remote_results["plan"] = {**remote_results["plan"],
                                  "at": (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()}
        config["remote"] = remote_results
        row.config_json = config
        db.session.commit()
        response = approve(client, second_person(app), build["id"], job["id"], job["planSha256"])
        assert response.status_code == 400 and "too old" in response.get_json()["error"]

    def test_apply_refuses_a_plan_swapped_after_approval(self, app, wi_install):
        job = ClusterProvisionJob(build_id=1, operation="create", status="applying",
                                  config_json={"tofu": {"resource": {}}},
                                  plan_sha256="a" * 64, approved_plan_sha256="a" * 64,
                                  requested_by_user_id=1, approved_by_user_id=2,
                                  approved_at=datetime.now(timezone.utc))
        from api.secret_encryption import encrypt_secret
        job.plan_cipher = encrypt_secret(base64.b64encode(b"another plan").decode())
        assert remote.approval_problem(job) is None
        with pytest.raises(PermissionError, match="does not match"):
            remote.run_spec(job, "apply")

    def test_approved_create_applies_and_reports_facts(self, app, client, admin_token, wi_install, audit_lines):
        account = create_wi_account(client, admin_token)
        build = create_build(client, admin_token, account)
        job = plan(client, admin_token, build["id"])
        response = approve(client, second_person(app), build["id"], job["id"], job["planSha256"])
        assert response.status_code == 200, response.get_json()
        data = response.get_json()["data"]
        assert data["status"] == "completed", (data.get("error"), data["provisioning"]["job"])
        row = db.session.get(ClusterBuild, build["id"])
        facts = row.cloud_facts_json
        assert facts["cluster"]["type"] == "ENHANCED_CLUSTER" and facts["resourceCount"] > 0
        assert state_store.has_resources(build["id"])
        events = [line["event"] for line in audit_lines]
        for event in ("plan-requested", "plan", "approved", "facts"):
            assert event in events, events
        text = json.dumps(audit_lines)
        assert "planB64" not in text and "Bearer" not in text
        done = db.session.get(ClusterProvisionJob, job["id"])
        assert done.plan_cipher is None and done.approved_by == "approver"

    def test_destroy_needs_a_second_person_too(self, app, client, admin_token, wi_install):
        account = create_wi_account(client, admin_token)
        build = create_build(client, admin_token, account, name="oke-02")
        job = plan(client, admin_token, build["id"])
        approver = second_person(app)
        assert approve(client, approver, build["id"], job["id"], job["planSha256"]).status_code == 200
        response = client.post(f"/api/cluster-builds/{build['id']}/provision/destroy",
                               json={"confirmName": "oke-02", "reason": "done"}, headers=auth_headers(admin_token))
        assert response.status_code == 202, response.get_json()
        destroy = response.get_json()["data"]["provisioning"]["job"]
        assert destroy["status"] == "awaiting_approval", destroy.get("error")
        assert approve(client, admin_token, build["id"], destroy["id"], destroy["planSha256"]).status_code == 403
        done = approve(client, approver, build["id"], destroy["id"], destroy["planSha256"])
        assert done.status_code == 200 and done.get_json()["data"]["status"] == "destroyed"
        assert not state_store.has_resources(build["id"])


# ---------------------------------------------------------------------------
# K3, K4, K22: the Job and its sign-in
# ---------------------------------------------------------------------------

class TestJob:
    def test_manifest_is_the_fixed_template(self, app, wi_install):
        manifest = remote.job_manifest(42, "apply", "kubesight-provision-42-apply-1")
        pod = manifest["spec"]["template"]["spec"]
        assert pod["serviceAccountName"] == "kubesight-provisioner" and pod["automountServiceAccountToken"]
        container = pod["containers"][0]
        assert container["image"] == DIGEST_IMAGE
        assert container["command"] == ["/usr/local/bin/kubesight-provision"] and "args" not in container
        assert container["securityContext"]["readOnlyRootFilesystem"] and pod["securityContext"]["runAsNonRoot"]
        assert manifest["spec"]["ttlSecondsAfterFinished"] > 0 and manifest["spec"]["backoffLimit"] == 0
        projected = next(v for v in pod["volumes"] if v["name"] == "backend-token")
        token = projected["projected"]["sources"][0]["serviceAccountToken"]
        assert token["audience"] == "kubesight-backend" and token["expirationSeconds"] == 600
        env = {e["name"]: e["value"] for e in container["env"]}
        assert env["OCI_RESOURCE_PRINCIPAL_VERSION"] == "2.2" and env["KUBESIGHT_PHASE"] == "apply"
        assert not any(k.startswith(("OCI_TENANCY", "OCI_USER", "OCI_FINGERPRINT", "TF_LOG")) for k in env)

    def test_manifest_needs_a_digest(self, app, wi_install, monkeypatch):
        monkeypatch.setenv("KUBESIGHT_PROVISIONER_IMAGE", "registry.areeba.com/kubesight-provisioner:1.0.9")
        with pytest.raises(remote.RemoteError, match="digest"):
            remote.job_manifest(1, "plan", "kubesight-provision-1-plan-1")

    def test_manifest_refuses_other_names(self, app, wi_install):
        with pytest.raises(remote.RemoteError):
            remote.job_manifest(1, "plan", "evil; rm -rf /")

    def _waiting_job(self):
        job = ClusterProvisionJob(build_id=1, operation="create", status="planning", runner="kubernetes",
                                  progress_json={"remote": {"phase": "plan", "plan": {
                                      "k8sJob": "kubesight-provision-7-plan-1", "attempt": 1}}})
        return job

    @pytest.mark.parametrize("info,ok", [
        ({"username": "system:serviceaccount:kubesight:kubesight-provisioner",
          "podName": "kubesight-provision-7-plan-1-x7k2p", "audiences": ["kubesight-backend"]}, True),
        ({"username": "system:serviceaccount:kubesight:kubesight-backend",
          "podName": "kubesight-provision-7-plan-1-x7k2p", "audiences": ["kubesight-backend"]}, False),
        ({"username": "system:serviceaccount:other:kubesight-provisioner",
          "podName": "kubesight-provision-7-plan-1-x7k2p", "audiences": ["kubesight-backend"]}, False),
        ({"username": "system:serviceaccount:kubesight:kubesight-provisioner",
          "podName": "kubesight-proof-provisioner", "audiences": ["kubesight-backend"]}, False),
        (None, False),
    ])
    def test_only_the_waiting_pod_signs_in(self, app, wi_install, info, ok):
        remote.set_token_reviewer(lambda token: info)
        assert (remote.authenticate(self._waiting_job(), "t") is None) is ok

    def test_endpoints_refuse_a_bad_token(self, app, client, wi_install):
        remote.set_token_reviewer(lambda token: None)
        response = client.get("/api/internal/provisioner/runs/1?phase=plan",
                              headers={"Authorization": "Bearer nope"})
        assert response.status_code == 401

    def test_facts_keep_only_the_fixed_list(self):
        facts = remote._clean_facts({
            "resourceCount": 3, "kubeconfig": "secret", "token": "x",
            "cluster": {"id": "ocid1.cluster.oc1..x", "privateEndpoint": {"address": "10.43.20.130", "port": 6443},
                        "token": "x"},
        })
        assert "kubeconfig" not in facts and "token" not in json.dumps(facts)
        assert facts["cluster"]["privateEndpoint"] == {"address": "10.43.20.130", "port": 6443}

    def test_encrypted_state_keeps_the_reported_count(self, app):
        build = ClusterBuild(name="enc", status="draft")
        db.session.add(build)
        db.session.commit()
        state_store.set_resource_count(build.id, 5)
        db.session.commit()
        state_store.write_state(build.id, json.dumps({"encrypted_data": "AAAA", "meta": {}, "encryption_version": "v0"}))
        assert state_store.has_resources(build.id)
        assert state_store.managed_resources(build.id) == []


# ---------------------------------------------------------------------------
# K20, K21: the created cluster
# ---------------------------------------------------------------------------

class TestCreatedCluster:
    def test_kubeconfig_holds_no_token(self):
        text = remote.exec_kubeconfig("oke-01", "https://10.43.20.130:6443", "Q0E=", "ocid1.cluster.oc1.me-dubai-1.x",
                                      "me-dubai-1")
        assert "token" not in text.replace("kubesight-oke-token", "")
        assert "command: kubesight-oke-token" in text and "certificate-authority-data" in text
        assert "insecure-skip-tls-verify" not in text

    def test_backend_is_never_cluster_admin(self, monkeypatch):
        items = remote.bind_manifests("ocid1.workload.subject")
        binding = next(i for i in items if i["kind"] == "ClusterRoleBinding")
        assert binding["roleRef"]["name"] == "kubesight-backend"
        role = next(i for i in items if i["kind"] == "ClusterRole")
        groups = {g for rule in role["rules"] for g in rule["apiGroups"] if "create" in rule["verbs"]}
        assert "admissionregistration.k8s.io" not in groups
        for rule in role["rules"]:
            assert not {"escalate", "bind", "impersonate", "*"} & set(rule["verbs"])
            if "rbac.authorization.k8s.io" in rule["apiGroups"]:
                assert "clusterroles" not in rule["resources"]
        monkeypatch.setenv("KUBESIGHT_OKE_BACKEND_ROLE", "cluster-admin")
        with pytest.raises(remote.RemoteError):
            remote.bind_manifests("x")

    def test_load_balancer_policy_names_every_annotation(self):
        policy = remote.load_balancer_policy()[0]
        expression = policy["spec"]["validations"][0]["expression"]
        for key, value in oci_wi.LB_ANNOTATIONS.items():
            assert f"'{key}'" in expression and f"== '{value}'" in expression
        assert expression.startswith("object.spec.type != 'LoadBalancer' ||")

    def test_onboarding_binds_then_registers_without_a_token(self, app, client, admin_token, wi_install, monkeypatch):
        calls = []

        def api(method, url, body, ca):
            calls.append((method, url))
            if url.endswith("/selfsubjectreviews"):
                return 201, {"status": {"userInfo": {"username": "ocid1.workload.backend"}}}
            return 200, {}

        oke_onboard.set_api(api)
        oke_onboard.set_ca_reader(lambda account, region, cluster_id: "Q0E=")
        registered = {}
        from api.services.cluster_build import onboard as build_onboard
        monkeypatch.setattr(build_onboard, "register_cluster",
                            lambda build, text: registered.setdefault("kubeconfig", text) and "custom-99")
        account = create_wi_account(client, admin_token)
        build = create_build(client, admin_token, account)
        row = db.session.get(ClusterBuild, build["id"])
        row.cloud_facts_json = {"cluster": {"id": "ocid1.cluster.oc1.me-dubai-1.x",
                                            "privateEndpoint": {"address": "10.43.20.130", "port": 6443}}}
        job = ClusterProvisionJob(build_id=row.id, operation="create", status="connecting", runner="kubernetes")
        db.session.add(job)
        db.session.commit()
        assert oke_onboard.onboard(job, row) == "custom-99"
        bind = remote._results(job)["bind"]["spec"]
        assert bind["subject"] == "ocid1.workload.backend"
        assert "token:" not in registered["kubeconfig"]
        assert ("GET", "https://10.43.20.130:6443/api/v1/namespaces/default") in calls


# ---------------------------------------------------------------------------
# The Job's own program, and the exec plugin
# ---------------------------------------------------------------------------

class TestProvisionScript:
    def test_sensitive_values_are_masked(self):
        document = {"format_version": "1.2", "variables": {"x": {"value": "secret"}}, "resource_changes": [{
            "address": "oci_containerengine_node_pool.workers", "type": "oci_containerengine_node_pool",
            "name": "workers", "mode": "managed",
            "change": {"actions": ["create"], "before": None,
                       "after": {"node_metadata": {"user_data": "c2VjcmV0"}, "name": "w"},
                       "after_sensitive": {"node_metadata": {"user_data": True}}},
        }]}
        out = provision.redact_plan(document)
        after = out["resource_changes"][0]["change"]["after"]
        assert after["node_metadata"]["user_data"] == "(sensitive value)" and after["name"] == "w"
        assert "variables" not in out

    def test_facts_from_tofu_show(self):
        document = {"values": {"root_module": {"resources": [
            {"mode": "managed", "type": "oci_containerengine_cluster", "address": "c", "values": {
                "id": "ocid1.cluster.oc1..c", "name": "oke", "kubernetes_version": "v1.36.4",
                "type": "ENHANCED_CLUSTER", "endpoints": [{"private_endpoint": "10.43.20.130:6443"}],
                "endpoint_config": [{"subnet_id": "s"}], "options": [{"service_lb_subnet_ids": ["lb"]}],
                "cluster_pod_network_options": [{"cni_type": "FLANNEL_OVERLAY"}]}},
            {"mode": "managed", "type": "oci_containerengine_node_pool", "address": "p", "values": {
                "id": "np", "node_shape": "VM.Standard.E6.Flex", "node_config_details": [{"size": 2}],
                "node_shape_config": [{"ocpus": 2, "memory_in_gbs": 16}],
                "nodes": [{"id": "n1", "private_ip": "10.43.20.200", "state": "ACTIVE"}]}},
        ]}}}
        facts = provision.facts_from_state(document)
        assert facts["cluster"]["privateEndpoint"] == {"address": "10.43.20.130", "port": 6443}
        assert facts["nodePools"][0]["size"] == 2 and facts["nodePools"][0]["nodeCount"] == 1
        assert facts["resourceCount"] == 2

    def test_tofu_env_drops_keys_and_debug(self, monkeypatch):
        for key in ("OCI_TENANCY_OCID", "OCI_PRIVATE_KEY_PATH", "TF_LOG"):
            monkeypatch.setenv(key, "x")
        env = provision.tofu_env({"buildId": 3, "encryption": {"enabled": True, "address": "https://bao:8200",
                                                               "keyName": "kubesight-tofu-state",
                                                               "caFile": "/etc/openbao-ca/ca.crt"}},
                                 "http://127.0.0.1:9", "bao-token")
        assert "OCI_TENANCY_OCID" not in env and "TF_LOG" not in env and "OCI_PRIVATE_KEY_PATH" not in env
        assert env["TF_HTTP_ADDRESS"] == "http://127.0.0.1:9/api/internal/tofu-state/3"
        assert "TF_HTTP_PASSWORD" not in env
        assert 'key_provider "openbao" "state"' in env["TF_ENCRYPTION"]
        assert env["TF_ENCRYPTION"].count("enforced = true") == 2

    def test_apply_refuses_a_hash_mismatch(self, monkeypatch):
        posted = []

        class FakeBackend:
            def call(self, method, path, body=None, attempts=4):
                posted.append(path)
                return {}

        spec = {"runId": 1, "buildId": 1, "planB64": base64.b64encode(b"plan").decode(),
                "approvedPlanSha256": "0" * 64, "config": {}}
        with pytest.raises(provision.ProvisionError, match="not the approved one"):
            provision.phase_apply(spec, FakeBackend(), "http://x")
        assert posted == []


class TestExecPlugin:
    def test_token_is_a_presigned_request(self):
        def signer(prepared):
            prepared.headers["authorization"] = 'Signature keyId="ST$abc"'
            prepared.headers["date"] = "Tue, 10 Nov 2026 10:00:00 GMT"

        token = oke_token.make_token("ocid1.cluster.oc1.me-dubai-1.x", "me-dubai-1", signer=signer)
        url = base64.urlsafe_b64decode(token).decode()
        assert url.startswith("https://containerengine.me-dubai-1.oraclecloud.com/cluster_request/ocid1.cluster")
        assert "authorization=Signature" in url and "date=" in url

    def test_credential_and_cache(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(oke_token, "CACHE_DIR", str(tmp_path))
        made = []
        monkeypatch.setattr(oke_token, "make_token", lambda c, r: made.append(1) or "tok")
        args = ["--cluster-id", "ocid1.cluster.oc1.me-dubai-1.x", "--region", "me-dubai-1"]
        assert oke_token.main(args) == 0 and oke_token.main(args) == 0
        lines = capsys.readouterr().out.strip().splitlines()
        credential = json.loads(lines[-1])
        assert credential["kind"] == "ExecCredential" and credential["status"]["token"] == "tok"
        assert len(made) == 1
        cached = next(tmp_path.iterdir())
        if os.name != "nt":
            assert (cached.stat().st_mode & 0o777) == 0o600

    def test_bad_arguments(self):
        assert oke_token.main(["--cluster-id", "x; rm", "--region", "me-dubai-1"]) == 2


# ---------------------------------------------------------------------------
# K4: nothing reaches the provisioner through KubeSight
# ---------------------------------------------------------------------------

class TestProvisionerGuard:
    @pytest.fixture(autouse=True)
    def _active(self, monkeypatch):
        monkeypatch.setenv("KUBESIGHT_PROVISIONER_SERVICE_ACCOUNT", "kubesight-provisioner")
        monkeypatch.setenv("KUBESIGHT_PROVISIONER_NAMESPACE", "kubesight")

    @pytest.mark.parametrize("args", [
        ["exec", "kubesight-provision-4-apply-1-abcde", "-n", "kubesight", "--", "sh"],
        ["create", "token", "kubesight-provisioner", "-n", "kubesight"],
        ["apply", "-f", "x.yaml", "--namespace=kubesight"],
        ["get", "secrets", "-n", "kubesight"],
        ["delete", "pod", "x", "-nkubesight"],
        ["debug", "node/x", "-n", "kubesight"],
    ])
    def test_own_cluster_namespace_is_read_only(self, args):
        from api.provisioner_guard import ProvisionerGuardError, check_kubectl

        with pytest.raises(ProvisionerGuardError):
            check_kubectl(args, kubeconfig_path=None)

    @pytest.mark.parametrize("args,path", [
        (["get", "pods", "-n", "kubesight"], None),
        (["logs", "kubesight-backend-0", "-n", "kubesight"], None),
        (["exec", "x", "-n", "kubesight", "--", "sh"], "/var/lib/kubesight/kubeconfigs/custom-3.enc"),
        (["scale", "deploy/web", "--replicas=2", "-n", "payments"], None),
    ])
    def test_everything_else_passes(self, args, path):
        from api.provisioner_guard import check_kubectl

        check_kubectl(args, kubeconfig_path=path)

    def test_a_manifest_running_as_the_provisioner_is_refused_anywhere(self, tmp_path):
        from api.provisioner_guard import ProvisionerGuardError, check_kubectl

        manifest = tmp_path / "job.yaml"
        manifest.write_text(
            "apiVersion: batch/v1\nkind: Job\nmetadata: {name: x, namespace: payments}\n"
            "spec:\n  template:\n    spec:\n      serviceAccountName: kubesight-provisioner\n"
            "      containers: [{name: c, image: busybox}]\n", encoding="utf-8")
        with pytest.raises(ProvisionerGuardError):
            check_kubectl(["apply", "-f", str(manifest)], kubeconfig_path="/some/other/cluster")

    def test_helm_changes_in_the_namespace_are_refused(self):
        from api.provisioner_guard import ProvisionerGuardError, check_helm

        with pytest.raises(ProvisionerGuardError):
            check_helm("kubesight", kubeconfig_path=None)
        check_helm("kubesight", kubeconfig_path="/other")
        check_helm("payments", kubeconfig_path=None)

    def test_inactive_without_the_provisioner(self, monkeypatch):
        from api.provisioner_guard import check_kubectl

        monkeypatch.delenv("KUBESIGHT_PROVISIONER_SERVICE_ACCOUNT")
        check_kubectl(["exec", "x", "-n", "kubesight"], kubeconfig_path=None)



# ---------------------------------------------------------------------------
# What the security review asked for
# ---------------------------------------------------------------------------

class TestReviewFixes:
    def test_plain_state_is_refused_for_real_builds(self, app, client, admin_token, wi_install, monkeypatch):
        monkeypatch.delenv("KUBESIGHT_ALLOW_PLAIN_STATE")
        account = create_wi_account(client, admin_token)
        build = create_build(client, admin_token, account)
        job = plan(client, admin_token, build["id"])
        assert job["status"] == "plan_failed" and "K15" in job["error"]

    def test_an_apply_is_never_launched_twice(self, app, wi_install):
        job = ClusterProvisionJob(build_id=1, operation="create", status="applying", runner="kubernetes",
                                  progress_json={"remote": {"phase": "apply", "apply": {
                                      "k8sJob": "kubesight-provision-1-apply-1", "attempt": 1, "finished": True}}},
                                  config_json={"remote": {"apply": {"result": "ok"}}})
        db.session.add(job)
        db.session.commit()

        class Boom(remote.SimulatedLauncher):
            def launch(self, *a, **k):
                raise AssertionError("launched a second apply")

        remote.set_launcher(Boom())
        assert remote.run_phase(job.id, "apply", lambda line: None, 60)["result"] == "ok"
        job.config_json = {"remote": {"apply": {"result": "failed", "message": "quota"}}}
        db.session.commit()
        with pytest.raises(remote.RemoteError, match="already ran"):
            remote.run_phase(job.id, "apply", lambda line: None, 60)

    def test_no_token_review_unless_a_step_waits(self, app, wi_install):
        calls = []
        remote.set_token_reviewer(lambda token: calls.append(token))
        job = ClusterProvisionJob(build_id=1, operation="create", status="awaiting_approval", runner="kubernetes",
                                  progress_json={"remote": {"phase": "plan", "plan": {
                                      "k8sJob": "kubesight-provision-1-plan-1", "finished": True}}})
        assert remote.authenticate(job, "spam") is not None
        assert calls == []

    def test_approval_age_counts_from_the_plan(self, app, wi_install):
        old = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
        job = ClusterProvisionJob(plan_sha256="a" * 64, approved_plan_sha256="a" * 64, plan_cipher="x",
                                  requested_by_user_id=1, approved_by_user_id=2,
                                  approved_at=datetime.now(timezone.utc),
                                  config_json={"remote": {"plan": {"at": old}}})
        assert "expired" in remote.approval_problem(job)

    def test_unknown_requester_cannot_approve_by_name(self, app, wi_install):
        job = ClusterProvisionJob(plan_sha256="a" * 64, approved_plan_sha256="a" * 64, plan_cipher="x",
                                  requested_by="hermes-agent", approved_by="hermes-agent", approved_by_user_id=5,
                                  approved_at=datetime.now(timezone.utc))
        assert "cannot approve" in remote.approval_problem(job)

    def test_created_cluster_limits_the_backend(self):
        items = remote.bind_manifests("ocid1.workload.backend")
        limits = next(i for i in items if i["metadata"]["name"] == remote.BACKEND_LIMITS
                      and i["kind"] == "ValidatingAdmissionPolicy")
        assert "'ocid1.workload.backend'" in limits["spec"]["matchConditions"][0]["expression"]
        assert "kube-system" in limits["spec"]["validations"][0]["expression"]
        with pytest.raises(remote.RemoteError):
            remote.bind_manifests("x' || true || '")


class TestGuardHoles:
    @pytest.fixture(autouse=True)
    def _active(self, monkeypatch):
        monkeypatch.setenv("KUBESIGHT_PROVISIONER_SERVICE_ACCOUNT", "kubesight-provisioner")
        monkeypatch.setenv("KUBESIGHT_PROVISIONER_NAMESPACE", "kubesight")

    @pytest.mark.parametrize("args", [
        ["get", "secrets", "-A"],
        ["get", "-n", "kubesight", "secret", "x"],
        ["get", "secrets.v1", "-n", "kubesight"],
        ["get", "pods,secrets", "--all-namespaces"],
        ["delete", "pod", "x", "-n=kubesight"],
        ["delete", "pod", "x", "-n", "default", "-n", "kubesight"],
    ])
    def test_refused(self, args):
        from api.provisioner_guard import ProvisionerGuardError, check_kubectl

        with pytest.raises(ProvisionerGuardError):
            check_kubectl(args, kubeconfig_path=None)

    def test_unreadable_or_unparsable_manifests_are_refused(self, tmp_path):
        from api.provisioner_guard import ProvisionerGuardError, check_kubectl

        with pytest.raises(ProvisionerGuardError):
            check_kubectl(["apply", "-f", str(tmp_path / "missing.yaml")], kubeconfig_path="/x")
        bad = tmp_path / "bad.yaml"
        bad.write_text("a: [unclosed", encoding="utf-8")
        with pytest.raises(ProvisionerGuardError):
            check_kubectl(["apply", "-f", str(bad)], kubeconfig_path="/x")

    def test_token_secret_for_the_provisioner_is_refused(self):
        from api.provisioner_guard import ProvisionerGuardError, check_manifest_text

        with pytest.raises(ProvisionerGuardError):
            check_manifest_text(
                "apiVersion: v1\nkind: Secret\ntype: kubernetes.io/service-account-token\nmetadata:\n"
                "  name: t\n  annotations: {kubernetes.io/service-account.name: kubesight-provisioner}\n",
                self_cluster=False)

    def test_helm_renders_go_through_the_guard(self, tmp_path, monkeypatch):
        import subprocess
        from api.provisioner_guard import helm_post_renderer_args

        assert helm_post_renderer_args(["upgrade", "r", "c", "-n", "payments"], kubeconfig_path=None) == [
            "--post-renderer", "kubesight-helm-guard"]
        assert helm_post_renderer_args(["list"], kubeconfig_path=None) == []
        script = Path(__file__).resolve().parents[1] / "kubesight_helm_guard.py"
        env = {**os.environ, "KUBESIGHT_GUARD_SELF_CLUSTER": "1"}
        sneaky = "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: x, namespace: kubesight}\n"
        done = subprocess.run([sys.executable, str(script)], input=sneaky, text=True, capture_output=True, env=env)
        assert done.returncode == 1 and "kubesight" in done.stderr
        fine = "apiVersion: v1\nkind: ConfigMap\nmetadata: {name: x, namespace: payments}\n"
        done = subprocess.run([sys.executable, str(script)], input=fine, text=True, capture_output=True, env=env)
        assert done.returncode == 0 and done.stdout == fine


def test_migration_run_is_not_enforced_but_still_writes_encrypted():
    """OpenTofu refuses enforced + an unencrypted fallback (1.13.9): the one
    migration run drops enforced on state, keeps aes_gcm as the method."""
    text = provision.encryption_config({"keyName": "kubesight-tofu-state", "migrateFromPlain": True})
    state = text.split("state {", 1)[1].split("plan {", 1)[0]
    assert "method   = method.aes_gcm.state" in state and "fallback" in state and "enforced" not in state
    assert text.split("plan {", 1)[1].count("enforced = true") == 1
    normal = provision.encryption_config({"keyName": "kubesight-tofu-state"})
    assert normal.count("enforced = true") == 2 and "unencrypted" not in normal
