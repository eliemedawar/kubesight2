"""A Helm app counts as installed only once its workloads run.

Helm installs without --wait, so "installed" used to mean only that the
manifests were accepted. Now the release's Deployments/StatefulSets/DaemonSets
must roll out; when they do not, the app is "not_ready" with what its pods are
doing, and a retry checks again — it never installs again, because the values
(passwords) are dropped once the chart is in.
"""

from __future__ import annotations

from api.db import db
from api.models import ClusterBuildApp
from api.services.ssh import SshCommandError

from tests.test_cluster_builder_apps import _app, _start, chart, fake, helm  # noqa: F401
from tests.test_cluster_builds import (  # noqa: F401
    SINGLE_CP_NODES,
    auth_headers,
    create_build,
    make_build_payload,
    ssh_profile,
)

LISTED = "deployment.apps/web\n"


def _listing(fake):
    fake.responders.insert(0, (
        lambda h, s: "get deployment,statefulset,daemonset -l" in s and "instance=web" in s, LISTED,
    ))


def test_the_app_waits_for_its_workloads(client, admin_token, ssh_profile, fake, chart, helm):  # noqa: F811
    _listing(fake)
    build = create_build(client, admin_token, ssh_profile,
                         make_build_payload(nodes=SINGLE_CP_NODES, apps=[_app(chart)]))
    data = _start(client, admin_token, build["id"])
    assert data["status"] == "completed", data.get("error")
    assert any("rollout status deployment.apps/web" in s for _, s in fake.calls)
    assert data["apps"][0]["status"] == "installed"


def test_an_app_that_does_not_run_fails_with_why_and_retry_only_checks(
    client, admin_token, ssh_profile, fake, chart, helm, app,  # noqa: F811
):
    _listing(fake)
    state = {"stuck": True}
    fake.responders.insert(0, (
        lambda h, s: "rollout status deployment.apps/web" in s and state["stuck"],
        SshCommandError("timeout", exit_code=1, output="error: timed out waiting for the condition"),
    ))
    fake.responders.insert(0, (
        lambda h, s: "get pods -l" in s and "instance=web" in s,
        "web-5d9c7c7b9-abcde   0/1   ImagePullBackOff   0   10m\n",
    ))
    build = create_build(client, admin_token, ssh_profile,
                         make_build_payload(nodes=SINGLE_CP_NODES, apps=[_app(chart)]))
    data = _start(client, admin_token, build["id"])
    assert data["status"] == "failed"
    assert "ImagePullBackOff" in (data["error"] or "")
    with app.app_context():
        row = ClusterBuildApp.query.one()
        assert row.status == "not_ready" and "ImagePullBackOff" in row.error
    assert len(helm["install"]) == 1

    # The image is fixed; Retry checks again and does not reinstall.
    state["stuck"] = False
    response = client.post(f"/api/cluster-builds/{build['id']}/retry", headers=auth_headers(admin_token))
    assert response.status_code == 200, response.get_json()
    assert response.get_json()["data"]["status"] == "completed"
    assert len(helm["install"]) == 1
    with app.app_context():
        db.session.expire_all()
        assert ClusterBuildApp.query.one().status == "installed"
