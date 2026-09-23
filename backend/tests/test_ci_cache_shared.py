"""The shared cache subtree: tools every service can use one copy of.

test_ci_cache_paths.py pins the per-service contract (with sharing switched
off). This file covers what changes when it is on, which is the default on the
one hand-made claim: which tools move to /kubesight-cache/_shared, which never
do, where sharing does not apply, and what the operator can do about it.
"""

from __future__ import annotations

import json

import pytest

from api.db import db
from api.models_ci import CiBuild, CiService
from api.services.ci import cache as cache_service
from api.services.ci import cache_layout
from api.services.ci.merge_checks import stages as merge_stages
from api.services.ci.runners import kubernetes as k8s
from api.services.ci.runners.base import StageExecution

MOUNT = cache_layout.CACHE_MOUNT_PATH
SHARED = f"{MOUNT}/_shared"
OWN = f"{MOUNT}/test123"


@pytest.fixture(autouse=True)
def _hand_made_claim(monkeypatch):
    monkeypatch.delenv("CI_CACHE_STORAGE_CLASS", raising=False)
    monkeypatch.setenv("CI_CACHE_CLAIM_NAME", "ci-cache")
    monkeypatch.delenv("CI_CACHE_SHARED", raising=False)
    yield
    k8s.set_kubectl_runner(None)


def _execution(slug="test123"):
    execution = StageExecution(
        build_id=1,
        build_number=1,
        service_slug=slug,
        stage_id=100,
        stage_name="Stage 0",
        stage_type="command",
        position=0,
        image="node:22",
        working_directory=None,
        commands=["npm ci"],
        env={},
        callback_url="http://kubesight/api/ci/callback",
        callback_token="token",
    )
    execution.plan = [execution]
    return execution


def _stage(slug="test123"):
    _, _, job = k8s.build_job_resources(_execution(slug))
    container = job["spec"]["template"]["spec"]["initContainers"][0]
    env = {item["name"]: item.get("value", "") for item in container["env"]}
    return env, container["command"][2]


# ---------------------------------------------------------------------------
# What moves, and what never does
# ---------------------------------------------------------------------------

def test_shareable_tools_point_at_the_shared_subtree_by_default():
    env, _ = _stage()
    assert env["KUBESIGHT_SHARED_CACHE_DIR"] == SHARED
    assert env["DC_DATA_DIR"] == f"{SHARED}/dependency-check-data"
    assert env["SEMGREP_CACHE_DIR"] == f"{SHARED}/semgrep"
    assert env["SEMGREP_VERSION_CACHE_PATH"] == f"{SHARED}/semgrep/version"
    assert env["npm_config_cache"] == f"{SHARED}/npm"
    assert env["YARN_CACHE_FOLDER"] == f"{SHARED}/yarn"
    assert env["npm_config_store_dir"] == f"{SHARED}/pnpm"
    assert env["PIP_CACHE_DIR"] == f"{SHARED}/pip"
    assert env["GOMODCACHE"] == f"{SHARED}/go/mod"


def test_tools_that_break_when_shared_stay_per_service():
    """Gradle's lock protocol cannot reach another pod, Maven's repository is not
    safe for concurrent writers, BuildKit's local export overwrites one index."""
    env, _ = _stage()
    assert env["KUBESIGHT_CACHE_DIR"] == OWN
    assert env["GRADLE_USER_HOME"] == f"{OWN}/gradle"
    assert env["GRADLE_BUILD_CACHE_DIR"] == f"{OWN}/gradle-build-cache"
    assert env["MAVEN_OPTS"] == f"-Dmaven.repo.local={OWN}/maven"
    assert env["BUILDKIT_CACHE_DIR"] == f"{OWN}/buildkit"
    assert env["GOCACHE"] == f"{OWN}/go/build"


def test_none_of_the_unsafe_tools_can_even_be_selected():
    for key in ("gradle", "maven", "buildkit", "gradle-build-cache"):
        assert key not in cache_layout.SHAREABLE_KEYS
    assert cache_layout.parse_shared(["gradle", "npm"]) == ("npm",)


def test_two_services_get_the_same_shared_paths_and_different_own_paths():
    a, _ = _stage("payments")
    b, _ = _stage("issuing")
    assert a["DC_DATA_DIR"] == b["DC_DATA_DIR"]
    assert a["npm_config_cache"] == b["npm_config_cache"]
    assert a["GRADLE_USER_HOME"] != b["GRADLE_USER_HOME"]


def test_no_service_slug_can_become_the_shared_directory():
    for slug in ("_shared", "__shared", "_SHARED", "-shared"):
        assert cache_layout.slug_dir(slug) != cache_layout.SHARED_DIR_NAME


def test_a_chosen_subset_shares_only_those(monkeypatch):
    monkeypatch.setenv("CI_CACHE_SHARED", "dependency-check,npm")
    env, _ = _stage()
    assert env["DC_DATA_DIR"] == f"{SHARED}/dependency-check-data"
    assert env["npm_config_cache"] == f"{SHARED}/npm"
    assert env["YARN_CACHE_FOLDER"] == f"{OWN}/yarn"
    assert env["SEMGREP_CACHE_DIR"] == f"{OWN}/semgrep"


def test_sharing_nothing_is_exactly_the_per_service_layout(monkeypatch):
    monkeypatch.setenv("CI_CACHE_SHARED", "none")
    env, script = _stage()
    assert env["KUBESIGHT_SHARED_CACHE_DIR"] == ""
    assert env["DC_DATA_DIR"] == f"{OWN}/dependency-check-data"
    assert "KUBESIGHT_SHARED_CACHE_DIR/" not in script


def test_a_per_service_claim_never_shares(monkeypatch):
    """Storage-class mode gives each service its own claim: there is no volume
    in common, so a "shared" directory would be shared with nobody."""
    monkeypatch.delenv("CI_CACHE_CLAIM_NAME", raising=False)
    monkeypatch.setenv("CI_CACHE_STORAGE_CLASS", "fast")
    env, script = _stage()
    assert env["KUBESIGHT_SHARED_CACHE_DIR"] == ""
    assert env["DC_DATA_DIR"] == f"{OWN}/dependency-check-data"
    assert "KUBESIGHT_SHARED_CACHE_DIR/" not in script


def test_caching_off_shares_nothing_either(monkeypatch):
    monkeypatch.delenv("CI_CACHE_CLAIM_NAME", raising=False)
    env, _ = _stage()
    assert env["KUBESIGHT_SHARED_CACHE_DIR"] == ""
    assert "DC_DATA_DIR" not in env


def test_a_stage_environment_still_wins_over_the_shared_path():
    execution = _execution()
    execution.env = {"npm_config_cache": "/somewhere/else"}
    _, _, job = k8s.build_job_resources(execution)
    env = {i["name"]: i.get("value", "") for i in
           job["spec"]["template"]["spec"]["initContainers"][0]["env"]}
    assert env["npm_config_cache"] == "/somewhere/else"


# ---------------------------------------------------------------------------
# The stage prep
# ---------------------------------------------------------------------------

def test_prep_creates_shared_scanner_dirs_in_the_shared_subtree():
    _, script = _stage()
    assert '"$KUBESIGHT_SHARED_CACHE_DIR/dependency-check-data"' in script
    assert '"$KUBESIGHT_SHARED_CACHE_DIR/semgrep"' in script
    assert '"$KUBESIGHT_CACHE_DIR/gradle/init.d"' in script
    assert '"$KUBESIGHT_CACHE_DIR/dependency-check-data"' not in script


def test_the_log_marks_shared_warmth_as_shared():
    """"warm" on a service's very first build is otherwise a mystery."""
    _, script = _stage()
    assert 'KS_WARM="$KS_WARM $KS_DIR(shared)"' in script
    assert 'echo "[kubesight] Shared: $KUBESIGHT_SHARED_CACHE_DIR"' in script


def test_the_mismatch_check_expects_the_shared_path_for_shared_tools():
    _, script = _stage()
    assert ('if [ "${DC_DATA_DIR:-}" != "$KUBESIGHT_SHARED_CACHE_DIR/dependency-check-data" ]'
            in script)
    assert 'if [ "${GRADLE_USER_HOME:-}" != "$KUBESIGHT_CACHE_DIR/gradle" ]' in script


def test_the_mismatch_check_matches_what_is_actually_injected():
    env = cache_layout.tool_env(OWN, SHARED, cache_layout.DEFAULT_SHARED)
    shared_dirs = cache_layout.shared_subdirs(cache_layout.DEFAULT_SHARED)
    for name, subdir in cache_layout.PATH_VARS:
        parent = SHARED if subdir in shared_dirs else OWN
        assert env[name] == f"{parent}/{subdir}", name


def test_prep_is_still_ascii():
    assert cache_layout.prep_script(shared=cache_layout.DEFAULT_SHARED).isascii()


# ---------------------------------------------------------------------------
# Dependency-Check: one writer per NVD database
# ---------------------------------------------------------------------------

def test_dependency_check_takes_a_lock_on_its_database():
    script = "\n".join(merge_stages._dependency_check_commands("high"))
    assert 'DC_LOCK="$DATA_DIR/.kubesight-scan.lock"' in script
    assert 'until mkdir "$DC_LOCK"' in script
    # Stale after ten minutes without a heartbeat, so a killed pod cannot
    # block every later scan.
    assert "-mmin +10" in script
    assert 'touch "$DC_LOCK"' in script
    assert "trap dc_unlock EXIT" in script
    # The lock is taken before the scan and dropped after it.
    assert script.index("until mkdir") < script.index("dependency-check.sh")
    assert script.index("dependency-check.sh") < script.rindex("dc_unlock")


# ---------------------------------------------------------------------------
# The operator's side
# ---------------------------------------------------------------------------

def _bound_cluster(applied=None):
    pvc = {
        "metadata": {"name": "ci-cache"},
        "spec": {"accessModes": ["ReadWriteMany"], "volumeName": "pv",
                 "resources": {"requests": {"storage": "20Gi"}}},
        "status": {"phase": "Bound", "capacity": {"storage": "20Gi"}},
    }

    def fake(args, input_text=None):
        if args[:2] == ["get", "pvc"]:
            return 0, json.dumps(pvc), ""
        if args[:2] == ["get", "pv"]:
            return 1, "", "not found"
        if args[:2] == ["get", "jobs"]:
            return 0, json.dumps({"items": []}), ""
        if args[:2] == ["apply", "-f"]:
            if applied is not None:
                applied.append(json.loads(input_text))
            return 0, "", ""
        return 1, "", "unexpected"

    return fake


def test_the_saved_setting_wins_over_the_environment(app, monkeypatch):
    monkeypatch.setenv("CI_CACHE_SHARED", "all")
    with app.app_context():
        assert cache_service.shared_tools() == cache_layout.SHAREABLE_KEYS
        cache_service.save_settings({"enabled": True, "claimName": "ci-cache"})
        k8s.set_kubectl_runner(_bound_cluster())
        state = cache_service.set_shared(["npm"])
        assert state["shared"]["tools"] == ["npm"]
        assert cache_service.shared_tools() == ("npm",)
        # An empty list is a real answer: share nothing.
        cache_service.set_shared([])
        assert cache_service.shared_tools() == ()


def test_unshareable_tools_are_refused_by_name(app):
    with app.app_context():
        with pytest.raises(cache_service.CacheError) as excinfo:
            cache_service.set_shared(["npm", "gradle"])
    assert "gradle" in str(excinfo.value)


def test_status_describes_the_shared_subtree(app):
    with app.app_context():
        cache_service.save_settings({"enabled": True, "claimName": "ci-cache"})
        k8s.set_kubectl_runner(_bound_cluster())
        state = cache_service.status()
    assert state["shared"]["path"] == SHARED
    assert state["shared"]["applies"] is True
    assert {o["key"] for o in state["shared"]["options"]} == set(cache_layout.SHAREABLE_KEYS)


def test_cleaning_the_shared_cache_removes_only_it(app):
    applied: list = []
    with app.app_context():
        cache_service.save_settings({"enabled": True, "claimName": "ci-cache"})
        k8s.set_kubectl_runner(_bound_cluster(applied))
        result = cache_service.clean(shared=True)
    script = applied[0]["spec"]["template"]["spec"]["containers"][0]["command"][2]
    assert script.startswith(f"rm -rf {SHARED};")
    assert result["target"] == "the shared cache"


def test_cleaning_the_shared_cache_waits_for_every_service(app):
    """Any running build may be reading it, not just one service's."""
    with app.app_context():
        service = CiService(name="Other", slug="other")
        db.session.add(service)
        db.session.commit()
        db.session.add(CiBuild(service_id=service.id, number=3, status="running"))
        db.session.commit()
        cache_service.save_settings({"enabled": True, "claimName": "ci-cache"})
        k8s.set_kubectl_runner(_bound_cluster())
        with pytest.raises(cache_service.CacheError) as excinfo:
            cache_service.clean(shared=True)
    assert "#3" in str(excinfo.value)
