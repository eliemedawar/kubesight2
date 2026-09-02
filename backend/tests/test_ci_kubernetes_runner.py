"""Kubernetes Job runner: manifest shape, status parsing, worker callbacks.

No cluster involved anywhere — the kubectl transport is injected, and the
manifest builder is a pure function.
"""

from __future__ import annotations

import base64
import json
from hashlib import sha256

import pytest

from api.db import db
from api.models_ci import CiBuild, CiBuildStage, CiService
from api.services.ci.runners import kubernetes as k8s
from api.services.ci.runners.base import (
    FAILED,
    QUEUED,
    RUNNING,
    SUCCEEDED,
    TIMEOUT,
    RunnerHandle,
    StageExecution,
)


def _execution(position, stage_type="command", **kw):
    return StageExecution(
        build_id=7,
        build_number=3,
        stage_id=100 + position,
        service_slug="payment-service",
        stage_name=f"Stage {position}",
        stage_type=stage_type,
        image=kw.get("image"),
        working_directory=kw.get("workdir"),
        commands=kw.get("commands", ["echo hello"]),
        env=kw.get("env", {}),
        secrets=kw.get("secrets", {}),
        artifacts=kw.get("artifacts", []),
        timeout_seconds=600,
        continue_on_failure=kw.get("cof", False),
        position=position,
        workspace_ref="payment-service-3",
        repository_url="https://bitbucket.org/areeba/payment-service.git",
        branch="develop",
        registry=kw.get("registry"),
        callback_url="http://backend:5000/api/ci/worker",
        callback_token="the-callback-token",
    )


def _plan(*executions):
    first = executions[0]
    first.plan = list(executions)
    return first


@pytest.fixture(autouse=True)
def _reset_kubectl():
    yield
    k8s.set_kubectl_runner(None)


# ---------------------------------------------------------------------------
# Manifest builder
# ---------------------------------------------------------------------------

def test_job_manifest_orders_stages_as_init_containers():
    first = _plan(
        _execution(0, "checkout", secrets={"KUBESIGHT_GIT_TOKEN": "tok",
                                           "KUBESIGHT_GIT_CREDENTIAL_TYPE": "oauth",
                                           "KUBESIGHT_GIT_PRINCIPAL": ""}),
        _execution(1, image="maven:3.9", commands=["mvn package"],
                   secrets={"NEXUS_PASSWORD": "s3cret"},
                   artifacts=[{"path": "target/*.jar", "type": "jar"}]),
        _execution(2, commands=["mvn deploy"], cof=True),
    )
    secret, policy, job = k8s.build_job_resources(first)

    assert [r["kind"] for r in (secret, policy, job)] == ["Secret", "NetworkPolicy", "Job"]
    assert job["metadata"]["name"] == "ci-b7-payment-service"

    spec = job["spec"]["template"]["spec"]
    names = [c["name"] for c in spec["initContainers"]]
    assert names == ["stage-0", "stage-1", "stage-2"]
    assert [c["name"] for c in spec["containers"]] == ["collector"]

    # Stage images: checkout uses the worker image; command stages their own.
    assert spec["initContainers"][1]["image"] == "maven:3.9"

    # No retry, no SA token, restricted pod.
    assert job["spec"]["backoffLimit"] == 0
    assert spec["automountServiceAccountToken"] is False
    for container in spec["initContainers"] + spec["containers"]:
        sc = container["securityContext"]
        assert sc["runAsNonRoot"] and sc["runAsUser"] == 65532
        assert sc["allowPrivilegeEscalation"] is False
        assert sc["readOnlyRootFilesystem"] is True
        assert sc["capabilities"] == {"drop": ["ALL"]}

    # Overall deadline covers every stage timeout plus scheduling slack.
    assert job["spec"]["activeDeadlineSeconds"] == 600 * 3 + 900

    # continue-on-failure positions are recorded for poll() to interpret.
    assert job["spec"]["template"]["metadata"]["annotations"][
        "kubesight.io/continue-on-failure-stages"
    ] == "2"


def test_secrets_travel_as_secret_refs_never_inline():
    first = _plan(
        _execution(0, "checkout", secrets={"KUBESIGHT_GIT_TOKEN": "clone-tok",
                                           "KUBESIGHT_GIT_CREDENTIAL_TYPE": "oauth",
                                           "KUBESIGHT_GIT_PRINCIPAL": ""}),
        _execution(1, secrets={"NEXUS_PASSWORD": "s3cret-value"}),
    )
    secret, _, job = k8s.build_job_resources(first)

    # Values live only in the Secret, base64 of the plaintext.
    assert base64.b64decode(secret["data"]["s1-NEXUS_PASSWORD"]).decode() == "s3cret-value"
    assert base64.b64decode(secret["data"]["callback-token"]).decode() == "the-callback-token"

    # The Job manifest itself never contains a secret value anywhere.
    dumped = json.dumps(job)
    assert "s3cret-value" not in dumped
    assert "clone-tok" not in dumped
    assert "the-callback-token" not in dumped

    stage1 = job["spec"]["template"]["spec"]["initContainers"][1]
    ref = next(e for e in stage1["env"] if e["name"] == "NEXUS_PASSWORD")
    assert ref["valueFrom"]["secretKeyRef"]["key"] == "s1-NEXUS_PASSWORD"


def test_collector_receives_artifact_specs_with_stage_positions():
    first = _plan(
        _execution(0, "checkout", secrets={"KUBESIGHT_GIT_TOKEN": "t",
                                           "KUBESIGHT_GIT_CREDENTIAL_TYPE": "oauth",
                                           "KUBESIGHT_GIT_PRINCIPAL": ""}),
        _execution(1, workdir="services/payment",
                   artifacts=[{"path": "target/*.jar", "type": "jar"}]),
    )
    _, _, job = k8s.build_job_resources(first)
    collector = job["spec"]["template"]["spec"]["containers"][0]
    specs = json.loads(
        next(e for e in collector["env"] if e["name"] == "KUBESIGHT_ARTIFACTS")["value"]
    )
    assert specs == [
        {"path": "target/*.jar", "type": "jar", "workdir": "services/payment", "stagePosition": 1}
    ]


def test_buildkit_stage_uses_client_only_and_docker_config(monkeypatch):
    monkeypatch.setenv("CI_BUILDKIT_ADDR", "tcp://buildkitd.kubesight-buildkit.svc.cluster.local:1234")
    registry = {
        "host": "nexus.company.local", "port": 8443, "repository": "payment-service",
        "tag": "develop-3", "dockerfile": "Dockerfile", "username": "ci",
        "password": "reg-pass", "verifyTls": False, "connectionId": 1,
    }
    first = _plan(
        _execution(0, "checkout", secrets={"KUBESIGHT_GIT_TOKEN": "t",
                                           "KUBESIGHT_GIT_CREDENTIAL_TYPE": "oauth",
                                           "KUBESIGHT_GIT_PRINCIPAL": ""}),
        _execution(1, "container_image", registry=registry),
    )
    secret, policy, job = k8s.build_job_resources(first)

    stage = job["spec"]["template"]["spec"]["initContainers"][1]
    script = stage["command"][2]
    assert "buildctl --addr tcp://buildkitd" in script
    assert "name=nexus.company.local/payment-service:develop-3,push=true" in script
    assert "registry.insecure=true" in script  # verifyTls False
    assert "--metadata-file /workspace/.kubesight/image-meta-1.json" in script
    # The buildctl client stays as restricted as every other stage.
    assert stage["securityContext"]["allowPrivilegeEscalation"] is False
    assert stage["securityContext"]["capabilities"] == {"drop": ["ALL"]}

    # Registry auth: a mounted docker config, never argv or plain env.
    config = json.loads(base64.b64decode(secret["data"]["docker-config"]))
    assert "nexus.company.local" in config["auths"]
    assert "reg-pass" not in json.dumps(job)

    # Egress opens the buildkitd namespace and the custom registry port.
    egress = json.dumps(policy["spec"]["egress"])
    assert "kubesight-buildkit" in egress
    assert "8443" in egress


def test_supported_stage_types_follow_buildkit_configuration(monkeypatch):
    adapter = k8s.KubernetesJobRunnerAdapter()
    monkeypatch.delenv("CI_BUILDKIT_ADDR", raising=False)
    assert adapter.supported_stage_types() == {"checkout", "command"}
    assert "BuildKit" in adapter.skip_reason("container_image")
    monkeypatch.setenv("CI_BUILDKIT_ADDR", "tcp://buildkitd:1234")
    assert "container_image" in adapter.supported_stage_types()
    assert adapter.skip_reason("container_image") is None


# ---------------------------------------------------------------------------
# Status parsing (fake kubectl)
# ---------------------------------------------------------------------------

def _fake_cluster(job_status, pod, log_lines=None):
    def runner(args, input_text=None):
        if args[:2] == ["get", "job"]:
            return 0, json.dumps({"metadata": {"name": args[2]}, "status": job_status}), ""
        if args[:2] == ["get", "pods"]:
            items = [pod] if pod else []
            return 0, json.dumps({"items": items}), ""
        if args[0] == "logs":
            if log_lines is None:
                return 1, "", "container is waiting to start"
            return 0, "\n".join(log_lines), ""
        return 0, "", ""

    return runner


def _pod(init_statuses, cof=""):
    return {
        "metadata": {"creationTimestamp": "2026-09-01T10:00:00Z",
                     "annotations": {"kubesight.io/continue-on-failure-stages": cof}},
        "spec": {"initContainers": [{"name": name} for name in
                                    [s["name"] for s in init_statuses]]},
        "status": {"initContainerStatuses": init_statuses},
    }


def _handle(container="stage-0"):
    return RunnerHandle(runner_id=1, external_ref=f"ci-b7-payment-service#{container}")


def test_poll_maps_init_container_states():
    adapter = k8s.KubernetesJobRunnerAdapter()

    k8s.set_kubectl_runner(_fake_cluster({}, _pod([
        {"name": "stage-0", "state": {"running": {}}},
        {"name": "stage-1", "state": {"waiting": {}}},
    ])))
    assert adapter.poll(_handle("stage-0")) == RUNNING
    assert adapter.poll(_handle("stage-1")) == QUEUED

    k8s.set_kubectl_runner(_fake_cluster({}, _pod([
        {"name": "stage-0", "state": {"terminated": {"exitCode": 0}}},
        {"name": "stage-1", "state": {"terminated": {"exitCode": 1}}},
    ])))
    assert adapter.poll(_handle("stage-0")) == SUCCEEDED
    assert adapter.poll(_handle("stage-1")) == FAILED


def test_poll_last_stage_waits_for_the_collector():
    """The last stage may not claim success while artifact upload can still fail."""
    adapter = k8s.KubernetesJobRunnerAdapter()
    pod = _pod([{"name": "stage-0", "state": {"terminated": {"exitCode": 0}}}])

    k8s.set_kubectl_runner(_fake_cluster({"active": 1}, pod))
    assert adapter.poll(_handle("stage-0")) == RUNNING  # collector uploading

    k8s.set_kubectl_runner(_fake_cluster({"succeeded": 1}, pod))
    assert adapter.poll(_handle("stage-0")) == SUCCEEDED

    k8s.set_kubectl_runner(_fake_cluster({"failed": 1}, pod))
    assert adapter.poll(_handle("stage-0")) == FAILED  # outputs missing


def test_poll_reads_the_real_exit_code_of_continue_on_failure_stages():
    """Exit 0 with a nonzero [kubesight-exit] marker is a FAILURE, not a pass."""
    adapter = k8s.KubernetesJobRunnerAdapter()
    pod = _pod(
        [
            {"name": "stage-1", "state": {"terminated": {"exitCode": 0}}},
            {"name": "stage-2", "state": {"waiting": {}}},
        ],
        cof="1",
    )
    k8s.set_kubectl_runner(_fake_cluster({}, pod, log_lines=["lint output", "[kubesight-exit] 2"]))
    assert adapter.poll(_handle("stage-1")) == FAILED

    k8s.set_kubectl_runner(_fake_cluster({}, pod, log_lines=["lint output", "[kubesight-exit] 0"]))
    assert adapter.poll(_handle("stage-1")) == SUCCEEDED


def test_poll_reports_timeout_on_deadline_exceeded():
    adapter = k8s.KubernetesJobRunnerAdapter()
    job_status = {"failed": 1, "conditions": [{"type": "Failed", "reason": "DeadlineExceeded"}]}
    pod = _pod([{"name": "stage-0", "state": {"running": {}}}])
    k8s.set_kubectl_runner(_fake_cluster(job_status, pod))
    assert adapter.poll(_handle("stage-0")) == TIMEOUT


def test_poll_fails_when_the_job_disappeared():
    adapter = k8s.KubernetesJobRunnerAdapter()

    def runner(args, input_text=None):
        return 1, "", "NotFound"

    k8s.set_kubectl_runner(runner)
    assert adapter.poll(_handle("stage-0")) == FAILED


# ---------------------------------------------------------------------------
# Worker callbacks
# ---------------------------------------------------------------------------

@pytest.fixture()
def running_build(app):
    with app.app_context():
        service = CiService(name="Payment Service", slug="payment-service")
        db.session.add(service)
        db.session.flush()
        build = CiBuild(
            service_id=service.id,
            number=1,
            status="running",
            branch="develop",
            pipeline_snapshot={"stages": []},
            worker_callback_token_hash=sha256(b"worker-token").hexdigest(),
        )
        db.session.add(build)
        db.session.flush()
        stage = CiBuildStage(build_id=build.id, position=1, name="Build", status="running")
        db.session.add(stage)
        db.session.commit()
        return build.id


def _worker_headers():
    return {"Authorization": "Bearer worker-token"}


def test_worker_callback_rejects_a_wrong_token(client, running_build):
    response = client.post(
        f"/api/ci/worker/builds/{running_build}/meta",
        json={"commitSha": "a" * 40},
        headers={"Authorization": "Bearer wrong"},
    )
    assert response.status_code == 401


def test_worker_meta_records_the_commit(app, client, running_build):
    response = client.post(
        f"/api/ci/worker/builds/{running_build}/meta",
        json={"commitSha": "ab12cd34" * 5},
        headers=_worker_headers(),
    )
    assert response.status_code == 200
    with app.app_context():
        assert db.session.get(CiBuild, running_build).commit_sha == "ab12cd34" * 5


def test_worker_meta_rejects_a_non_hex_commit(app, client, running_build):
    client.post(
        f"/api/ci/worker/builds/{running_build}/meta",
        json={"commitSha": "$(rm -rf /)"},
        headers=_worker_headers(),
    )
    with app.app_context():
        assert db.session.get(CiBuild, running_build).commit_sha is None


def test_worker_uploads_a_file_artifact(app, client, running_build):
    import io as _io

    response = client.post(
        f"/api/ci/worker/builds/{running_build}/artifacts",
        data={
            "name": "app.jar",
            "type": "jar",
            "stagePosition": "1",
            "file": (_io.BytesIO(b"jar-bytes"), "app.jar"),
        },
        headers=_worker_headers(),
        content_type="multipart/form-data",
    )
    assert response.status_code == 201
    with app.app_context():
        from api.models_ci import CiArtifact

        row = CiArtifact.query.filter_by(build_id=running_build).one()
        assert row.artifact_type == "jar"
        assert row.storage_backend == "local"
        assert row.checksum_sha256 == sha256(b"jar-bytes").hexdigest()
        assert row.build_stage_id is not None


def test_worker_records_a_pushed_image_by_coordinates(app, client, running_build):
    response = client.post(
        f"/api/ci/worker/builds/{running_build}/artifacts",
        json={
            "name": "payment-service",
            "type": "container-image",
            "uri": "nexus.company.local/payment-service:develop-1",
            "digest": "sha256:" + "c" * 64,
            "stagePosition": 1,
        },
        headers=_worker_headers(),
    )
    assert response.status_code == 201
    with app.app_context():
        from api.models_ci import CiArtifact

        row = CiArtifact.query.filter_by(build_id=running_build).one()
        assert row.artifact_type == "container-image"
        assert row.uri.endswith(":develop-1")
        assert row.digest.startswith("sha256:")
        assert row.storage_backend == "registry"


def test_worker_cannot_write_to_a_finished_build(app, client, running_build):
    with app.app_context():
        build = db.session.get(CiBuild, running_build)
        build.status = "success"
        db.session.add(build)
        db.session.commit()
    response = client.post(
        f"/api/ci/worker/builds/{running_build}/meta",
        json={"commitSha": "a" * 40},
        headers=_worker_headers(),
    )
    assert response.status_code == 409
