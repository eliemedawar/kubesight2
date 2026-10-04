"""The build cache with more than one thing running at once.

Three situations, each run for real - the generated shell, as several
processes at the same time against one cache directory:

* parallel stages of ONE build (one pod): they must agree on one cache slot,
  and their package-manager installs must take turns;
* two builds of the SAME service (two pods): the second must get its own slot
  for the tools that cannot share across pods, and slot 0 must come back when
  the first is done;
* builds of DIFFERENT services: each stays in its own subtree, slot 0.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
import tempfile

from api.services.ci import cache_layout
from api.services.ci.runners import kubernetes as k8s

SH = shutil.which("sh")
needs_sh = pytest.mark.skipif(not SH or not shutil.which("stat"), reason="needs sh and stat")

# A hang guard only; MSYS sh forks slowly under load.
_TIMEOUT = 300

_REPORT = (
    'echo "SLOT=$KUBESIGHT_CACHE_SLOT"\n'
    'echo "SLOT_DIR=$KUBESIGHT_CACHE_SLOT_DIR"\n'
    'echo "GRADLE_USER_HOME=$GRADLE_USER_HOME"\n'
    'echo "MAVEN_OPTS=$MAVEN_OPTS"\n'
    'echo "YARN_CACHE_FOLDER=$YARN_CACHE_FOLDER"\n'
    'echo "BUILDKIT_CACHE_DIR=$BUILDKIT_CACHE_DIR"\n'
    'echo "TRIVY_CACHE_DIR=${TRIVY_CACHE_DIR:-}"\n'
    'echo "npm_config_cache=$npm_config_cache"\n'
)


def _script_file(text: str) -> str:
    """Scripts go to sh as a FILE: on Windows a long `sh -c` argument passes
    through the command-line quoting rules and loses some of its quotes."""
    handle = tempfile.NamedTemporaryFile("w", suffix=".sh", delete=False, newline="\n", encoding="utf-8")
    with handle:
        handle.write(text)
    return handle.name


def _stage_env(volume: Path, slug: str, build_id: str, extra=None):
    base = f"{volume.as_posix()}/{cache_layout.slug_dir(slug)}"
    env = dict(os.environ)
    env.update(cache_layout.tool_env(base))
    env.update(
        {
            "KUBESIGHT_CACHE_DIR": base,
            "KUBESIGHT_BUILD_ID": build_id,
            "TRIVY_CACHE_DIR": f"{base}/trivy",
            "KUBESIGHT_WORKSPACE": (volume.parent / f"ws-{build_id}").as_posix(),
            # Renewal is tested on its own; elsewhere no background loop.
            "KUBESIGHT_CACHE_LEASE_HEARTBEAT": "0",
        }
    )
    env.update(extra or {})
    return env, base


def _prep(slots=cache_layout.DEFAULT_SLOTS, stale_seconds=cache_layout.DEFAULT_LEASE_STALE_SECONDS):
    return cache_layout.prep_script(slots=slots, stale_seconds=stale_seconds) + _REPORT


def _parse(stdout: str):
    values = {}
    for line in stdout.splitlines():
        if "=" in line and not line.startswith("["):
            key, _, value = line.partition("=")
            values[key] = value
    return values


def _run_stage(volume, slug, build_id, *, script=None, extra=None):
    env, base = _stage_env(volume, slug, build_id, extra)
    done = subprocess.run(
        [SH, _script_file(script or _prep())], env=env, capture_output=True, text=True, timeout=_TIMEOUT
    )
    assert done.returncode == 0, done.stderr
    return _parse(done.stdout), base, done


def _start_stage(volume, slug, build_id, *, script=None):
    env, _ = _stage_env(volume, slug, build_id)
    return subprocess.Popen(
        [SH, _script_file(script or _prep())], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )


def _finish(proc):
    out, err = proc.communicate(timeout=_TIMEOUT)
    assert proc.returncode == 0, err
    return _parse(out)


@pytest.fixture
def volume(tmp_path):
    path = tmp_path / "kubesight-cache"
    path.mkdir()
    return path


def _release(base: str, build_id: str):
    """What the collector does at the end of a build."""
    leases = Path(base) / cache_layout.LEASES_DIR_NAME
    for lease in leases.iterdir():
        owner = lease / "owner"
        if owner.is_file() and owner.read_text().strip() == build_id:
            shutil.rmtree(lease)


# ---------------------------------------------------------------------------
# One build at a time: exactly what it was before slots
# ---------------------------------------------------------------------------

@needs_sh
def test_a_lone_build_uses_slot_0_which_is_the_services_own_directories(volume):
    seen, base, _ = _run_stage(volume, "payments", "11")
    assert seen["SLOT"] == "0"
    assert seen["SLOT_DIR"] == base
    assert seen["GRADLE_USER_HOME"] == f"{base}/gradle"
    assert f"-Dmaven.repo.local={base}/maven" in seen["MAVEN_OPTS"]
    assert (Path(base) / ".leases/0/owner").read_text().strip() == "11"
    # The init script went where Gradle will read it.
    assert (Path(base) / "gradle/init.d" / cache_layout.GRADLE_INIT_SCRIPT_NAME).is_file()


@needs_sh
def test_every_stage_of_one_build_keeps_the_same_slot(volume):
    first, base, _ = _run_stage(volume, "payments", "11")
    second, _, _ = _run_stage(volume, "payments", "11")
    assert first["SLOT"] == second["SLOT"] == "0"
    assert sorted(p.name for p in (Path(base) / ".leases").iterdir()) == ["0"]


@needs_sh
def test_a_finished_build_gives_slot_0_back(volume):
    _, base, _ = _run_stage(volume, "payments", "11")
    _release(base, "11")
    seen, _, _ = _run_stage(volume, "payments", "12")
    assert seen["SLOT"] == "0"


# ---------------------------------------------------------------------------
# Two builds of the SAME service at once
# ---------------------------------------------------------------------------

@needs_sh
def test_a_second_build_of_the_service_gets_its_own_slot_for_the_unshareable_tools(volume):
    _run_stage(volume, "payments", "11")  # build 11 is still running
    seen, base, done = _run_stage(volume, "payments", "12")
    slot = f"{base}/slots/1"
    assert seen["SLOT"] == "1"
    assert seen["SLOT_DIR"] == slot
    assert seen["GRADLE_USER_HOME"] == f"{slot}/gradle"
    assert seen["YARN_CACHE_FOLDER"] == f"{slot}/yarn"
    assert seen["BUILDKIT_CACHE_DIR"] == f"{slot}/buildkit"
    assert seen["TRIVY_CACHE_DIR"] == f"{slot}/trivy"
    assert f"-Dmaven.repo.local={slot}/maven" in seen["MAVEN_OPTS"]
    # Maven's lock flags survive the rewrite.
    assert cache_layout.MAVEN_LOCK_OPTS in seen["MAVEN_OPTS"]
    # Tools that are safe with concurrent writers stay where they are warm.
    assert seen["npm_config_cache"] == f"{base}/npm"
    assert (Path(slot) / "gradle/init.d" / cache_layout.GRADLE_INIT_SCRIPT_NAME).is_file()
    assert "cache slot 1" in done.stdout


@needs_sh
def test_two_builds_starting_at_the_same_moment_never_share_a_slot(volume):
    for attempt in range(3):
        builds = [str(100 + attempt * 10 + n) for n in range(3)]
        procs = [_start_stage(volume, "payments", build) for build in builds]
        slots = [_finish(proc)["SLOT"] for proc in procs]
        assert sorted(slots) == ["0", "1", "2"], slots
        base = f"{volume.as_posix()}/payments"
        for build in builds:
            _release(base, build)


@needs_sh
def test_parallel_stages_of_one_build_agree_on_one_slot(volume):
    """Members of a parallel group start together. The one that loses the
    mkdir must see its own build's lease - even in the instant before the
    winner has written the owner - and not wander off to slot 1."""
    _run_stage(volume, "payments", "11")  # another build holds slot 0
    procs = [_start_stage(volume, "payments", "12") for _ in range(4)]
    slots = {_finish(proc)["SLOT"] for proc in procs}
    assert slots == {"1"}


@needs_sh
def test_a_stage_environment_override_is_left_alone_in_another_slot(volume):
    _run_stage(volume, "payments", "11")
    seen, _, _ = _run_stage(
        volume, "payments", "12", extra={"GRADLE_USER_HOME": "/somewhere/else"}
    )
    assert seen["SLOT"] == "1"
    assert seen["GRADLE_USER_HOME"] == "/somewhere/else"


@needs_sh
def test_when_every_slot_is_taken_the_build_runs_cold_but_alone(volume):
    for build in ("1", "2"):
        _run_stage(volume, "payments", build, script=_prep(slots=2))
    seen, _, done = _run_stage(volume, "payments", "3", script=_prep(slots=2))
    assert seen["SLOT"] == "private"
    assert seen["GRADLE_USER_HOME"].endswith("/.kubesight/cache-private/gradle")
    assert "start cold in this build" in done.stdout


@needs_sh
def test_a_lease_nobody_renewed_is_taken_over(volume):
    """A cancelled build's pod is deleted before its collector runs, so its
    lease is never released. Once stale, the next build takes slot 0 back."""
    _, base, _ = _run_stage(volume, "payments", "11")
    old = time.time() - 3600
    os.utime(Path(base) / ".leases/0", (old, old))
    seen, _, done = _run_stage(volume, "payments", "12")
    assert seen["SLOT"] == "0"
    assert (Path(base) / ".leases/0/owner").read_text().strip() == "12"
    assert "stopped renewing it" in done.stdout


@needs_sh
def test_a_live_lease_is_not_taken_over(volume):
    _, base, _ = _run_stage(volume, "payments", "11")
    seen, _, _ = _run_stage(volume, "payments", "12")
    assert seen["SLOT"] == "1"
    assert (Path(base) / ".leases/0/owner").read_text().strip() == "11"


@needs_sh
def test_the_lease_is_renewed_while_a_stage_runs(volume):
    _, base, _ = _run_stage(volume, "payments", "11")
    lease = Path(base) / ".leases/0"
    old = time.time() - 3600
    os.utime(lease, (old, old))
    env, _ = _stage_env(volume, "payments", "11", {"KUBESIGHT_CACHE_LEASE_HEARTBEAT": "1"})
    subprocess.run(
        [SH, _script_file(_prep() + "sleep 4\n")], env=env, capture_output=True, text=True, timeout=_TIMEOUT
    )
    assert lease.stat().st_mtime > time.time() - 600


# ---------------------------------------------------------------------------
# Different services at once
# ---------------------------------------------------------------------------

@needs_sh
def test_different_services_at_once_each_keep_their_own_slot_0(volume):
    procs = {slug: _start_stage(volume, slug, str(n)) for n, slug in enumerate(("payments", "issuing", "acquiring-ui"))}
    for slug, proc in procs.items():
        seen = _finish(proc)
        base = f"{volume.as_posix()}/{slug}"
        assert seen["SLOT"] == "0", slug
        assert seen["GRADLE_USER_HOME"] == f"{base}/gradle"


# ---------------------------------------------------------------------------
# The collector releases the lease
# ---------------------------------------------------------------------------

@needs_sh
def test_the_collector_releases_only_this_builds_slot(volume, tmp_path):
    _, base, _ = _run_stage(volume, "payments", "11")
    _run_stage(volume, "payments", "12")
    env = dict(os.environ)
    env.update(
        {
            "KUBESIGHT_CALLBACK_URL": "http://127.0.0.1:9/api/ci",
            "KUBESIGHT_CALLBACK_TOKEN": "t",
            "KUBESIGHT_BUILD_ID": "11",
            "KUBESIGHT_CACHE_DIR": base,
            "KUBESIGHT_ARTIFACTS": "[]",
            "KUBESIGHT_IMAGES": "[]",
        }
    )
    import sys

    done = subprocess.run(
        [sys.executable, "-c", k8s._COLLECTOR_SCRIPT],
        env=env, capture_output=True, text=True, timeout=_TIMEOUT, cwd=tmp_path,
    )
    assert done.returncode == 0, done.stderr
    assert "released cache slot 0" in done.stdout
    leases = sorted(p.name for p in (Path(base) / ".leases").iterdir())
    assert leases == ["1"]


def test_the_collector_is_told_where_the_cache_is(monkeypatch):
    monkeypatch.setenv("CI_CACHE_CLAIM_NAME", "ci-cache")
    monkeypatch.setenv("CI_CACHE_SHARED", "none")
    from tests.test_ci_cache_paths import _execution, _job  # noqa: WPS433

    job = _job(_execution())
    collector = job["spec"]["template"]["spec"]["containers"][0]
    env = {item["name"]: item.get("value") for item in collector["env"]}
    assert env["KUBESIGHT_CACHE_DIR"] == f"{cache_layout.CACHE_MOUNT_PATH}/test123"


# ---------------------------------------------------------------------------
# Installs inside one pod take turns
# ---------------------------------------------------------------------------

@pytest.fixture
def node_project(tmp_path):
    """A workspace two parallel stages share, and a fake yarn that notices when
    another install is running at the same moment."""
    ws = tmp_path / "ws"
    (ws / ".kubesight").mkdir(parents=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    log = tmp_path / "log"
    inside = tmp_path / "inside"
    yarn = bin_dir / "yarn"
    yarn.write_text(
        "#!/bin/sh\n"
        f'LOCK="{ws.as_posix()}/.kubesight/locks/node-install"\n'
        f'if [ -d "$LOCK" ]; then echo "locked $*" >> "{log.as_posix()}"; '
        f'else echo "free $*" >> "{log.as_posix()}"; fi\n'
        'case "${1:-install}" in install|--*)\n'
        f'  if ! mkdir "{inside.as_posix()}" 2>/dev/null; then echo OVERLAP >> "{log.as_posix()}"; fi\n'
        "  sleep 2\n"
        f'  rmdir "{inside.as_posix()}" 2>/dev/null\n'
        "  mkdir -p node_modules/x\n"
        '  if [ -n "${FAIL_INSTALL:-}" ]; then exit 3; fi ;;\n'
        "esac\n"
        "exit 0\n"
    )
    yarn.chmod(0o755)
    projects = []
    for name in ("web", "admin"):
        src = ws / "source" / name
        src.mkdir(parents=True)
        (src / "package.json").write_text('{"name": "%s"}\n' % name)
        (src / "yarn.lock").write_text("x@1\n")
        projects.append(src)
    cache = tmp_path / "cache"
    cache.mkdir()
    return {"ws": ws, "bin": bin_dir, "log": log, "projects": projects, "cache": cache}


def _nm_env(project, extra=None):
    env = dict(os.environ)
    env["PATH"] = project["bin"].as_posix() + os.pathsep + env.get("PATH", "")
    env["KUBESIGHT_CACHE_DIR"] = project["cache"].as_posix()
    env["KUBESIGHT_WORKSPACE"] = project["ws"].as_posix()
    env.update(extra or {})
    return env


def _nm_start(project, cwd, commands, stage, extra=None):
    script = "set -eu\n" + cache_layout.node_modules_wrap(commands, image="node:16", stage=stage) + "\n"
    return subprocess.Popen(
        [SH, _script_file(script)], cwd=cwd, env=_nm_env(project, extra),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


@needs_sh
def test_two_parallel_installs_in_one_pod_take_turns(node_project):
    web, admin = node_project["projects"]
    procs = [
        _nm_start(node_project, web, "yarn install --frozen-lockfile\nyarn build", "Web"),
        _nm_start(node_project, admin, "yarn install --frozen-lockfile\nyarn build", "Admin"),
    ]
    outputs = [proc.communicate(timeout=_TIMEOUT) for proc in procs]
    assert all(proc.returncode == 0 for proc in procs), outputs
    log = node_project["log"].read_text().splitlines()
    assert "OVERLAP" not in log
    # The installs ran under the lock; the builds did not wait for it.
    assert log.count("locked install --frozen-lockfile") == 2
    assert log.count("free build") == 2
    assert not (node_project["ws"] / ".kubesight/locks/node-install").exists()


@needs_sh
def test_a_failed_install_lets_go_of_the_lock(node_project):
    web, admin = node_project["projects"]
    failed = _nm_start(node_project, web, "yarn install", "Web", {"FAIL_INSTALL": "1"})
    failed.communicate(timeout=_TIMEOUT)
    assert failed.returncode == 3
    assert not (node_project["ws"] / ".kubesight/locks/node-install").exists()
    after = _nm_start(node_project, admin, "yarn install", "Admin")
    out, err = after.communicate(timeout=_TIMEOUT)
    assert after.returncode == 0, err
    assert "waiting for" not in out


@needs_sh
def test_a_stale_install_lock_is_taken_over(node_project):
    lock = node_project["ws"] / ".kubesight/locks/node-install"
    lock.mkdir(parents=True)
    (lock / "owner").write_text("Gone (pod 1 1)\n")
    old = time.time() - 3600
    os.utime(lock, (old, old))
    proc = _nm_start(node_project, node_project["projects"][0], "yarn install", "Web")
    out, err = proc.communicate(timeout=_TIMEOUT)
    assert proc.returncode == 0, err
    assert "is stale; taking it" in out


@needs_sh
def test_a_waiting_install_says_who_it_waits_for(node_project):
    web, admin = node_project["projects"]
    first = _nm_start(node_project, web, "yarn install", "Install web")
    lock = node_project["ws"] / ".kubesight/locks/node-install"
    deadline = time.time() + 120
    while not (lock / "owner").exists() and time.time() < deadline:
        time.sleep(0.1)
    second = _nm_start(node_project, admin, "yarn install", "Install admin")
    out, _ = second.communicate(timeout=_TIMEOUT)
    first.communicate(timeout=_TIMEOUT)
    assert second.returncode == 0
    assert "waiting for Install web" in out


@pytest.mark.parametrize(
    "words, expected",
    [
        ("yarn", True),
        ("yarn --frozen-lockfile", True),
        ("yarn install", True),
        ("yarn --cwd web install", True),
        ("yarn add left-pad", True),
        ("yarn build", False),
        ("yarn run test", False),
        ("npm ci", True),
        ("npm install", True),
        ("npm run build", False),
        ("pnpm i", True),
        ("pnpm run lint", False),
    ],
)
@needs_sh
def test_which_calls_count_as_installs(words, expected):
    lines = cache_layout._node_install_lock_lines("[t]", "s")
    fn = "\n".join(lines)
    done = subprocess.run(
        [SH, _script_file(fn + f"\nif ks_nm_is_install {words}; then echo yes; else echo no; fi\n")],
        capture_output=True, text=True, timeout=_TIMEOUT,
    )
    assert done.stdout.strip() == ("yes" if expected else "no")
