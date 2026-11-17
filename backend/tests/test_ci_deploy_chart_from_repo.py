"""A Helm Deploy stage whose chart travels with the code (chart source
"repository").

What is locked here:

* the target names the chart folder (and an optional values file) in the
  service's repository; an Inventory target reads and signs exactly as before;
* a build takes the chart and values file of the commit it built, records them
  as a new version of the Inventory chart (not made current), and deploys that
  version;
* values stack: the release's own values, then the repository's values file,
  then the images;
* a chart folder that is not a chart fails the stage before anything deploys.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from api.db import db
from api.models import HelmChartTemplate
from api.models_ci import CiService
from api.services import helm_chart_template_service as charts
from api.services.ci import deploy_config
from tests.test_ci_deploy_stage import (  # noqa: F401 — ``service`` is a fixture
    _approvals,
    _build,
    _logs,
    _run,
    _save,
    service,
)
from tests.test_ci_deploy_stage_helm import FakeHelm, _helm_deploy, _helm_stages, _patched
from tests.test_helm_values_import import _slug

CHART_YAML = "apiVersion: v2\nname: payments\nversion: 1.4.0\nappVersion: '2.0'\n"
CHART_VALUES = "replicaCount: 1\nimage:\n  repository: registry.local/payments\n  tag: '1.0'\nfeature:\n  enabled: false\n"
DEPLOYMENT = (
    "apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: {{ .Release.Name }}\n"
    "spec:\n  replicas: {{ .Values.replicaCount }}\n"
)
ENV_VALUES = "replicaCount: 3\nfeature:\n  enabled: true\n  mode: strict\n"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture()
def repo(tmp_path):
    """A repository with charts/payments and deploy/values-uat.yaml on main."""
    root = tmp_path / "payments-api"
    (root / "charts" / "payments" / "templates").mkdir(parents=True)
    (root / "deploy").mkdir()
    (root / "charts" / "payments" / "Chart.yaml").write_text(CHART_YAML, encoding="utf-8")
    (root / "charts" / "payments" / "values.yaml").write_text(CHART_VALUES, encoding="utf-8")
    (root / "charts" / "payments" / "templates" / "deployment.yaml").write_text(DEPLOYMENT, encoding="utf-8")
    (root / "deploy" / "values-uat.yaml").write_text(ENV_VALUES, encoding="utf-8")
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    _git(root, "-c", "user.email=ci@example.com", "-c", "user.name=ci", "add", ".")
    _git(root, "-c", "user.email=ci@example.com", "-c", "user.name=ci", "commit", "-q", "-m", "chart")
    return root


@pytest.fixture()
def local_git(repo):
    """Point every Git fetch at the local repository (no network, no token)."""
    with patch.object(charts, "_git_connection", return_value=(str(repo), "", "")):
        yield repo


def _repo_target(chart_id, **helm):
    deploy = _helm_deploy(chart_id)
    deploy["helm"].update({"chartSource": "repository", "chartPath": "charts/payments",
                           "valuesPath": "deploy/values-uat.yaml", **helm})
    return deploy


# ---------------------------------------------------------------------------
# Configuration rules
# ---------------------------------------------------------------------------

def test_an_inventory_target_reads_and_signs_as_before():
    plain = deploy_config.normalize(_helm_deploy("payments"), "deploy", "Deploy")
    assert "chartSource" not in plain["helm"] and not deploy_config.chart_from_repository(plain)
    sent = _helm_deploy("payments")
    sent["helm"]["chartSource"] = "inventory"
    assert deploy_config.signature(deploy_config.normalize(sent, "deploy", "Deploy")) == deploy_config.signature(plain)


def test_a_repository_target_names_the_chart_folder_and_values_file():
    config = deploy_config.normalize(_repo_target("payments", chartPath="./charts/payments/"), "deploy", "Deploy")
    assert deploy_config.chart_from_repository(config)
    assert config["helm"]["chartPath"] == "charts/payments"
    assert config["helm"]["valuesPath"] == "deploy/values-uat.yaml"
    assert config["helm"]["chartVersion"] == "" and config["helm"]["valuesFile"] == ""


@pytest.mark.parametrize("helm, message", [
    ({"chartPath": ""}, "name the chart folder"),
    ({"chartPath": "../secrets"}, "not a chart folder path"),
    ({"valuesPath": "deploy/values.json"}, "must be a .yaml file"),
    ({"chartSource": "s3"}, "not where a chart comes from"),
])
def test_a_repository_target_that_cannot_work_is_refused(helm, message):
    with pytest.raises(deploy_config.DeployConfigError, match=message):
        deploy_config.normalize(_repo_target("payments", **helm), "deploy", "Deploy")


# ---------------------------------------------------------------------------
# Reading the chart from a commit
# ---------------------------------------------------------------------------

def test_the_chart_and_values_of_one_commit_are_read(local_git):
    first = _git(local_git, "rev-parse", "HEAD")
    # A later commit changes the values; the first commit still reads as it was.
    (local_git / "deploy" / "values-uat.yaml").write_text("replicaCount: 9\n", encoding="utf-8")
    _git(local_git, "-c", "user.email=ci@example.com", "-c", "user.name=ci", "commit", "-q", "-am", "more")
    taken = charts.prepare_repository_chart(
        repository_url="https://bitbucket.org/areeba/payments-api", credential_profile_id=None,
        commit=first, branch="main", chart_path="charts/payments", values_path="deploy/values-uat.yaml",
    )
    assert taken["ref"] == first
    assert taken["prepared"]["version"] == "1.4.0" and taken["prepared"]["name"] == "payments"
    assert "templates/deployment.yaml" in taken["prepared"]["files"]
    assert taken["values"] == {"replicaCount": 3, "feature": {"enabled": True, "mode": "strict"}}


def test_a_folder_without_a_chart_is_refused(local_git):
    with pytest.raises(charts.ChartTemplateError, match="no Chart.yaml"):
        charts.prepare_repository_chart(
            repository_url="x", credential_profile_id=None, commit="", branch="main", chart_path="deploy",
        )
    with pytest.raises(charts.ChartTemplateError, match="does not exist"):
        charts.prepare_repository_chart(
            repository_url="x", credential_profile_id=None, commit="", branch="main", chart_path="charts/nope",
        )


# ---------------------------------------------------------------------------
# A build deploys the chart of its commit
# ---------------------------------------------------------------------------

def _with_repository(app, service_id):
    with app.app_context():
        row = db.session.get(CiService, service_id)
        row.repository_url = "https://bitbucket.org/areeba/payments-api"
        db.session.commit()


def test_a_build_records_the_repository_chart_and_deploys_it(app, client, admin_token, service, local_git):
    _approvals(app, 0)
    _with_repository(app, service.id)
    chart_id = _slug(client, admin_token)
    response = _save(client, admin_token, service.pipeline_id, _helm_stages(_repo_target(chart_id)))
    assert response.status_code == 200, response.get_json()
    fake = FakeHelm(
        releases={"payments": {"name": "payments", "revision": "4", "status": "deployed"}},
        current={"replicaCount": 6, "database": {"password": "live-password"}, "feature": {"enabled": False}},
    )
    installed, runner = _patched(fake)
    with installed, runner:
        build_id = _run(app, client, admin_token, service.id)

    data = _build(client, admin_token, build_id)
    assert data["status"] == "success", data
    values = fake.installed_values[0]
    # The release's own values are kept; the repository's values file wins over
    # them; the image goes in last.
    assert values["database"]["password"] == "live-password"
    assert values["replicaCount"] == 3
    assert values["feature"] == {"enabled": True, "mode": "strict"}
    assert values["image"]["tag"] == "2.0.0"

    version = f"1.4.0+b{data['number']}"
    with app.app_context():
        chart = HelmChartTemplate.query.filter_by(slug=chart_id).first()
        recorded = [item for item in chart.versions if item.version == version]
        assert recorded and recorded[0].source_type == "git-chart"
        assert chart.version != version  # recorded, not made current
    log = _logs(client, admin_token, build_id, data["stages"][2]["id"])
    assert f"Chart charts/payments at the tip of main recorded as payments {version}" in log


def test_a_bad_chart_folder_fails_the_stage_and_deploys_nothing(app, client, admin_token, service, local_git):
    _approvals(app, 0)
    _with_repository(app, service.id)
    chart_id = _slug(client, admin_token)
    _save(client, admin_token, service.pipeline_id, _helm_stages(_repo_target(chart_id, chartPath="deploy")))
    fake = FakeHelm(releases={"payments": {"name": "payments", "revision": "4", "status": "deployed"}})
    installed, runner = _patched(fake)
    with installed, runner:
        build_id = _run(app, client, admin_token, service.id)

    data = _build(client, admin_token, build_id)
    assert data["status"] == "failed"
    assert not [call for call in fake.calls if call[:2] == ["upgrade", "--install"]]
    log = _logs(client, admin_token, build_id, data["stages"][2]["id"])
    assert "no Chart.yaml" in log and "Nothing was deployed" in log


# ---------------------------------------------------------------------------
# Checks on the rendered chart: it must render, and keep the platform rules
# ---------------------------------------------------------------------------

from api.services.ci import chart_policy  # noqa: E402

GOOD_POD = """
apiVersion: apps/v1
kind: Deployment
metadata: {name: api}
spec:
  template:
    spec:
      imagePullSecrets: [{name: regcred}]
      securityContext: {runAsNonRoot: true}
      containers:
        - name: api
          image: registry.areeba.com/ai-guard/admin@sha256:abc
          securityContext: {allowPrivilegeEscalation: false}
          resources: {requests: {cpu: 100m, memory: 128Mi}, limits: {memory: 256Mi}}
          env:
            - {name: DATABASE_URL, value: "host=pooler-ai-guard.databases.svc sslmode=verify-full"}
"""


def test_a_chart_that_keeps_the_rules_has_no_findings():
    assert chart_policy.check("ai-cluster", GOOD_POD) == []


@pytest.mark.parametrize("manifest, rule", [
    ("apiVersion: v1\nkind: Service\nmetadata: {name: x}\nspec: {type: LoadBalancer}\n", "R7 one front door"),
    ("apiVersion: networking.k8s.io/v1\nkind: Ingress\nmetadata: {name: x}\n", "R7 one front door"),
    ("apiVersion: v1\nkind: Secret\nmetadata: {name: x}\nstringData: {password: hunter2hunter2}\n", "R4 secrets"),
    ("apiVersion: rbac.authorization.k8s.io/v1\nkind: ClusterRole\nmetadata: {name: x}\n", "rights"),
    (GOOD_POD.replace("registry.areeba.com/ai-guard/admin@sha256:abc", "nginx:1.27"), "R3 images"),
    (GOOD_POD.replace("pooler-ai-guard.databases.svc sslmode=verify-full", "pg-ai-rw sslmode=require"), "R5 database"),
    ("apiVersion: v1\nkind: ConfigMap\nmetadata: {name: x}\ndata: {card: '4012 8888 8888 1881'}\n", "R13 no card data"),
])
def test_each_broken_rule_is_a_fail(manifest, rule):
    findings = chart_policy.check("ai-cluster", manifest)
    assert any(item["level"] == "FAIL" and item["rule"] == rule for item in findings), findings


def test_a_rule_set_must_be_known():
    with pytest.raises(deploy_config.DeployConfigError, match="not a rule set"):
        deploy_config.normalize(_repo_target("payments", policy="anything"), "deploy", "Deploy")
    config = deploy_config.normalize(_repo_target("payments", policy="ai-cluster"), "deploy", "Deploy")
    assert config["helm"]["policy"] == "ai-cluster"


class _RendersLoadBalancer(FakeHelm):
    def __call__(self, access, args, extra_env=None):
        if args[0] == "template":
            self.calls.append(list(args))
            return "apiVersion: v1\nkind: Service\nmetadata: {name: payments}\nspec: {type: LoadBalancer}\n"
        return super().__call__(access, args, extra_env)


def test_a_chart_breaking_the_rules_deploys_nothing(app, client, admin_token, service, local_git):
    _approvals(app, 0)
    _with_repository(app, service.id)
    chart_id = _slug(client, admin_token)
    _save(client, admin_token, service.pipeline_id, _helm_stages(_repo_target(chart_id, policy="ai-cluster")))
    fake = _RendersLoadBalancer(releases={"payments": {"name": "payments", "revision": "4", "status": "deployed"}})
    installed, runner = _patched(fake)
    with installed, runner:
        build_id = _run(app, client, admin_token, service.id)

    data = _build(client, admin_token, build_id)
    assert data["status"] == "failed"
    assert not [call for call in fake.calls if call[:2] == ["upgrade", "--install"]]
    log = _logs(client, admin_token, build_id, data["stages"][2]["id"])
    assert "1 FAIL" in log and "Service of type LoadBalancer" in log
    assert "nothing was deployed" in log


class _DoesNotRender(FakeHelm):
    def __call__(self, access, args, extra_env=None):
        if args[0] == "template":
            from api.services.helm_service import HelmCommandError

            raise HelmCommandError("template: payments/templates/deployment.yaml:3: nil pointer")
        return super().__call__(access, args, extra_env)


def test_a_repository_chart_that_does_not_render_deploys_nothing(app, client, admin_token, service, local_git):
    _approvals(app, 0)
    _with_repository(app, service.id)
    chart_id = _slug(client, admin_token)
    _save(client, admin_token, service.pipeline_id, _helm_stages(_repo_target(chart_id)))
    fake = _DoesNotRender(releases={"payments": {"name": "payments", "revision": "4", "status": "deployed"}})
    installed, runner = _patched(fake)
    with installed, runner:
        build_id = _run(app, client, admin_token, service.id)

    data = _build(client, admin_token, build_id)
    assert data["status"] == "failed"
    assert not [call for call in fake.calls if call[:2] == ["upgrade", "--install"]]
    log = _logs(client, admin_token, build_id, data["stages"][2]["id"])
    assert "does not render" in log and "nil pointer" in log


# ---------------------------------------------------------------------------
# The Chart check stage, and Chart rules in a pull request's merge check
# ---------------------------------------------------------------------------

from api.models_ci import CiPipeline, CiPipelineStage  # noqa: E402
from api.services.ci import chart_check_stage  # noqa: E402
from api.services.ci import merge_checks  # noqa: E402
from api.services.ci.merge_checks import metrics as merge_metrics  # noqa: E402
from api.services.ci.merge_checks import policy as merge_policy  # noqa: E402
from api.services.ci.merge_checks import stages as merge_stages  # noqa: E402

LOAD_BALANCER = "apiVersion: v1\nkind: Service\nmetadata: {name: payments}\nspec: {type: LoadBalancer}\n"


def _check_stages(**env):
    return [
        {"name": "Checkout", "stageType": "checkout", "runnerLabels": ["mock"]},
        {"name": "Chart rules", "stageType": "chart_check", "env": {
            "KUBESIGHT_CHART_PATH": "charts/payments", "KUBESIGHT_VALUES_PATH": "deploy/values-uat.yaml",
            "KUBESIGHT_CHART_POLICY": "ai-cluster", **env}},
    ]


def _run_check(app, client, admin_token, service, rendered):
    seen = {}

    def fake_render(files, values, namespace="default"):
        seen.update(files=files, values=values)
        return (rendered, "") if rendered is not None else (None, "template: nil pointer")

    with patch.object(chart_check_stage, "render", side_effect=fake_render):
        build_id = _run(app, client, admin_token, service.id)
    return build_id, seen


def test_a_chart_check_stage_passes_a_chart_that_keeps_the_rules(app, client, admin_token, service, local_git):
    _with_repository(app, service.id)
    assert _save(client, admin_token, service.pipeline_id, _check_stages()).status_code == 200
    build_id, seen = _run_check(app, client, admin_token, service, GOOD_POD)
    data = _build(client, admin_token, build_id)
    assert data["status"] == "success", data
    # The chart and values file of the build's commit were rendered.
    assert "templates/deployment.yaml" in seen["files"] and seen["values"]["replicaCount"] == 3
    log = _logs(client, admin_token, build_id, data["stages"][1]["id"])
    assert "##kubesight-metric tool=chart status=ok problems=0" in log


def test_a_chart_check_stage_fails_on_a_broken_rule_and_reports_it(app, client, admin_token, service, local_git):
    _with_repository(app, service.id)
    _save(client, admin_token, service.pipeline_id, _check_stages())
    build_id, _ = _run_check(app, client, admin_token, service, LOAD_BALANCER)
    data = _build(client, admin_token, build_id)
    assert data["status"] == "failed"
    log = _logs(client, admin_token, build_id, data["stages"][1]["id"])
    assert "Service of type LoadBalancer" in log
    with app.app_context():
        from api.models_ci import CiBuild

        reported = merge_metrics.collect(db.session.get(CiBuild, build_id), ["chart"])
    assert reported["chart"]["status"] == "ok" and reported["chart"]["problems"] == 1


def test_a_chart_that_does_not_render_is_a_problem(app, client, admin_token, service, local_git):
    _with_repository(app, service.id)
    _save(client, admin_token, service.pipeline_id, _check_stages())
    build_id, _ = _run_check(app, client, admin_token, service, None)
    data = _build(client, admin_token, build_id)
    assert data["status"] == "failed"
    assert "does not render" in _logs(client, admin_token, build_id, data["stages"][1]["id"])


def test_a_chart_check_stage_needs_a_chart_folder(client, admin_token, service):
    response = _save(client, admin_token, service.pipeline_id, _check_stages(KUBESIGHT_CHART_PATH=""))
    assert response.status_code == 400 and "chart folder" in response.get_json()["error"]
    response = _save(client, admin_token, service.pipeline_id, _check_stages(KUBESIGHT_CHART_POLICY="nope"))
    assert response.status_code == 400 and "not a rule set" in response.get_json()["error"]


def test_merge_checks_take_the_chart_from_the_deploy_stage_and_block_on_any_broken_rule(
        app, client, admin_token, service):
    _approvals(app, 0)
    chart_id = _slug(client, admin_token)
    _save(client, admin_token, service.pipeline_id,
          _helm_stages(_repo_target(chart_id, policy="ai-cluster")))
    with app.app_context():
        row = db.session.get(CiService, service.id)
        target = merge_checks.chart_target(row)
        assert target == {"chartPath": "charts/payments", "valuesPath": "deploy/values-uat.yaml",
                          "policy": "ai-cluster", "namespace": "payments"}
        assert "chart" in merge_checks.effective_tools(row, None)
    built = merge_stages.build_stages(["eslint", "chart"], {}, chart=target)
    # A server stage, so it comes last; its settings are its environment.
    assert built[-1]["stageType"] == "chart_check"
    assert built[-1]["env"]["KUBESIGHT_CHART_PATH"] == "charts/payments"
    assert built[-1]["env"]["KUBESIGHT_CHECK_TOOL"] == "chart"
    # One broken rule is enough to block, whatever the service's limits.
    verdict = merge_policy.evaluate({}, {"chart": {"status": "ok", "problems": 1}})
    assert verdict["verdict"] == "blocked" and "Chart rules" in verdict["reasons"][0]
    assert merge_policy.evaluate({}, {"chart": {"status": "ok", "problems": 0}})["verdict"] == "allowed"


def test_chart_rules_need_a_deploy_stage_with_a_repository_chart(app, service):
    with app.app_context():
        row = db.session.get(CiService, service.id)
        with pytest.raises(merge_checks.MergeCheckError, match="Chart rules"):
            merge_checks.save_config(row, {"toolsMode": "custom", "tools": ["chart"]})


# ---------------------------------------------------------------------------
# "Values only from Git": the release's own values are not kept
# ---------------------------------------------------------------------------

def test_values_from_git_only_is_written_only_when_on():
    off = deploy_config.normalize(_repo_target("payments"), "deploy", "Deploy")
    assert "valuesFromGitOnly" not in off["helm"] and not deploy_config.values_from_git_only(off)
    on = deploy_config.normalize(_repo_target("payments", valuesFromGitOnly=True), "deploy", "Deploy")
    assert deploy_config.values_from_git_only(on)
    assert deploy_config.signature(on) != deploy_config.signature(off)
    # An Inventory chart has no Git to take its values from: the switch is ignored.
    inventory = _helm_deploy("payments")
    inventory["helm"]["valuesFromGitOnly"] = True
    assert not deploy_config.values_from_git_only(deploy_config.normalize(inventory, "deploy", "Deploy"))


def test_values_only_from_git_drop_what_the_release_held(app, client, admin_token, service, local_git):
    _approvals(app, 0)
    _with_repository(app, service.id)
    chart_id = _slug(client, admin_token)
    _save(client, admin_token, service.pipeline_id, _helm_stages(_repo_target(chart_id, valuesFromGitOnly=True)))
    fake = FakeHelm(
        releases={"payments": {"name": "payments", "revision": "4", "status": "deployed"}},
        current={"replicaCount": 6, "database": {"password": "set-by-hand"}, "legacy": True},
    )
    installed, runner = _patched(fake)
    with installed, runner:
        build_id = _run(app, client, admin_token, service.id)

    data = _build(client, admin_token, build_id)
    assert data["status"] == "success", data
    # Never asked for: the release's values are not read.
    assert not [call for call in fake.calls if call[:2] == ["get", "values"]]
    values = fake.installed_values[0]
    assert "database" not in values and "legacy" not in values
    assert values["replicaCount"] == 3  # the values file, not the release's 6
    assert values["feature"] == {"enabled": True, "mode": "strict"}
    assert values["image"]["tag"] == "2.0.0"
    log = _logs(client, admin_token, build_id, data["stages"][2]["id"])
    assert "values only from Git" in log and "not kept" in log


def test_the_signature_covers_where_the_chart_comes_from():
    base = deploy_config.signature(deploy_config.normalize(_repo_target("payments"), "deploy", "Deploy"))
    for change in ({"chartPath": "charts/other"}, {"valuesPath": "deploy/values-prod.yaml"},
                   {"policy": "ai-cluster"}, {"valuesFromGitOnly": True}):
        changed = deploy_config.normalize(_repo_target("payments", **change), "deploy", "Deploy")
        assert deploy_config.signature(changed) != base, change
