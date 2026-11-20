"""Build workspaces on the node's own disk (CI_WORKSPACE_HOST_DIR).

What is locked here:

* unset, the pod is exactly what it was (an emptyDir, no extra container);
* set, /workspace is <dir>/<job name> on the node, a root-only first step
  hands it to the build user with nothing but CHOWN and DAC_OVERRIDE, and the
  stages themselves keep the restricted context;
* the sweep removes other builds' folders only once their deadline passed,
  never this build's and never anything not named like a build's Job — a
  setting that points at a shared directory cannot delete what else is there;
* the collector empties the folder after uploading;
* a bad setting refuses the build instead of mounting something surprising.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time

import pytest

from api.services.ci.runners import kubernetes as k8s
from api.services.ci.runners.base import RunnerError
from tests.test_ci_kubernetes_runner import _execution, _plan


def _job(monkeypatch, host_dir=None, selector=None):
    if host_dir is None:
        monkeypatch.delenv("CI_WORKSPACE_HOST_DIR", raising=False)
    else:
        monkeypatch.setenv("CI_WORKSPACE_HOST_DIR", host_dir)
    if selector is None:
        monkeypatch.delenv("CI_WORKSPACE_HOST_NODE_SELECTOR", raising=False)
    else:
        monkeypatch.setenv("CI_WORKSPACE_HOST_NODE_SELECTOR", selector)
    first = _plan(_execution(0, "checkout"), _execution(1, image="node:22-alpine", commands=["yarn build"]))
    _, _, job = k8s.build_job_resources(first)
    return job["spec"]["template"]["spec"]


def _volume(spec, name):
    return next((v for v in spec["volumes"] if v["name"] == name), None)


def test_unset_keeps_the_emptydir_and_adds_nothing(monkeypatch):
    spec = _job(monkeypatch)
    assert "emptyDir" in _volume(spec, "workspace")
    assert _volume(spec, "workspaces-root") is None
    assert [c["name"] for c in spec["initContainers"]] == ["stage-0", "stage-1"]
    assert "nodeSelector" not in spec
    collector_env = {e["name"] for e in spec["containers"][0]["env"]}
    assert "KUBESIGHT_WORKSPACE_EMPTY_AT_END" not in collector_env


def test_set_puts_the_workspace_on_the_node(monkeypatch):
    spec = _job(monkeypatch, "/data/kubesight-ci/workspaces/", selector="kubesight.io/ci-disk=true")
    assert _volume(spec, "workspace") == {
        "name": "workspace",
        "hostPath": {"path": "/data/kubesight-ci/workspaces/ci-b7-payment-service", "type": "DirectoryOrCreate"},
    }
    assert _volume(spec, "workspaces-root")["hostPath"]["path"] == "/data/kubesight-ci/workspaces"
    assert spec["nodeSelector"] == {"kubesight.io/ci-disk": "true"}

    names = [c["name"] for c in spec["initContainers"]]
    assert names == [k8s.WORKSPACE_PREP_CONTAINER, "stage-0", "stage-1"]
    prep = spec["initContainers"][0]
    sc = prep["securityContext"]
    assert sc["runAsUser"] == 0 and sc["allowPrivilegeEscalation"] is False
    assert sc["readOnlyRootFilesystem"] is True
    assert sc["capabilities"] == {"drop": ["ALL"], "add": ["CHOWN", "DAC_OVERRIDE"]}
    env = {e["name"]: e["value"] for e in prep["env"]}
    assert env["KUBESIGHT_JOB_NAME"] == "ci-b7-payment-service"
    # Outlives the Job's own deadline, so a sweep never takes a live build's folder.
    assert int(env["KUBESIGHT_WORKSPACE_TTL"]) > 600 * 2 + 900

    # Stages keep the restricted context and never see the sweep's mount.
    for stage in spec["initContainers"][1:]:
        assert stage["securityContext"]["runAsUser"] == 65532
        assert all(m["name"] != "workspaces-root" for m in stage["volumeMounts"])
    collector_env = {e["name"]: e.get("value") for e in spec["containers"][0]["env"]}
    assert collector_env["KUBESIGHT_WORKSPACE_EMPTY_AT_END"] == "1"


@pytest.mark.parametrize("value", ["data/ws", "/data/../etc", "/data", "/"])
def test_a_bad_directory_refuses_the_build(monkeypatch, value):
    with pytest.raises(RunnerError):
        _job(monkeypatch, value)


def test_off_means_unset(monkeypatch):
    spec = _job(monkeypatch, "off")
    assert "emptyDir" in _volume(spec, "workspace")


# ---------------------------------------------------------------------------
# The prep script, run for real
# ---------------------------------------------------------------------------

def _sh():
    path = shutil.which("sh")
    if not path:
        pytest.skip("no POSIX shell")
    return path


def _run_prep(tmp_path, job="ci-b9-web", ttl=3600, stale_minutes=60):
    root = tmp_path / "workspaces"
    ws = root / job
    ws.mkdir(parents=True, exist_ok=True)
    script = k8s.workspace_prep_script().replace(
        "ws=/workspace; root=/kubesight-workspaces",
        f"ws='{ws.as_posix()}'; root='{root.as_posix()}'",
    )
    script_file = tmp_path / "prep.sh"
    script_file.write_text(script, encoding="utf-8")
    env = {
        **os.environ,
        "KUBESIGHT_JOB_NAME": job,
        "KUBESIGHT_WORKSPACE_TTL": str(ttl),
        "KUBESIGHT_WORKSPACE_STALE_MINUTES": str(stale_minutes),
    }
    result = subprocess.run([_sh(), str(script_file)], env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    return root, result.stdout


def _old_build(root, name, *, expires=None, age_minutes=0):
    folder = root / name
    (folder / "source" / "node_modules").mkdir(parents=True)
    (folder / "source" / "node_modules" / "big.bin").write_bytes(b"x" * 10)
    if expires is not None:
        (folder / k8s.WORKSPACE_STAMP).write_text(f"expires={expires}\n")
    if age_minutes:
        stamp = time.time() - age_minutes * 60
        os.utime(folder, (stamp, stamp))
    return folder


def test_the_sweep_removes_only_finished_build_folders(tmp_path):
    root = tmp_path / "workspaces"
    now = int(time.time())
    expired = _old_build(root, "ci-b1-old", expires=now - 10)
    live = _old_build(root, "ci-b2-live", expires=now + 3600)
    unstamped_old = _old_build(root, "ci-b3-crashed", age_minutes=600)
    unstamped_new = _old_build(root, "ci-b4-starting")
    not_ours = root / "containerd"
    not_ours.mkdir()
    (not_ours / "keep").write_text("images")
    stray_file = root / "ci-b5-file"
    stray_file.write_text("not a folder")

    root, out = _run_prep(tmp_path)

    assert not expired.exists()
    assert not unstamped_old.exists()
    assert live.exists() and unstamped_new.exists()
    assert (not_ours / "keep").exists() and stray_file.exists()
    assert "cleared 2 old" in out
    # This build's own folder is stamped with its deadline.
    stamp = (root / "ci-b9-web" / k8s.WORKSPACE_STAMP).read_text()
    assert int(stamp.split("=")[1]) >= now + 3600


def test_the_sweep_never_takes_this_builds_folder(tmp_path):
    root = tmp_path / "workspaces"
    _old_build(root, "ci-b9-web", expires=int(time.time()) - 100)
    root, _ = _run_prep(tmp_path, job="ci-b9-web")
    assert (root / "ci-b9-web" / "source").exists()


# ---------------------------------------------------------------------------
# The collector's cleanup
# ---------------------------------------------------------------------------

def _empty_workspace_fn():
    source = k8s._COLLECTOR_SCRIPT
    start = source.index("def empty_workspace")
    end = source.index("\nif failures:")
    namespace = {"os": os, "shutil": shutil}
    exec(source[start:end], namespace)
    return namespace["empty_workspace"]


def test_the_collector_empties_the_folder_and_keeps_the_stamp(tmp_path, monkeypatch, capsys):
    ws = tmp_path / "ci-b9-web"
    (ws / "source" / "dist").mkdir(parents=True)
    (ws / ".kubesight").mkdir()
    (ws / ".kubesight" / "build.env").write_text("A=1")
    (ws / k8s.WORKSPACE_STAMP).write_text("expires=1")
    monkeypatch.setenv("KUBESIGHT_WORKSPACE_PATH", str(ws))

    monkeypatch.delenv("KUBESIGHT_WORKSPACE_EMPTY_AT_END", raising=False)
    _empty_workspace_fn()()
    assert (ws / "source").exists()  # emptyDir installations: untouched

    monkeypatch.setenv("KUBESIGHT_WORKSPACE_EMPTY_AT_END", "1")
    _empty_workspace_fn()()
    assert sorted(os.listdir(ws)) == [k8s.WORKSPACE_STAMP]
    assert "emptied the build's folder" in capsys.readouterr().out
