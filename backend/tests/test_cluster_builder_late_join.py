"""A worker that registers after kubeadm stopped waiting still joins.

Seen live: the network's DNS timed out on the node names, the kubelet
registered two minutes late, and kubeadm failed with "error writing CRISocket
for this node: nodes ... not found" while the node was in fact joining.
"""

from __future__ import annotations

from api.models import ClusterBuild
from api.db import db
from api.services.cluster_build import executor
from api.services.ssh import SshCommandError, set_transport_factory
from tests.test_cluster_builds import (  # noqa: F401 — fixtures are used by name
    SINGLE_CP_NODES,
    auth_headers,
    build_default_fake,
    create_build,
    fake_ssh,
    make_build_payload,
    ssh_profile,
)

LATE = (
    "[kubelet-start] Waiting for the kubelet to perform the TLS Bootstrap\n"
    "error execution phase kubelet-wait-bootstrap: error writing CRISocket for this node: "
    'nodes "w-1" not found\n'
)


def _start(client, admin_token, ssh_profile, fake):  # noqa: F811
    set_transport_factory(lambda: fake)
    build = create_build(client, admin_token, ssh_profile, make_build_payload(nodes=SINGLE_CP_NODES))
    client.post(f"/api/cluster-builds/{build['id']}/preflight", headers=auth_headers(admin_token))
    client.post(f"/api/cluster-builds/{build['id']}/start",
                json={"ackWarnings": ["ack"]}, headers=auth_headers(admin_token))
    return client.get(f"/api/cluster-builds/{build['id']}",
                      headers=auth_headers(admin_token)).get_json()["data"]


def test_a_late_registration_still_joins(client, admin_token, ssh_profile, fake_ssh, monkeypatch):  # noqa: F811
    monkeypatch.setattr(executor.time, "sleep", lambda s: None)
    fake = build_default_fake({"10.0.0.11": ("cp-1", "control_plane"), "10.0.0.21": ("w-1", "worker")})
    fake.responders.insert(0, (
        lambda host, script: "kubeadm join" in script and host == "10.0.0.21",
        SshCommandError("boom", exit_code=1, output=LATE),
    ))
    fake.responders.insert(0, (lambda host, script: "get node w-1 -o name" in script, "node/w-1\n"))
    data = _start(client, admin_token, ssh_profile, fake)
    assert data["status"] == "completed", data.get("error")
    annotated = [s for _, s in fake.calls if "annotate node w-1" in s]
    assert annotated and "cri-socket=unix:///var/run/containerd/containerd.sock" in annotated[0]


def test_a_node_that_never_registers_still_fails(client, admin_token, ssh_profile, fake_ssh, monkeypatch):  # noqa: F811
    monkeypatch.setattr(executor.time, "sleep", lambda s: None)
    clock = iter(range(0, 10_000, 60))
    monkeypatch.setattr(executor.time, "monotonic", lambda: next(clock))
    fake = build_default_fake({"10.0.0.11": ("cp-1", "control_plane"), "10.0.0.21": ("w-1", "worker")})
    fake.responders.insert(0, (
        lambda host, script: "kubeadm join" in script and host == "10.0.0.21",
        SshCommandError("boom", exit_code=1, output=LATE),
    ))
    data = _start(client, admin_token, ssh_profile, fake)
    assert data["status"] == "failed" and "join_workers" in (data["error"] or "")


def test_node_names_go_into_etc_hosts(app):
    from api.models import ClusterBuildNode

    build = ClusterBuild(nodes=[
        ClusterBuildNode(role="control_plane", hostname="test-vm-1", address="10.4.112.2"),
        ClusterBuildNode(role="worker", hostname="test-vm-2", address="10.4.112.3"),
        ClusterBuildNode(role="worker", hostname="bad;name", address="10.4.112.4"),
    ])
    script = executor._hosts_script(build)
    assert "10.4.112.2 test-vm-1 # kubesight-node" in script
    assert "10.4.112.3 test-vm-2 # kubesight-node" in script
    assert "bad;name" not in script
    assert "grep -v '# kubesight-node$' /etc/hosts" in script


def test_a_failed_join_is_reset_and_tried_again_on_its_own(client, admin_token, ssh_profile, fake_ssh):  # noqa: F811
    fake = build_default_fake({"10.0.0.11": ("cp-1", "control_plane"), "10.0.0.21": ("w-1", "worker")})
    state = {"failures": 0}

    def _fail_once(host, script):
        if "kubeadm join" in script and host == "10.0.0.21" and state["failures"] < 1:
            state["failures"] += 1
            return True
        return False

    fake.responders.insert(0, (_fail_once, SshCommandError("boom", exit_code=1, output="kubelet exploded")))
    data = _start(client, admin_token, ssh_profile, fake)
    assert data["status"] == "completed", data.get("error")
    worker_scripts = [s for host, s in fake.calls if host == "10.0.0.21"]
    reset_at = next(i for i, s in enumerate(worker_scripts) if "kubeadm reset -f" in s)
    joins = [i for i, s in enumerate(worker_scripts) if "kubeadm join" in s]
    assert len(joins) == 2 and joins[0] < reset_at < joins[1]
