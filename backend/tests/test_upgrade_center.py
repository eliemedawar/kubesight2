"""Upgrade Center: release calendar, readiness, the kubeadm run, requests."""

import json
import threading
import time
from datetime import date

import pytest

from api.cluster_access import ClusterAccess
from api.db import db
from api.models import DeploymentRequestSetting, User
from tests.conftest import auth_headers

# --- Release calendar -------------------------------------------------------


def test_support_status_states():
    from api.upgrade_releases import support_status

    today = date(2026, 10, 14)
    assert support_status("v1.33.6", today=today)["state"] == "ended"
    ending = support_status("v1.34.4", today=today)
    assert ending["state"] == "ending" and ending["daysLeft"] == 13
    assert support_status("v1.36.1", today=today)["state"] == "supported"
    assert support_status("unknown", today=today)["state"] == "unknown"


def test_upgrade_path_is_one_minor_per_hop():
    from api.upgrade_releases import upgrade_path

    patches = {35: "v1.35.7", 36: None, 37: "v1.37.1"}
    hops = upgrade_path("v1.34.4", "v1.37.1", lambda _major, minor: patches.get(minor))
    assert [h["minor"] for h in hops] == ["1.35", "1.36", "1.37"]
    assert hops[0]["version"] == "v1.35.7"
    assert hops[1]["label"] == "v1.36.x"  # patch not known yet
    assert hops[-1]["latest"] is True
    assert upgrade_path("v1.37.1", "v1.37.1", lambda *_: None) == []


# --- Readiness ----------------------------------------------------------------


def _node(name, version="v1.34.4", cp=False, ready=True):
    labels = {"node-role.kubernetes.io/control-plane": ""} if cp else {}
    return {
        "metadata": {"name": name, "labels": labels},
        "spec": {},
        "status": {
            "nodeInfo": {"kubeletVersion": version},
            "conditions": [{"type": "Ready", "status": "True" if ready else "False"}],
        },
    }


def _reads(**overrides):
    base = {
        "version -o json": {"serverVersion": {"gitVersion": "v1.34.4"}},
        "get nodes -o json": {"items": [_node("cp-1", cp=True), _node("w-1"), _node("w-2")]},
        "get pods --all-namespaces -o json": {"items": []},
        "get pvc --all-namespaces -o json": {"items": []},
        "get storageclass -o json": {"items": []},
        "get pdb --all-namespaces -o json": {"items": []},
        "get apiservices -o json": {"items": []},
        "get deployments,statefulsets --all-namespaces -o json": {"items": []},
        "config view --minify -o json": {"clusters": []},
    }
    base.update(overrides)
    return base


def _kubectl_from(responses, delay=0.0, calls=None):
    def run(access, args, timeout=None):
        key = " ".join(args)
        if calls is not None:
            calls.append(key)
        if delay:
            time.sleep(delay)
        if key not in responses:
            raise RuntimeError(f"Unexpected kubectl args: {args}")
        value = responses[key]
        if isinstance(value, Exception):
            raise value
        return value if isinstance(value, str) else json.dumps(value)

    return run


def test_reads_run_in_parallel():
    from api.upgrade_checks import READS, fetch_reads

    run = _kubectl_from(_reads(), delay=0.3)
    started = time.monotonic()
    results = fetch_reads(ClusterAccess(cluster_id="c", context_name="c"), run)
    elapsed = time.monotonic() - started
    assert set(results) == set(READS)
    # Eight reads of 0.3 s one after another would take 2.4 s.
    assert elapsed < 1.5


def test_pdb_allowing_no_disruption_blocks_a_multi_node_cluster():
    from api.upgrade_provider import run_extended_prechecks

    pdb = {
        "metadata": {"namespace": "payments", "name": "payments-api"},
        "status": {"expectedPods": 3, "currentHealthy": 3, "desiredHealthy": 3, "disruptionsAllowed": 0},
    }
    run = _kubectl_from(_reads(**{"get pdb --all-namespaces -o json": {"items": [pdb]}}))
    result = run_extended_prechecks(ClusterAccess(cluster_id="c", context_name="c"), "v1.35.7", run)
    by_key = {c["key"]: c for c in result["checks"]}
    assert by_key["pdb"]["status"] == "failed"
    assert by_key["pdb"]["group"] == "workloads"
    assert "payments/payments-api" in by_key["pdb"]["objects"]
    assert by_key["pdb"]["fix"]
    assert result["canUpgrade"] is False
    assert result["summary"]["failed"] == 1


def test_single_replica_and_metrics_from_apiservices():
    from api.upgrade_checks import run_checks

    deployment = {"kind": "Deployment", "metadata": {"namespace": "kafka", "name": "registry"}, "spec": {"replicas": 1}}
    metrics = {
        "metadata": {"name": "v1beta1.metrics.k8s.io"},
        "status": {"conditions": [{"type": "Available", "status": "True"}]},
    }
    run = _kubectl_from(_reads(**{
        "get deployments,statefulsets --all-namespaces -o json": {"items": [deployment]},
        "get apiservices -o json": {"items": [metrics]},
    }))
    result = run_checks(ClusterAccess(cluster_id="c", context_name="c"), "v1.35.7", run)
    by_key = {c["key"]: c for c in result["checks"]}
    assert by_key["single_replica"]["status"] == "warning"
    assert by_key["metrics"]["status"] == "passed"


# --- The kubeadm run ----------------------------------------------------------


class FakeCluster:
    """Enough of a kubeadm cluster for the executor: nodes, versions, events."""

    def __init__(self, cps=3, workers=3, version="v1.34.4"):
        self.server = version
        self.nodes = {f"cp-{i}": _node(f"cp-{i}", version, cp=True) for i in range(1, cps + 1)}
        self.nodes.update({f"w-{i}": _node(f"w-{i}", version) for i in range(1, workers + 1)})
        self.events = []
        self.drain_timeouts = []
        self.fail_drain = set()
        self.lock = threading.Lock()

    def kubectl(self, access, args, timeout=None):
        args = list(args)
        if args[:2] == ["--namespace", "default"]:
            args = args[2:]
        key = " ".join(args)
        with self.lock:
            if key == "get nodes -o json":
                return json.dumps({"items": list(self.nodes.values())})
            if key == "version -o json":
                return json.dumps({"serverVersion": {"gitVersion": self.server}})
            if key == "get pods --all-namespaces -o json":
                return json.dumps({"items": []})
            if key.startswith("get pods -n kube-system -l component=etcd"):
                return json.dumps({"items": [{"metadata": {"name": "etcd-cp-1"}, "spec": {"nodeName": "cp-1"}}]})
            if args[0] == "exec":
                self.events.append(("snapshot", args[3]))
                return ""
            if args[0] in ("cordon", "uncordon"):
                self.events.append((args[0], args[1]))
                return ""
            if args[0] == "drain":
                self.events.append(("drain", args[1]))
                self.drain_timeouts.append((args[-1], timeout))
                if args[1] in self.fail_drain:
                    raise RuntimeError("error when evicting pods: Cannot evict pod as it would violate the pod's disruption budget.")
                return ""
            if args[:2] == ["get", "node"]:
                return json.dumps(self.nodes[args[2]])
        raise RuntimeError(f"unexpected kubectl {args}")

    def run_on_node(self, access, run_kubectl, node, script):
        target = script.split('K8S_VER="', 1)[1].split('"', 1)[0]
        with self.lock:
            self.events.append(("upgrade", node, "apply" if "kubeadm upgrade apply" in script else "node"))
            self.nodes[node]["status"]["nodeInfo"]["kubeletVersion"] = f"v{target}"
            if "kubeadm upgrade apply" in script:
                self.server = f"v{target}"
        return "ok"


@pytest.fixture()
def fast_executor(monkeypatch):
    import api.upgrade_executor as ex

    monkeypatch.setattr(ex, "POD_POLL_INTERVAL_SECONDS", 0)
    monkeypatch.setattr(ex, "PAUSE_POLL_SECONDS", 0.01)
    return ex


def _start(ex, cluster):
    from api.upgrade_jobs import create_job

    job = create_job(cluster_id="kubeadm-1", target_version="v1.35.7", provider="kubeadm")
    return job["jobId"]


def test_kubeadm_run_upgrades_every_control_plane_then_workers_in_waves(app, fast_executor, monkeypatch):
    from api.upgrade_jobs import get_job

    ex = fast_executor
    cluster = FakeCluster(cps=3, workers=3)
    monkeypatch.setattr(ex, "_run_on_node", cluster.run_on_node)
    job_id = _start(ex, cluster)
    ex._execute_kubeadm_upgrade(
        ClusterAccess(cluster_id="kubeadm-1", context_name="k"), "v1.35.7", cluster.kubectl, job_id,
        options={"waveSize": 2, "drainTimeoutMinutes": 7},
    )
    upgrades = [e for e in cluster.events if e[0] == "upgrade"]
    # Every control plane — not only the first — and the first one with `apply`.
    assert upgrades[:3] == [("upgrade", "cp-1", "apply"), ("upgrade", "cp-2", "node"), ("upgrade", "cp-3", "node")]
    assert {e[1] for e in upgrades[3:]} == {"w-1", "w-2", "w-3"}
    # The etcd snapshot came before anything was touched.
    assert cluster.events[0][0] == "snapshot"
    # Drain carries the plan's timeout, and the process timeout is longer than it.
    assert all(flag == "--timeout=7m" and timeout and timeout > 420 for flag, timeout in cluster.drain_timeouts)
    # No worker was drained before the control planes were done.
    first_worker_drain = next(i for i, e in enumerate(cluster.events) if e == ("drain", "w-1"))
    last_cp_upgrade = max(i for i, e in enumerate(cluster.events) if e[0] == "upgrade" and e[1].startswith("cp-"))
    assert first_worker_drain > last_cp_upgrade

    job = get_job(job_id)
    assert job["status"] == "completed"
    assert job["snapshot"]["status"] == "done"
    nodes = {n["name"]: n for n in job["nodes"]}
    assert [nodes[w]["wave"] for w in ("w-1", "w-2", "w-3")] == [1, 1, 2]
    assert all(n["status"] == "done" and n["versionAfter"] == "v1.35.7" for n in nodes.values())
    assert all(state == "done" for n in nodes.values() for state in n["phases"].values())
    assert any("Wave 2 of 2" in line["message"] for line in job["log"])
    assert [s["status"] for s in job["stages"]] == ["done", "done", "done", "done"]


def test_stop_before_the_first_node_changes_nothing(app, fast_executor, monkeypatch):
    from api.upgrade_jobs import change_stored, get_job

    ex = fast_executor
    cluster = FakeCluster(cps=1, workers=2)
    monkeypatch.setattr(ex, "_run_on_node", cluster.run_on_node)
    job_id = _start(ex, cluster)
    change_stored(job_id, lambda p: p.setdefault("control", {}).update(stop=True))
    ex._execute_kubeadm_upgrade(ClusterAccess(cluster_id="kubeadm-1", context_name="k"), "v1.35.7",
                                cluster.kubectl, job_id, options={"etcdSnapshot": False})
    assert not [e for e in cluster.events if e[0] == "upgrade"]
    assert get_job(job_id)["status"] == "stopped"


def test_drain_timeout_uncordons_the_node_and_stops(app, fast_executor, monkeypatch):
    ex = fast_executor
    cluster = FakeCluster(cps=1, workers=2)
    cluster.fail_drain.add("w-1")
    monkeypatch.setattr(ex, "_run_on_node", cluster.run_on_node)
    job_id = _start(ex, cluster)
    with pytest.raises(RuntimeError, match="did not finish"):
        ex._execute_kubeadm_upgrade(ClusterAccess(cluster_id="kubeadm-1", context_name="k"), "v1.35.7",
                                    cluster.kubectl, job_id, options={"etcdSnapshot": False})
    assert ("uncordon", "w-1") in cluster.events
    assert ("upgrade", "w-1", "node") not in cluster.events


def test_resume_skips_nodes_already_on_the_target(app, fast_executor, monkeypatch):
    ex = fast_executor
    cluster = FakeCluster(cps=2, workers=2)
    cluster.server = "v1.35.7"
    for name in ("cp-1", "cp-2", "w-1"):
        cluster.nodes[name]["status"]["nodeInfo"]["kubeletVersion"] = "v1.35.7"
    monkeypatch.setattr(ex, "_run_on_node", cluster.run_on_node)
    job_id = _start(ex, cluster)
    ex._execute_kubeadm_upgrade(ClusterAccess(cluster_id="kubeadm-1", context_name="k"), "v1.35.7",
                                cluster.kubectl, job_id)
    assert [e[1] for e in cluster.events if e[0] == "upgrade"] == ["w-2"]
    assert not [e for e in cluster.events if e[0] == "snapshot"]


# --- Requests over the API (mock mode: staging-eu-west is simulated kubeadm) ---

CLUSTER = "staging-eu-west"


@pytest.fixture()
def simulated(monkeypatch):
    from api.services import upgrade_center as center

    center._MOCK_KUBEADM.clear()
    monkeypatch.setattr(center, "MOCK_STEP_SECONDS", 0)
    yield center
    center._MOCK_KUBEADM.clear()


def _require(count):
    row = DeploymentRequestSetting.query.first() or DeploymentRequestSetting(recipients=[], group_ids=[])
    row.required_approvals = count
    row.cluster_required_approvals = {}
    db.session.add(row)
    db.session.commit()


def _wait_for(client, token, job_id, statuses, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        job = client.get(f"/api/upgrades/jobs/{job_id}", headers=auth_headers(token)).get_json()["data"]
        if job["status"] in statuses:
            return job
        time.sleep(0.1)
    raise AssertionError(f"job stayed {job['status']}")


def test_fleet_lists_clusters_with_support_and_mode(client, admin_token, simulated):
    response = client.get("/api/upgrades/fleet", headers=auth_headers(admin_token))
    assert response.status_code == 200
    data = response.get_json()["data"]
    rows = {r["id"]: r for r in data["clusters"]}
    assert rows[CLUSTER]["mode"] == "automatic"
    assert rows[CLUSTER]["controlPlanes"] == 3
    assert rows[CLUSTER]["nextHop"]["version"] == "v1.35.7"
    assert rows[CLUSTER]["support"]["state"] in ("ending", "ended", "supported")
    assert rows["prod-us-east"]["mode"] == "manual"
    assert data["calendar"]


def test_viewer_cannot_see_the_fleet(client, viewer_token):
    assert client.get("/api/upgrades/fleet", headers=auth_headers(viewer_token)).status_code == 403


def test_request_needs_the_typed_phrase(client, admin_token, simulated):
    response = client.post("/api/upgrades/requests", headers=auth_headers(admin_token),
                           json={"clusterId": CLUSTER, "targetVersion": "v1.35.7", "confirmation": "yes"})
    assert response.status_code == 400
    assert response.get_json()["data"]["requiredConfirmation"] == "UPGRADE Staging EU-West TO v1.35.7"


def _non_admin_requester(monkeypatch, center):
    """The operator, allowed to start upgrades on every cluster (the seeded
    operator role has no upgrades:start)."""
    operator = User.query.filter_by(username="operator").first()
    real_has, real_access = center._has, center._can_access
    monkeypatch.setattr(center, "_has", lambda user, perm: True if user is operator else real_has(user, perm))
    monkeypatch.setattr(center, "_can_access", lambda user, cid: True if user is operator else real_access(user, cid))
    return operator


def test_request_approval_run_and_history(app, client, admin_token, simulated, monkeypatch):
    from api.services import approvals_service
    from api.services.upgrade_center import create_request, decide

    _require(1)
    operator = _non_admin_requester(monkeypatch, simulated)
    job, err, status = create_request(
        operator, CLUSTER, "v1.35.7", confirmation="UPGRADE Staging EU-West TO v1.35.7",
        options={"waveSize": 2, "drainTimeoutMinutes": 5},
    )
    assert err is None and status == 201, err
    assert job["status"] == "awaiting_approval"
    assert job["approvals"]["required"] == 1
    assert job["options"]["waveSize"] == 2
    job_id = job["jobId"]

    # A second request for the same cluster waits for this one.
    again = client.post("/api/upgrades/requests", headers=auth_headers(admin_token), json={
        "clusterId": CLUSTER, "targetVersion": "v1.35.7", "confirmation": "UPGRADE Staging EU-West TO v1.35.7"})
    assert again.status_code == 409

    # Nobody approves their own upgrade.
    _data, own_err, own_status = decide(operator, job_id, "approve", None)
    assert own_status == 403 and "own" in own_err

    admin = User.query.filter_by(username="admin").first()
    inbox = approvals_service.inbox(admin)
    item = next(i for i in inbox["items"] if i["kind"] == "cluster_upgrade")
    assert item["canDecide"] is True
    assert item["actions"]["approve"].endswith(f"/requests/{job_id}/approve")
    assert approvals_service.count_waiting(admin) >= 1

    approved = client.post(f"/api/upgrades/requests/{job_id}/approve", headers=auth_headers(admin_token),
                           json={"note": "go"})
    assert approved.status_code == 200, approved.get_json()
    done = _wait_for(client, admin_token, job_id, ("completed", "failed"))
    assert done["status"] == "completed", done.get("error")
    assert all(n["status"] == "done" for n in done["nodes"])
    assert [n["wave"] for n in done["nodes"] if n["role"] == "worker"] == [1, 1, 2, 2]

    history = client.get("/api/upgrades/jobs", headers=auth_headers(admin_token)).get_json()["data"]["items"]
    assert history[0]["jobId"] == job_id and "log" not in history[0]
    fleet = client.get("/api/upgrades/fleet", headers=auth_headers(admin_token)).get_json()["data"]
    row = next(r for r in fleet["clusters"] if r["id"] == CLUSTER)
    assert row["version"] == "v1.35.7"
    assert row["openJob"] is None


def test_an_admin_upgrade_needs_no_approval(app, client, admin_token, simulated):
    _require(1)  # the cluster asks for one approval; an admin's upgrade skips it
    workspace = client.get(f"/api/upgrades/clusters/{CLUSTER}", headers=auth_headers(admin_token)).get_json()["data"]
    assert workspace["requiredApprovals"] == 0 and workspace["clusterApprovals"] == 1
    response = client.post("/api/upgrades/requests", headers=auth_headers(admin_token), json={
        "clusterId": CLUSTER, "targetVersion": "v1.35.7", "confirmation": "UPGRADE Staging EU-West TO v1.35.7"})
    assert response.status_code == 201, response.get_json()
    job = response.get_json()["data"]
    assert job["status"] in ("queued", "running")
    assert job["approvals"]["required"] == 0
    done = _wait_for(client, admin_token, job["jobId"], ("completed", "failed"))
    assert done["status"] == "completed", done.get("error")


def test_an_admin_upgrade_in_a_window_is_scheduled_without_approval(app, client, admin_token, simulated):
    _require(1)
    response = client.post("/api/upgrades/requests", headers=auth_headers(admin_token), json={
        "clusterId": CLUSTER, "targetVersion": "v1.35.7", "confirmation": "UPGRADE Staging EU-West TO v1.35.7",
        "schedule": {"mode": "window", "start": "2099-01-01T02:00:00Z", "end": "2099-01-01T05:00:00Z"}})
    assert response.status_code == 201, response.get_json()
    assert response.get_json()["data"]["status"] == "scheduled"


def test_blocker_needs_an_admin_waiver(app, client, admin_token, simulated):
    _require(0)
    phrase = "UPGRADE Staging EU-West TO v1.36.4"
    blocked = client.post("/api/upgrades/requests", headers=auth_headers(admin_token), json={
        "clusterId": CLUSTER, "targetVersion": "v1.36.4", "confirmation": phrase})
    # Two minors at once is refused before readiness even runs.
    assert blocked.status_code == 400


def test_pause_and_resume_a_simulated_run(app, client, admin_token, simulated, monkeypatch):
    import api.upgrade_executor as ex

    monkeypatch.setattr(ex, "PAUSE_POLL_SECONDS", 0.05)
    simulated.MOCK_STEP_SECONDS = 0.05
    _require(0)
    response = client.post("/api/upgrades/requests", headers=auth_headers(admin_token), json={
        "clusterId": CLUSTER, "targetVersion": "v1.35.7", "confirmation": "UPGRADE Staging EU-West TO v1.35.7",
        "options": {"etcdSnapshot": False}})
    assert response.status_code == 201, response.get_json()
    job_id = response.get_json()["data"]["jobId"]
    assert client.post(f"/api/upgrades/jobs/{job_id}/pause", headers=auth_headers(admin_token)).status_code == 200
    paused = _wait_for(client, admin_token, job_id, ("paused", "completed"))
    assert paused["status"] == "paused"
    assert client.post(f"/api/upgrades/jobs/{job_id}/resume", headers=auth_headers(admin_token)).status_code == 200
    done = _wait_for(client, admin_token, job_id, ("completed", "failed"), timeout=30)
    assert done["status"] == "completed"


def test_runbook_for_a_manual_cluster(client, admin_token, simulated):
    response = client.get("/api/upgrades/clusters/prod-us-east/runbook", headers=auth_headers(admin_token))
    assert response.status_code == 200
    data = response.get_json()["data"]
    assert data["mode"] == "manual"
    assert data["steps"]


def test_kubeadm_guided_runbook_has_a_step_per_node():
    from api.services.upgrade_center import build_runbook

    snap = {
        "provider": "kubeadm", "version": "v1.34.4", "pools": [], "providerSupport": {}, "modeReason": "",
        "nodes": [
            {"name": "cp-1", "role": "control-plane", "version": "v1.34.4"},
            {"name": "cp-2", "role": "control-plane", "version": "v1.34.4"},
            {"name": "w-1", "role": "worker", "version": "v1.35.7"},
        ],
    }
    book = build_runbook(snap, "v1.35.7", "lab")
    keys = [s["key"] for s in book["steps"]]
    assert keys == ["snapshot", "node:cp-1", "node:cp-2", "node:w-1"]
    cp1 = book["steps"][1]
    assert any("kubeadm upgrade apply -y v1.35.7" in c["text"] for c in cp1["commands"])
    assert any("kubeadm upgrade node" in c["text"] for c in book["steps"][2]["commands"])
    assert book["steps"][3]["state"] == "done"  # w-1 already on the target
    assert book["observedDone"] is False


def test_eks_runbook_ticks_off_from_versions():
    from api.services.upgrade_center import build_runbook

    snap = {
        "provider": "eks", "version": "v1.36.2-eks", "pools": ["ng-a"], "providerSupport": {}, "modeReason": "",
        "nodes": [{"name": "n1", "role": "worker", "version": "v1.36.2-eks", "pool": "ng-a"}],
    }
    book = build_runbook(snap, "v1.36.4", "analytics")
    assert book["steps"][0]["state"] == "done"
    assert "--kubernetes-version 1.36" in book["steps"][0]["commands"][0]["text"]
    assert book["steps"][1]["state"] == "done"
    assert book["placeholders"]["clusterName"] == "analytics"
    assert book["observedDone"] is True
