"""Bringing workloads from an existing cluster into a built one.

Three things are worth pinning down and nothing else really is:
  * a picked Deployment travels with what it needs, and with nothing that
    belongs to the source cluster (uid, clusterIP, nodePort, bound volume);
  * a missing image is reported per workload and never blocks — the operator
    removes those workloads or acknowledges them;
  * the copy runs as the last build phase, and a workload that cannot become
    ready does not fail an otherwise good cluster.
"""

from __future__ import annotations

import json

import pytest

from api.db import db
from api.models import ClusterBuild, RegistryConnection
from api.services import registry_client
from api.services.cluster_build import service as svc
from api.services.cluster_build import workloads as wl
from api.services.ssh import set_transport_factory

from tests.test_cluster_builds import (
    SINGLE_CP_NODES,
    auth_headers,
    build_default_fake,
    create_build,
    make_build_payload,
    run_full_build,
)
from tests.test_cluster_builds import (  # noqa: F401 — fixtures resolved by name
    fake_ssh,
    no_network_cni_manifest,
    ssh_profile,
)

SOURCE = "custom-9"


# ---------------------------------------------------------------------------
# A fake source cluster
# ---------------------------------------------------------------------------

def deployment(name="api", namespace="core", *, image="registry.local/team/api:1.4"):
    return {
        "apiVersion": "apps/v1",
        "kind": "Deployment",
        "metadata": {
            "name": name,
            "namespace": namespace,
            "uid": "0d1c-uid",
            "resourceVersion": "84213",
            "generation": 7,
            "creationTimestamp": "2026-01-02T03:04:05Z",
            "managedFields": [{"manager": "kubectl"}],
            "annotations": {
                "deployment.kubernetes.io/revision": "3",
                "kubectl.kubernetes.io/last-applied-configuration": "{}",
                "team": "payments",
            },
            "labels": {"app": name},
        },
        "spec": {
            "replicas": 2,
            "selector": {"matchLabels": {"app": name}},
            "template": {
                "metadata": {"labels": {"app": name}},
                "spec": {
                    "serviceAccountName": "api-sa",
                    "imagePullSecrets": [{"name": "regcred"}],
                    "volumes": [
                        {"name": "cfg", "configMap": {"name": "api-config"}},
                        {"name": "data", "persistentVolumeClaim": {"claimName": "api-data"}},
                    ],
                    "containers": [{
                        "name": "api",
                        "image": image,
                        "envFrom": [{"secretRef": {"name": "api-env"}}],
                        "env": [{
                            "name": "OTHER",
                            "valueFrom": {"configMapKeyRef": {"name": "api-config", "key": "k"}},
                        }],
                    }],
                    "initContainers": [
                        {"name": "wait", "image": "registry.local/tools/wait:2"}
                    ],
                },
            },
        },
        "status": {"readyReplicas": 2},
    }


def cronjob(name="nightly", namespace="core"):
    return {
        "apiVersion": "batch/v1",
        "kind": "CronJob",
        "metadata": {"name": name, "namespace": namespace, "uid": "cj"},
        "spec": {
            "schedule": "0 2 * * *",
            "jobTemplate": {"spec": {"template": {"spec": {"containers": [
                {"name": "job", "image": "registry.local/team/batch:9"}
            ]}}}},
        },
        "status": {},
    }


def support_objects(namespace="core"):
    return [
        {"apiVersion": "v1", "kind": "ServiceAccount",
         "metadata": {"name": "api-sa", "namespace": namespace, "uid": "sa"},
         "secrets": [{"name": "api-sa-token-xyz"}]},
        {"apiVersion": "v1", "kind": "ServiceAccount",
         "metadata": {"name": "default", "namespace": namespace}},
        {"apiVersion": "v1", "kind": "Secret", "type": "kubernetes.io/dockerconfigjson",
         "metadata": {"name": "regcred", "namespace": namespace},
         "data": {".dockerconfigjson": "e30="}},
        {"apiVersion": "v1", "kind": "Secret", "type": "Opaque",
         "metadata": {"name": "api-env", "namespace": namespace},
         "data": {"TOKEN": "c2hoaA=="}},
        {"apiVersion": "v1", "kind": "Secret",
         "type": "kubernetes.io/service-account-token",
         "metadata": {"name": "api-sa-token-xyz", "namespace": namespace},
         "data": {"token": "bm9wZQ=="}},
        {"apiVersion": "v1", "kind": "Secret", "type": "helm.sh/release.v1",
         "metadata": {"name": "sh.helm.release.v1.api.v3", "namespace": namespace},
         "data": {"release": "eA=="}},
        {"apiVersion": "v1", "kind": "ConfigMap",
         "metadata": {"name": "api-config", "namespace": namespace},
         "data": {"k": "v"}},
        {"apiVersion": "v1", "kind": "ConfigMap",
         "metadata": {"name": "kube-root-ca.crt", "namespace": namespace},
         "data": {"ca.crt": "x"}},
        {"apiVersion": "v1", "kind": "ConfigMap",
         "metadata": {"name": "unrelated", "namespace": namespace}, "data": {}},
        {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
         "metadata": {"name": "api-data", "namespace": namespace,
                      "annotations": {"pv.kubernetes.io/bind-completed": "yes"}},
         "spec": {"volumeName": "pvc-3f2a", "storageClassName": "fast",
                  "resources": {"requests": {"storage": "5Gi"}}},
         "status": {"phase": "Bound"}},
        {"apiVersion": "v1", "kind": "Service",
         "metadata": {"name": "api", "namespace": namespace},
         "spec": {"selector": {"app": "api"}, "type": "NodePort",
                  "clusterIP": "10.96.4.5", "clusterIPs": ["10.96.4.5"],
                  "ports": [{"port": 80, "targetPort": 8080, "nodePort": 31234}]},
         "status": {"loadBalancer": {}}},
        {"apiVersion": "v1", "kind": "Service",
         "metadata": {"name": "other", "namespace": namespace},
         "spec": {"selector": {"app": "other"}, "clusterIP": "10.96.9.9"}},
        {"apiVersion": "networking.k8s.io/v1", "kind": "Ingress",
         "metadata": {"name": "api-ing", "namespace": namespace},
         "spec": {"rules": [{"host": "api.example.com", "http": {"paths": [
             {"path": "/", "pathType": "Prefix",
              "backend": {"service": {"name": "api", "port": {"number": 80}}}}
         ]}}]}},
    ]


@pytest.fixture()
def source_cluster(monkeypatch):
    """A stand-in for kubectl against the source cluster.

    Records every call so "one support read per namespace" is testable.
    """
    state = {"calls": [], "workloads": [deployment(), cronjob()],
             "support": support_objects(), "pvs": [], "pods": []}

    def fake_kubectl_json(cluster_id, args, timeout=60):
        state["calls"].append((cluster_id, list(args)))
        if cluster_id != SOURCE:
            raise wl.WorkloadSourceError(f"Cluster '{cluster_id}' is not connected.")
        verb, resources = args[0], args[1]
        if verb != "get":
            raise AssertionError(f"unexpected kubectl verb {verb}")
        if "namespaces" == resources:
            return {"items": [{"metadata": {"name": "core"}}]}
        if resources == "persistentvolumes":
            return {"items": list(state["pvs"])}
        if resources == "pods":
            return {"items": list(state["pods"])}
        wanted = set(resources.split(","))
        if wanted & set(wl.WORKLOAD_KINDS.values()):
            named = args[2] if len(args) > 2 and not args[2].startswith("-") else None
            items = [
                doc for doc in state["workloads"]
                if wl.WORKLOAD_KINDS[doc["kind"]] in wanted
            ]
            if named:
                match = [
                    doc for doc in items
                    if doc["metadata"]["name"] == named
                ]
                if not match:
                    raise wl.WorkloadSourceError(
                        f'Error from server (NotFound): "{named}" not found'
                    )
                return match[0]
            return {"items": items}
        return {"items": list(state["support"])}

    monkeypatch.setattr(wl, "_kubectl_json", fake_kubectl_json)
    monkeypatch.setattr(wl, "_check_access", lambda *a, **k: None)
    return state


@pytest.fixture()
def registry(app):
    row = RegistryConnection(
        name="nexus", base_url="nexus.example.com:8083",
        image_hosts="registry.local", username="svc", enabled=True,
        enforcement="block",
    )
    db.session.add(row)
    db.session.commit()
    return row


@pytest.fixture()
def registry_has(monkeypatch):
    """Control which images the registry claims to hold."""
    present = {"present": set(), "all": True}

    def check_manifest(base_url, repository, reference, **kwargs):
        if present["all"] or f"{repository}:{reference}" in present["present"]:
            return registry_client.FOUND, "Image found."
        return registry_client.NOT_FOUND, "Manifest not found."

    monkeypatch.setattr(registry_client, "check_manifest", check_manifest)
    return present


def selection(items=None, *, registry_id=None):
    payload = {
        "sourceClusterId": SOURCE,
        "sourceClusterName": "areeba-prod-01",
        "items": items if items is not None
        else [{"namespace": "core", "kind": "Deployment", "name": "api"}],
    }
    if registry_id:
        payload["registryConnectionId"] = registry_id
    return payload


def by_kind(documents):
    out = {}
    for doc in documents:
        out.setdefault(doc["kind"], []).append(doc)
    return out


# ---------------------------------------------------------------------------
# Selection validation
# ---------------------------------------------------------------------------

class TestNormalizeSelection:
    def test_none_for_nothing_selected(self):
        assert wl.normalize_selection(None) is None
        assert wl.normalize_selection({"sourceClusterId": SOURCE, "items": []}) is None

    def test_requires_a_source_when_items_are_given(self):
        with pytest.raises(ValueError, match="sourceClusterId"):
            wl.normalize_selection({"items": [{"namespace": "core", "kind": "Namespace"}]})

    def test_rejects_kinds_it_cannot_copy(self):
        with pytest.raises(ValueError, match="Cannot copy kind"):
            wl.normalize_selection(selection(
                [{"namespace": "core", "kind": "Pod", "name": "x"}]
            ))

    def test_named_workload_needs_a_name(self):
        with pytest.raises(ValueError, match="needs a name"):
            wl.normalize_selection(selection(
                [{"namespace": "core", "kind": "Deployment"}]
            ))

    def test_whole_namespace_subsumes_individual_picks(self):
        result = wl.normalize_selection(selection([
            {"namespace": "core", "kind": "Deployment", "name": "api"},
            {"namespace": "core", "kind": "Namespace"},
            {"namespace": "web", "kind": "Deployment", "name": "ui"},
        ]))
        assert result["items"] == [
            {"namespace": "core", "kind": "Namespace", "name": ""},
            {"namespace": "web", "kind": "Deployment", "name": "ui"},
        ]

    def test_duplicates_collapse(self):
        result = wl.normalize_selection(selection([
            {"namespace": "core", "kind": "Deployment", "name": "api"},
            {"namespace": "core", "kind": "Deployment", "name": "api"},
        ]))
        assert len(result["items"]) == 1

    def test_refuses_an_absurd_selection(self):
        with pytest.raises(ValueError, match="at most"):
            wl.normalize_selection(selection([
                {"namespace": f"ns{i}", "kind": "Namespace"}
                for i in range(wl.MAX_ITEMS + 1)
            ]))

    def test_replacing_a_selection_keeps_what_already_landed(self):
        existing = {"items": [], "applied": [{"at": "then", "namespaces": ["core"]}]}
        replaced = wl.replace_selection(existing, selection())
        assert replaced["applied"] == existing["applied"]
        # Clearing the selection still keeps the history.
        cleared = wl.replace_selection(existing, None)
        assert cleared["items"] == [] and cleared["applied"]


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------

class TestExportPickedWorkload:
    def test_brings_everything_the_pod_spec_references(self, app, source_cluster):
        export = wl.export_selection(wl.normalize_selection(selection()))
        kinds = by_kind(export.documents)
        assert [doc["metadata"]["name"] for doc in kinds["Deployment"]] == ["api"]
        assert {doc["metadata"]["name"] for doc in kinds["ConfigMap"]} == {"api-config"}
        assert {doc["metadata"]["name"] for doc in kinds["Secret"]} == {"regcred", "api-env"}
        assert {doc["metadata"]["name"] for doc in kinds["ServiceAccount"]} == {"api-sa"}
        assert {doc["metadata"]["name"] for doc in kinds["PersistentVolumeClaim"]} == {"api-data"}
        # The Service that selects it, and the Ingress that routes to it.
        assert {doc["metadata"]["name"] for doc in kinds["Service"]} == {"api"}
        assert {doc["metadata"]["name"] for doc in kinds["Ingress"]} == {"api-ing"}
        assert kinds["Namespace"][0]["metadata"]["name"] == "core"
        # Not referenced, not copied.
        assert "unrelated" not in {
            doc["metadata"]["name"] for doc in kinds.get("ConfigMap", [])
        }

    def test_skips_secrets_that_belong_to_the_source_cluster(self, app, source_cluster):
        export = wl.export_selection(wl.normalize_selection(selection()))
        names = {doc["metadata"]["name"] for doc in export.documents}
        assert "api-sa-token-xyz" not in names
        assert "sh.helm.release.v1.api.v3" not in names
        assert "kube-root-ca.crt" not in names
        assert "default" not in {
            doc["metadata"]["name"] for doc in export.documents
            if doc["kind"] == "ServiceAccount"
        }

    def test_strips_everything_the_source_cluster_assigned(self, app, source_cluster):
        export = wl.export_selection(wl.normalize_selection(selection()))
        kinds = by_kind(export.documents)
        deploy = kinds["Deployment"][0]
        assert "status" not in deploy
        for key in ("uid", "resourceVersion", "generation", "creationTimestamp",
                    "managedFields"):
            assert key not in deploy["metadata"]
        annotations = deploy["metadata"]["annotations"]
        assert annotations == {"team": "payments"}

        service = kinds["Service"][0]
        assert "clusterIP" not in service["spec"] and "clusterIPs" not in service["spec"]
        assert "nodePort" not in service["spec"]["ports"][0]
        assert service["spec"]["type"] == "NodePort"

        pvc = kinds["PersistentVolumeClaim"][0]
        assert "volumeName" not in pvc["spec"]
        assert pvc["spec"]["storageClassName"] == "fast"
        assert "annotations" not in pvc["metadata"]

        sa = kinds["ServiceAccount"][0]
        assert "secrets" not in sa

    def test_serviceaccount_token_secrets_are_dropped_from_the_account(
        self, app, source_cluster
    ):
        export = wl.export_selection(wl.normalize_selection(selection()))
        payload = wl.to_yaml(export.documents)
        assert "api-sa-token-xyz" not in payload

    def test_apply_order_puts_data_before_the_things_that_mount_it(
        self, app, source_cluster
    ):
        export = wl.export_selection(wl.normalize_selection(selection()))
        order = [doc["kind"] for doc in export.documents]
        assert order.index("Namespace") == 0
        assert order.index("Secret") < order.index("Deployment")
        assert order.index("ConfigMap") < order.index("Deployment")
        assert order.index("Service") < order.index("Deployment")
        assert order.index("Deployment") < order.index("Ingress")

    def test_images_include_init_containers(self, app, source_cluster):
        export = wl.export_selection(wl.normalize_selection(selection()))
        # Init containers first — the order pods actually pull in.
        assert export.images == [
            "registry.local/tools/wait:2", "registry.local/team/api:1.4",
        ]

    def test_a_vanished_pick_is_reported_not_fatal(self, app, source_cluster):
        export = wl.export_selection(wl.normalize_selection(selection([
            {"namespace": "core", "kind": "Deployment", "name": "api"},
            {"namespace": "core", "kind": "Deployment", "name": "ghost"},
        ])))
        assert export.missing == ["core/Deployment ghost"]
        assert len(export.workloads) == 1

    def test_a_missing_reference_becomes_a_warning(self, app, source_cluster):
        source_cluster["support"] = [
            doc for doc in source_cluster["support"]
            if doc["metadata"]["name"] != "api-config"
        ]
        export = wl.export_selection(wl.normalize_selection(selection()))
        assert any("api-config" in warning for warning in export.warnings)

    def test_the_service_caveat_is_stated(self, app, source_cluster):
        export = wl.export_selection(wl.normalize_selection(selection()))
        joined = " ".join(export.warnings)
        assert "node ports" in joined
        # Whether the claims have anywhere to bind is the storage plan's answer,
        # per claim — a blanket "they will stay Pending" here would be wrong the
        # moment somebody points them at an NFS export.
        assert "StorageClass" not in joined

    def test_support_objects_are_read_once_per_namespace(self, app, source_cluster):
        wl.export_selection(wl.normalize_selection(selection([
            {"namespace": "core", "kind": "Deployment", "name": "api"},
            {"namespace": "core", "kind": "CronJob", "name": "nightly"},
        ])))
        support_reads = [
            call for call in source_cluster["calls"]
            if "configmaps" in call[1][1]
        ]
        assert len(support_reads) == 1


class TestExportWholeNamespace:
    def test_brings_every_workload_kind_and_its_configuration(self, app, source_cluster):
        export = wl.export_selection(wl.normalize_selection(selection(
            [{"namespace": "core", "kind": "Namespace"}]
        )))
        kinds = by_kind(export.documents)
        assert {w["kind"] for w in export.workloads} == {"Deployment", "CronJob"}
        # A whole namespace takes every Service, not only the selected ones.
        assert {doc["metadata"]["name"] for doc in kinds["Service"]} == {"api", "other"}
        assert "unrelated" in {doc["metadata"]["name"] for doc in kinds["ConfigMap"]}
        assert "api-sa-token-xyz" not in {
            doc["metadata"]["name"] for doc in export.documents
        }

    def test_controller_owned_objects_are_left_behind(self, app, source_cluster):
        owned = deployment(name="owned")
        owned["metadata"]["ownerReferences"] = [{"kind": "ReplicaSet", "name": "rs"}]
        source_cluster["workloads"].append(owned)
        export = wl.export_selection(wl.normalize_selection(selection(
            [{"namespace": "core", "kind": "Namespace"}]
        )))
        assert "owned" not in {w["name"] for w in export.workloads}


# ---------------------------------------------------------------------------
# Image availability
# ---------------------------------------------------------------------------

class TestImageAvailability:
    def test_asks_the_selected_registry_even_for_another_host(
        self, app, source_cluster, registry, monkeypatch
    ):
        asked = []

        def check_manifest(base_url, repository, reference, **kwargs):
            asked.append((base_url, repository, reference))
            return registry_client.FOUND, "Image found."

        monkeypatch.setattr(registry_client, "check_manifest", check_manifest)
        result = wl.plan(selection(registry_id=registry.id))
        assert asked == [
            ("nexus.example.com:8083", "tools/wait", "2"),
            ("nexus.example.com:8083", "team/api", "1.4"),
        ]
        assert result["counts"]["missingImages"] == 0
        assert all(w["imageStatus"] == "ok" for w in result["workloads"])

    def test_missing_images_are_reported_per_workload_and_never_block(
        self, app, source_cluster, registry, registry_has
    ):
        registry_has["all"] = False
        registry_has["present"] = {"tools/wait:2"}
        result = wl.plan(selection(registry_id=registry.id))
        assert result["counts"]["missingImages"] == 1
        assert result["missingWorkloads"] == [{
            "namespace": "core", "kind": "Deployment", "name": "api",
            "missingImages": ["registry.local/team/api:1.4"],
        }]
        # Nothing in the payload says "blocked" — the enforcement=block on the
        # connection deliberately does not apply to copying what already runs.
        assert "blocking" not in result

    def test_no_registry_selected_means_not_checked(self, app, source_cluster):
        result = wl.plan(selection())
        assert result["workloads"][0]["imageStatus"] == "not_checked"
        assert result["counts"]["missingImages"] == 0

    def test_registry_options_lists_enabled_connections(self, app, registry):
        assert [row["name"] for row in wl.registry_options()] == ["nexus"]


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

class TestPreflightChecks:
    def _build(self, selection_payload):
        build = ClusterBuild(name="wl", workloads_json=wl.normalize_selection(selection_payload))
        return build

    def test_no_selection_no_checks(self, app):
        assert wl.preflight_checks(ClusterBuild(name="x")) == []

    def test_missing_images_warn_but_never_fail(
        self, app, source_cluster, registry, registry_has
    ):
        registry_has["all"] = False
        checks = wl.preflight_checks(self._build(selection(registry_id=registry.id)))
        images = next(c for c in checks if c["id"] == "workload_images")
        assert images["status"] == "warn"
        assert "core/api" in images["detail"]
        assert not any(check["status"] == "fail" for check in checks)

    def test_unreachable_source_warns_with_the_consequence_spelled_out(self, app, monkeypatch):
        monkeypatch.setattr(wl, "_check_access", lambda *a, **k: None)
        monkeypatch.setattr(
            wl, "_kubectl_json",
            lambda *a, **k: (_ for _ in ()).throw(wl.WorkloadSourceError("boom")),
        )
        checks = wl.preflight_checks(self._build(selection()))
        assert [c["status"] for c in checks] == ["warn"]
        assert "fails the build" in checks[0]["hint"]

    def test_no_registry_selected_warns_that_nothing_was_checked(self, app, source_cluster):
        checks = wl.preflight_checks(self._build(selection()))
        images = next(c for c in checks if c["id"] == "workload_images")
        assert images["status"] == "warn"
        assert "No registry was selected" in images["detail"]

    def test_note_ids_are_stable(self, app, source_cluster):
        first = wl.preflight_checks(self._build(selection()))
        second = wl.preflight_checks(self._build(selection()))
        assert [c["id"] for c in first] == [c["id"] for c in second]


# ---------------------------------------------------------------------------
# The build phase
# ---------------------------------------------------------------------------

HOSTS = {"10.0.0.11": ("cp-1", "control_plane"), "10.0.0.21": ("w-1", "worker")}


def nfs_reachable(fake, server="10.4.1.20"):
    """Let the preflight port probe succeed. Without this the build refuses to
    start, which is the whole point of the check."""
    fake.responders.insert(0, (
        lambda h, s, srv=server: f"/dev/tcp/{srv}/2049" in s, "KS_NFS=open" + chr(10),
    ))


def bound_claims(fake, phase="Bound"):
    """Answer the phase's per-claim bound check. Registered ahead of the
    default fake's generic '{.status.phase}' responder (the smoke pod)."""
    fake.responders.insert(0, (
        lambda h, s: "get pvc" in s and "status.phase" in s, phase,
    ))


def applied_manifests(fake):
    """The YAML uploaded to the primary control plane, decoded."""
    import base64
    import re

    out = {}
    for _, script in fake.calls:
        match = re.search(
            r"echo (\S+) \| base64 -d > (/etc/kubernetes/kubesight-workloads-[^\s]+)",
            script,
        )
        if match:
            out[match.group(2)] = base64.b64decode(match.group(1)).decode("utf-8")
    return out


class TestWorkloadPhase:
    def test_copies_the_selection_and_records_what_landed(
        self, client, admin_token, ssh_profile, fake_ssh, app, source_cluster, registry,
        registry_has,
    ):
        fake = build_default_fake(HOSTS)
        set_transport_factory(lambda: fake)
        data = run_full_build(
            client, admin_token, ssh_profile, fake,
            make_build_payload(
                nodes=SINGLE_CP_NODES,
                workloads=selection(registry_id=registry.id),
            ),
        )
        assert data["status"] == "completed", data.get("error")
        assert "workloads" in {step["phase"] for step in data["steps"]}

        manifests = applied_manifests(fake)
        assert list(manifests) == ["/etc/kubernetes/kubesight-workloads-core.yaml"]
        yaml_text = manifests["/etc/kubernetes/kubesight-workloads-core.yaml"]
        assert "kind: Deployment" in yaml_text
        assert "name: api-config" in yaml_text
        assert "0d1c-uid" not in yaml_text

        # The uploaded manifest can carry Secret data, so it is deleted again.
        upload_call = next(
            script for _, script in fake.calls
            if "kubesight-workloads-core.yaml" in script and "apply" in script
        )
        assert "rm -f /etc/kubernetes/kubesight-workloads-core.yaml" in upload_call

        record = data["workloadSelection"]["result"]
        assert record["namespaces"] == ["core"]
        assert record["workloads"] == [
            {"namespace": "core", "kind": "Deployment", "name": "api"}
        ]
        assert record["notReady"] == []
        assert data["workloads"]["appliedRuns"] == 1

    def test_secret_data_never_reaches_a_step_log(
        self, client, admin_token, ssh_profile, fake_ssh, app, source_cluster
    ):
        fake = build_default_fake(HOSTS)
        set_transport_factory(lambda: fake)
        data = run_full_build(
            client, admin_token, ssh_profile, fake,
            make_build_payload(nodes=SINGLE_CP_NODES, workloads=selection()),
        )
        step = next(s for s in data["steps"] if s["phase"] == "workloads")
        log = client.get(
            f"/api/cluster-builds/{data['id']}/logs", headers=auth_headers(admin_token)
        ).get_json()["data"]["items"]
        tail = next(item["logTail"] for item in log if item["id"] == step["id"])
        assert "c2hoaA==" not in tail          # the Opaque secret's value
        assert "content hidden" in tail        # what the upload logs instead

    def test_a_workload_that_never_becomes_ready_does_not_fail_the_build(
        self, client, admin_token, ssh_profile, fake_ssh, app, source_cluster
    ):
        from api.services.ssh import SshCommandError

        fake = build_default_fake(HOSTS)
        # The copied Deployment's rollout times out; the CNI/CoreDNS rollouts the
        # build itself waits on must still succeed.
        fake.responders.insert(0, (
            lambda h, s: "rollout status deployment/api" in s,
            SshCommandError("timed out waiting for the condition", 1, "0 of 2 updated"),
        ))
        set_transport_factory(lambda: fake)
        data = run_full_build(
            client, admin_token, ssh_profile, fake,
            make_build_payload(nodes=SINGLE_CP_NODES, workloads=selection()),
        )
        assert data["status"] == "completed", data.get("error")
        assert data["workloadSelection"]["result"]["notReady"] == ["core/api"]

    def test_an_unreadable_source_fails_the_phase_with_the_reason(
        self, client, admin_token, ssh_profile, fake_ssh, app, source_cluster, monkeypatch
    ):
        fake = build_default_fake(HOSTS)
        set_transport_factory(lambda: fake)
        build = create_build(
            client, admin_token, ssh_profile,
            make_build_payload(nodes=SINGLE_CP_NODES, workloads=selection()),
        )
        client.post(f"/api/cluster-builds/{build['id']}/preflight",
                    headers=auth_headers(admin_token))
        monkeypatch.setattr(
            wl, "_kubectl_json",
            lambda *a, **k: (_ for _ in ()).throw(wl.WorkloadSourceError("API down")),
        )
        client.post(f"/api/cluster-builds/{build['id']}/start",
                    json={"ackWarnings": ["ack"]}, headers=auth_headers(admin_token))
        data = client.get(f"/api/cluster-builds/{build['id']}",
                          headers=auth_headers(admin_token)).get_json()["data"]
        assert data["status"] == "failed"
        assert "API down" in data["error"]
        # Everything before the copy still happened: the cluster exists.
        assert data["resultClusterId"]

    def test_a_build_without_a_selection_never_runs_the_phase(
        self, client, admin_token, ssh_profile, fake_ssh, app
    ):
        fake = build_default_fake(HOSTS)
        set_transport_factory(lambda: fake)
        data = run_full_build(
            client, admin_token, ssh_profile, fake,
            make_build_payload(nodes=SINGLE_CP_NODES),
        )
        assert "workloads" not in {step["phase"] for step in data["steps"]}
        assert data["workloads"]["itemCount"] == 0


# ---------------------------------------------------------------------------
# Day two
# ---------------------------------------------------------------------------

@pytest.fixture()
def finished_build(client, admin_token, ssh_profile, fake_ssh, app):
    fake = build_default_fake(HOSTS)
    fake.add(
        lambda h, s: "kubeadm token create --print-join-command" in s,
        "kubeadm join 10.0.0.100:6443 --token abcdef.0123456789abcdef "
        "--discovery-token-ca-cert-hash sha256:" + "1" * 64 + "\n",
    )
    set_transport_factory(lambda: fake)
    data = run_full_build(
        client, admin_token, ssh_profile, fake,
        make_build_payload(nodes=SINGLE_CP_NODES),
    )
    assert data["status"] == "completed", data.get("error")
    return {"build": data, "fake": fake}


class TestBringWorkloadsDayTwo:
    def _select(self, client, token, build_id, payload):
        return client.put(
            f"/api/cluster-builds/{build_id}/workloads",
            json={"workloads": payload}, headers=auth_headers(token),
        )

    def test_selects_and_applies_against_a_live_cluster(
        self, client, admin_token, finished_build, source_cluster, registry, registry_has
    ):
        build_id = finished_build["build"]["id"]
        response = self._select(
            client, admin_token, build_id, selection(registry_id=registry.id)
        )
        assert response.status_code == 200, response.get_json()
        assert response.get_json()["data"]["workloads"]["workloadCount"] == 1

        plan = client.get(f"/api/cluster-builds/{build_id}/workload-plan",
                          headers=auth_headers(admin_token)).get_json()["data"]
        assert plan["counts"]["workloads"] == 1

        response = client.post(f"/api/cluster-builds/{build_id}/bring-workloads",
                               json={}, headers=auth_headers(admin_token))
        assert response.status_code == 200, response.get_json()
        data = response.get_json()["data"]
        assert data["status"] == "completed", data.get("error")
        assert data["workloads"]["appliedRuns"] == 1
        assert applied_manifests(finished_build["fake"])

    def test_missing_images_need_an_explicit_acknowledgement(
        self, client, admin_token, finished_build, source_cluster, registry, registry_has
    ):
        registry_has["all"] = False
        build_id = finished_build["build"]["id"]
        self._select(client, admin_token, build_id, selection(registry_id=registry.id))

        response = client.post(f"/api/cluster-builds/{build_id}/bring-workloads",
                               json={}, headers=auth_headers(admin_token))
        assert response.status_code == 400
        assert "ackMissingImages" in response.get_json()["error"]

        response = client.post(
            f"/api/cluster-builds/{build_id}/bring-workloads",
            json={"ackMissingImages": True}, headers=auth_headers(admin_token),
        )
        assert response.status_code == 200, response.get_json()
        data = response.get_json()["data"]
        assert data["status"] == "completed", data.get("error")
        ack = data["workloadSelection"]["imageAck"]
        assert ack["workloads"] == ["core/Deployment/api"]
        assert ack["acknowledgedBy"]

    def test_a_second_run_appends_to_the_history(
        self, client, admin_token, finished_build, source_cluster
    ):
        build_id = finished_build["build"]["id"]
        self._select(client, admin_token, build_id, selection())
        client.post(f"/api/cluster-builds/{build_id}/bring-workloads",
                    json={}, headers=auth_headers(admin_token))
        self._select(client, admin_token, build_id, selection(
            [{"namespace": "core", "kind": "CronJob", "name": "nightly"}]
        ))
        data = client.post(f"/api/cluster-builds/{build_id}/bring-workloads",
                           json={}, headers=auth_headers(admin_token)).get_json()["data"]
        applied = data["workloadSelection"]["applied"]
        assert len(applied) == 2
        assert applied[0]["workloads"][0]["name"] == "api"
        assert applied[1]["workloads"][0]["name"] == "nightly"

    def test_refuses_while_machines_are_queued_to_join(
        self, client, admin_token, finished_build, source_cluster
    ):
        build_id = finished_build["build"]["id"]
        self._select(client, admin_token, build_id, selection())
        client.post(
            f"/api/cluster-builds/{build_id}/nodes",
            json={"nodes": [{"role": "worker", "hostname": "w-2", "address": "10.0.0.22"}]},
            headers=auth_headers(admin_token),
        )
        response = client.post(f"/api/cluster-builds/{build_id}/bring-workloads",
                               json={}, headers=auth_headers(admin_token))
        assert response.status_code == 400
        assert "queued to join" in response.get_json()["error"]

    def test_nothing_selected_is_refused(self, client, admin_token, finished_build):
        response = client.post(
            f"/api/cluster-builds/{finished_build['build']['id']}/bring-workloads",
            json={}, headers=auth_headers(admin_token),
        )
        assert response.status_code == 400
        assert "at least one" in response.get_json()["error"]


# ---------------------------------------------------------------------------
# Pickers
# ---------------------------------------------------------------------------

class TestPickerRoutes:
    def test_namespaces_carry_per_kind_counts(
        self, client, admin_token, app, source_cluster
    ):
        response = client.get(
            f"/api/cluster-builds/workload-sources/{SOURCE}/namespaces",
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 200, response.get_json()
        items = response.get_json()["data"]["items"]
        assert items == [{
            "name": "core", "counts": {"Deployment": 1, "CronJob": 1},
            "total": 2, "system": False,
        }]

    def test_workloads_list_shows_the_images_each_one_runs(
        self, client, admin_token, app, source_cluster
    ):
        data = client.get(
            f"/api/cluster-builds/workload-sources/{SOURCE}/namespaces/core/workloads",
            headers=auth_headers(admin_token),
        ).get_json()["data"]
        assert [item["kind"] for item in data["items"]] == ["Deployment", "CronJob"]
        assert data["items"][0]["images"] == [
            "registry.local/tools/wait:2", "registry.local/team/api:1.4",
        ]
        assert data["items"][1]["schedule"] == "0 2 * * *"

    def test_an_unreachable_source_is_a_502_not_a_crash(
        self, client, admin_token, app, source_cluster
    ):
        response = client.get(
            "/api/cluster-builds/workload-sources/custom-404/namespaces",
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 502
        assert "not connected" in response.get_json()["error"]

    def test_plan_route_answers_before_a_build_exists(
        self, client, admin_token, app, source_cluster, registry, registry_has
    ):
        registry_has["all"] = False
        response = client.post(
            "/api/cluster-builds/workload-plan",
            json=selection(registry_id=registry.id), headers=auth_headers(admin_token),
        )
        assert response.status_code == 200, response.get_json()
        data = response.get_json()["data"]
        assert data["counts"]["workloads"] == 1
        assert len(data["missingWorkloads"]) == 1
        assert data["namespaces"] == ["core"]

    def test_sources_route_lists_registries_alongside_clusters(
        self, client, admin_token, app, registry
    ):
        data = client.get("/api/cluster-builds/workload-sources",
                          headers=auth_headers(admin_token)).get_json()["data"]
        assert [row["name"] for row in data["registries"]] == ["nexus"]
        assert "items" in data


def test_json_column_round_trips_the_selection(app, source_cluster):
    """The selection survives a commit — a JSON column is not mutation-tracked,
    which is exactly the bug this guards."""
    build = ClusterBuild(name="wl", k8s_version="1.32.4",
                         control_plane_endpoint="10.0.0.100:6443")
    build.workloads_json = wl.normalize_selection(selection())
    db.session.add(build)
    db.session.commit()
    db.session.expire_all()
    reloaded = db.session.get(ClusterBuild, build.id)
    assert json.loads(json.dumps(reloaded.workloads_json))["items"][0]["name"] == "api"
    assert svc.serialize_build(reloaded)["workloads"]["workloadCount"] == 1


# ---------------------------------------------------------------------------
# Storage for the copied claims
# ---------------------------------------------------------------------------

from api.services.cluster_build import storage as st  # noqa: E402


def pvc(name="api-data", namespace="core", *, size="5Gi",
        modes=("ReadWriteOnce",), storage_class="fast", selector=True):
    spec = {
        "accessModes": list(modes),
        "resources": {"requests": {"storage": size}},
    }
    if storage_class is not None:
        spec["storageClassName"] = storage_class
    if selector:
        # A selector matches labels on the SOURCE cluster's PVs, so it is a
        # guaranteed Pending claim if it survives the copy.
        spec["selector"] = {"matchLabels": {"volume": "gold"}}
    return {
        "apiVersion": "v1", "kind": "PersistentVolumeClaim",
        "metadata": {"name": name, "namespace": namespace}, "spec": spec,
    }


NFS_ANSWERS = {
    "default": "fresh", "nfsServer": "10.4.1.20", "nfsExportRoot": "/exports/ks",
}


def source_pv(claim="core/api-data", *, nfs=True, capacity="20Gi", driver=None):
    namespace, name = claim.split("/")
    spec = {
        "capacity": {"storage": capacity},
        "accessModes": ["ReadWriteMany"],
        "claimRef": {"kind": "PersistentVolumeClaim",
                     "namespace": namespace, "name": name},
    }
    if nfs:
        spec["nfs"] = {"server": "10.9.9.9", "path": "/vol/prod/api"}
    elif driver:
        spec["csi"] = {"driver": driver}
    else:
        spec["hostPath"] = {"path": "/mnt/data"}
    return {"apiVersion": "v1", "kind": "PersistentVolume",
            "metadata": {"name": f"pv-{name}"}, "spec": spec}


def pv_items(*pvs):
    return lambda cluster_id, args: {"items": list(pvs)}


class TestStorageAnswers:
    def test_nothing_selected_is_none(self):
        assert st.normalize(None) is None
        assert st.normalize({}) is None

    def test_rejects_a_relative_or_escaping_export_root(self):
        for bad in ("exports/ks", "/exports/../etc", "/exports/ks; rm -rf /"):
            with pytest.raises(ValueError, match="export root"):
                st.normalize({**NFS_ANSWERS, "nfsExportRoot": bad})

    def test_rejects_a_bogus_server_or_mount_option(self):
        with pytest.raises(ValueError, match="NFS server"):
            st.normalize({**NFS_ANSWERS, "nfsServer": "10.4.1.20 && reboot"})
        with pytest.raises(ValueError, match="mount option"):
            st.normalize({**NFS_ANSWERS, "nfsMountOptions": "hard,$(id)"})

    def test_splits_mount_options_and_trims_the_export_root(self):
        answers = st.normalize({
            **NFS_ANSWERS, "nfsExportRoot": "/exports/ks/",
            "nfsMountOptions": "nfsvers=4.1,  hard",
        })
        assert answers["nfsExportRoot"] == "/exports/ks"
        assert answers["nfsMountOptions"] == ["nfsvers=4.1", "hard"]

    def test_a_claim_decision_overrides_the_default(self):
        answers = st.normalize({
            **NFS_ANSWERS,
            "claims": {"core/api-data": {"source": "none"},
                       "core/cache": {"source": "reuse", "readOnly": True}},
        })
        assert st.decision_for(answers, "core/api-data")["source"] == "none"
        assert st.decision_for(answers, "core/cache") == {
            "source": "reuse", "readOnly": True}
        # Anything not named takes the copy-wide default.
        assert st.decision_for(answers, "core/other")["source"] == "fresh"

    def test_rejects_an_unknown_decision(self):
        with pytest.raises(ValueError, match="source must be one of"):
            st.normalize({**NFS_ANSWERS, "claims": {"a/b": {"source": "magic"}}})

    def test_needs_nfs_reads_the_decisions_not_the_cluster(self):
        assert st.needs_nfs(NFS_ANSWERS) is True
        assert st.needs_nfs({"default": "none"}) is False
        assert st.needs_nfs(
            {"default": "none", "claims": {"a/b": {"source": "reuse"}}}
        ) is True


class TestResolveClaims:
    def test_fresh_gets_a_path_under_the_export_root(self):
        plan = st.resolve([pvc()], st.normalize(NFS_ANSWERS))[0]
        assert plan.source == "fresh"
        assert plan.server == "10.4.1.20"
        assert plan.path == "/exports/ks/core/api-data"
        assert plan.capacity == "5Gi"          # the claim's request
        assert plan.pv_name == "kubesight-core-api-data"
        assert not plan.error

    def test_fresh_without_a_server_is_an_error_on_the_row(self):
        plans = st.resolve([pvc()], {"default": "fresh"})
        assert "NFS server and export root must be filled in" in plans[0].error
        assert st.errors(plans)
        assert not plans[0].authors_pv

    def test_reuse_takes_the_source_path_and_its_real_size(self):
        index = st.source_volume_index(pv_items(source_pv()), "custom-9")
        plans = st.resolve(
            [pvc()], st.normalize({**NFS_ANSWERS, "default": "reuse"}),
            source_index=index,
        )
        plan = plans[0]
        assert plan.reusable is True
        assert (plan.server, plan.path) == ("10.9.9.9", "/vol/prod/api")
        # The volume's size, not the claim's request: a 5Gi claim on a 20Gi
        # export must not be relabelled 5Gi.
        assert plan.capacity == "20Gi"
        assert plan.source_pv == "pv-api-data"

    def test_reuse_is_refused_when_the_source_volume_is_not_nfs(self):
        index = st.source_volume_index(
            pv_items(source_pv(nfs=False, driver="csi.vsphere.vmware.com")), "custom-9"
        )
        plans = st.resolve(
            [pvc()], st.normalize({**NFS_ANSWERS, "default": "reuse"}),
            source_index=index,
        )
        assert plans[0].reusable is False
        assert "csi.vsphere.vmware.com" in plans[0].reuse_blocked
        assert "cannot reuse" in plans[0].error

    def test_reuse_is_refused_when_the_claim_is_unbound_over_there(self):
        plans = st.resolve(
            [pvc()], st.normalize({**NFS_ANSWERS, "default": "reuse"}),
            source_index={},
        )
        assert "not bound to a volume" in plans[0].reuse_blocked

    def test_class_needs_a_name_from_somewhere(self):
        plans = st.resolve([pvc(storage_class=None)], {"default": "class"})
        assert "no StorageClass name was given" in plans[0].error
        # The claim's own class counts as a name.
        plans = st.resolve([pvc(storage_class="fast")], {"default": "class"})
        assert not plans[0].error and plans[0].storage_class == "fast"
        # An explicit answer wins over the claim's.
        plans = st.resolve(
            [pvc(storage_class="fast")],
            {"default": "class", "storageClassName": "nfs-sc"},
        )
        assert plans[0].storage_class == "nfs-sc"

    def test_none_authors_nothing(self):
        plan = st.resolve([pvc()], {"default": "none"})[0]
        assert plan.authors_pv is False and plan.pv_name == ""

    def test_the_consumer_fsgroup_is_carried_onto_the_claim(self):
        deploy = deployment()
        deploy["spec"]["template"]["spec"]["securityContext"] = {"fsGroup": 2000}
        consumers = st.consumers([deploy])
        assert consumers["core/api-data"]["fsGroup"] == 2000
        assert consumers["core/api-data"]["workloads"] == ["Deployment api"]
        plan = st.resolve(
            [pvc()], st.normalize(NFS_ANSWERS), consumer_index=consumers
        )[0]
        assert plan.fs_group == 2000
        assert plan.workloads == ["Deployment api"]

    def test_summary_counts_what_will_happen(self):
        plans = st.resolve(
            [pvc("a"), pvc("b"), pvc("c")],
            st.normalize({
                **NFS_ANSWERS,
                "claims": {"core/b": {"source": "none"},
                           "core/c": {"source": "class"}},
                "storageClassName": "sc",
            }),
        )
        summary = st.summarize(plans)
        assert summary["claims"] == 3
        assert summary["volumesToCreate"] == 1
        assert summary["pending"] == 1


class TestAuthoredVolumes:
    def _plan(self, answers=None, **kwargs):
        return st.resolve([pvc()], st.normalize(answers or NFS_ANSWERS), **kwargs)[0]

    def test_the_pv_pre_binds_to_exactly_one_claim(self):
        doc = st.pv_document(self._plan(), source_cluster="custom-9")
        assert doc["kind"] == "PersistentVolume"
        assert doc["spec"]["claimRef"] == {
            "apiVersion": "v1", "kind": "PersistentVolumeClaim",
            "namespace": "core", "name": "api-data",
        }
        assert doc["spec"]["nfs"] == {
            "server": "10.4.1.20", "path": "/exports/ks/core/api-data"}
        assert doc["metadata"]["annotations"]["kubesight.io/copied-from"] == "custom-9"

    def test_reclaim_policy_is_always_retain(self):
        # Deleting a copied claim must never be able to delete NFS data.
        index = st.source_volume_index(pv_items(source_pv()), "custom-9")
        for source in ("fresh", "reuse"):
            plan = self._plan({**NFS_ANSWERS, "default": source}, source_index=index)
            doc = st.pv_document(plan)
            assert doc["spec"]["persistentVolumeReclaimPolicy"] == "Retain"

    def test_storage_class_is_empty_string_not_absent(self):
        # Absent means "use the default class", which would provision instead of
        # taking the volume we just made.
        doc = st.pv_document(self._plan())
        assert doc["spec"]["storageClassName"] == ""

    def test_read_only_reaches_the_mount(self):
        index = st.source_volume_index(pv_items(source_pv()), "custom-9")
        plan = st.resolve(
            [pvc()],
            st.normalize({**NFS_ANSWERS, "default": "reuse",
                          "claims": {"core/api-data": {"source": "reuse",
                                                       "readOnly": True}}}),
            source_index=index,
        )[0]
        assert st.pv_document(plan)["spec"]["nfs"]["readOnly"] is True

    def test_mount_options_travel(self):
        plan = self._plan({**NFS_ANSWERS, "nfsMountOptions": "nfsvers=4.1,hard"})
        assert st.pv_document(plan)["spec"]["mountOptions"] == ["nfsvers=4.1", "hard"]

    def test_the_claim_is_repointed_and_its_selector_dropped(self):
        plan = self._plan()
        rewritten = st.rewrite_claim(pvc(), plan)["spec"]
        assert rewritten["volumeName"] == "kubesight-core-api-data"
        assert rewritten["storageClassName"] == ""
        assert "selector" not in rewritten

    def test_a_class_claim_keeps_no_volume_name(self):
        plan = st.resolve([pvc()], {"default": "class", "storageClassName": "sc"})[0]
        rewritten = st.rewrite_claim(pvc(), plan)["spec"]
        assert rewritten["storageClassName"] == "sc"
        assert "volumeName" not in rewritten
        assert "selector" not in rewritten


class TestPrepareScript:
    def test_only_fresh_claims_get_a_directory(self):
        index = st.source_volume_index(pv_items(source_pv("core/reused")), "custom-9")
        answers = st.normalize({
            **NFS_ANSWERS,
            "claims": {"core/reused": {"source": "reuse"},
                       "core/left": {"source": "none"}},
        })
        plans = st.resolve(
            [pvc("api-data"), pvc("reused"), pvc("left")], answers,
            source_index=index,
        )
        script = st.prepare_script(plans, answers)
        assert "core/api-data" in script
        # A reused path is never touched: the workload still running over there
        # owns those permissions.
        assert "reused" not in script
        assert "left" not in script

    def test_the_export_is_unmounted_even_on_failure(self):
        answers = st.normalize(NFS_ANSWERS)
        script = st.prepare_script(st.resolve([pvc()], answers), answers)
        assert "mount -t nfs 10.4.1.20:/exports/ks" in script
        assert "trap" in script and "umount" in script

    def test_fsgroup_narrows_the_permissions(self):
        deploy = deployment()
        deploy["spec"]["template"]["spec"]["securityContext"] = {"fsGroup": 2000}
        answers = st.normalize(NFS_ANSWERS)
        plans = st.resolve([pvc()], answers, consumer_index=st.consumers([deploy]))
        script = st.prepare_script(plans, answers)
        assert "chown :2000" in script and "chmod 2770" in script
        assert "0777" not in script

    def test_without_an_fsgroup_it_says_why_it_is_world_writable(self):
        answers = st.normalize(NFS_ANSWERS)
        script = st.prepare_script(st.resolve([pvc()], answers), answers)
        assert "chmod 0777" in script
        assert "no fsGroup" in script

    def test_nothing_to_do_is_no_script(self):
        assert st.prepare_script(
            st.resolve([pvc()], {"default": "none"}), {"default": "none"}
        ) is None


class TestStoragePreflight:
    def _plans(self, answers, **kwargs):
        return st.resolve([pvc()], st.normalize(answers), **kwargs)

    def test_an_unreachable_export_fails_rather_than_warns(self):
        # The deliberate exception to "workload checks only warn": the phase
        # mounts this export, so it would fail the build anyway - later.
        checks = st.preflight_checks(
            self._plans(NFS_ANSWERS),
            st.normalize(NFS_ANSWERS),
            probe=lambda target, script: "KS_NFS=closed",
            probe_targets=[("w-1", object())],
        )
        nfs = next(c for c in checks if c["id"] == "workload_storage_nfs")
        assert nfs["status"] == "fail"
        assert "w-1" in nfs["detail"]

    def test_a_reachable_export_passes(self):
        checks = st.preflight_checks(
            self._plans(NFS_ANSWERS), st.normalize(NFS_ANSWERS),
            probe=lambda target, script: "KS_NFS=open\n",
            probe_targets=[("w-1", object()), ("w-2", object())],
        )
        assert next(
            c for c in checks if c["id"] == "workload_storage_nfs"
        )["status"] == "pass"

    def test_a_probe_that_cannot_run_warns_instead_of_failing(self):
        def boom(target, script):
            raise RuntimeError("ssh down")

        checks = st.preflight_checks(
            self._plans(NFS_ANSWERS), st.normalize(NFS_ANSWERS),
            probe=boom, probe_targets=[("w-1", object())],
        )
        assert next(
            c for c in checks if c["id"] == "workload_storage_nfs"
        )["status"] == "warn"

    def test_no_probe_means_no_reachability_check(self):
        checks = st.preflight_checks(self._plans(NFS_ANSWERS), st.normalize(NFS_ANSWERS))
        assert not any(c["id"] == "workload_storage_nfs" for c in checks)

    def test_pending_claims_warn_with_their_names(self):
        checks = st.preflight_checks(
            self._plans({"default": "none"}), {"default": "none"}
        )
        pending = next(c for c in checks if c["id"] == "workload_storage_pending")
        assert pending["status"] == "warn"
        assert "core/api-data" in pending["detail"]

    def test_an_invalid_decision_fails(self):
        checks = st.preflight_checks(
            self._plans({"default": "fresh"}), {"default": "fresh"}
        )
        assert next(
            c for c in checks if c["id"] == "workload_storage_invalid"
        )["status"] == "fail"

    def test_a_live_writer_on_a_reused_export_is_named(self):
        answers = st.normalize({**NFS_ANSWERS, "default": "reuse"})
        plans = st.resolve(
            [pvc()], answers,
            source_index=st.source_volume_index(pv_items(source_pv()), "custom-9"),
            writer_index={"core/api-data": ["api-7c9", "api-8d2"]},
        )
        checks = st.preflight_checks(plans, answers)
        dual = next(c for c in checks if c["id"] == "workload_storage_dual_writer")
        assert dual["status"] == "warn"
        assert "2 pods still mounting it" in dual["detail"]
        assert "corruption" in dual["hint"]

    def test_the_plan_check_states_retain(self):
        checks = st.preflight_checks(self._plans(NFS_ANSWERS), st.normalize(NFS_ANSWERS))
        plan_check = next(c for c in checks if c["id"] == "workload_storage_plan")
        assert plan_check["status"] == "pass"
        assert "Retain" in plan_check["detail"]


class TestSourceWriters:
    def test_only_pods_that_actually_mount_the_claim_count(self):
        pods = {"items": [
            {"metadata": {"name": "api-1"}, "status": {"phase": "Running"},
             "spec": {"volumes": [
                 {"name": "d", "persistentVolumeClaim": {"claimName": "api-data"}}]}},
            {"metadata": {"name": "web-1"}, "status": {"phase": "Running"},
             "spec": {"volumes": [{"name": "c", "configMap": {"name": "x"}}]}},
            {"metadata": {"name": "old-1"}, "status": {"phase": "Succeeded"},
             "spec": {"volumes": [
                 {"name": "d", "persistentVolumeClaim": {"claimName": "api-data"}}]}},
        ]}
        writers = st.source_writers(lambda cid, args: pods, "custom-9", "core")
        assert writers == {"core/api-data": ["api-1"]}

    def test_a_failed_pod_read_is_not_fatal(self):
        def boom(cid, args):
            raise RuntimeError("forbidden")

        assert st.source_writers(boom, "custom-9", "core") == {}


# ---------------------------------------------------------------------------
# The phase, with storage
# ---------------------------------------------------------------------------

def with_storage(answers, items=None, registry_id=None):
    payload = selection(items, registry_id=registry_id)
    payload["storage"] = answers
    return payload


class TestWorkloadStorageInThePhase:
    def _run(self, client, admin_token, ssh_profile, fake, payload):
        return run_full_build(
            client, admin_token, ssh_profile, fake,
            make_build_payload(nodes=SINGLE_CP_NODES, workloads=payload),
        )

    def test_authors_a_volume_and_repoints_the_claim(
        self, client, admin_token, ssh_profile, fake_ssh, app, source_cluster
    ):
        fake = build_default_fake(HOSTS)
        bound_claims(fake)
        nfs_reachable(fake)
        set_transport_factory(lambda: fake)
        data = self._run(
            client, admin_token, ssh_profile, fake, with_storage(NFS_ANSWERS)
        )
        assert data["status"] == "completed", data.get("error")

        manifests = applied_manifests(fake)
        # Cluster-scoped volumes get their own file, and it sorts first.
        assert "/etc/kubernetes/kubesight-workloads-cluster.yaml" in manifests
        volumes = manifests["/etc/kubernetes/kubesight-workloads-cluster.yaml"]
        assert "kind: PersistentVolume" in volumes
        assert "server: 10.4.1.20" in volumes
        assert "path: /exports/ks/core/api-data" in volumes
        assert "persistentVolumeReclaimPolicy: Retain" in volumes
        assert "name: api-data" in volumes           # the claimRef

        namespaced = manifests["/etc/kubernetes/kubesight-workloads-core.yaml"]
        assert "volumeName: kubesight-core-api-data" in namespaced

        # The directory was made on the export, and the export unmounted again.
        mkdir_call = next(
            script for _, script in fake.calls
            if "mount -t nfs" in script
        )
        assert "mkdir -p" in mkdir_call and "core/api-data" in mkdir_call
        assert "umount" in mkdir_call

        record = data["workloadSelection"]["result"]
        assert record["volumes"] == [{
            "claim": "core/api-data", "source": "fresh",
            "pv": "kubesight-core-api-data",
            "target": "10.4.1.20:/exports/ks/core/api-data",
            "capacity": "5Gi", "readOnly": False,
        }]
        assert record["unboundClaims"] == []

    def test_reuse_points_at_the_source_export_and_creates_no_directory(
        self, client, admin_token, ssh_profile, fake_ssh, app, source_cluster
    ):
        source_cluster["pvs"] = [{
            "apiVersion": "v1", "kind": "PersistentVolume",
            "metadata": {"name": "pv-prod-api"},
            "spec": {
                "capacity": {"storage": "50Gi"},
                "accessModes": ["ReadWriteMany"],
                "nfs": {"server": "10.9.9.9", "path": "/vol/prod/api"},
                "claimRef": {"kind": "PersistentVolumeClaim",
                             "namespace": "core", "name": "api-data"},
            },
        }]
        fake = build_default_fake(HOSTS)
        bound_claims(fake)
        nfs_reachable(fake)
        set_transport_factory(lambda: fake)
        data = self._run(
            client, admin_token, ssh_profile, fake,
            with_storage({**NFS_ANSWERS, "default": "reuse"}),
        )
        assert data["status"] == "completed", data.get("error")

        volumes = applied_manifests(fake)["/etc/kubernetes/kubesight-workloads-cluster.yaml"]
        assert "server: 10.9.9.9" in volumes
        assert "path: /vol/prod/api" in volumes
        assert "storage: 50Gi" in volumes            # the volume's real size
        # Nothing is written to an export somebody else is using.
        assert not any("mount -t nfs" in script for _, script in fake.calls)
        assert data["workloadSelection"]["result"]["volumes"][0]["source"] == "reuse"

    def test_an_impossible_decision_fails_before_anything_is_applied(
        self, client, admin_token, ssh_profile, fake_ssh, app, source_cluster
    ):
        fake = build_default_fake(HOSTS)
        set_transport_factory(lambda: fake)
        build = create_build(
            client, admin_token, ssh_profile,
            make_build_payload(
                nodes=SINGLE_CP_NODES,
                # "fresh" with no server: the row cannot be honoured.
                workloads=with_storage({"default": "fresh"}),
            ),
        )
        # Preflight says so first, and says it as a failure.
        preflight = client.post(
            f"/api/cluster-builds/{build['id']}/preflight",
            headers=auth_headers(admin_token),
        ).get_json()["data"]
        checks = [
            check
            for node in preflight["nodes"] for check in node["checks"]
            if check["id"] == "workload_storage_invalid"
        ]
        assert checks and checks[0]["status"] == "fail"
        assert preflight["status"] == "fail"
        # And the build cannot start at all, so nothing was applied.
        response = client.post(
            f"/api/cluster-builds/{build['id']}/start",
            json={"ackWarnings": ["ack"]}, headers=auth_headers(admin_token),
        )
        assert response.status_code == 400
        assert not applied_manifests(fake)

    def test_a_claim_that_does_not_bind_is_reported_not_fatal(
        self, client, admin_token, ssh_profile, fake_ssh, app, source_cluster
    ):
        fake = build_default_fake(HOSTS)
        bound_claims(fake, phase="Pending")
        nfs_reachable(fake)
        set_transport_factory(lambda: fake)
        data = self._run(
            client, admin_token, ssh_profile, fake, with_storage(NFS_ANSWERS)
        )
        assert data["status"] == "completed", data.get("error")
        assert data["workloadSelection"]["result"]["unboundClaims"] == [
            "core/api-data (Pending)"
        ]

    def test_pending_claims_need_no_nfs_answers_at_all(
        self, client, admin_token, ssh_profile, fake_ssh, app, source_cluster
    ):
        fake = build_default_fake(HOSTS)
        set_transport_factory(lambda: fake)
        data = self._run(
            client, admin_token, ssh_profile, fake, with_storage({"default": "none"})
        )
        assert data["status"] == "completed", data.get("error")
        assert "/etc/kubernetes/kubesight-workloads-cluster.yaml" not in applied_manifests(fake)
        assert not any("mount -t nfs" in script for _, script in fake.calls)

    def test_the_nfs_client_is_installed_only_when_a_volume_needs_it(
        self, client, admin_token, ssh_profile, fake_ssh, app, source_cluster
    ):
        fake = build_default_fake(HOSTS)
        bound_claims(fake)
        nfs_reachable(fake)
        set_transport_factory(lambda: fake)
        self._run(client, admin_token, ssh_profile, fake, with_storage(NFS_ANSWERS))
        assert any("nfs-common" in script for _, script in fake.calls)

        other = build_default_fake(HOSTS)
        set_transport_factory(lambda: other)
        run_full_build(
            client, admin_token, ssh_profile, other,
            make_build_payload(name="no-nfs", nodes=SINGLE_CP_NODES,
                               workloads=with_storage({"default": "none"})),
        )
        assert not any("nfs-common" in script for _, script in other.calls)

    def test_the_export_is_probed_from_the_worker_at_preflight(
        self, client, admin_token, ssh_profile, fake_ssh, app, source_cluster
    ):
        fake = build_default_fake(HOSTS)
        # The worker cannot reach the export.
        fake.responders.insert(0, (
            lambda h, s: "/dev/tcp/10.4.1.20/2049" in s, "KS_NFS=closed\n",
        ))
        set_transport_factory(lambda: fake)
        build = create_build(
            client, admin_token, ssh_profile,
            make_build_payload(nodes=SINGLE_CP_NODES,
                               workloads=with_storage(NFS_ANSWERS)),
        )
        preflight = client.post(
            f"/api/cluster-builds/{build['id']}/preflight",
            headers=auth_headers(admin_token),
        ).get_json()["data"]
        nfs = [
            check for node in preflight["nodes"] for check in node["checks"]
            if check["id"] == "workload_storage_nfs"
        ]
        assert nfs and nfs[0]["status"] == "fail"
        # Probed from the worker, which is where the pod will actually mount.
        probes = [s for h, s in fake.calls if "/dev/tcp/10.4.1.20/2049" in s]
        assert probes
        assert any(h == "10.0.0.21" for h, s in fake.calls
                   if "/dev/tcp/10.4.1.20/2049" in s)


class TestStorageInThePlanPayload:
    def test_the_plan_route_prices_the_storage(
        self, client, admin_token, app, source_cluster
    ):
        response = client.post(
            "/api/cluster-builds/workload-plan",
            json=with_storage(NFS_ANSWERS), headers=auth_headers(admin_token),
        )
        assert response.status_code == 200, response.get_json()
        storage_section = response.get_json()["data"]["storage"]
        assert storage_section["summary"]["volumesToCreate"] == 1
        row = storage_section["claims"][0]
        assert row["key"] == "core/api-data"
        assert row["source"] == "fresh"
        assert row["target"] == "10.4.1.20:/exports/ks/core/api-data"
        assert row["pvName"] == "kubesight-core-api-data"
        assert storage_section["errors"] == []

    def test_the_plan_says_whether_reuse_is_even_possible(
        self, client, admin_token, app, source_cluster
    ):
        source_cluster["pvs"] = [{
            "apiVersion": "v1", "kind": "PersistentVolume",
            "metadata": {"name": "pv-x"},
            "spec": {"capacity": {"storage": "9Gi"},
                     "csi": {"driver": "csi.vsphere.vmware.com"},
                     "claimRef": {"kind": "PersistentVolumeClaim",
                                  "namespace": "core", "name": "api-data"}},
        }]
        data = client.post(
            "/api/cluster-builds/workload-plan",
            json=with_storage({"default": "none"}), headers=auth_headers(admin_token),
        ).get_json()["data"]
        row = data["storage"]["claims"][0]
        assert row["reusable"] is False
        assert "csi.vsphere.vmware.com" in row["reuseBlocked"]
        assert row["sourcePv"] == "pv-x"

    def test_a_storage_answer_survives_a_round_trip_on_a_finished_build(
        self, client, admin_token, finished_build, source_cluster
    ):
        build_id = finished_build["build"]["id"]
        response = client.put(
            f"/api/cluster-builds/{build_id}/workloads",
            json={"workloads": with_storage(NFS_ANSWERS)},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 200, response.get_json()
        stored = response.get_json()["data"]["workloadSelection"]["storage"]
        assert stored["nfsServer"] == "10.4.1.20"
        assert stored["default"] == "fresh"

    def test_a_bad_export_root_is_refused_at_the_api(
        self, client, admin_token, app, source_cluster
    ):
        response = client.post(
            "/api/cluster-builds/workload-plan",
            json=with_storage({**NFS_ANSWERS, "nfsExportRoot": "/exports/../etc"}),
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 400
        assert "export root" in response.get_json()["error"]
