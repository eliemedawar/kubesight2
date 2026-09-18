"""The persistent cache, from the stage container's point of view.

test_ci_cache.py covers what an operator does to the volume — create it, switch
it on, measure it, empty it. This file covers what a BUILD sees: that the
variables arrive, that they point inside this service's subtree and nowhere
else, that the mount is there to back them, and that a service with an awkward
slug still gets a path instead of an exception.

No cluster: ``build_job_resources`` is pure, so every manifest here is asserted
rather than applied.
"""

from __future__ import annotations

import re

import pytest

from api.services.ci import cache_layout
from api.services.ci.runners import kubernetes as k8s
from api.services.ci.runners.base import StageExecution

CLAIM = "ci-cache"
MOUNT = cache_layout.CACHE_MOUNT_PATH


@pytest.fixture(autouse=True)
def _hand_made_claim(monkeypatch):
    """The mode this cluster actually runs in: no StorageClass anywhere, one
    PersistentVolume made by hand, every service inside it."""
    monkeypatch.delenv("CI_CACHE_STORAGE_CLASS", raising=False)
    monkeypatch.setenv("CI_CACHE_CLAIM_NAME", CLAIM)
    monkeypatch.delenv("CI_BUILDKIT_LOCAL_CACHE", raising=False)
    monkeypatch.delenv("CI_BUILDKIT_REGISTRY_CACHE", raising=False)
    monkeypatch.delenv("CI_BUILDKIT_CACHE_REPO", raising=False)
    yield
    k8s.set_kubectl_runner(None)


def _execution(position=0, stage_type="command", *, slug="test123", **kwargs):
    return StageExecution(
        build_id=1,
        build_number=1,
        service_slug=slug,
        stage_id=100 + position,
        stage_name=f"Stage {position}",
        stage_type=stage_type,
        position=position,
        image=kwargs.pop("image", "gradle:7.3.2-jdk11"),
        working_directory=kwargs.pop("working_directory", None),
        commands=kwargs.pop("commands", ["./gradlew bootJar"]),
        env=kwargs.pop("env", {}),
        callback_url="http://kubesight/api/ci/callback",
        callback_token="token",
        **kwargs,
    )


def _plan(*executions):
    plan = list(executions)
    for execution in plan:
        execution.plan = plan
    return plan[0]


def _job(*executions):
    _, _, job = k8s.build_job_resources(_plan(*executions))
    return job


def _env_map(container):
    return {item["name"]: item.get("value", "") for item in container["env"]}


def _stage_env(*executions, index=0):
    return _env_map(_job(*executions)["spec"]["template"]["spec"]["initContainers"][index])


# ---------------------------------------------------------------------------
# The variables a pipeline is written against
# ---------------------------------------------------------------------------

def test_every_cache_variable_is_injected_into_a_command_stage():
    """A stage script says $DC_DATA_DIR and expects it to be there. If these are
    not injected, the pipeline text in CI-CACHE.md is a lie."""
    env = _stage_env(_execution())
    base = f"{MOUNT}/test123"

    assert env["KUBESIGHT_CACHE_DIR"] == base
    assert env["KUBESIGHT_SERVICE_SLUG"] == "test123"
    assert env["GRADLE_USER_HOME"] == f"{base}/gradle"
    assert env["GRADLE_BUILD_CACHE_DIR"] == f"{base}/gradle-build-cache"
    assert env["DC_DATA_DIR"] == f"{base}/dependency-check-data"
    assert env["SEMGREP_CACHE_DIR"] == f"{base}/semgrep"
    assert env["BUILDKIT_CACHE_DIR"] == f"{base}/buildkit"


def test_the_cache_variables_reach_checkout_and_image_stages_too():
    """Not just command stages: the checkout writes into the same workspace and
    the image stage exports its layer cache, so every stage gets the same map."""
    registry = {
        "host": "nexus:9443",
        "repository": "test123",
        "tag": "1.0.0",
        "dockerfile": "Dockerfile",
    }
    job = _job(
        _execution(0, "checkout", repository_url="https://bitbucket/areebasal/issuing.git"),
        _execution(1, "command"),
        _execution(2, "container_image", registry=registry),
    )
    for container in job["spec"]["template"]["spec"]["initContainers"]:
        assert _env_map(container)["KUBESIGHT_CACHE_DIR"] == f"{MOUNT}/test123"


def test_KUBESIGHT_CACHE_still_means_what_it_used_to():
    """The original name is not dropped. A pipeline written against it keeps
    working, and points at exactly the same directory as the new one."""
    env = _stage_env(_execution())
    assert env["KUBESIGHT_CACHE"] == env["KUBESIGHT_CACHE_DIR"]


def test_a_stage_can_still_override_any_of_them():
    """These are defaults, not policy. A project that needs its own layout says
    so in the stage's Environment and wins."""
    env = _stage_env(
        _execution(env={"DC_DATA_DIR": "/workspace/source/dc-data"}),
    )
    assert env["DC_DATA_DIR"] == "/workspace/source/dc-data"
    # …and overriding one leaves the rest alone.
    assert env["SEMGREP_CACHE_DIR"] == f"{MOUNT}/test123/semgrep"


# ---------------------------------------------------------------------------
# Service scoping
# ---------------------------------------------------------------------------

def test_two_services_never_share_a_cache_directory():
    """Two services in one Gradle directory fight over the same lock files, and
    fail builds with errors that look nothing like the cause."""
    issuing = _stage_env(_execution(slug="test123"))
    ledger = _stage_env(_execution(slug="ledger-ui"))

    assert issuing["KUBESIGHT_CACHE_DIR"] != ledger["KUBESIGHT_CACHE_DIR"]
    for name in ("GRADLE_USER_HOME", "GRADLE_BUILD_CACHE_DIR", "DC_DATA_DIR",
                 "SEMGREP_CACHE_DIR", "BUILDKIT_CACHE_DIR", "MAVEN_OPTS"):
        assert issuing[name] != ledger[name], name

    # Nor may one service's path be a prefix of another's: /kubesight-cache/pay
    # must not sit inside /kubesight-cache/payments, or "clean pay" takes both.
    assert not ledger["KUBESIGHT_CACHE_DIR"].startswith(
        issuing["KUBESIGHT_CACHE_DIR"] + "/"
    )


def test_a_service_is_scoped_by_path_in_both_storage_modes(monkeypatch):
    """A per-service claim is isolated by the claim already — it gets the same
    per-service subtree anyway, so one rule explains the layout and `clean` can
    find a service's files without knowing which mode made the volume."""
    shared = k8s.cache_base_path("test123")

    monkeypatch.delenv("CI_CACHE_CLAIM_NAME", raising=False)
    monkeypatch.setenv("CI_CACHE_STORAGE_CLASS", "nfs-client")
    own_claim = k8s.cache_base_path("test123")

    assert shared == own_claim == f"{MOUNT}/test123"


def test_every_injected_path_stays_inside_this_services_subtree():
    """The whole isolation claim rests on this: not one variable may escape."""
    env = _stage_env(_execution())
    base = env["KUBESIGHT_CACHE_DIR"]
    for name, value in env.items():
        if not value.startswith(MOUNT) and "/kubesight-cache" not in value:
            continue
        assert base in value, f"{name}={value} leaves {base}"


# ---------------------------------------------------------------------------
# Slugs that are not tidy
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "slug, expected",
    [
        ("test123", "test123"),
        ("Payment Service", "payment-service"),
        ("AREEBA_Issuing", "areeba-issuing"),
        ("../../etc", "etc"),
        ("svc/../../root", "svc-root"),
        ("-leading-and-trailing-", "leading-and-trailing"),
        ("a" * 90, "a" * 63),
    ],
)
def test_a_slug_becomes_one_safe_directory_name(slug, expected):
    """No traversal, no shell metacharacter, no path separator — a service owns
    exactly one directory and cannot name its way out of it."""
    assert cache_layout.slug_dir(slug) == expected


@pytest.mark.parametrize("slug", ["", "   ", "...", "///", "###", None])
def test_an_unusable_slug_still_gets_a_path_of_its_own(slug):
    """Archived and disabled services reach this code too. Computing a path must
    not raise — and two services whose slugs both sanitise to nothing still need
    two directories, so the fallback hashes rather than sharing a constant."""
    directory = cache_layout.slug_dir(slug)
    assert directory and "/" not in directory
    assert directory != cache_layout.slug_dir("also-unusable-...")


def test_a_disabled_service_builds_a_manifest_without_a_special_case():
    """Nothing in the cache path depends on a service's state: whether it may
    build is decided long before a manifest is rendered."""
    job = _job(_execution(slug="Archived Service (2019)"))
    env = _env_map(job["spec"]["template"]["spec"]["initContainers"][0])
    assert env["KUBESIGHT_CACHE_DIR"] == f"{MOUNT}/archived-service-2019"


@pytest.mark.parametrize("slug", ["Payment Service", "a" * 90, "AREEBA_Issuing", "..."])
def test_the_directory_name_is_also_a_legal_kubernetes_name(slug, monkeypatch):
    """In storage-class mode the same sanitised slug becomes a PVC name, which
    Kubernetes rejects outright if it is not DNS-1123. One rule has to satisfy
    both, so the directory rule is held to the stricter of the two."""
    directory = cache_layout.slug_dir(slug)
    assert re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", directory), directory
    assert len(directory) <= 63

    monkeypatch.delenv("CI_CACHE_CLAIM_NAME", raising=False)
    monkeypatch.setenv("CI_CACHE_STORAGE_CLASS", "nfs-client")
    claim = k8s.cache_claim(slug)["metadata"]["name"]
    assert re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", claim), claim
    assert len(claim) <= 253


# ---------------------------------------------------------------------------
# The volume behind the variables
# ---------------------------------------------------------------------------

def test_the_cache_volume_is_mounted_on_every_container():
    """Variables pointing at a path nothing mounted is the failure mode this
    guards: builds that look configured and are silently cold."""
    registry = {"host": "nexus:9443", "repository": "test123", "tag": "1.0.0"}
    job = _job(
        _execution(0, "checkout", repository_url="https://bitbucket/areebasal/issuing.git"),
        _execution(1, "command"),
        _execution(2, "container_image", registry=registry),
    )
    spec = job["spec"]["template"]["spec"]

    volume = next(v for v in spec["volumes"] if v["name"] == "cache")
    assert volume["persistentVolumeClaim"]["claimName"] == CLAIM

    containers = spec["initContainers"] + spec["containers"]
    for container in containers:
        paths = [m["mountPath"] for m in container["volumeMounts"] if m["name"] == "cache"]
        assert MOUNT in paths, container["name"]
        # The path it used to live at, same claim, so a pipeline that still
        # hardcodes /cache/<slug> reads the same warm directories.
        assert cache_layout.LEGACY_MOUNT_PATH in paths, container["name"]


def test_the_volume_is_handed_to_the_build_user_by_group():
    """uid 65532 has a read-only root filesystem and no CAP_CHOWN, so it cannot
    take a volume that arrives owned by root. Without fsGroup the first mkdir
    fails and every build runs cold."""
    spec = _job(_execution())["spec"]["template"]["spec"]
    assert spec["securityContext"]["fsGroup"] == cache_layout.CACHE_FS_GROUP
    # OnRootMismatch keeps the chown to the first pod rather than re-walking a
    # full cache on every build.
    assert spec["securityContext"]["fsGroupChangePolicy"] == "OnRootMismatch"
    assert spec["initContainers"][0]["securityContext"]["runAsUser"] == 65532


def test_nothing_is_mounted_or_injected_when_caching_is_off(monkeypatch):
    """A cluster with no storage to give should not have builds demanding it."""
    monkeypatch.delenv("CI_CACHE_CLAIM_NAME", raising=False)
    monkeypatch.delenv("CI_CACHE_STORAGE_CLASS", raising=False)
    job = _job(_execution())
    spec = job["spec"]["template"]["spec"]

    assert not [v for v in spec["volumes"] if v["name"] == "cache"]
    env = _env_map(spec["initContainers"][0])
    assert env["KUBESIGHT_CACHE_DIR"] == ""
    for name in ("GRADLE_USER_HOME", "GRADLE_BUILD_CACHE_DIR", "DC_DATA_DIR",
                 "SEMGREP_CACHE_DIR", "BUILDKIT_CACHE_DIR"):
        assert name not in env


# ---------------------------------------------------------------------------
# The prep that makes the directories real
# ---------------------------------------------------------------------------

def test_a_stage_creates_the_directories_its_tools_will_not_create():
    """dependency-check refuses a missing --data directory and buildctl wants
    its export destination to exist, so the five recommended subdirectories are
    made up front rather than left to the pipeline to remember."""
    script = _job(_execution())["spec"]["template"]["spec"]["initContainers"][0]["command"][2]

    assert "mkdir -p" in script
    for name in ("gradle", "gradle-build-cache", "dependency-check-data",
                 "semgrep", "buildkit"):
        assert f'"$KUBESIGHT_CACHE_DIR/{name}"' in script


def test_an_unwritable_cache_warns_and_lets_the_build_run():
    """A cache that cannot be written is a slow build, not a failed one — and
    the log has to say so, or "why is this still slow" is unanswerable."""
    script = _job(_execution())["spec"]["template"]["spec"]["initContainers"][0]["command"][2]
    assert "is not writable" in script
    # The prep is outside the stage's own `set -e` subshell, so it cannot be
    # what fails the stage.
    assert script.index("mkdir -p \"$KUBESIGHT_CACHE_DIR") < script.index("\nset -e\n")


def test_gradles_build_cache_is_wired_up_by_an_init_script():
    """Gradle has no environment variable for its build cache directory, so
    GRADLE_BUILD_CACHE_DIR on its own does nothing. The init script in
    $GRADLE_USER_HOME/init.d is what makes --build-cache persistent."""
    script = _job(_execution())["spec"]["template"]["spec"]["initContainers"][0]["command"][2]
    assert "gradle/init.d/kubesight-build-cache.gradle" in script
    assert "settings.buildCache" in script
    # It configures WHERE, never whether: a build that does not ask for the
    # cache is unaffected.
    assert "--build-cache" not in script.split("KS_GRADLE_INIT")[0]


def test_the_gradle_init_script_can_be_switched_off(monkeypatch):
    """A project with its own init.d convention must be able to opt out without
    losing the rest of the cache."""
    monkeypatch.setenv("CI_CACHE_GRADLE_INIT", "off")
    script = _job(_execution())["spec"]["template"]["spec"]["initContainers"][0]["command"][2]
    assert "kubesight-build-cache.gradle" not in script
    assert '"$KUBESIGHT_CACHE_DIR/gradle"' in script


# ---------------------------------------------------------------------------
# BuildKit
# ---------------------------------------------------------------------------

_REGISTRY = {
    "host": "nexus:9443",
    "repository": "test123",
    "tag": "1.0.0",
    "dockerfile": "Dockerfile",
}


def _image_script(**registry):
    job = _job(_execution(0, "container_image", registry={**_REGISTRY, **registry}))
    return job["spec"]["template"]["spec"]["initContainers"][0]["command"][2]


def test_buildkit_layer_caching_is_off_unless_asked_for():
    script = _image_script()
    assert "--import-cache" not in script
    assert "--export-cache" not in script


def test_the_local_layer_cache_lives_on_the_volume(monkeypatch):
    monkeypatch.setenv("CI_BUILDKIT_LOCAL_CACHE", "1")
    script = _image_script()
    assert "--export-cache type=local,dest=$BUILDKIT_CACHE_DIR,mode=max" in script


def test_the_local_layer_cache_is_only_imported_once_it_exists(monkeypatch):
    """buildctl fails the whole build on a --import-cache src with no
    index.json, which is exactly an empty cache on the first run."""
    monkeypatch.setenv("CI_BUILDKIT_LOCAL_CACHE", "1")
    script = _image_script()
    assert 'if [ -s "$BUILDKIT_CACHE_DIR/index.json" ]' in script
    # Unquoted on the command line, so an empty value adds no argument at all.
    assert "$KS_BK_IMPORT --export-cache" in script


def test_the_local_layer_cache_is_skipped_when_there_is_no_volume(monkeypatch):
    monkeypatch.setenv("CI_BUILDKIT_LOCAL_CACHE", "1")
    monkeypatch.delenv("CI_CACHE_CLAIM_NAME", raising=False)
    monkeypatch.delenv("CI_CACHE_STORAGE_CLASS", raising=False)
    script = _image_script()
    assert "type=local" not in script


def test_the_registry_layer_cache_can_be_collected_under_one_repository(monkeypatch):
    """Where image repositories are governed, an extra :buildcache tag in them
    gets noticed. This puts every service's cache in one place instead."""
    monkeypatch.setenv("CI_BUILDKIT_REGISTRY_CACHE", "1")
    monkeypatch.setenv("CI_BUILDKIT_CACHE_REPO", "registry.areeba.com/cache")
    script = _image_script()
    ref = "registry.areeba.com/cache/test123:buildcache"
    assert f"--import-cache type=registry,ref={ref}" in script
    assert f"--export-cache type=registry,ref={ref},mode=max" in script


def test_without_that_repository_the_cache_sits_beside_its_image(monkeypatch):
    monkeypatch.setenv("CI_BUILDKIT_REGISTRY_CACHE", "1")
    script = _image_script()
    assert "--import-cache type=registry,ref=nexus:9443/test123:buildcache" in script


def test_both_layer_caches_can_be_on_at_once(monkeypatch):
    """The registry copy survives the builder moving node; the local copy is far
    cheaper to read when it lands back on its volume."""
    monkeypatch.setenv("CI_BUILDKIT_REGISTRY_CACHE", "1")
    monkeypatch.setenv("CI_BUILDKIT_LOCAL_CACHE", "1")
    script = _image_script()
    assert "type=registry" in script and "type=local" in script


# ---------------------------------------------------------------------------
# What must never be cached
# ---------------------------------------------------------------------------

def test_no_cache_path_points_into_the_workspace():
    """The workspace holds the checkout — .git with its credential, the build's
    own output, and $KUBESIGHT_ENV. It is an emptyDir for that reason, and
    nothing may quietly persist it."""
    env = _stage_env(_execution())
    for name, value in env.items():
        if name in ("KUBESIGHT_WORKSPACE", "KUBESIGHT_SOURCE", "KUBESIGHT_ENV"):
            continue
        assert "/workspace" not in value, f"{name}={value} would cache the workspace"


def test_secrets_are_never_written_to_the_cache_volume():
    """A secret reaches a stage as an environment variable from a per-build
    Secret and dies with the pod. Nothing mounts it onto persistent storage."""
    job = _job(
        _execution(
            0,
            "checkout",
            repository_url="https://bitbucket/areebasal/issuing.git",
            secrets={
                "KUBESIGHT_GIT_TOKEN": "t",
                "KUBESIGHT_GIT_CREDENTIAL_TYPE": "oauth",
                "KUBESIGHT_GIT_PRINCIPAL": "",
            },
        )
    )
    spec = job["spec"]["template"]["spec"]
    for volume in spec["volumes"]:
        if "secret" not in volume:
            continue
        for container in spec["initContainers"] + spec["containers"]:
            for mount in container["volumeMounts"]:
                if mount["name"] == volume["name"]:
                    assert not mount["mountPath"].startswith(MOUNT)
                    assert not mount["mountPath"].startswith(cache_layout.LEGACY_MOUNT_PATH)


def test_a_scan_gated_image_stage_still_defines_its_cache_variable(monkeypatch):
    """That stage emits the buildctl prelude itself, because the archive name,
    the scan and the push all need $KS_TAG resolved exactly once. The cache
    prelude has to come with it: the flags reference $KS_BK_IMPORT, and every
    stage script runs under `set -u`, so an undefined one fails the stage."""
    monkeypatch.setenv("CI_BUILDKIT_LOCAL_CACHE", "1")
    script = _job(
        _execution(
            0,
            "container_image",
            registry={**_REGISTRY, "tag": "V${APP_VERSION}-1", "tagIsTemplate": True},
            image_scan={"threshold": "critical", "onFail": "block"},
        )
    )["spec"]["template"]["spec"]["initContainers"][0]["command"][2]

    assert 'KS_BK_IMPORT=""' in script
    assert script.index('KS_BK_IMPORT=""') < script.index("$KS_BK_IMPORT --export-cache")
