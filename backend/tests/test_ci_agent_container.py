"""The agent's container mode: the argv it builds for `docker run`.

The agent is a standalone, dependency-free script, so it is loaded by path
here. Nothing in this file needs docker — the command builder is pure, which is
the point: the shape of that argv decides whether secrets leak into ps, whether
files come back owned by root, and whether an SELinux host can read the
workspace at all.
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

AGENT_PATH = Path(__file__).resolve().parents[2] / "agent" / "kubesight-agent.py"


@pytest.fixture(scope="module")
def agent():
    spec = importlib.util.spec_from_file_location("kubesight_agent", AGENT_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _task(**kw):
    task = {
        "taskId": 42,
        "buildId": 7,
        "image": "gradle:9.1.0-jdk17",
        "workingDirectory": "",
        "env": {},
        "hostAliases": [],
        "resources": {},
    }
    task.update(kw)
    return task


def _argv(agent, **kw):
    return agent.container_command(
        "docker", _task(**kw), "/work/profile-45", "/work/.cache",
        "gradle build", uid_gid="1000:1000",
    )


# ---------------------------------------------------------------------------
# The argv
# ---------------------------------------------------------------------------

def test_the_stage_runs_in_its_image_with_the_clusters_paths(agent):
    """The same $KUBESIGHT_WORKSPACE the Kubernetes runner exports, so one
    pipeline's commands are correct on both."""
    argv = _argv(agent)
    joined = " ".join(argv)

    assert argv[:3] == ["docker", "run", "--rm"]
    assert "--name kubesight-task-42" in joined
    assert "-v /work/profile-45:/workspace:z" in joined
    assert "-v /work/.cache:/cache:z" in joined
    assert "-w /workspace/source" in joined
    assert "-e KUBESIGHT_WORKSPACE=/workspace" in joined
    assert "-e KUBESIGHT_SOURCE=/workspace/source" in joined
    # The image and the script are the tail, in that order.
    assert argv[-5:] == ["gradle:9.1.0-jdk17", "/bin/sh", "-e", "-c", "gradle build"]


def test_files_come_back_owned_by_the_agent_not_root(agent):
    """Without --user the workspace fills with root-owned files the agent
    cannot clean up on its next build."""
    assert "--user" in _argv(agent)
    assert "1000:1000" in _argv(agent)


def test_the_mounts_are_selinux_labelled(agent):
    """On RHEL — which is what this runs on — a bind mount without :z is
    unreadable inside the container, and every command fails on Permission
    denied."""
    argv = _argv(agent)
    assert [part for part in argv if part.endswith(":z")]
    plain = agent.container_command(
        "podman", _task(), "/work/b", "/work/.cache", "true", selinux=False
    )
    assert not [part for part in plain if part.endswith(":z")]


def test_a_secret_never_reaches_argv(agent):
    """It is passed as `-e NAME`, so the runtime reads the value from this
    process's environment — the same rule the git token follows, for the same
    reason: argv is readable by every user on the machine."""
    argv = _argv(agent, env={"NEXUS_USER": "ci", "NEXUS_PASSWORD": "s3cret-value"})
    joined = " ".join(argv)

    assert "s3cret-value" not in joined
    assert "ci" not in joined.split()  # not smuggled in as a bare value either
    index = argv.index("NEXUS_PASSWORD")
    assert argv[index - 1] == "-e"


def test_the_cache_is_mounted_and_the_tools_point_at_it(agent):
    """A container starts with an empty ~/.gradle every time; without this a
    containerised stage is slower than the host one it replaced."""
    joined = " ".join(_argv(agent))
    assert "-e GRADLE_USER_HOME=/cache/gradle" in joined
    assert "-e MAVEN_OPTS=-Dmaven.repo.local=/cache/maven" in joined
    assert "-e KUBESIGHT_CACHE=/cache" in joined


def test_a_stage_that_sets_a_tool_path_itself_wins(agent):
    """Same precedence as the Kubernetes runner: the pipeline's own value is
    passed through rather than overridden by the injected default."""
    argv = _argv(agent, env={"GRADLE_USER_HOME": "/workspace/.gradle"})
    joined = " ".join(argv)
    assert "-e GRADLE_USER_HOME=/cache/gradle" not in joined
    index = argv.index("GRADLE_USER_HOME")
    assert argv[index - 1] == "-e"  # passthrough form, value from the environment


def test_the_working_directory_is_honoured(agent):
    assert "-w /workspace/source/api" in " ".join(_argv(agent, workingDirectory="api"))
    assert "-w /workspace/source/api" in " ".join(_argv(agent, workingDirectory="/api/"))


def test_host_aliases_finally_do_something_on_an_agent(agent):
    """They already travel in the task payload and the agent could not use
    them: it cannot write /etc/hosts. A container can."""
    argv = _argv(
        agent,
        hostAliases=[{"ip": "10.4.23.182", "hostnames": ["registry.areeba.com", "nexus"]}],
    )
    joined = " ".join(argv)
    assert "--add-host registry.areeba.com:10.4.23.182" in joined
    assert "--add-host nexus:10.4.23.182" in joined


def test_resource_limits_are_passed_when_set(agent):
    joined = " ".join(_argv(agent, resources={"memory": "4Gi", "cpu": "2"}))
    assert "--memory 4Gi" in joined
    assert "--cpus 2" in joined
    assert "--memory" not in " ".join(_argv(agent))


def test_every_container_is_labelled_for_cleanup(agent):
    """An agent that is killed mid-build must be able to find its own leftovers
    without touching anything else on the machine."""
    joined = " ".join(_argv(agent))
    assert "--label kubesight.agent=1" in joined
    assert "--label kubesight.build=7" in joined


# ---------------------------------------------------------------------------
# The mode
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "value, expected",
    [(None, "auto"), ("auto", "auto"), ("always", "always"), ("never", "never"),
     ("NEVER", "never"), ("maybe", "auto"), ("", "auto")],
)
def test_the_stage_can_choose_its_mode(agent, value, expected):
    env = {} if value is None else {"KUBESIGHT_CONTAINER": value}
    assert agent.container_mode({"env": env}) == expected


def test_containers_are_linux_only(agent, monkeypatch):
    """A container on macOS is a Linux VM, which is exactly what an iOS build
    cannot use — so a mac agent never containerises."""
    monkeypatch.setattr(agent, "_runtime_cache", None)
    monkeypatch.setattr(agent.platform, "system", lambda: "Darwin")
    assert agent.container_runtime() == ""


def test_the_flag_turns_it_off(agent, monkeypatch):
    monkeypatch.setattr(agent, "_runtime_cache", None)
    monkeypatch.setattr(agent, "_containers_disabled", True)
    assert agent.container_runtime() == ""


def test_a_runtime_whose_daemon_is_down_does_not_count(agent, monkeypatch):
    """The client existing is not the same as being able to run anything, and
    "docker: command found" would route image stages to a machine that fails
    every one of them."""
    monkeypatch.setattr(agent, "_runtime_cache", None)
    monkeypatch.setattr(agent, "_containers_disabled", False)
    monkeypatch.setattr(agent.platform, "system", lambda: "Linux")
    monkeypatch.setattr(agent.shutil, "which", lambda name: "/usr/bin/" + name)

    class Failed:
        returncode = 1

    monkeypatch.setattr(agent.subprocess, "run", lambda *a, **k: Failed())
    assert agent.container_runtime() == ""


# ---------------------------------------------------------------------------
# The wiring: what run_task actually does with a task that has an image
# ---------------------------------------------------------------------------

class _Client:
    """Records the calls the agent makes back to KubeSight."""

    def __init__(self):
        self.posts = []

    def post_json(self, path, payload, timeout=60):
        self.posts.append((path, payload))
        return None

    def post_file(self, *args, **kw):  # pragma: no cover - no artifacts here
        pass

    def result(self):
        return next(body for path, body in self.posts if path.endswith("/result"))


def _prepared(tmp_path, **kw):
    root = tmp_path / "agent"
    (root / "profile-45" / "source").mkdir(parents=True)
    task = {
        "taskId": 42,
        "claimToken": "tok",
        "buildId": 7,
        "workspace": "profile-45",
        "stageType": "command",
        "image": "gradle:9.1.0-jdk17",
        "commands": ["gradle build"],
        "env": {},
        "timeoutSeconds": 600,
    }
    task.update(kw)
    return str(root), task


def test_run_task_runs_the_stage_in_the_image_and_cleans_up(agent, tmp_path, monkeypatch):
    root, task = _prepared(tmp_path)
    ran = {}
    removed = []

    monkeypatch.setattr(agent, "container_runtime", lambda: "docker")
    monkeypatch.setattr(agent, "pull_image", lambda *a, **k: None)
    monkeypatch.setattr(
        agent, "stream",
        lambda argv, cwd, env, shipper, **kw: ran.setdefault("argv", argv) and 0 or 0,
    )
    monkeypatch.setattr(
        agent.subprocess, "run",
        lambda argv, **kw: removed.append(argv) or type("R", (), {"returncode": 0})(),
    )

    client = _Client()
    agent.run_task(client, task, root)

    assert ran["argv"][:2] == ["docker", "run"]
    assert ran["argv"][-5:] == ["gradle:9.1.0-jdk17", "/bin/sh", "-e", "-c", "gradle build"]
    # The container is removed by name whatever happened, because it outlives a
    # killed client.
    assert removed and removed[0][:3] == ["docker", "rm", "-f"]
    assert client.result()["exitCode"] == 0


def test_always_without_a_runtime_fails_the_stage(agent, tmp_path, monkeypatch):
    """Running it on the machine instead would be a different build, reported
    as the same one."""
    root, task = _prepared(tmp_path, env={"KUBESIGHT_CONTAINER": "always"})
    monkeypatch.setattr(agent, "container_runtime", lambda: "")

    client = _Client()
    agent.run_task(client, task, root)

    result = client.result()
    assert result["exitCode"] == 1
    assert "no usable docker or podman" in result["error"]


def test_no_runtime_falls_back_to_the_machine_and_says_so(agent, tmp_path, monkeypatch):
    root, task = _prepared(tmp_path)
    calls = {}
    monkeypatch.setattr(agent, "container_runtime", lambda: "")
    monkeypatch.setattr(
        agent, "stream",
        lambda argv, cwd, env, shipper, **kw: calls.setdefault("argv", argv) and 0 or 0,
    )

    client = _Client()
    agent.run_task(client, task, root)

    assert calls["argv"][0] == "/bin/sh"
    logged = " ".join(
        line["content"]
        for path, body in client.posts
        if path.endswith("/logs")
        for line in body["lines"]
    )
    assert "ignored" in logged and "gradle:9.1.0-jdk17" in logged


def test_docker_runs_as_the_agents_own_uid(agent, monkeypatch):
    monkeypatch.setattr(agent.os, "getuid", lambda: 1001, raising=False)
    monkeypatch.setattr(agent.os, "getgid", lambda: 1001, raising=False)
    assert agent.container_user("docker") == "1001:1001"


def test_rootless_podman_is_left_to_map_the_user_itself(agent, monkeypatch):
    """Its container root already IS the invoking user; forcing --user maps
    into the subuid range and leaves files the agent cannot delete."""
    monkeypatch.setattr(agent.os, "getuid", lambda: 1001, raising=False)
    monkeypatch.setattr(agent.os, "getgid", lambda: 1001, raising=False)
    assert agent.container_user("podman") is None
    # Root podman is not rootless, so the mapping is the plain one again.
    monkeypatch.setattr(agent.os, "getuid", lambda: 0, raising=False)
    monkeypatch.setattr(agent.os, "getgid", lambda: 0, raising=False)
    assert agent.container_user("podman") == "0:0"


def test_a_pinned_runtime_is_the_only_one_tried(agent, monkeypatch):
    """--runtime docker means docker: a podman installed on the machine later
    must not quietly change how these builds run."""
    tried = []
    monkeypatch.setattr(agent, "_runtime_cache", None)
    monkeypatch.setattr(agent, "_containers_disabled", False)
    monkeypatch.setattr(agent, "_runtime_pinned", "docker")
    monkeypatch.setattr(agent.platform, "system", lambda: "Linux")
    monkeypatch.setattr(agent.shutil, "which", lambda name: "/usr/bin/" + name)

    def fake_run(argv, **kw):
        tried.append(argv[0])
        return type("R", (), {"returncode": 1})()

    monkeypatch.setattr(agent.subprocess, "run", fake_run)
    assert agent.container_runtime() == ""
    assert tried == ["docker"]  # podman never consulted

