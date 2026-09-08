"""Build cache: the settings that drive it, and the cluster work behind the UI.

No cluster involved — the runner's kubectl transport is injected, so every
manifest this sends is asserted rather than applied.
"""

from __future__ import annotations

import json

import pytest

from api.db import db
from api.models_ci import CiBuild, CiService
from api.services.ci import cache as cache_service
from api.services.ci.runners import kubernetes as k8s
from tests.conftest import auth_headers


@pytest.fixture(autouse=True)
def _reset_kubectl():
    yield
    k8s.set_kubectl_runner(None)


def _bound_pvc(name="ci-cache", phase="Bound", volume="kubesight-kubesight-ci-ci-cache"):
    return {
        "metadata": {"name": name},
        "spec": {
            "accessModes": ["ReadWriteMany"],
            "volumeName": volume,
            "resources": {"requests": {"storage": "20Gi"}},
        },
        "status": {"phase": phase, "capacity": {"storage": "20Gi"}},
    }


def _nfs_pv(name="kubesight-kubesight-ci-ci-cache"):
    return {
        "metadata": {"name": name},
        "spec": {
            "capacity": {"storage": "20Gi"},
            "accessModes": ["ReadWriteMany"],
            "persistentVolumeReclaimPolicy": "Retain",
            "nfs": {"server": "10.4.27.17", "path": "/datauat/NFS-DATA/ci-cache"},
        },
    }


def _cluster(*, pvc=None, pv=None, jobs=None, logs="", apply_result=(0, "", ""), applied=None):
    """A fake cluster that records what was applied to it."""

    def fake(args, input_text=None):
        if args[:2] == ["get", "pvc"]:
            return (0, json.dumps(pvc), "") if pvc else (1, "", 'pvc "ci-cache" not found')
        if args[:2] == ["get", "pv"]:
            return (0, json.dumps(pv), "") if pv else (1, "", "not found")
        if args[:2] == ["get", "jobs"]:
            return 0, json.dumps({"items": jobs or []}), ""
        if args[0] == "logs":
            return 0, logs, ""
        if args[:2] == ["apply", "-f"]:
            if applied is not None:
                applied.append(json.loads(input_text))
            return apply_result
        return 1, "", f"unexpected: {' '.join(args)}"

    return fake


# ---------------------------------------------------------------------------
# Where the setting comes from
# ---------------------------------------------------------------------------

def test_the_environment_still_decides_when_nothing_has_been_saved(app, monkeypatch):
    """Every install that predates the UI keeps behaving exactly as configured."""
    monkeypatch.setenv("CI_CACHE_CLAIM_NAME", "ci-cache")
    monkeypatch.delenv("CI_CACHE_STORAGE_CLASS", raising=False)
    with app.app_context():
        assert cache_service.stored_settings() == {}
        assert cache_service.runtime_config() == {"claimName": "ci-cache", "storageClass": ""}
        assert cache_service.status()["source"] == "environment"


def test_a_saved_setting_wins_over_the_environment_in_both_directions(app, monkeypatch):
    """A switch that cannot turn something OFF is not a switch, so the saved
    value beats the variable even when the variable says yes."""
    monkeypatch.setenv("CI_CACHE_CLAIM_NAME", "from-the-configmap")
    with app.app_context():
        cache_service.save_settings({"enabled": True, "claimName": "chosen-here"})
        assert cache_service.runtime_config()["claimName"] == "chosen-here"

        cache_service.save_settings({"enabled": False})
        assert cache_service.runtime_config() == {"claimName": "", "storageClass": ""}
        # And the runner agrees — this is the seam the whole feature hangs on.
        assert k8s.cache_enabled() is False


def test_turning_it_on_reaches_the_runner(app, monkeypatch):
    monkeypatch.delenv("CI_CACHE_CLAIM_NAME", raising=False)
    monkeypatch.delenv("CI_CACHE_STORAGE_CLASS", raising=False)
    with app.app_context():
        cache_service.save_settings({"enabled": True, "claimName": "ci-cache"})
        assert k8s.cache_enabled() is True
        assert k8s.cache_claim_name("payment-service") == "ci-cache"
        # One claim for everybody means each service needs its own subtree.
        assert k8s.cache_base_path("payment-service") == "/cache/payment-service"


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

def test_status_warns_when_caching_is_on_with_no_claim_behind_it(app, monkeypatch):
    monkeypatch.delenv("CI_CACHE_STORAGE_CLASS", raising=False)
    with app.app_context():
        cache_service.save_settings({"enabled": True, "claimName": "ci-cache"})
        k8s.set_kubectl_runner(_cluster())
        state = cache_service.status()
    assert state["enabled"] is True
    assert state["claim"]["exists"] is False
    assert any("fail at its first stage" in warning for warning in state["warnings"])


def test_status_describes_the_backing_store_it_found(app):
    with app.app_context():
        cache_service.save_settings({"enabled": True, "claimName": "ci-cache"})
        k8s.set_kubectl_runner(_cluster(pvc=_bound_pvc(), pv=_nfs_pv()))
        state = cache_service.status()
    assert state["claim"]["phase"] == "Bound"
    assert state["volume"]["backing"] == {
        "type": "nfs",
        "server": "10.4.27.17",
        "path": "/datauat/NFS-DATA/ci-cache",
    }
    assert state["warnings"] == []


def test_status_warns_when_the_claim_is_not_where_builds_run(app, monkeypatch):
    """A claim can only be mounted from its own namespace, so this would be a
    cache that silently does nothing."""
    monkeypatch.setenv("CI_KUBERNETES_NAMESPACE", "kubesight-ci")
    with app.app_context():
        cache_service.save_settings(
            {"enabled": True, "claimName": "ci-cache", "namespace": "somewhere-else"}
        )
        k8s.set_kubectl_runner(_cluster(pvc=_bound_pvc(), pv=_nfs_pv()))
        state = cache_service.status()
    assert any("only be mounted from its own namespace" in w for w in state["warnings"])


# ---------------------------------------------------------------------------
# Creating the volume
# ---------------------------------------------------------------------------

def test_creating_an_nfs_volume_prebinds_it_and_remembers_it(app):
    applied: list = []
    with app.app_context():
        k8s.set_kubectl_runner(_cluster(applied=applied))
        cache_service.create_volume(
            {
                "backing": "nfs",
                "nfsServer": "10.4.27.17",
                "nfsPath": "/datauat/NFS-DATA/ci-cache",
                "size": "20Gi",
            }
        )
        saved = cache_service.stored_settings()

    volume, claim = applied[0]["items"]
    assert volume["spec"]["nfs"] == {
        "server": "10.4.27.17",
        "path": "/datauat/NFS-DATA/ci-cache",
    }
    assert volume["spec"]["accessModes"] == ["ReadWriteMany"]
    # No provisioner is involved, and the pair is bound to each other only.
    assert volume["spec"]["storageClassName"] == ""
    assert volume["spec"]["claimRef"] == {"namespace": "kubesight-ci", "name": "ci-cache"}
    assert claim["spec"]["volumeName"] == volume["metadata"]["name"]
    # Retain, or deleting the claim would throw away every cached dependency.
    assert volume["spec"]["persistentVolumeReclaimPolicy"] == "Retain"
    assert saved["backing"] == {
        "type": "nfs",
        "server": "10.4.27.17",
        "path": "/datauat/NFS-DATA/ci-cache",
    }
    # Creating storage is not the same as switching caching on: the export must
    # be writable by the build uid first, which only a real write proves.
    assert saved["enabled"] is False


def test_creating_a_node_local_volume_pins_it_to_that_node(app):
    applied: list = []
    with app.app_context():
        k8s.set_kubectl_runner(_cluster(applied=applied))
        cache_service.create_volume(
            {"backing": "local", "node": "worker-1", "path": "/var/lib/kubesight/ci-cache"}
        )
    volume = applied[0]["items"][0]
    assert volume["spec"]["local"] == {"path": "/var/lib/kubesight/ci-cache"}
    assert volume["spec"]["accessModes"] == ["ReadWriteOnce"]
    terms = volume["spec"]["nodeAffinity"]["required"]["nodeSelectorTerms"]
    assert terms[0]["matchExpressions"][0]["values"] == ["worker-1"]


@pytest.mark.parametrize(
    "payload, fragment",
    [
        ({"backing": "nfs", "nfsServer": "10.4.27.17", "nfsPath": "relative"}, "absolute path"),
        ({"backing": "nfs", "nfsPath": "/exports/cache"}, "NFS server address"),
        (
            {"backing": "nfs", "nfsServer": "10.4.27.17", "nfsPath": "/x", "size": "20 gigs"},
            "20Gi",
        ),
        ({"backing": "carrier-pigeon"}, "nfs or local"),
    ],
)
def test_create_rejects_input_that_would_produce_a_broken_volume(app, payload, fragment):
    with app.app_context():
        k8s.set_kubectl_runner(_cluster())
        with pytest.raises(cache_service.CacheError) as excinfo:
            cache_service.create_volume(payload)
    assert fragment in str(excinfo.value)


def test_create_refuses_to_touch_a_claim_that_already_exists(app):
    with app.app_context():
        k8s.set_kubectl_runner(_cluster(pvc=_bound_pvc(), pv=_nfs_pv()))
        with pytest.raises(cache_service.CacheError) as excinfo:
            cache_service.create_volume(
                {"backing": "nfs", "nfsServer": "10.4.27.17", "nfsPath": "/exports/cache"}
            )
    message = str(excinfo.value)
    assert "already exists" in message
    # Says what is safe about the alternative, because deleting storage reads
    # as dangerous even when it is not.
    assert "untouched" in message


def test_create_explains_the_missing_cluster_role_rather_than_the_raw_403(app):
    """PersistentVolumes are cluster-scoped, so this is the one thing the
    namespaced CI role cannot grant — and the message has to say so."""
    forbidden = (
        1,
        "",
        'persistentvolumes is forbidden: User "system:serviceaccount:kubesight:kubesight-backend" '
        'cannot create resource "persistentvolumes" at the cluster scope',
    )
    with app.app_context():
        k8s.set_kubectl_runner(_cluster(apply_result=forbidden))
        with pytest.raises(cache_service.CacheError) as excinfo:
            cache_service.create_volume(
                {"backing": "nfs", "nfsServer": "10.4.27.17", "nfsPath": "/exports/cache"}
            )
    assert "ci-cache-rbac.yaml" in str(excinfo.value)


# ---------------------------------------------------------------------------
# On / off
# ---------------------------------------------------------------------------

def test_enabling_without_a_bound_claim_is_refused(app):
    """Turning it on with nothing behind it fails every build at its first
    stage — strictly worse than the cold builds it replaces."""
    with app.app_context():
        k8s.set_kubectl_runner(_cluster())
        with pytest.raises(cache_service.CacheError) as excinfo:
            cache_service.set_enabled(True)
        assert "no claim named" in str(excinfo.value)

        k8s.set_kubectl_runner(_cluster(pvc=_bound_pvc(phase="Pending")))
        with pytest.raises(cache_service.CacheError) as excinfo:
            cache_service.set_enabled(True)
    assert "not Bound" in str(excinfo.value)


def test_disabling_deletes_nothing(app):
    """The volume and its contents survive, so re-enabling is warm rather than
    a fresh cold start."""
    calls: list = []

    def fake(args, input_text=None):
        calls.append(args)
        return _cluster(pvc=_bound_pvc(), pv=_nfs_pv())(args, input_text)

    with app.app_context():
        k8s.set_kubectl_runner(fake)
        cache_service.set_enabled(True)
        assert cache_service.runtime_config()["claimName"] == "ci-cache"
        state = cache_service.set_enabled(False)

    assert state["enabled"] is False
    assert not [args for args in calls if args and args[0] == "delete"]


# ---------------------------------------------------------------------------
# Measure and clean
# ---------------------------------------------------------------------------

def _service(name="Payment Service", slug="payment-service"):
    row = CiService(name=name, slug=slug, application_type="java")
    db.session.add(row)
    db.session.commit()
    return row


def test_cleaning_one_service_removes_only_its_subtree(app):
    applied: list = []
    with app.app_context():
        service = _service()
        cache_service.save_settings({"enabled": True, "claimName": "ci-cache"})
        k8s.set_kubectl_runner(_cluster(pvc=_bound_pvc(), pv=_nfs_pv(), applied=applied))
        result = cache_service.clean(service_id=service.id)

    job = applied[0]
    assert result["target"] == "Payment Service"
    assert job["metadata"]["labels"]["kubesight.io/cache-op"] == "clean"
    script = job["spec"]["template"]["spec"]["containers"][0]["command"][2]
    assert script.startswith("rm -rf /cache/payment-service")
    # Same identity a stage container runs with, so the restricted Pod Security
    # Standard admits it and the files it removes are ones it owns.
    pod = job["spec"]["template"]["spec"]
    assert pod["securityContext"]["fsGroup"] == 65532
    assert pod["containers"][0]["securityContext"]["runAsUser"] == 65532
    assert pod["volumes"][0]["persistentVolumeClaim"]["claimName"] == "ci-cache"


def test_cleaning_everything_empties_the_mount_without_removing_it(app):
    applied: list = []
    with app.app_context():
        cache_service.save_settings({"enabled": True, "claimName": "ci-cache"})
        k8s.set_kubectl_runner(_cluster(pvc=_bound_pvc(), pv=_nfs_pv(), applied=applied))
        cache_service.clean(all_services=True)
    script = applied[0]["spec"]["template"]["spec"]["containers"][0]["command"][2]
    assert "-mindepth 1" in script and "/cache" in script


def test_cleaning_is_refused_while_a_build_is_running(app):
    """Deleting a Gradle cache underneath a build fails it with errors that
    look nothing like the cause."""
    with app.app_context():
        service = _service()
        db.session.add(CiBuild(service_id=service.id, number=7, status="running"))
        db.session.commit()
        cache_service.save_settings({"enabled": True, "claimName": "ci-cache"})
        k8s.set_kubectl_runner(_cluster(pvc=_bound_pvc(), pv=_nfs_pv()))

        with pytest.raises(cache_service.CacheError) as excinfo:
            cache_service.clean(service_id=service.id)
        assert "#7" in str(excinfo.value)

        with pytest.raises(cache_service.CacheError):
            cache_service.clean(all_services=True)


def test_a_second_maintenance_job_waits_for_the_first(app):
    running_job = {
        "metadata": {
            "name": "ci-cache-clean-1",
            "creationTimestamp": "2026-09-08T10:00:00Z",
            "labels": {"kubesight.io/cache-op": "clean"},
        },
        "status": {"active": 1},
    }
    with app.app_context():
        cache_service.save_settings({"enabled": True, "claimName": "ci-cache"})
        k8s.set_kubectl_runner(_cluster(pvc=_bound_pvc(), pv=_nfs_pv(), jobs=[running_job]))
        with pytest.raises(cache_service.CacheError) as excinfo:
            cache_service.measure()
    assert "still running" in str(excinfo.value)


def test_measured_sizes_are_read_back_from_the_job_log(app):
    finished = {
        "metadata": {
            "name": "ci-cache-measure-1",
            "creationTimestamp": "2026-09-08T10:00:00Z",
            "labels": {"kubesight.io/cache-op": "measure"},
        },
        "status": {"succeeded": 1, "completionTime": "2026-09-08T10:01:00Z"},
    }
    logs = "412M\t/cache/payment-service\n96M\t/cache/ledger-ui\nnoise\n508M\t/cache\ndone\n"
    with app.app_context():
        cache_service.save_settings({"enabled": True, "claimName": "ci-cache"})
        k8s.set_kubectl_runner(
            _cluster(pvc=_bound_pvc(), pv=_nfs_pv(), jobs=[finished], logs=logs)
        )
        state = cache_service.status()

    usage = state["maintenance"]["usage"]
    assert {"service": "payment-service", "size": "412M"} in usage
    assert {"service": "(total)", "size": "508M"} in usage
    # A line that is not a du row is skipped rather than guessed at.
    assert all(row["service"] != "noise" for row in usage)


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def test_the_cache_routes_answer_the_ui(app, client, admin_token):
    with app.app_context():
        k8s.set_kubectl_runner(_cluster(pvc=_bound_pvc(), pv=_nfs_pv()))

        response = client.get("/api/ci/cache", headers=auth_headers(admin_token))
        assert response.status_code == 200
        body = response.get_json()["data"]
        assert body["mountPath"] == "/cache"
        assert [tool["tool"] for tool in body["tools"]][:2] == ["Maven", "Gradle"]

        on = client.put(
            "/api/ci/cache", json={"enabled": True}, headers=auth_headers(admin_token)
        )
        assert on.status_code == 200
        assert on.get_json()["data"]["enabled"] is True

        off = client.put(
            "/api/ci/cache", json={"enabled": False}, headers=auth_headers(admin_token)
        )
        assert off.get_json()["data"]["enabled"] is False


def test_clean_needs_a_target(app, client, admin_token):
    with app.app_context():
        k8s.set_kubectl_runner(_cluster(pvc=_bound_pvc(), pv=_nfs_pv()))
        response = client.post("/api/ci/cache/clean", json={}, headers=auth_headers(admin_token))
    assert response.status_code == 400
    assert "all: true" in response.get_json()["error"]


def test_the_cache_is_not_readable_without_a_token(app, client):
    assert client.get("/api/ci/cache").status_code in (401, 403)
