"""New VMs keep their network profiles where RHEL 9 keeps them.

VMware's Linux guest customization writes the address it sets as
/etc/sysconfig/network-scripts/ifcfg-<nic>. Once a new VM answers SSH,
KubeSight moves its profiles to NetworkManager keyfiles in
/etc/NetworkManager/system-connections (nmcli connection migrate), drops the
template's leftover profile on the same card and gives the active one its name.
"""

from __future__ import annotations

from api.db import db
from api.models import ClusterBuild, ClusterProvisionJob
from api.services.cluster_build.provisioning import jobs
from api.services.ssh import set_transport_factory
from api.services.ssh.transport import SshTarget

from tests.test_cluster_builds import FakeSshTransport


def _job(app):
    build = ClusterBuild(name="net", k8s_version="1.32.4", topology_type="single_cp",
                         endpoint_mode="manual_endpoint", cni_plugin="calico",
                         pod_cidr="10.244.0.0/16", service_cidr="10.96.0.0/12")
    db.session.add(build)
    db.session.commit()
    job = ClusterProvisionJob(build_id=build.id, operation="create", status="connecting")
    db.session.add(job)
    db.session.commit()
    return job


def test_the_script_migrates_to_keyfiles_and_never_takes_the_link_down():
    script = jobs.NETWORK_KEYFILE_SCRIPT
    assert "nmcli connection migrate" in script
    assert "connection.id" in script  # the active profile takes the template's name
    # Only an inactive profile on the same card is deleted; nothing is brought down.
    assert "connection down" not in script and "device disconnect" not in script


def test_each_vm_is_tidied_and_logged(app):
    with app.app_context():
        job = _job(app)
        fake = FakeSshTransport()
        fake.add(lambda host, script: host == "10.0.0.1" and "connection migrate" in script,
                 "KS_NET_DELETED=ens33\nKS_NET_RENAMED=ens33:ens33\nKS_NET=migrated\n")
        fake.add(lambda host, script: host == "10.0.0.2" and "connection migrate" in script,
                 "KS_NET=keyfile-already\n")
        set_transport_factory(lambda: fake)
        try:
            jobs._network_keyfiles(job, [("vm-1", SshTarget(host="10.0.0.1")),
                                         ("vm-2", SshTarget(host="10.0.0.2"))])
        finally:
            set_transport_factory(None)
        db.session.refresh(job)
        assert "vm-1: profiles moved to /etc/NetworkManager/system-connections, named ens33" in job.log_tail
        assert "vm-2" not in job.log_tail  # already keyfiles: nothing to say
        assert {host for host, _ in fake.calls} == {"10.0.0.1", "10.0.0.2"}


def test_a_vm_that_cannot_be_tidied_does_not_fail_the_job(app):
    with app.app_context():
        job = _job(app)
        fake = FakeSshTransport()
        fake.add(lambda host, script: True, RuntimeError("connection reset"))
        set_transport_factory(lambda: fake)
        try:
            jobs._network_keyfiles(job, [("vm-1", SshTarget(host="10.0.0.1"))])
        finally:
            set_transport_factory(None)
        db.session.refresh(job)
        assert "could not move its profiles" in job.log_tail
