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
        host_aliases=kw.get("host_aliases", []),
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


def test_checkout_authenticates_git_per_credential_type():
    """Git over HTTPS needs a fixed username per credential type.

    An Atlassian API token clones as ``x-bitbucket-api-token-auth`` even though
    the same token pairs with the account email on the REST API; sending the
    email here earns a 401. Interactive prompting stays off so a rejected
    credential reports that 401 instead of "could not read Username".
    """
    first = _plan(
        _execution(0, "checkout", secrets={
            "KUBESIGHT_GIT_TOKEN": "tok",
            "KUBESIGHT_GIT_CREDENTIAL_TYPE": "api_token",
            "KUBESIGHT_GIT_PRINCIPAL": "someone@areeba.com",
        }),
    )
    _, _, job = k8s.build_job_resources(first)
    script = job["spec"]["template"]["spec"]["initContainers"][0]["command"][2]

    assert 'api_token) GIT_USER="x-bitbucket-api-token-auth"' in script
    assert '*)         GIT_USER="x-token-auth"' in script
    assert "GIT_TERMINAL_PROMPT=0" in script
    # The principal is a REST-API identity; it must never become the git user.
    assert "KUBESIGHT_GIT_PRINCIPAL" not in script


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


def test_extra_egress_ports_open_dependency_repositories(monkeypatch):
    """A self-hosted Nexus serves Maven on its own port, and a blocked port
    surfaces as a connect timeout inside Gradle rather than as anything
    network-shaped. Operators declare those ports; typos are ignored, not fatal.
    """
    monkeypatch.setenv("CI_EXTRA_EGRESS_PORTS", "4443, 8081 ,,not-a-port,4443")
    first = _plan(
        _execution(0, "checkout", secrets={"KUBESIGHT_GIT_TOKEN": "t",
                                           "KUBESIGHT_GIT_CREDENTIAL_TYPE": "oauth",
                                           "KUBESIGHT_GIT_PRINCIPAL": ""}),
    )
    _, policy, _ = k8s.build_job_resources(first)

    ports = [p["port"] for rule in policy["spec"]["egress"] for p in rule.get("ports", [])]
    assert ports.count(4443) == 1  # de-duplicated
    assert 8081 in ports
    assert 443 in ports  # the standing TLS rule is untouched


def test_no_extra_egress_ports_by_default(monkeypatch):
    monkeypatch.delenv("CI_EXTRA_EGRESS_PORTS", raising=False)
    first = _plan(
        _execution(0, "checkout", secrets={"KUBESIGHT_GIT_TOKEN": "t",
                                           "KUBESIGHT_GIT_CREDENTIAL_TYPE": "oauth",
                                           "KUBESIGHT_GIT_PRINCIPAL": ""}),
    )
    _, policy, _ = k8s.build_job_resources(first)
    ports = [p["port"] for rule in policy["spec"]["egress"] for p in rule.get("ports", [])]
    assert ports == [53, 53, 5000, 443]


def test_host_aliases_become_pod_level_entries():
    """Kubernetes writes /etc/hosts per POD, so every stage's aliases merge into
    one list and the kubelet applies them before any stage container runs — no
    stage ever has to append to a file on a read-only root filesystem."""
    first = _plan(
        _execution(0, "checkout", host_aliases=[
            {"ip": "10.10.10.20", "hostnames": ["nexus.areeba.com", "nexus"]},
        ], secrets={"KUBESIGHT_GIT_TOKEN": "t",
                    "KUBESIGHT_GIT_CREDENTIAL_TYPE": "oauth",
                    "KUBESIGHT_GIT_PRINCIPAL": ""}),
        _execution(1, commands=["mvn package"], host_aliases=[
            {"ip": "10.10.10.30", "hostnames": ["db.internal"]},
            {"ip": "10.10.10.20", "hostnames": ["nexus"]},  # already covered
        ]),
    )
    _, _, job = k8s.build_job_resources(first)

    assert job["spec"]["template"]["spec"]["hostAliases"] == [
        {"ip": "10.10.10.20", "hostnames": ["nexus.areeba.com", "nexus"]},
        {"ip": "10.10.10.30", "hostnames": ["db.internal"]},
    ]
    # The commands are untouched: no `echo >> /etc/hosts` is injected.
    stage1 = job["spec"]["template"]["spec"]["initContainers"][1]
    assert "/etc/hosts" not in stage1["command"][2]


def test_pod_spec_omits_host_aliases_when_none_configured():
    """Existing stages have no aliases; the key must not appear at all."""
    first = _plan(
        _execution(0, "checkout", secrets={"KUBESIGHT_GIT_TOKEN": "t",
                                           "KUBESIGHT_GIT_CREDENTIAL_TYPE": "oauth",
                                           "KUBESIGHT_GIT_PRINCIPAL": ""}),
        _execution(1, commands=["mvn package"]),
    )
    _, _, job = k8s.build_job_resources(first)
    assert "hostAliases" not in job["spec"]["template"]["spec"]


def test_workspace_listing_parses_entries_and_confines_paths():
    """The path is interpolated into a shell command, so escaping it is a
    security boundary, not tidying."""
    import pytest as _pytest

    from api.services.ci import workspace as workspace_service

    assert workspace_service._normalize("") == "/workspace"
    assert workspace_service._normalize("/workspace/source/") == "/workspace/source"
    assert workspace_service._normalize("source") == "/workspace/source"

    for bad in ["/etc", "/workspace/../etc", "/workspace/a'b", "/workspace/$(id)", "/workspace/a`b`"]:
        with _pytest.raises(workspace_service.WorkspaceError):
            workspace_service._normalize(bad)


def test_workspace_listing_reads_ls_output():
    adapter = k8s.KubernetesJobRunnerAdapter()
    calls = []

    def fake(args, input_text=None):
        calls.append(args)
        if args[0] == "get" and args[1] == "job":
            return 0, json.dumps({"metadata": {"name": "ci-b7-payment-service"}}), ""
        if args[0] == "get" and args[1] == "pods":
            return 0, json.dumps({"items": [{"metadata": {"name": "ci-b7-pod", "creationTimestamp": "1"}}]}), ""
        if args[0] == "exec":
            return 0, "dir\t0\tsource\nfile\t4096\tapp.jar\nbroken-line\n", ""
        return 1, "", "unexpected"

    k8s.set_kubectl_runner(fake)
    entries = adapter.list_workspace(
        RunnerHandle(runner_id=1, external_ref="ci-b7-payment-service#stage-1"), "/workspace"
    )

    # Directories first, then files, each case-insensitively by name.
    assert entries == [
        {"name": "source", "type": "dir", "size": 0},
        {"name": "app.jar", "type": "file", "size": 4096},
    ]
    exec_args = next(args for args in calls if args[0] == "exec")
    assert exec_args[1] == "ci-b7-pod"
    assert "-c" in exec_args and "stage-1" in exec_args


def test_inline_dockerfile_is_mounted_beside_the_context(monkeypatch):
    """An inline Dockerfile must not be written into the checkout: buildctl
    takes context and dockerfile as separate locals, so it is mounted read-only
    and the repository is left exactly as cloned."""
    monkeypatch.setenv("CI_BUILDKIT_ADDR", "tcp://buildkitd:1234")
    registry = {
        "host": "nexus.company.local:9443", "port": 9443, "repository": "profile-ms",
        "tag": "V1.0.27", "dockerfile": "Dockerfile", "username": "ci",
        "password": "reg-pass", "verifyTls": True, "connectionId": 1,
        "dockerfileContent": "FROM alpine\nADD app.jar app.jar\n",
    }
    first = _plan(
        _execution(0, "checkout", secrets={"KUBESIGHT_GIT_TOKEN": "t",
                                           "KUBESIGHT_GIT_CREDENTIAL_TYPE": "oauth",
                                           "KUBESIGHT_GIT_PRINCIPAL": ""}),
        _execution(1, "container_image", registry=registry),
    )
    secret, _, job = k8s.build_job_resources(first)

    assert "inline-dockerfile" in secret["data"]
    spec = job["spec"]["template"]["spec"]
    stage = spec["initContainers"][1]
    script = stage["command"][2]
    assert "--local context=/workspace/source " in script
    assert f"--local dockerfile={k8s.INLINE_DOCKERFILE_DIR} " in script
    assert "--opt filename=Dockerfile " in script
    mount = next(m for m in stage["volumeMounts"] if m["name"] == "inline-dockerfile")
    assert mount == {
        "name": "inline-dockerfile",
        "mountPath": k8s.INLINE_DOCKERFILE_DIR,
        "readOnly": True,
    }
    volume = next(v for v in spec["volumes"] if v["name"] == "inline-dockerfile")
    assert volume["secret"]["items"] == [{"key": "inline-dockerfile", "path": "Dockerfile"}]


def test_without_inline_dockerfile_the_repository_file_is_used(monkeypatch):
    monkeypatch.setenv("CI_BUILDKIT_ADDR", "tcp://buildkitd:1234")
    registry = {
        "host": "nexus.company.local", "port": None, "repository": "profile-ms",
        "tag": "V1.0.27", "dockerfile": "docker/Dockerfile", "username": "ci",
        "password": "p", "verifyTls": True, "connectionId": 1,
    }
    first = _plan(
        _execution(0, "checkout", secrets={"KUBESIGHT_GIT_TOKEN": "t",
                                           "KUBESIGHT_GIT_CREDENTIAL_TYPE": "oauth",
                                           "KUBESIGHT_GIT_PRINCIPAL": ""}),
        _execution(1, "container_image", registry=registry),
    )
    secret, _, job = k8s.build_job_resources(first)

    assert "inline-dockerfile" not in secret["data"]
    spec = job["spec"]["template"]["spec"]
    assert not [v for v in spec["volumes"] if v["name"] == "inline-dockerfile"]
    script = spec["initContainers"][1]["command"][2]
    assert "--local dockerfile=/workspace/source/docker " in script
    assert "--opt filename=Dockerfile " in script


def test_every_stage_exits_zero_so_the_collector_always_runs():
    """Kubernetes starts main containers only when EVERY initContainer
    succeeded, so a stage that exits non-zero means the collector never runs and
    the artifacts of stages that DID succeed are lost. Every stage therefore
    exits 0 and reports its real code as a marker, guarding on a shared flag so
    later stages still decline to run."""
    first = _plan(
        _execution(0, "checkout", secrets={"KUBESIGHT_GIT_TOKEN": "t",
                                           "KUBESIGHT_GIT_CREDENTIAL_TYPE": "oauth",
                                           "KUBESIGHT_GIT_PRINCIPAL": ""}),
        _execution(1, commands=["gradle build"]),
    )
    _, _, job = k8s.build_job_resources(first)
    for container in job["spec"]["template"]["spec"]["initContainers"]:
        script = container["command"][2]
        assert script.rstrip().endswith("exit 0")
        assert k8s._EXIT_MARKER in script
        assert k8s._SKIP_MARKER in script
        assert k8s._FAIL_FLAG in script


def test_continue_on_failure_does_not_stop_later_stages():
    """A cof stage records its failure without writing the flag — that is
    precisely what 'continue' means."""
    normal = k8s._wrap_stage_script("false", continue_on_failure=False)
    cof = k8s._wrap_stage_script("false", continue_on_failure=True)
    assert f': > "$KS_FLAG"' in normal
    assert f': > "$KS_FLAG"' not in cof


def test_poll_maps_markers_to_stage_outcomes():
    adapter = k8s.KubernetesJobRunnerAdapter()
    logs = {}

    def fake(args, input_text=None):
        if args[0] == "get" and args[1] == "job":
            return 0, json.dumps({"status": {"succeeded": 1}}), ""
        if args[0] == "get" and args[1] == "pods":
            return 0, json.dumps({"items": [{
                "metadata": {"name": "p", "creationTimestamp": "1", "annotations": {}},
                "status": {"initContainerStatuses": [
                    {"name": "stage-0", "state": {"terminated": {"exitCode": 0}}},
                    {"name": "stage-1", "state": {"terminated": {"exitCode": 0}}},
                ]},
                "spec": {"initContainers": [{"name": "stage-0"}, {"name": "stage-1"}]},
            }]}), ""
        if args[0] == "logs":
            container = args[args.index("-c") + 1]
            return 0, logs.get(container, ""), ""
        return 1, "", ""

    k8s.set_kubectl_runner(fake)
    handle = k8s.RunnerHandle(runner_id=1, external_ref="job#stage-0")

    logs["stage-0"] = "building\n[kubesight-exit] 0\n"
    assert adapter.poll(handle) == k8s.SUCCEEDED

    logs["stage-0"] = "boom\n[kubesight-exit] 1\n"
    assert adapter.poll(handle) == k8s.FAILED

    logs["stage-0"] = "[kubesight] Skipped: an earlier stage failed.\n[kubesight-skip]\n"
    assert adapter.poll(handle) == k8s.SKIPPED


def test_no_cache_volume_unless_a_storage_class_is_configured(monkeypatch):
    """Nothing should demand storage on a cluster that has none to give."""
    monkeypatch.delenv("CI_CACHE_STORAGE_CLASS", raising=False)
    first = _plan(
        _execution(0, "checkout", secrets={"KUBESIGHT_GIT_TOKEN": "t",
                                           "KUBESIGHT_GIT_CREDENTIAL_TYPE": "oauth",
                                           "KUBESIGHT_GIT_PRINCIPAL": ""}),
        _execution(1, commands=["gradle build"]),
    )
    _, _, job = k8s.build_job_resources(first)
    spec = job["spec"]["template"]["spec"]
    assert not [v for v in spec["volumes"] if v["name"] == "cache"]
    stage = spec["initContainers"][1]
    cache_env = next(e for e in stage["env"] if e["name"] == "KUBESIGHT_CACHE")
    assert cache_env["value"] == ""


def test_cache_volume_is_per_service_and_mounted_everywhere(monkeypatch):
    """One PVC per service: a service's builds warm each other's cache while
    different services stay isolated."""
    monkeypatch.setenv("CI_CACHE_STORAGE_CLASS", "nfs-client")
    first = _plan(
        _execution(0, "checkout", secrets={"KUBESIGHT_GIT_TOKEN": "t",
                                           "KUBESIGHT_GIT_CREDENTIAL_TYPE": "oauth",
                                           "KUBESIGHT_GIT_PRINCIPAL": ""}),
        _execution(1, commands=["gradle build"]),
    )
    _, _, job = k8s.build_job_resources(first)
    spec = job["spec"]["template"]["spec"]

    volume = next(v for v in spec["volumes"] if v["name"] == "cache")
    assert volume["persistentVolumeClaim"]["claimName"] == "ci-cache-payment-service"
    for container in spec["initContainers"]:
        assert {"name": "cache", "mountPath": "/cache"} in container["volumeMounts"]
    stage = spec["initContainers"][1]
    assert next(e for e in stage["env"] if e["name"] == "KUBESIGHT_CACHE")["value"] == "/cache"

    claim = k8s.cache_claim("payment-service")
    assert claim["spec"]["accessModes"] == ["ReadWriteOnce"]
    assert claim["spec"]["storageClassName"] == "nfs-client"
    # No ownerReference: the cache must outlive the build that created it.
    assert "ownerReferences" not in claim["metadata"]


def test_registry_layer_cache_is_opt_in(monkeypatch):
    monkeypatch.setenv("CI_BUILDKIT_ADDR", "tcp://buildkitd:1234")
    registry = {
        "host": "nexus:9443", "port": 9443, "repository": "profile-ms", "tag": "v1",
        "dockerfile": "Dockerfile", "username": "u", "password": "p",
        "verifyTls": True, "connectionId": 1,
    }
    plan = lambda: _plan(
        _execution(0, "checkout", secrets={"KUBESIGHT_GIT_TOKEN": "t",
                                           "KUBESIGHT_GIT_CREDENTIAL_TYPE": "oauth",
                                           "KUBESIGHT_GIT_PRINCIPAL": ""}),
        _execution(1, "container_image", registry=registry),
    )

    monkeypatch.delenv("CI_BUILDKIT_REGISTRY_CACHE", raising=False)
    _, _, job = k8s.build_job_resources(plan())
    assert "--export-cache" not in job["spec"]["template"]["spec"]["initContainers"][1]["command"][2]

    monkeypatch.setenv("CI_BUILDKIT_REGISTRY_CACHE", "1")
    _, _, job = k8s.build_job_resources(plan())
    script = job["spec"]["template"]["spec"]["initContainers"][1]["command"][2]
    assert "--import-cache type=registry,ref=nexus:9443/profile-ms:buildcache" in script
    assert "--export-cache type=registry,ref=nexus:9443/profile-ms:buildcache,mode=max" in script
