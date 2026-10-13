"""Inventory Helm charts installed on the cluster a build makes.

Picked in the wizard (installed by the apps phase once the cluster is up) or on
day two. What matters: the values are the person's whole values.yaml layered on
the chart's defaults and the chosen values-*.yaml; passwords are kept encrypted,
shown masked and dropped once installed; a chart that would not install fails
preflight; a failed install resumes at that chart; and on a cluster that needs
approval a day-two chart waits in a change bundle that never holds its values.
"""

from __future__ import annotations

import base64
import io
import zipfile

import pytest

from api.db import db
from api.models import ChangeBundle, ClusterBuildApp, DeploymentRequestSetting
from api.services import helm_service
from api.services.cluster_build import apps as apps_mod
from api.services.cluster_build import service as build_service
from api.services.ssh import set_transport_factory

from tests.test_cluster_builds import (
    SINGLE_CP_NODES,
    auth_headers,
    build_default_fake,
    create_build,
    make_build_payload,
)
from tests.test_cluster_builder_addons_proxy import (  # noqa: F401
    pinned_manifests_without_network,
    reset_ssh_transport,
    ssh_profile,
)

HOSTS = {"10.0.0.11": ("cp-1", "control_plane"), "10.0.0.21": ("w-1", "worker")}
PASSWORD = "s3cr3t-db-pass"

CHART = {
    "web/Chart.yaml": "apiVersion: v2\nname: web\nversion: 1.2.0\nappVersion: '1.0'\n",
    "web/values.yaml": (
        "replicaCount: 1\nimage:\n  repository: nginx\n  tag: '1.27'\n"
        "database:\n  host: pg\n  password: default-pass\n"
    ),
    "web/values-uat.yaml": "replicaCount: 2\ndatabase:\n  host: pg-uat\n",
    "web/templates/deployment.yaml": (
        "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: {{ .Release.Name }}\n"
    ),
}


def _zip(entries):
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for path, content in entries.items():
            archive.writestr(path, content)
    return base64.b64encode(stream.getvalue()).decode("ascii")


@pytest.fixture(autouse=True)
def _no_approvals(no_cluster_approvals):
    yield


@pytest.fixture()
def chart(client, admin_token):
    response = client.post(
        "/api/helm/chart-templates/import/archive",
        headers=auth_headers(admin_token),
        json={"filename": "web.zip", "archiveBase64": _zip(CHART)},
    )
    assert response.status_code == 201, response.get_json()
    data = response.get_json()["data"]
    uat = next(f["path"] for f in data["chart"]["valuesFiles"] if "uat" in f["path"])
    return {"slug": data["id"], "uat": uat}


@pytest.fixture()
def helm(monkeypatch):
    """No helm binary here: renders and installs are recorded instead."""
    calls = {"render": [], "install": [], "fail": {}}

    def render(app, values_yaml):
        calls["render"].append((app.release_name, values_yaml))
        return None

    def install(payload, context):
        calls["install"].append((payload, context))
        error = calls["fail"].pop(payload["releaseName"], None)
        if error:
            return None, error, 400
        return {"output": f"release {payload['releaseName']} deployed"}, None, 200

    monkeypatch.setattr(apps_mod, "_render_locally", render)
    monkeypatch.setattr(apps_mod, "_install_release", install)
    monkeypatch.setattr(build_service, "_existing_release", lambda *a: False)
    return calls


def _app(chart, **overrides):
    item = {
        "chartSlug": chart["slug"],
        "valuesFile": chart["uat"],
        "namespace": "shop",
        "releaseName": "web",
        "valuesYaml": f"database:\n  password: {PASSWORD}\n",
    }
    item.update(overrides)
    return item


def _start(client, token, build_id):
    response = client.post(f"/api/cluster-builds/{build_id}/preflight", headers=auth_headers(token))
    verdict = response.get_json()["data"]
    if verdict["status"] == "fail":
        return verdict
    response = client.post(
        f"/api/cluster-builds/{build_id}/start", json={"ackWarnings": ["ack"]},
        headers=auth_headers(token),
    )
    assert response.status_code == 200, response.get_json()
    return client.get(f"/api/cluster-builds/{build_id}", headers=auth_headers(token)).get_json()["data"]


@pytest.fixture()
def fake(ssh_profile):
    fake = build_default_fake(HOSTS)
    set_transport_factory(lambda: fake)
    return fake


# ---------------------------------------------------------------------------

class TestValues:
    def test_editor_starts_from_defaults_and_the_values_file(self, client, admin_token, chart):
        response = client.get(
            f"/api/cluster-builds/app-values?chart={chart['slug']}&valuesFile={chart['uat']}",
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 200, response.get_json()
        data = response.get_json()["data"]
        assert "replicaCount: 2" in data["valuesYaml"]
        assert "pg-uat" in data["valuesYaml"]
        # Import blanked the password, and it is required.
        assert "default-pass" not in data["valuesYaml"]
        assert any(i["path"] == "database.password" for i in data["requiredInputs"])

    def test_unknown_values_file_is_refused(self, client, admin_token, chart):
        response = client.get(
            f"/api/cluster-builds/app-values?chart={chart['slug']}&valuesFile=nope.yaml",
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 400

    def test_overlay_must_be_a_mapping(self, app, chart):
        from api.services import helm_chart_template_service as charts

        with app.app_context():
            with pytest.raises(charts.ChartTemplateError, match="mapping"):
                charts.build_values_yaml(chart["slug"], {}, "", "", "- a\n- b\n")
            with pytest.raises(charts.ChartTemplateError, match="not valid YAML"):
                charts.build_values_yaml(chart["slug"], {}, "", "", "a: [1, 2\n")

    def test_overlay_fills_required_inputs_and_wins(self, app, chart):
        from api.services import helm_chart_template_service as charts

        with app.app_context():
            with pytest.raises(charts.ChartTemplateError, match="required"):
                charts.build_values_yaml(chart["slug"], {}, "", chart["uat"], "")
            text = charts.build_values_yaml(
                chart["slug"], {}, "", chart["uat"],
                f"replicaCount: 5\ndatabase:\n  password: {PASSWORD}\n",
            )
        assert "replicaCount: 5" in text
        assert PASSWORD in text and "pg-uat" in text

    def test_inventory_template_installs_take_a_whole_values_yaml(self, app, chart):
        from api.services import helm_chart_template_service as charts

        with app.app_context():
            prepared = charts.prepare_chart_template_payload({
                "chartSource": "template", "chartTemplateId": chart["slug"],
                "valuesYaml": f"database:\n  password: {PASSWORD}\n",
            })
        assert PASSWORD in prepared["valuesYaml"]


class TestWizardBuild:
    def test_installs_after_the_cluster_with_its_values(
        self, client, admin_token, ssh_profile, fake, chart, helm, app,
    ):
        build = create_build(client, admin_token, ssh_profile, make_build_payload(
            nodes=SINGLE_CP_NODES,
            apps=[_app(chart), _app(chart, releaseName="web-2", namespace="shop-2",
                                    valuesFile="")],
        ))
        assert [a["releaseName"] for a in build["apps"]] == ["web", "web-2"]
        data = _start(client, admin_token, build["id"])
        assert data["status"] == "completed", data.get("error")

        assert [p["releaseName"] for p, _ in helm["install"]] == ["web", "web-2"]
        payload, context = helm["install"][0]
        assert context == f"cluster_build:{build['id']}"
        assert payload["clusterId"] == data["resultClusterId"]
        assert payload["chartSource"] == "template" and payload["valuesFile"] == chart["uat"]
        assert PASSWORD in payload["valuesYaml"]
        assert all(a["status"] == "installed" for a in data["apps"])
        assert any(s["phase"] == "apps" and s["status"] == "completed" for s in data["steps"])
        with app.app_context():
            # The passwords are not kept once the chart is in.
            assert all(row.values_cipher is None for row in ClusterBuildApp.query.all())

    def test_values_are_encrypted_and_shown_masked(
        self, client, admin_token, ssh_profile, chart, helm, app,
    ):
        build = create_build(client, admin_token, ssh_profile, make_build_payload(
            nodes=SINGLE_CP_NODES, apps=[_app(chart)],
        ))
        shown = build["apps"][0]["valuesYaml"]
        assert PASSWORD not in shown and apps_mod.MASK in shown
        with app.app_context():
            row = ClusterBuildApp.query.one()
            assert PASSWORD not in row.values_cipher
            assert PASSWORD in apps_mod.values_yaml_of(row)

        # Sending the mask back keeps the stored password; editing the rest works.
        response = client.put(
            f"/api/cluster-builds/{build['id']}",
            json={"apps": [{**build["apps"][0], "valuesYaml": shown + "replicaCount: 3\n"}]},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 200, response.get_json()
        with app.app_context():
            text = apps_mod.values_yaml_of(ClusterBuildApp.query.one())
        assert PASSWORD in text and "replicaCount: 3" in text

    def test_a_mask_with_nothing_behind_it_is_refused(
        self, client, admin_token, ssh_profile, chart, helm,
    ):
        payload = make_build_payload(
            nodes=SINGLE_CP_NODES,
            apps=[_app(chart, valuesYaml=f"database:\n  password: '{apps_mod.MASK}'\n")],
        )
        payload["connectionProfileId"] = ssh_profile["id"]
        response = client.post("/api/cluster-builds", json=payload, headers=auth_headers(admin_token))
        assert response.status_code == 400
        assert "no saved value" in response.get_json()["error"]

    def test_missing_password_fails_preflight(
        self, client, admin_token, ssh_profile, fake, chart, helm,
    ):
        build = create_build(client, admin_token, ssh_profile, make_build_payload(
            nodes=SINGLE_CP_NODES, apps=[_app(chart, valuesYaml="replicaCount: 2\n")],
        ))
        verdict = _start(client, admin_token, build["id"])
        assert verdict["status"] == "fail"
        check = next(c for c in verdict["buildChecks"] if c["id"].startswith("app_"))
        assert check["status"] == "fail" and "required" in check["detail"]
        assert not helm["install"]

    def test_chart_that_does_not_render_fails_preflight(
        self, client, admin_token, ssh_profile, fake, chart, helm, monkeypatch,
    ):
        monkeypatch.setattr(apps_mod, "_render_locally", lambda app, values: "template: bad value")
        build = create_build(client, admin_token, ssh_profile, make_build_payload(
            nodes=SINGLE_CP_NODES, apps=[_app(chart)],
        ))
        verdict = _start(client, admin_token, build["id"])
        assert verdict["status"] == "fail"
        assert any("bad value" in c["detail"] for c in verdict["buildChecks"])

    def test_duplicate_release_in_a_namespace_is_refused(
        self, client, admin_token, ssh_profile, chart, helm,
    ):
        payload = make_build_payload(nodes=SINGLE_CP_NODES, apps=[_app(chart), _app(chart)])
        payload["connectionProfileId"] = ssh_profile["id"]
        response = client.post("/api/cluster-builds", json=payload, headers=auth_headers(admin_token))
        assert response.status_code == 400
        assert "twice" in response.get_json()["error"]

    def test_failed_install_resumes_at_that_chart(
        self, client, admin_token, ssh_profile, fake, chart, helm,
    ):
        helm["fail"]["web-2"] = "Error: INSTALLATION FAILED: timed out"
        build = create_build(client, admin_token, ssh_profile, make_build_payload(
            nodes=SINGLE_CP_NODES,
            apps=[_app(chart), _app(chart, releaseName="web-2")],
        ))
        data = _start(client, admin_token, build["id"])
        assert data["status"] == "failed"
        assert "timed out" in data["error"]
        by_name = {a["releaseName"]: a for a in data["apps"]}
        assert by_name["web"]["status"] == "installed"
        assert by_name["web-2"]["status"] == "failed"
        # The cluster stays registered.
        assert data["resultClusterId"]

        before = len(helm["install"])
        response = client.post(f"/api/cluster-builds/{build['id']}/retry", headers=auth_headers(admin_token))
        assert response.status_code == 200, response.get_json()
        data = response.get_json()["data"]
        assert data["status"] == "completed", data.get("error")
        assert [p["releaseName"] for p, _ in helm["install"][before:]] == ["web-2"]

    def test_a_vms_only_build_takes_no_charts(self, client, admin_token, chart, helm):
        response = client.post("/api/cluster-builds", json={
            "name": "vms", "machineSource": "vmware", "vmsOnly": True,
            "apps": [_app(chart)],
        }, headers=auth_headers(admin_token))
        assert response.status_code == 400

    def test_needs_helm_install(self, client, admin_token, ssh_profile, chart, helm, app, monkeypatch):
        import api.access_engine as access_engine

        real = access_engine.user_has_permission
        monkeypatch.setattr(
            access_engine, "user_has_permission",
            lambda user, perm: False if perm == "helm:install" else real(user, perm),
        )
        payload = make_build_payload(nodes=SINGLE_CP_NODES, apps=[_app(chart)])
        payload["connectionProfileId"] = ssh_profile["id"]
        response = client.post("/api/cluster-builds", json=payload, headers=auth_headers(admin_token))
        assert response.status_code == 403


@pytest.fixture()
def built(client, admin_token, ssh_profile, fake, helm):
    build = create_build(client, admin_token, ssh_profile, make_build_payload(nodes=SINGLE_CP_NODES))
    data = _start(client, admin_token, build["id"])
    assert data["status"] == "completed", data.get("error")
    return data


class TestDayTwo:
    def test_installs_on_a_cluster_that_needs_no_approval(
        self, client, admin_token, built, chart, helm,
    ):
        response = client.post(
            f"/api/cluster-builds/{built['id']}/apps", json={"apps": [_app(chart)]},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 200, response.get_json()
        data = response.get_json()["data"]
        assert data["status"] == "completed", data.get("error")
        assert data["apps"][0]["status"] == "installed"
        assert data["buildSeconds"] == built["buildSeconds"]
        payload, _ = helm["install"][-1]
        assert PASSWORD in payload["valuesYaml"]

    def test_an_existing_release_is_not_overwritten(
        self, client, admin_token, built, chart, helm, monkeypatch,
    ):
        monkeypatch.setattr(build_service, "_existing_release", lambda *a: True)
        response = client.post(
            f"/api/cluster-builds/{built['id']}/apps", json={"apps": [_app(chart)]},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 400
        assert "already in that namespace" in response.get_json()["error"]

    def test_queued_for_approval_without_its_values(
        self, client, admin_token, built, chart, helm, app, monkeypatch,
    ):
        cluster_id = built["resultClusterId"]
        with app.app_context():
            row = DeploymentRequestSetting.query.first()
            row.cluster_required_approvals = {cluster_id: 1}
            db.session.commit()
        rendered = (
            "apiVersion: v1\nkind: Secret\nmetadata:\n  name: web\n"
            f"stringData:\n  password: {PASSWORD}\n"
        )
        monkeypatch.setattr(helm_service, "ensure_helm_installed", lambda: None)
        monkeypatch.setattr(helm_service, "release_exists", lambda *a, **k: False)
        monkeypatch.setattr(
            helm_service, "run_helm",
            lambda access, args: rendered if args[0] == "template" else "",
        )

        response = client.post(
            f"/api/cluster-builds/{built['id']}/apps", json={"apps": [_app(chart)]},
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 202, response.get_json()
        data = response.get_json()["data"]
        assert data["pendingApproval"] and data["bundleIds"]
        assert data["status"] == "completed"  # the cluster is not touched yet
        queued = data["apps"][0]
        assert queued["status"] == "awaiting_approval" and queued["bundleId"]

        with app.app_context():
            bundle = db.session.get(ChangeBundle, queued["bundleId"])
            item = bundle.items[0]
            stored = str(item.new_payload_json) + str(item.yaml_preview)
            assert PASSWORD not in stored
            assert f"{apps_mod.REF_PREFIX}{queued['id']}" in stored

            # The reference opens exactly that install and nothing else.
            from api.services import helm_chart_template_service as charts

            ref = f"{apps_mod.REF_PREFIX}{queued['id']}"
            good = {"clusterId": cluster_id, "namespace": "shop", "releaseName": "web"}
            assert PASSWORD in apps_mod.values_for_ref(ref, slug=chart["slug"], payload=good)
            for bad in ({**good, "namespace": "other"}, {**good, "clusterId": "custom-999"}):
                with pytest.raises(charts.ChartTemplateError):
                    apps_mod.values_for_ref(ref, slug=chart["slug"], payload=bad)

            # Approved and applied: the next tick records it and drops the values.
            bundle.status = "completed"
            item.status = "succeeded"
            db.session.commit()
            assert apps_mod.advance_queued() == 1
            row = db.session.get(ClusterBuildApp, queued["id"])
            assert row.status == "installed" and row.values_cipher is None
            with pytest.raises(charts.ChartTemplateError):
                apps_mod.values_for_ref(ref, slug=chart["slug"], payload=good)

    def test_a_rejected_bundle_marks_the_chart_rejected(self, app, built, chart, helm):
        with app.app_context():
            build = build_service.get_build(built["id"])
            row = ClusterBuildApp(chart_slug=chart["slug"], chart_version="1.2.0",
                                  namespace="shop", release_name="web", status="awaiting_approval")
            build.apps.append(row)
            bundle = ChangeBundle(status="rejected")
            db.session.add(bundle)
            db.session.commit()
            row.bundle_id = bundle.id
            db.session.commit()
            apps_mod.advance_queued()
            assert db.session.get(ClusterBuildApp, row.id).status == "rejected"

    def test_a_failed_chart_can_be_withdrawn(self, client, admin_token, built, chart, helm):
        helm["fail"]["web"] = "Error: boom"
        data = client.post(
            f"/api/cluster-builds/{built['id']}/apps", json={"apps": [_app(chart)]},
            headers=auth_headers(admin_token),
        ).get_json()["data"]
        assert data["status"] == "failed"
        response = client.delete(
            f"/api/cluster-builds/{built['id']}/apps/{data['apps'][0]['id']}",
            headers=auth_headers(admin_token),
        )
        assert response.status_code == 200, response.get_json()
        data = response.get_json()["data"]
        assert data["status"] == "completed" and data["apps"] == []

    def test_needs_execute_permission(self, client, viewer_token, built, chart):
        response = client.post(
            f"/api/cluster-builds/{built['id']}/apps", json={"apps": [_app(chart)]},
            headers=auth_headers(viewer_token),
        )
        assert response.status_code == 403
