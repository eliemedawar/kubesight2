"""Where a build's caches live, and what every tool has to be told to find them.

One module, because two very different callers need the *same* answer and must
never drift:

* ``runners/kubernetes.py`` mounts the volume and injects the variables into
  every stage container;
* ``cache.py`` runs maintenance Jobs against that same volume — it measures and
  empties the very directories the runner writes.

If those two disagreed about a single path, "clean" would empty a directory no
build ever used and the cache card would report zero on a volume that is full.

The shape on disk
-----------------

::

    /kubesight-cache/                 the volume
      <service-slug>/                 KUBESIGHT_CACHE_DIR — one subtree per service
        gradle/                       GRADLE_USER_HOME       (dependencies, wrapper dists)
          init.d/                     …and the init script that wires the build cache
        gradle-build-cache/           GRADLE_BUILD_CACHE_DIR (task outputs)
        dependency-check-data/        DC_DATA_DIR            (the NVD database)
        semgrep/                      SEMGREP_CACHE_DIR      (downloaded rulesets)
        buildkit/                     BUILDKIT_CACHE_DIR     (exported layer cache)
        maven/ npm/ yarn/ pnpm/ pip/ go/ cargo/ composer/ nuget/ xdg/

Every service gets its own subtree **in both storage modes**. A per-service
PersistentVolumeClaim is isolated already, but keeping the slug in the path
means one rule explains the layout, and ``clean`` can delete one service's cache
by path without having to know which mode produced it.

What is deliberately NOT here
-----------------------------

No secret, no credential, no log. Secrets reach a stage as environment variables
from a per-build Kubernetes Secret and are gone when the pod is; the checkout
credential and ``$KUBESIGHT_ENV`` live in the workspace ``emptyDir``, not on
this volume. Nothing in this module points a tool at anything under
``/workspace``, which is what keeps ``.git/config`` and the build's own output
off persistent storage. Build *outputs* are not cached either: Gradle's
task-output cache is content-addressed and validated by Gradle, which is a
different thing from keeping a ``build/`` directory around between builds.
"""

from __future__ import annotations

import hashlib
import re
from typing import Dict, List

# The volume, as every stage container sees it.
CACHE_MOUNT_PATH = "/kubesight-cache"

# The path this volume used to be mounted at, kept as a SECOND mount of the same
# claim. Pipelines written before the rename hardcode /cache/<slug>; they keep
# working, byte for byte the same directories, and can move to
# $KUBESIGHT_CACHE_DIR whenever their owner gets to it. Drop this mount once no
# pipeline mentions /cache — nothing in KubeSight itself emits it any more.
LEGACY_MOUNT_PATH = "/cache"

# uid/gid the stage containers run as. A PersistentVolume arrives owned by root,
# and stage containers have a read-only root filesystem and no CAP_CHOWN, so the
# volume is handed to them by GROUP (fsGroup on the pod) instead.
CACHE_FS_GROUP = 65532

# Created up front by every stage, because these are the ones a tool will NOT
# create for itself: dependency-check refuses a missing --data directory,
# buildctl wants its export destination to exist, and Gradle's init.d has to be
# there before Gradle reads it. The rest (maven/, npm/, go/…) are made by the
# tool that owns them, on first use.
PRECREATED_SUBDIRS = (
    "gradle",
    "gradle/init.d",
    "gradle-build-cache",
    "dependency-check-data",
    "semgrep",
    "buildkit",
)

# Gradle reads every *.gradle in $GRADLE_USER_HOME/init.d before the build, so
# this makes --build-cache use the persistent directory with no change to the
# pipeline. It configures WHERE the cache is, never whether it is on: a build
# that does not pass --build-cache (or set org.gradle.caching) is unaffected.
# Directories whose contents prove a TOOL has used this cache before. Every one
# of them is created by the tool that owns it, never by prep_script — a probe
# that included ``gradle/`` would report warm on the first build, because this
# script has just put an init.d inside it.
WARMTH_PROBE_DIRS = (
    "gradle/caches",
    "gradle/wrapper",
    "gradle-build-cache",
    "maven",
    "npm",
    "dependency-check-data",
    "semgrep",
)

GRADLE_INIT_SCRIPT_NAME = "kubesight-build-cache.gradle"
GRADLE_INIT_SCRIPT = """\
// Written by KubeSight before every stage. Points Gradle's local build cache at
// the persistent volume. It does NOT enable caching - that is still
// --build-cache (or org.gradle.caching=true), which keeps this inert for builds
// that have not asked for it.
def kubesightCacheDir = System.getenv('GRADLE_BUILD_CACHE_DIR')
if (kubesightCacheDir) {
    gradle.settingsEvaluated { settings ->
        settings.buildCache {
            local {
                directory = new File(kubesightCacheDir)
                removeUnusedEntriesAfterDays = 30
            }
        }
    }
}
"""


def slug_dir(service_slug: str) -> str:
    """The one directory name a service is allowed to own.

    Lowercased, non-alphanumerics folded to a dash, trimmed to 63 characters —
    so a service called ``Payments (UAT)`` cannot escape its subtree or produce
    a path a shell would have to quote.

    A slug that sanitises to nothing — blank, or punctuation only — falls back
    to a hash of the raw value rather than to a shared constant. Two unusable
    slugs must still get two directories: sharing one would have them fighting
    over the same Gradle lock files, which fails builds in ways that look
    nothing like the cause. Archived and disabled services come through here
    too, and get a path without complaint — the caller decides whether to
    build, this only decides where.
    """
    raw = str(service_slug or "")
    safe = re.sub(r"[^a-z0-9-]+", "-", raw.lower()).strip("-")[:63].rstrip("-")
    if safe:
        return safe
    digest = hashlib.sha1(raw.encode("utf-8", "replace")).hexdigest()[:12]
    return f"service-{digest}"


def service_cache_dir(service_slug: str, mount_path: str = CACHE_MOUNT_PATH) -> str:
    """``$KUBESIGHT_CACHE_DIR`` — this service's subtree, in either mode."""
    return f"{mount_path.rstrip('/')}/{slug_dir(service_slug)}"


def precreated_dirs(base: str) -> List[str]:
    return [f"{base}/{name}" for name in PRECREATED_SUBDIRS]


def tool_env(base: str) -> Dict[str, str]:
    """Point every build tool KubeSight might meet at this service's subtree.

    Injected on every stage rather than left to each pipeline: a cache nobody
    remembered to wire up is a cache that does nothing, and the correct variable
    name differs per tool. A stage's own Environment still wins — the runner
    merges ``execution.env`` after this.

    None of these are mount points; they are plain paths inside the one volume,
    which is what lets a single hand-made PersistentVolume hold every tool's
    cache at once. ``{}`` when caching is off, so a pipeline can reference
    ``$KUBESIGHT_CACHE_DIR`` unconditionally and simply get a cold build.
    """
    if not base:
        return {}
    return {
        # --- the path itself, under both names ------------------------------
        "KUBESIGHT_CACHE_DIR": base,
        # The original name, kept because pipelines and the agent runner use
        # it. Same value; neither is more real than the other.
        "KUBESIGHT_CACHE": base,
        # --- JVM -------------------------------------------------------------
        # -Dmaven.repo.local as a JVM property works on every Maven version;
        # MAVEN_ARGS would only be read by 3.9+.
        "MAVEN_OPTS": f"-Dmaven.repo.local={base}/maven",
        # Dependencies, wrapper distributions and Gradle's own caches.
        "GRADLE_USER_HOME": f"{base}/gradle",
        # Task outputs. Gradle has no environment variable for this, so the init
        # script above reads THIS variable and configures the build cache from
        # it — which is the only reason both exist.
        "GRADLE_BUILD_CACHE_DIR": f"{base}/gradle-build-cache",
        # --- scanners ---------------------------------------------------------
        # OWASP Dependency-Check's local NVD database. Building it takes many
        # minutes and an API key; losing it every build is why a scan stage is
        # slow, and why an unreachable NVD fails the build outright instead of
        # scanning against the data it already had.
        "DC_DATA_DIR": f"{base}/dependency-check-data",
        # Semgrep's downloaded rulesets and its version check.
        "SEMGREP_CACHE_DIR": f"{base}/semgrep",
        "SEMGREP_VERSION_CACHE_PATH": f"{base}/semgrep/version",
        # --- container images ---------------------------------------------------
        # Where buildctl exports its layer cache when CI_BUILDKIT_LOCAL_CACHE is
        # on. buildkitd's own cache is an emptyDir that dies with its pod.
        "BUILDKIT_CACHE_DIR": f"{base}/buildkit",
        # --- everything else ----------------------------------------------------
        "npm_config_cache": f"{base}/npm",
        "YARN_CACHE_FOLDER": f"{base}/yarn",
        # pnpm reads npm_config_* too; this is its content-addressable store.
        "npm_config_store_dir": f"{base}/pnpm",
        "PIP_CACHE_DIR": f"{base}/pip",
        "GOMODCACHE": f"{base}/go/mod",
        "GOCACHE": f"{base}/go/build",
        "CARGO_HOME": f"{base}/cargo",
        "COMPOSER_CACHE_DIR": f"{base}/composer",
        "NUGET_PACKAGES": f"{base}/nuget",
        # Catch-all for everything that respects the XDG base directories (yarn
        # berry, pip's http cache, Playwright, sccache...). HOME is /tmp and
        # dies with the pod, so without this they each start cold.
        "XDG_CACHE_HOME": f"{base}/xdg",
    }


# The variables whose whole job is to name a directory inside the cache, and the
# subdirectory each one must name. MAVEN_OPTS is left out on purpose: it is a
# string of JVM flags, not a path, so it cannot be compared this way.
PATH_VARS = (
    ("GRADLE_USER_HOME", "gradle"),
    ("GRADLE_BUILD_CACHE_DIR", "gradle-build-cache"),
    ("DC_DATA_DIR", "dependency-check-data"),
    ("SEMGREP_CACHE_DIR", "semgrep"),
)


def _mismatch_checks() -> List[str]:
    """Shell that says when a tool has been pointed away from the cache.

    This is the failure that looks like nothing at all: the volume is mounted,
    writable and full of other tools' files, and one tool still starts cold
    every build because something overrode its variable.

    Two ways that happens, and both are easy to do by accident:

    * a stage's own Environment wins over the injected value, by design — so
      setting ``GRADLE_USER_HOME`` there REPLACES the correct path;
    * Kubernetes does not shell-expand environment values. It expands
      ``$(VAR)``, not ``$VAR``, so an Environment entry of
      ``$KUBESIGHT_CACHE_DIR/gradle`` reaches the container as that literal
      string. Gradle then treats it as a relative path, creates it under the
      workspace, and loses it with the pod. The line below prints the value, so
      a stray ``$`` is visible rather than inferred.

    ``export`` inside the stage's own commands is fine and is the documented way
    to do it — a shell runs those, and expands them.
    """
    lines: List[str] = []
    for name, subdir in PATH_VARS:
        lines.extend(
            [
                f'    if [ "${{{name}:-}}" != "$KUBESIGHT_CACHE_DIR/{subdir}" ]; then',
                f'      echo "[kubesight] {name}=${{{name}:-(unset)}}" >&2',
                f'      echo "[kubesight]   ^ not $KUBESIGHT_CACHE_DIR/{subdir} - that tool'
                ' starts cold every build. A stage Environment value overrides the'
                ' injected one, and Kubernetes does not expand \\$VAR in it." >&2',
                "    fi",
            ]
        )
    return lines


def prep_script(*, gradle_init: bool = True) -> str:
    """Shell that makes this service's subtree exist. Run by every stage.

    Runs OUTSIDE the stage's own ``set -e`` subshell and never exits non-zero: a
    cache that cannot be written is a slow build, not a failed one. It says so
    on stderr instead, because "why is this still slow?" is otherwise
    unanswerable from the log.

    Idempotent by construction — ``mkdir -p`` and one overwritten file — so it
    costs a few milliseconds on the stages that do not need it, and repairs the
    layout on the one that does. Reads ``$KUBESIGHT_CACHE_DIR`` rather than
    taking the path as an argument, so the same text is correct for every
    service and does nothing at all when caching is off.
    """
    dirs = " ".join(f'"$KUBESIGHT_CACHE_DIR/{name}"' for name in PRECREATED_SUBDIRS)
    init_path = f'"$KUBESIGHT_CACHE_DIR/gradle/init.d/{GRADLE_INIT_SCRIPT_NAME}"'
    lines = [
        'if [ -n "${KUBESIGHT_CACHE_DIR:-}" ]; then',
        f"  if mkdir -p {dirs} 2>/dev/null; then",
    ]
    if gradle_init:
        lines.append(f"    cat > {init_path} <<'KS_GRADLE_INIT' 2>/dev/null || true")
        lines.extend(GRADLE_INIT_SCRIPT.rstrip("\n").split("\n"))
        lines.append("KS_GRADLE_INIT")
    else:
        lines.append("    :")
    lines.extend(
        [
            # Say what the cache is, on every stage, in one line.
            #
            # Without this a cold build and a warm one produce identical logs,
            # and "why is Gradle still downloading its distribution on the
            # second run?" has no answer visible anywhere — the three causes
            # (switched off, unwritable, genuinely first run) look the same.
            # On every stage rather than only the first because the drawer shows
            # ONE stage's log at a time, and the stage somebody opens to ask the
            # question is the slow one, not the checkout.
            # PER-TOOL, not one verdict for the volume.
            #
            # A single warm/cold line is worse than useless here: the scan
            # stages fill dependency-check-data/ and semgrep/ earlier in the
            # SAME build, so the volume reads "warm" while Gradle's own
            # directories are empty and Gradle re-downloads its distribution
            # every time. That is exactly the bug this has to be able to show.
            #
            # Probing directories the tools own, never ones prep_script writes
            # into: `gradle/` holds the init.d created moments ago, so it would
            # report warm on a first build — the build whose answer matters.
            "    KS_WARM=",
            "    KS_COLD=",
            "    for KS_DIR in " + " ".join(WARMTH_PROBE_DIRS) + "; do",
            '      if [ -n "$(ls -A "$KUBESIGHT_CACHE_DIR/$KS_DIR" 2>/dev/null)" ]; then',
            '        KS_WARM="$KS_WARM $KS_DIR"',
            "      else",
            '        KS_COLD="$KS_COLD $KS_DIR"',
            "      fi",
            "    done",
            '    echo "[kubesight] Cache: $KUBESIGHT_CACHE_DIR"',
            '    echo "[kubesight]   warm:${KS_WARM:- (nothing yet)}"',
            '    echo "[kubesight]   cold:${KS_COLD:- (none)}"',
        ]
        + _mismatch_checks()
        + [
            "  else",
            '    echo "[kubesight] Cache directory $KUBESIGHT_CACHE_DIR is not writable;'
            ' this build runs cold." >&2',
            "  fi",
            "else",
            # The single most common reason a build is still slow, and until now
            # the least visible one: nothing is wrong, nobody turned it on.
            '  echo "[kubesight] Cache: off. Every build starts cold."',
            '  echo "[kubesight] Turn it on under Runners -> Build cache, or see CI-CACHE.md."',
            "fi",
        ]
    )
    return "\n".join(lines) + "\n"
