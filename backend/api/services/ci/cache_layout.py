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
        node-modules/                 <lockfile-key>.tar     (a whole node_modules per lockfile)
        maven/ npm/ yarn/ pnpm/ pip/ go/ cargo/ composer/ nuget/ xdg/
      _shared/                        KUBESIGHT_SHARED_CACHE_DIR - one subtree for everybody
        dependency-check-data/        the NVD database is the same for every service
        npm/ pnpm/ pip/ go/mod/ semgrep/

Every service gets its own subtree **in both storage modes**. A per-service
PersistentVolumeClaim is isolated already, but keeping the slug in the path
means one rule explains the layout, and ``clean`` can delete one service's cache
by path without having to know which mode produced it.

The shared subtree
------------------

Some caches hold nothing specific to a service: the NVD database is the same
for everybody, and npm, pnpm, pip and Go modules are content-addressed stores
built for many projects to share. One copy per service costs a cold build per
service and N copies of the same bytes, so on the one hand-made volume those
tools point at ``_shared/`` instead (``SHAREABLE_TOOLS``, operator-selectable).
``_shared`` can never collide with a service: ``slug_dir`` folds ``_`` to ``-``
and strips leading dashes.

NOT shared, on purpose: Gradle's user home (its cross-process locking pings the
lock owner over localhost, which a build in another pod cannot hear, so builds
time out waiting for a lock nobody will release), Maven's local repository (not
safe for concurrent writers, and leaks SNAPSHOTs between services), yarn
classic's cache (two installs unpacking the same package at once corrupt it -
"Extracting tar content ... the file appears to be corrupt" - and the broken
entry then fails every later build that needs it), BuildKit's
export (``type=local`` rewrites one ``index.json``, so services would evict each
other) and Gradle's build cache (keyed by task inputs, so services almost never
hit each other's entries anyway).

A per-service claim (storage-class mode) has no volume in common to share, so
there everything stays per service.

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
    "node-modules",
)

# --- cache slots: two builds of ONE service at the same time -------------------
#
# A service's subtree is safe for one build at a time, and for the parallel
# stages of that build: they share one pod, so one kernel and one network
# namespace, and Gradle's lock protocol (a ping to the lock owner over
# localhost) and Trivy's file locks work between them. Two BUILDS of the same
# service are two pods, usually on two nodes, and those tools cannot coordinate
# across that - Gradle times out on a lock its owner never hears about, yarn
# classic corrupts its cache, two BuildKit exports overwrite one index.json.
#
# So each running build leases a SLOT: slot 0 is the service's own directories,
# exactly as before, and a second build at the same time gets
# ``<service>/slots/1/`` for these tools - cold the first time, warm after.
# The lease is a directory under ``<service>/.leases/`` (mkdir is atomic on
# NFS), owned by the build id, renewed while the build runs and released by the
# collector at its end. A build that died without releasing goes stale.
# Everything not listed here is either safe with concurrent writers (npm,
# pnpm, pip and Go are content-addressed; node_modules archives are written
# aside and renamed) or locked separately (Dependency-Check).
SLOT_SUBDIRS = ("gradle", "gradle-build-cache", "maven", "buildkit", "trivy", "yarn")
# The variables pointing into a slot directory, re-pointed by the stage prep
# when this build holds a slot other than 0. MAVEN_OPTS is handled apart: it is
# a string of flags with the path inside it.
SLOT_VARS = (
    ("GRADLE_USER_HOME", "gradle"),
    ("GRADLE_BUILD_CACHE_DIR", "gradle-build-cache"),
    ("BUILDKIT_CACHE_DIR", "buildkit"),
    ("TRIVY_CACHE_DIR", "trivy"),
    ("YARN_CACHE_FOLDER", "yarn"),
)
SLOTS_DIR_NAME = "slots"
LEASES_DIR_NAME = ".leases"
DEFAULT_SLOTS = 4
# A lease nobody renewed for this long belongs to a build that is gone
# (cancelled, its pod deleted). Renewed every minute while any stage runs; the
# margin covers the gap between two stages, which includes an image pull.
DEFAULT_LEASE_STALE_SECONDS = 900

# Maven's local repository has no cross-process locking unless asked for. Two
# Mavens writing it at once - parallel stages of one build - can leave a
# truncated jar or pom that then fails every later build until the cache is
# emptied. These turn on Maven Resolver's file locks (Maven 3.9 and newer;
# older versions ignore the properties). The locks live in the repository's own
# .locks/ directory.
MAVEN_LOCK_OPTS = (
    "-Daether.syncContext.named.factory=file-lock "
    "-Daether.syncContext.named.nameMapper=file-gav"
)


def _top(name: str) -> str:
    return name.split("/", 1)[0]


GRADLE_INIT_SCRIPT_NAME = "kubesight-build-cache.gradle"
# Only `directory`: it exists on every Gradle from 3.5 to 9. Retention is left
# at Gradle's default (7 days) on purpose - `removeUnusedEntriesAfterDays` was
# removed in Gradle 9.0, and setting it here failed every Gradle 9 build at
# settings time, whether or not it used --build-cache.
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
            }
        }
    }
}
"""


# The one subtree every service uses. A leading underscore is something
# slug_dir can never produce, so no service can own - or clean - it by accident.
SHARED_DIR_NAME = "_shared"

# Tools whose cache may live in the shared subtree: key, label, subdirectory,
# and every variable that points into it. Only tools that are safe with
# concurrent writers from different pods, and whose contents are not specific
# to one service, belong here - the module docstring says why Gradle, Maven and
# BuildKit do not.
SHAREABLE_TOOLS = (
    ("dependency-check", "Dependency-Check (NVD)", "dependency-check-data", ("DC_DATA_DIR",)),
    ("semgrep", "Semgrep", "semgrep", ("SEMGREP_CACHE_DIR", "SEMGREP_VERSION_CACHE_PATH")),
    ("npm", "npm", "npm", ("npm_config_cache",)),
    ("pnpm", "pnpm", "pnpm", ("npm_config_store_dir",)),
    ("pip", "pip", "pip", ("PIP_CACHE_DIR",)),
    ("go", "Go modules", "go/mod", ("GOMODCACHE",)),
)
SHAREABLE_KEYS = tuple(key for key, _, _, _ in SHAREABLE_TOOLS)
# Shared unless an operator says otherwise: each one is a cold build per
# service and a duplicate copy of the same bytes when it is not.
DEFAULT_SHARED = SHAREABLE_KEYS
_SHAREABLE_SUBDIR = {key: subdir for key, _, subdir, _ in SHAREABLE_TOOLS}


def parse_shared(value) -> tuple:
    """A shared-tools setting - a list, or a comma-separated ``CI_CACHE_SHARED``
    string - as known keys in canonical order.

    ``None`` means "never configured" and gives the default; an empty list,
    ``"none"`` or ``"off"`` shares nothing; ``"all"`` shares every shareable
    tool. Unknown names are dropped rather than refused: a stale setting naming
    a tool this version no longer shares must not stop builds.
    """
    if value is None:
        return DEFAULT_SHARED
    if isinstance(value, str):
        text = value.strip().lower()
        if text in ("", "none", "off", "0", "false"):
            return ()
        if text in ("all", "default", "on", "1", "true"):
            return SHAREABLE_KEYS
        wanted = {part.strip() for part in text.split(",")}
    else:
        wanted = {str(part).strip().lower() for part in value}
    return tuple(key for key in SHAREABLE_KEYS if key in wanted)


def shared_subdirs(shared) -> set:
    """The per-tool subdirectories that live under ``_shared/`` for this set."""
    return {_SHAREABLE_SUBDIR[key] for key in shared if key in _SHAREABLE_SUBDIR}


def shared_cache_dir(mount_path: str = CACHE_MOUNT_PATH) -> str:
    """``$KUBESIGHT_SHARED_CACHE_DIR`` - the subtree every service shares."""
    return f"{mount_path.rstrip('/')}/{SHARED_DIR_NAME}"


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


def tool_env(base: str, shared_base: str = "", shared=()) -> Dict[str, str]:
    """Point every build tool KubeSight might meet at this service's subtree.

    Injected on every stage rather than left to each pipeline: a cache nobody
    remembered to wire up is a cache that does nothing, and the correct variable
    name differs per tool. A stage's own Environment still wins — the runner
    merges ``execution.env`` after this.

    None of these are mount points; they are plain paths inside the one volume,
    which is what lets a single hand-made PersistentVolume hold every tool's
    cache at once. ``{}`` when caching is off, so a pipeline can reference
    ``$KUBESIGHT_CACHE_DIR`` unconditionally and simply get a cold build.

    With ``shared_base`` and a set of ``shared`` tool keys, those tools'
    variables point into the shared subtree instead - same subdirectory name,
    different parent - and ``KUBESIGHT_SHARED_CACHE_DIR`` names it.
    """
    if not base:
        return {}
    env = _service_tool_env(base)
    if shared_base and shared:
        env["KUBESIGHT_SHARED_CACHE_DIR"] = shared_base
        for key, _, _, names in SHAREABLE_TOOLS:
            if key not in shared:
                continue
            for name in names:
                env[name] = shared_base + env[name][len(base):]
    return env


def _service_tool_env(base: str) -> Dict[str, str]:
    return {
        # --- the path itself, under both names ------------------------------
        "KUBESIGHT_CACHE_DIR": base,
        # The original name, kept because pipelines and the agent runner use
        # it. Same value; neither is more real than the other.
        "KUBESIGHT_CACHE": base,
        # --- JVM -------------------------------------------------------------
        # -Dmaven.repo.local as a JVM property works on every Maven version;
        # MAVEN_ARGS would only be read by 3.9+.
        "MAVEN_OPTS": f"-Dmaven.repo.local={base}/maven {MAVEN_LOCK_OPTS}",
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


def _mismatch_checks(shared=()) -> List[str]:
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
    shared_dirs = shared_subdirs(shared)
    for name, subdir in PATH_VARS:
        parent = _parent_var(subdir, shared_dirs)
        lines.extend(
            [
                f'    if [ "${{{name}:-}}" != "{parent}/{subdir}" ]; then',
                f'      echo "[kubesight] {name}=${{{name}:-(unset)}}" >&2',
                f'      echo "[kubesight]   ^ not {parent}/{subdir} - that tool'
                ' starts cold every build. A stage Environment value overrides the'
                ' injected one, and Kubernetes does not expand \\$VAR in it." >&2',
                "    fi",
            ]
        )
    return lines


def _parent_var(subdir: str, shared_dirs) -> str:
    """Which directory variable a cache subdirectory lives under, in the shell."""
    if subdir in shared_dirs:
        return "$KUBESIGHT_SHARED_CACHE_DIR"
    if _top(subdir) in SLOT_SUBDIRS:
        return "$KUBESIGHT_CACHE_SLOT_DIR"
    return "$KUBESIGHT_CACHE_DIR"


def lease_script(*, slots: int = DEFAULT_SLOTS, stale_seconds: int = DEFAULT_LEASE_STALE_SECONDS) -> List[str]:
    """Shell that picks this build's cache slot and exports where it is.

    Sets ``KUBESIGHT_CACHE_SLOT`` (``0``, ``1``... or ``private``) and
    ``KUBESIGHT_CACHE_SLOT_DIR``, and re-points the slot tools' variables when
    the slot is not 0. Every stage runs it; the first takes a lease and every
    later stage of the same build finds it again by the build id, so one build
    keeps one slot from its first stage to its last.

    Never fails a stage. Without a build id, or on a volume it cannot write,
    the slot is 0 - what every build used before slots existed.

    Staleness is measured on the cache volume's clock: a file created there now
    against the lease's last renewal, both stamped by the NFS server. A node
    whose clock is ten minutes off would otherwise break every lease, or none.
    """
    slots = max(1, int(slots or DEFAULT_SLOTS))
    stale_seconds = max(60, int(stale_seconds or DEFAULT_LEASE_STALE_SECONDS))
    stale_minutes = max(1, stale_seconds // 60)
    repoint = []
    for name, subdir in SLOT_VARS:
        repoint.append(
            f'    if [ "${{{name}:-}}" = "$KUBESIGHT_CACHE_DIR/{subdir}" ]; then'
            f' {name}="$KUBESIGHT_CACHE_SLOT_DIR/{subdir}"; export {name}; fi'
        )
    return [
        "  KUBESIGHT_CACHE_SLOT=0",
        '  KUBESIGHT_CACHE_SLOT_DIR="$KUBESIGHT_CACHE_DIR"',
        f'  KS_LEASES="$KUBESIGHT_CACHE_DIR/{LEASES_DIR_NAME}"',
        "  KS_LEASE=",
        '  KS_ME="${KUBESIGHT_BUILD_ID:-}"',
        '  if [ -n "$KS_ME" ] && mkdir -p "$KS_LEASES" 2>/dev/null; then',
        # The owner of a lease, allowing for the instant between another
        # stage's mkdir and its write of the owner file - a sibling of the
        # SAME build, starting in parallel, must not mistake it for a stranger.
        "    ks_lease_owner() {",
        "      KS_LO=",
        "      for KS_TRY in 1 2 3; do",
        '        KS_LO=$(cat "$1/owner" 2>/dev/null || true)',
        '        if [ -n "$KS_LO" ] || [ ! -d "$1" ]; then return 0; fi',
        "        sleep 1 2>/dev/null || return 0",
        "      done",
        "    }",
        "    ks_lease_stale() {",
        '      KS_REF="$KS_LEASES/.now.${HOSTNAME:-pod}.$$"',
        "      KS_NOW=",
        '      if : > "$KS_REF" 2>/dev/null; then',
        '        KS_NOW=$(stat -c %Y "$KS_REF" 2>/dev/null || true)',
        '        rm -f "$KS_REF" 2>/dev/null',
        "      fi",
        '      KS_THEN=$(stat -c %Y "$1" 2>/dev/null || true)',
        '      if [ -n "$KS_NOW" ] && [ -n "$KS_THEN" ]; then',
        f'        [ $((KS_NOW - KS_THEN)) -gt {stale_seconds} ]',
        "        return",
        "      fi",
        f'      [ -n "$(find "$1" -maxdepth 0 -mmin +{stale_minutes} 2>/dev/null)" ]',
        "    }",
        # An earlier stage of this build already holds one: keep it.
        "    KS_N=0",
        f'    while [ "$KS_N" -lt {slots} ]; do',
        '      if [ "$(cat "$KS_LEASES/$KS_N/owner" 2>/dev/null || true)" = "$KS_ME" ]; then',
        '        KS_LEASE="$KS_LEASES/$KS_N"',
        "        break",
        "      fi",
        "      KS_N=$((KS_N + 1))",
        "    done",
        # Otherwise the lowest free one, so a service that only ever runs one
        # build at a time always lands on slot 0 - its existing warm cache.
        '    if [ -z "$KS_LEASE" ]; then',
        "      KS_N=0",
        "      KS_BROKEN=0",
        f'      while [ "$KS_N" -lt {slots} ]; do',
        '        KS_L="$KS_LEASES/$KS_N"',
        '        if mkdir "$KS_L" 2>/dev/null; then',
        '          echo "$KS_ME" > "$KS_L/owner" 2>/dev/null',
        '          KS_LEASE="$KS_L"',
        "          break",
        "        fi",
        '        ks_lease_owner "$KS_L"',
        '        if [ "$KS_LO" = "$KS_ME" ]; then',
        '          KS_LEASE="$KS_L"',
        "          break",
        "        fi",
        f'        if [ "$KS_BROKEN" -lt {slots} ] && ks_lease_stale "$KS_L"; then',
        "          KS_BROKEN=$((KS_BROKEN + 1))",
        '          echo "[kubesight] Cache slot $KS_N was held by build ${KS_LO:-?}, which stopped renewing it; taking it over."',
        # Renamed away before it is deleted: only one of two stages breaking
        # the same stale lease can win the rename, so they cannot both
        # delete a lease the other has just re-made.
        '          KS_GONE="$KS_LEASES/.stale-$KS_N-${HOSTNAME:-pod}-$$"',
        '          if mv "$KS_L" "$KS_GONE" 2>/dev/null; then rm -rf "$KS_GONE" 2>/dev/null; fi',
        "          continue",
        "        fi",
        "        KS_N=$((KS_N + 1))",
        "      done",
        "    fi",
        '    if [ -n "$KS_LEASE" ]; then',
        '      KUBESIGHT_CACHE_SLOT="${KS_LEASE##*/}"',
        '      touch "$KS_LEASE" 2>/dev/null || true',
        '      if [ "$KUBESIGHT_CACHE_SLOT" != 0 ]; then',
        f'        KUBESIGHT_CACHE_SLOT_DIR="$KUBESIGHT_CACHE_DIR/{SLOTS_DIR_NAME}/$KUBESIGHT_CACHE_SLOT"',
        "      fi",
        # Renewed while this stage runs, and by a finished parallel stage
        # while it waits for the pod to end. It stops on its own once the
        # shell that started it is gone or the lease is no longer this
        # build's - released by the collector, or broken by another build.
        '      KS_HB="${KUBESIGHT_CACHE_LEASE_HEARTBEAT:-60}"',
        '      if [ "$KS_HB" -gt 0 ] 2>/dev/null && command -v sleep >/dev/null 2>&1; then',
        "        KS_HB_PARENT=$$",
        '        ( while sleep "$KS_HB"; do',
        '            kill -0 "$KS_HB_PARENT" 2>/dev/null || exit 0',
        '            [ "$(cat "$KS_LEASE/owner" 2>/dev/null || true)" = "$KS_ME" ] || exit 0',
        '            touch "$KS_LEASE" 2>/dev/null || exit 0',
        "          done ) </dev/null >/dev/null 2>&1 &",
        "      fi",
        "    else",
        "      KUBESIGHT_CACHE_SLOT=private",
        '      KUBESIGHT_CACHE_SLOT_DIR="${KUBESIGHT_WORKSPACE:-/workspace}/.kubesight/cache-private"',
        f'      echo "[kubesight] All {slots} cache slots of this service are held by other builds running now;'
        ' Gradle, Maven, yarn, BuildKit and Trivy start cold in this build. CI_CACHE_SLOTS sets how many there are."',
        "    fi",
        "  fi",
        "  export KUBESIGHT_CACHE_SLOT KUBESIGHT_CACHE_SLOT_DIR",
        '  if [ "$KUBESIGHT_CACHE_SLOT_DIR" != "$KUBESIGHT_CACHE_DIR" ]; then',
        *repoint,
        # Only the injected repository path is moved; a stage that set its
        # own MAVEN_OPTS without it keeps what it said.
        '    case "${MAVEN_OPTS:-}" in',
        '      *"-Dmaven.repo.local=$KUBESIGHT_CACHE_DIR/maven"*)',
        '        MAVEN_OPTS=$(printf \'%s\' "$MAVEN_OPTS" | sed "s|-Dmaven.repo.local=$KUBESIGHT_CACHE_DIR/maven|-Dmaven.repo.local=$KUBESIGHT_CACHE_SLOT_DIR/maven|")',
        "        export MAVEN_OPTS ;;",
        "    esac",
        '    if [ "$KUBESIGHT_CACHE_SLOT" != private ]; then',
        '      echo "[kubesight] Another build of this service is running, so this one uses cache slot'
        ' $KUBESIGHT_CACHE_SLOT for Gradle, Maven, yarn, BuildKit and Trivy: $KUBESIGHT_CACHE_SLOT_DIR"',
        "    fi",
        "  fi",
    ]


# Every variable tool_env points into the cache, as a path. MAVEN_OPTS is a
# string of flags and is rewritten apart; the two KUBESIGHT_CACHE names are
# the directory itself.
_COLD_FALLBACK_VARS = tuple(
    name
    for name in _service_tool_env("/x")
    if name not in ("KUBESIGHT_CACHE_DIR", "KUBESIGHT_CACHE", "MAVEN_OPTS")
) + ("TRIVY_CACHE_DIR",)


def _cold_fallback(dirs: str) -> List[str]:
    """Shell that moves every tool off a cache it cannot write.

    "Runs cold" has to be true. Leaving the variables pointing at the
    unwritable volume does not give a cold build, it gives a failed one:
    Gradle cannot unpack its native library into GRADLE_USER_HOME
    ("Failed to load native library 'libnative-platform.so'"), Maven cannot
    write its repository, Dependency-Check cannot create its database. So each
    of them is pointed at the same layout under the workspace instead - empty,
    gone with the pod, but writable. node_modules is not saved there: an
    archive nobody will ever restore is only disk spent.
    """
    names = " ".join(_COLD_FALLBACK_VARS)
    return [
        '    KS_COLD_DIR="${KUBESIGHT_WORKSPACE:-/tmp}/.kubesight/cache-cold"',
        '    KS_OLD="$KUBESIGHT_CACHE_DIR"',
        '    KS_OLD_SHARED="${KUBESIGHT_SHARED_CACHE_DIR:-}"',
        f"    for KS_VAR in {names}; do",
        '      eval "KS_VAL=\\${$KS_VAR:-}"',
        '      KS_NEW="$KS_VAL"',
        '      case "$KS_VAL" in',
        '        "$KS_OLD"/*) KS_NEW="$KS_COLD_DIR${KS_VAL#"$KS_OLD"}" ;;',
        "      esac",
        '      if [ -n "$KS_OLD_SHARED" ]; then',
        '        case "$KS_VAL" in',
        '          "$KS_OLD_SHARED"/*) KS_NEW="$KS_COLD_DIR/_shared${KS_VAL#"$KS_OLD_SHARED"}" ;;',
        "        esac",
        "      fi",
        '      if [ "$KS_NEW" != "$KS_VAL" ]; then eval "$KS_VAR=\\$KS_NEW; export $KS_VAR"; fi',
        "    done",
        '    case "${MAVEN_OPTS:-}" in',
        '      *"-Dmaven.repo.local=$KS_OLD"*)',
        '        MAVEN_OPTS=$(printf \'%s\' "$MAVEN_OPTS" | sed "s|-Dmaven.repo.local=$KS_OLD|-Dmaven.repo.local=$KS_COLD_DIR|")',
        "        export MAVEN_OPTS ;;",
        "    esac",
        '    KUBESIGHT_CACHE_DIR="$KS_COLD_DIR"',
        '    KUBESIGHT_CACHE="$KS_COLD_DIR"',
        '    KUBESIGHT_CACHE_SLOT=cold',
        '    KUBESIGHT_CACHE_SLOT_DIR="$KS_COLD_DIR"',
        '    if [ -n "$KS_OLD_SHARED" ]; then KUBESIGHT_SHARED_CACHE_DIR="$KS_COLD_DIR/_shared"; fi',
        "    KUBESIGHT_NODE_MODULES_CACHE=0",
        "    export KUBESIGHT_CACHE_DIR KUBESIGHT_CACHE KUBESIGHT_CACHE_SLOT KUBESIGHT_CACHE_SLOT_DIR"
        " KUBESIGHT_SHARED_CACHE_DIR KUBESIGHT_NODE_MODULES_CACHE",
        f"    mkdir -p {dirs} 2>/dev/null || true",
        '    echo "[kubesight] Every tool uses $KS_COLD_DIR for this build instead.'
        " Fix the volume's ownership to make the cache work: see CI-CACHE.md.\" >&2",
    ]


def prep_script(
    *,
    gradle_init: bool = True,
    shared=(),
    slots: int = DEFAULT_SLOTS,
    stale_seconds: int = DEFAULT_LEASE_STALE_SECONDS,
) -> str:
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

    ``shared`` is the set of tool keys whose directories live under
    ``$KUBESIGHT_SHARED_CACHE_DIR``: those are created, probed and checked
    there instead. It is known at manifest time, so it shapes the text.
    """
    shared_dirs = shared_subdirs(shared)
    paths = [f'"{_parent_var(name, shared_dirs)}/{name}"' for name in PRECREATED_SUBDIRS]
    dirs = " ".join(paths)
    shared_probes = [name for name in WARMTH_PROBE_DIRS if name in shared_dirs]
    slot_probes = [
        name for name in WARMTH_PROBE_DIRS
        if name not in shared_dirs and _top(name) in SLOT_SUBDIRS
    ]
    own_probes = [
        name for name in WARMTH_PROBE_DIRS
        if name not in shared_dirs and name not in slot_probes
    ]
    init_path = f'"$KUBESIGHT_CACHE_SLOT_DIR/gradle/init.d/{GRADLE_INIT_SCRIPT_NAME}"'
    lines = (
        ['if [ -n "${KUBESIGHT_CACHE_DIR:-}" ]; then']
        + lease_script(slots=slots, stale_seconds=stale_seconds)
        # The error is kept, not thrown away: "not writable" alone cannot tell
        # a root-owned directory from a read-only export from an NFS server
        # squashing every write to nobody, and each has a different fix.
        + [f"  if KS_MKDIR_ERR=$(mkdir -p {dirs} 2>&1); then"]
    )
    if gradle_init:
        # Written aside and renamed into place, and only when it changed. Every
        # stage of every build of this service writes this one file, and a
        # plain `cat >` truncates it first: a Gradle starting in a parallel
        # stage at that moment read half a script and failed to compile it.
        # The temporary name ends in .tmp, never .gradle, because Gradle runs
        # every *.gradle in init.d.
        lines.extend(
            [
                f"    KS_GI={init_path}",
                '    KS_GI_TMP=$(mktemp "$KS_GI.XXXXXX.tmp" 2>/dev/null || echo "$KS_GI.$$.tmp")',
                "    if cat > \"$KS_GI_TMP\" 2>/dev/null <<'KS_GRADLE_INIT'",
            ]
        )
        lines.extend(GRADLE_INIT_SCRIPT.rstrip("\n").split("\n"))
        lines.extend(
            [
                "KS_GRADLE_INIT",
                "    then",
                '      if cmp -s "$KS_GI_TMP" "$KS_GI" 2>/dev/null; then rm -f "$KS_GI_TMP";'
                ' else mv -f "$KS_GI_TMP" "$KS_GI" 2>/dev/null || rm -f "$KS_GI_TMP"; fi',
                "    else",
                '      rm -f "$KS_GI_TMP" 2>/dev/null',
                "    fi",
            ]
        )
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
        ]
        + [
            line
            for parent, names in (
                ("$KUBESIGHT_CACHE_SLOT_DIR", slot_probes),
                ("$KUBESIGHT_CACHE_DIR", own_probes),
            )
            if names
            for line in (
                "    for KS_DIR in " + " ".join(names) + "; do",
                f'      if [ -n "$(ls -A "{parent}/$KS_DIR" 2>/dev/null)" ]; then',
                '        KS_WARM="$KS_WARM $KS_DIR"',
                "      else",
                '        KS_COLD="$KS_COLD $KS_DIR"',
                "      fi",
                "    done",
            )
        ]
        + (
            [
                # Marked as shared, so "warm" on a service's FIRST build is
                # explained rather than surprising.
                "    for KS_DIR in " + " ".join(shared_probes) + "; do",
                '      if [ -n "$(ls -A "$KUBESIGHT_SHARED_CACHE_DIR/$KS_DIR" 2>/dev/null)" ]; then',
                '        KS_WARM="$KS_WARM $KS_DIR(shared)"',
                "      else",
                '        KS_COLD="$KS_COLD $KS_DIR(shared)"',
                "      fi",
                "    done",
            ]
            if shared_probes
            else []
        )
        + ['    echo "[kubesight] Cache: $KUBESIGHT_CACHE_DIR"']
        + (['    echo "[kubesight] Shared: $KUBESIGHT_SHARED_CACHE_DIR"'] if shared_dirs else [])
        + [
            '    echo "[kubesight]   warm:${KS_WARM:- (nothing yet)}"',
            '    echo "[kubesight]   cold:${KS_COLD:- (none)}"',
        ]
        + _mismatch_checks(shared)
        + [
            "  else",
            '    echo "[kubesight] Cache directory $KUBESIGHT_CACHE_DIR is not writable;'
            ' this build runs cold." >&2',
            '    echo "[kubesight]   ${KS_MKDIR_ERR:-mkdir failed without saying why}" >&2',
            '    echo "[kubesight]   running as $(id 2>/dev/null || echo "uid $(id -u 2>/dev/null)");'
            ' $(ls -ld /kubesight-cache 2>/dev/null)" >&2',
        ]
        + _cold_fallback(dirs)
        + [
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


# --- node_modules -------------------------------------------------------------
#
# The package managers' own caches (npm/, yarn/, pnpm/ above) hold downloaded
# TARBALLS. They make yarn's "Fetching packages" fast and do nothing at all for
# "[4/4] Building fresh packages": that step runs every dependency's install
# script (node-sass compiling itself with node-gyp, electron fetching a binary,
# core-js, husky...) into a node_modules that is empty on every build, because
# the workspace is an emptyDir. No package-manager setting changes that - only
# keeping node_modules itself does.
#
# So an install stage keeps it: one tar per lockfile, keyed on everything that
# decides what an install produces. Same key, same tree, and the install that
# follows the restore finds nothing to do. One tar rather than the directory
# itself because node_modules is a hundred thousand small files and the volume
# is NFS, where each one is a round trip; a single archive streams.
#
# Per-service ONLY, never offered under _shared/: two services with different
# lockfiles share nothing, and native modules are built for one image.
NODE_MODULES_SUBDIR = "node-modules"
NODE_MODULES_KEEP = 3

# A stage is an install stage when its commands run a package manager's install.
# Matched on the text, because that is all a stage is: "corepack yarn install
# --frozen-lockfile", a bare "yarn", "npm ci", "pnpm i" and the lockfile-
# detection one-liner the default pipelines emit all count.
_NODE_INSTALL_RE = re.compile(
    r"(?:^|[\s;&|(])(?:yarn|pnpm)(?:[ \t]+-{1,2}[\w-]+(?:=\S+)?)*"
    r"(?:\s+(?:install|i)\b|[ \t]*(?:$|[;&|)]))"
    r"|(?:^|[\s;&|(])npm\s+(?:ci|install|i)(?:\s|$|[;&|)])",
    re.MULTILINE,
)


def runs_node_install(commands) -> bool:
    """Whether these stage commands install a Node project's dependencies."""
    text = commands if isinstance(commands, str) else "\n".join(commands or [])
    return bool(_NODE_INSTALL_RE.search(text))


def _sh_quote(value: str) -> str:
    return "'" + str(value).replace("'", "'\"'\"'") + "'"


# One install at a time per build pod. Parallel stages share the workspace and
# the service's yarn cache, and two installs at once - into one node_modules,
# or unpacking into one yarn cache - leave either broken. Worse, a stage that
# saves its node_modules while a sibling is still installing into it archives a
# half-built tree under a key that every later build trusts. So the restore,
# each install command and the save take this lock; the rest of each stage
# (the build, the tests) still runs side by side. The lock is in the
# workspace, which is local to the pod, so staleness by mtime is safe there.
NODE_INSTALL_LOCK_STALE_MINUTES = 2
NODE_INSTALL_LOCK_HEARTBEAT_SECONDS = 20


def _node_install_lock_lines(tag: str, stage: str) -> List[str]:
    owner = _sh_quote(stage or "another stage")
    return [
        "ks_nm_lock() {",
        "  KS_NM_LOCK=",
        # Kubernetes and both agents set it. Without one there is no pod-wide
        # place to lock in, and no sibling to lock against.
        '  [ -n "${KUBESIGHT_WORKSPACE:-}" ] || return 0',
        "  command -v sleep >/dev/null 2>&1 || return 0",
        '  KS_NM_LOCKS="$KUBESIGHT_WORKSPACE/.kubesight/locks"',
        '  mkdir -p "$KS_NM_LOCKS" 2>/dev/null || return 0',
        '  KS_NM_TRY="$KS_NM_LOCKS/node-install"',
        "  KS_NM_WAIT0=",
        '  until mkdir "$KS_NM_TRY" 2>/dev/null; do',
        f'    if [ -n "$(find "$KS_NM_TRY" -maxdepth 0 -mmin +{NODE_INSTALL_LOCK_STALE_MINUTES} 2>/dev/null)" ]; then',
        f'      echo "{tag} the install lock held by $(cat "$KS_NM_TRY/owner" 2>/dev/null || echo a stage) is stale; taking it."',
        '      rm -rf "$KS_NM_TRY" 2>/dev/null',
        "      continue",
        "    fi",
        '    if [ -z "$KS_NM_WAIT0" ]; then KS_NM_WAIT0=$(date +%s); KS_NM_SAID=; KS_NM_TRIES=0; fi',
        # Said once, naming the holder - which may take a moment to appear,
        # between its mkdir and its write of the owner file.
        "    KS_NM_TRIES=$((KS_NM_TRIES + 1))",
        '    KS_NM_WHO=$(cat "$KS_NM_TRY/owner" 2>/dev/null || true)',
        '    if [ -z "$KS_NM_SAID" ] && { [ -n "$KS_NM_WHO" ] || [ "$KS_NM_TRIES" -ge 3 ]; }; then',
        "      KS_NM_SAID=1",
        f'      echo "{tag} waiting for ${{KS_NM_WHO:-another stage}},'
        ' which is installing in this build pod; two installs at once break node_modules and the yarn cache."',
        "    fi",
        "    sleep 1",
        "  done",
        f"  KS_NM_TOKEN={owner}\" (${{HOSTNAME:-pod}} $$ $(date +%s))\"",
        '  echo "$KS_NM_TOKEN" > "$KS_NM_TRY/owner" 2>/dev/null',
        '  KS_NM_LOCK="$KS_NM_TRY"',
        '  if [ -n "$KS_NM_WAIT0" ]; then',
        f'    echo "{tag} waited $(( $(date +%s) - KS_NM_WAIT0 ))s for the install lock."',
        "  fi",
        # Renewed while held. If the shell holding it dies part way through (a
        # timeout's TERM), the renewer removes the lock itself - but only while
        # it still carries this holder's token.
        "  KS_NM_PARENT=$$",
        "  ( while sleep " + str(NODE_INSTALL_LOCK_HEARTBEAT_SECONDS) + "; do",
        '      [ "$(cat "$KS_NM_LOCK/owner" 2>/dev/null || true)" = "$KS_NM_TOKEN" ] || exit 0',
        '      if ! kill -0 "$KS_NM_PARENT" 2>/dev/null; then rm -rf "$KS_NM_LOCK"; exit 0; fi',
        '      touch "$KS_NM_LOCK" 2>/dev/null || exit 0',
        "    done ) </dev/null >/dev/null 2>&1 &",
        "  KS_NM_RENEW=$!",
        "}",
        "ks_nm_unlock() {",
        # Reaped straight away so bash (macOS /bin/sh) does not print a
        # "Terminated: 15" job notice into the build log.
        '  if [ -n "${KS_NM_RENEW:-}" ]; then kill "$KS_NM_RENEW" 2>/dev/null || true; wait "$KS_NM_RENEW" 2>/dev/null || true; fi',
        "  KS_NM_RENEW=",
        '  if [ -n "${KS_NM_LOCK:-}" ]; then rm -rf "$KS_NM_LOCK" 2>/dev/null || true; fi',
        "  KS_NM_LOCK=",
        "  return 0",
        "}",
        # Which calls are installs. The same rules as runs_node_install, on the
        # words of one call: npm ci|install|i|add, pnpm install|i|add, and
        # yarn with nothing but flags, or install|add.
        "ks_nm_is_install() {",
        '  KS_NM_TOOL="$1"',
        "  shift",
        '  case "$KS_NM_TOOL" in',
        '    npm) case "${1:-}" in ci|install|i|add) return 0 ;; esac ;;',
        '    pnpm) case "${1:-}" in install|i|add) return 0 ;; esac ;;',
        "    yarn)",
        "      while [ $# -gt 0 ]; do",
        '        case "$1" in',
        "          --cwd|--modules-folder|--cache-folder|--mutex|--network-timeout) shift 2 ;;",
        "          -*) shift ;;",
        "          install|add) return 0 ;;",
        "          *) return 1 ;;",
        "        esac",
        "      done",
        "      return 0 ;;",
        "  esac",
        "  return 1",
        "}",
        # Runs one call, under the lock when it is an install. The status is
        # captured with && || so `set -e` cannot leave with the lock held.
        "ks_nm_run() {",
        # The words to judge: the call itself, or what corepack is told to run.
        '  KS_NM_PROBE="$*"',
        '  if [ "$1" = corepack ]; then shift; KS_NM_PROBE="$*"; set -- corepack "$@"; fi',
        '  if [ -z "${KS_NM_LOCK:-}" ] && ks_nm_is_install $KS_NM_PROBE; then',
        "    ks_nm_lock || true",
        '    command "$@" && KS_NM_RUN_RC=0 || KS_NM_RUN_RC=$?',
        "    ks_nm_unlock",
        '    return "$KS_NM_RUN_RC"',
        "  fi",
        '  command "$@"',
        "}",
        'yarn() { ks_nm_run yarn "$@"; }',
        'pnpm() { ks_nm_run pnpm "$@"; }',
        'corepack() { ks_nm_run corepack "$@"; }',
    ]


def node_modules_wrap(
    commands: str,
    *,
    image: str = "",
    workdir: str = "",
    keep: int = NODE_MODULES_KEEP,
    stage: str = "",
) -> str:
    """The stage's own commands, with a node_modules restore before them and a
    save after them.

    Runs inside the stage's ``set -e`` subshell, already in its working
    directory. Both halves are functions called with ``|| true`` - which also
    suspends ``set -e`` inside them - so a cache that cannot be read or written
    is a cold install, never a failed stage. The save only runs when the
    commands succeeded: ``set -e`` has left the subshell otherwise, and a
    non-zero status that did not trip it is kept and returned unchanged.

    The key covers what decides the tree an install produces: package.json,
    every lockfile, .npmrc/.yarnrc, the node version, the CPU architecture, the
    stage image (native modules are built against its libc) and the working
    directory. NODE_ENV too, because production installs leave out
    devDependencies.

    The same text runs on every runner: the Kubernetes one wraps its stage
    script with it, and the server wraps an agent task's commands with it
    before sending them, so the Linux and Mac agents need no copy of their own
    - only a ``KUBESIGHT_NODE_MODULES_DIR`` to put the archives in.

    A restore is never re-saved - the same key means the same inputs - and an
    ``npm ci`` after one would delete it, so that one call becomes
    ``npm install --no-save``: the tree already matches the lockfile, which is
    all ``npm ci`` guarantees. ``KUBESIGHT_NODE_MODULES_CACHE=0`` in the stage's
    Environment turns the whole thing off for that stage.
    """
    keep = max(1, int(keep or NODE_MODULES_KEEP))
    tag = "[kubesight] node_modules cache:"
    lines = [
        "ks_nm_prepare() {",
        "  KS_NM_KEY=",
        # KUBESIGHT_NODE_MODULES_DIR is how an agent names its own per-service
        # directory (its cache is one folder for every service, and a Mac agent
        # has no KUBESIGHT_CACHE_DIR at all). Kubernetes leaves it unset and the
        # service's own subtree is the answer.
        '  KS_NM_DIR="${KUBESIGHT_NODE_MODULES_DIR:-}"',
        f'  if [ -z "$KS_NM_DIR" ] && [ -n "${{KUBESIGHT_CACHE_DIR:-}}" ]; then KS_NM_DIR="$KUBESIGHT_CACHE_DIR/{NODE_MODULES_SUBDIR}"; fi',
        '  [ -n "$KS_NM_DIR" ] || return 0',
        '  case "${KUBESIGHT_NODE_MODULES_CACHE:-1}" in',
        "    0|false|off|no|FALSE|OFF|NO)",
        f'      echo "{tag} off for this stage (KUBESIGHT_NODE_MODULES_CACHE)."',
        "      return 0 ;;",
        "  esac",
        "  KS_NM_LOCKS=",
        "  for KS_F in yarn.lock package-lock.json npm-shrinkwrap.json pnpm-lock.yaml; do",
        '    if [ -f "$KS_F" ]; then KS_NM_LOCKS="$KS_NM_LOCKS $KS_F"; fi',
        "  done",
        '  if [ -z "$KS_NM_LOCKS" ]; then',
        f'    echo "{tag} no lockfile in $PWD to key it on; installing cold."',
        "    return 0",
        "  fi",
        "  if ! command -v tar >/dev/null 2>&1; then",
        f'    echo "{tag} this image has no tar; installing cold."',
        "    return 0",
        "  fi",
        "  KS_NM_SUM=",
        # shasum is what a Mac has; cksum is POSIX and always there.
        "  for KS_T in sha256sum sha1sum shasum md5sum cksum; do",
        '    if command -v "$KS_T" >/dev/null 2>&1; then KS_NM_SUM=$KS_T; break; fi',
        "  done",
        '  [ -n "$KS_NM_SUM" ] || return 0',
        "  KS_NM_KEY=$( {",
        "    echo v1",
        f"    echo {_sh_quote(image)}",
        f"    echo {_sh_quote(workdir)}",
        "    cat package.json $KS_NM_LOCKS .npmrc .yarnrc .yarnrc.yml 2>/dev/null",
        "    node -v 2>/dev/null",
        # -s as well as -m: a host-mode agent builds native modules for its OS.
        "    uname -sm 2>/dev/null",
        '    echo "NODE_ENV=${NODE_ENV:-}"',
        "  } | \"$KS_NM_SUM\" | tr -dc '0-9a-f' | cut -c1-16 )",
        '  [ -n "$KS_NM_KEY" ] || return 0',
        '  KS_NM_ARCHIVE="$KS_NM_DIR/$KS_NM_KEY.tar"',
        # Checked in, or left by an earlier stage of this build: either way it
        # is not ours to replace, and not a clean result to save.
        "  if [ -e node_modules ]; then",
        f'    echo "{tag} node_modules is already here; leaving it alone."',
        "    KS_NM_KEY=",
        "    return 0",
        "  fi",
        '  if [ -f "$KS_NM_ARCHIVE" ]; then',
        "    KS_NM_T0=$(date +%s)",
        '    if tar -xf - < "$KS_NM_ARCHIVE" 2>/dev/null; then',
        "      KUBESIGHT_NODE_MODULES_RESTORED=1",
        "      export KUBESIGHT_NODE_MODULES_RESTORED",
        # The mtime is what pruning keeps by, so a key in use stays.
        '      touch "$KS_NM_ARCHIVE" 2>/dev/null || true',
        f'      echo "{tag} restored $KS_NM_KEY in $(( $(date +%s) - KS_NM_T0 ))s;'
        ' the install below should find nothing to do."',
        "    else",
        f'      echo "{tag} $KS_NM_KEY could not be read; discarding it and installing cold." >&2',
        '      rm -f "$KS_NM_ARCHIVE" 2>/dev/null',
        "      find . -path ./.git -prune -o -name node_modules -type d -prune"
        " -exec rm -rf {} + 2>/dev/null",
        "    fi",
        "  else",
        f'    echo "{tag} nothing saved for this lockfile yet ($KS_NM_KEY);'
        ' this install runs cold and is saved after."',
        "  fi",
        "}",
        "ks_nm_save() {",
        '  [ -n "${KS_NM_KEY:-}" ] || return 0',
        '  [ -z "${KUBESIGHT_NODE_MODULES_RESTORED:-}" ] || return 0',
        # Every node_modules, not just the root one: a workspace monorepo keeps
        # one per package, and those are built by the same step 4.
        "  KS_NM_DIRS=$(find . -path ./.git -prune -o -name node_modules -type d -prune"
        " -print 2>/dev/null)",
        '  if [ -z "$KS_NM_DIRS" ]; then',
        f'    echo "{tag} the install left no node_modules; nothing to save."',
        "    return 0",
        "  fi",
        '  if ! mkdir -p "$KS_NM_DIR" 2>/dev/null; then',
        f'    echo "{tag} $KS_NM_DIR is not writable; not saved." >&2',
        "    return 0",
        "  fi",
        # Through stdin/stdout rather than -f: GNU tar reads a "host:" prefix
        # in an -f argument as a remote tape, and every tar streams the same.
        # Written aside and renamed, so a build that dies mid-write - or a second
        # pod saving the same key - never leaves half an archive under the real
        # name.
        '  KS_NM_TMP="$KS_NM_DIR/.$KS_NM_KEY.${HOSTNAME:-pod}.$$.partial"',
        "  KS_NM_T0=$(date +%s)",
        '  if tar -cf - $KS_NM_DIRS 2>/dev/null > "$KS_NM_TMP"'
        ' && mv -f "$KS_NM_TMP" "$KS_NM_ARCHIVE" 2>/dev/null; then',
        '    KS_NM_SIZE=$(du -sh "$KS_NM_ARCHIVE" 2>/dev/null | cut -f1)',
        f'    echo "{tag} saved $KS_NM_KEY (${{KS_NM_SIZE:-?}}) in $(( $(date +%s) - KS_NM_T0 ))s;'
        ' the next build with this lockfile skips the install."',
        "  else",
        '    rm -f "$KS_NM_TMP" 2>/dev/null',
        f'    echo "{tag} could not write $KS_NM_ARCHIVE; not saved." >&2',
        "    return 0",
        "  fi",
        # Keep the archive just written plus the newest keep-1 others. It is
        # left out of the listing rather than trusted to sort first: on a
        # filesystem with coarse mtimes several archives can share a timestamp,
        # and ls -t would then order them by name and could prune the new one.
        f'  ls -1t "$KS_NM_DIR"/*.tar 2>/dev/null | grep -vxF "$KS_NM_ARCHIVE" | tail -n +{keep}'
        ' | while read -r KS_OLD; do rm -f "$KS_OLD"; done',
        '  find "$KS_NM_DIR" -name ".*.partial" -mmin +60 -exec rm -f {} + 2>/dev/null',
        "  return 0",
        "}",
    ]
    lines.extend(_node_install_lock_lines(tag, stage))
    lines.extend([
        "npm() {",
        '  if [ "${1:-}" = ci ] && [ -n "${KUBESIGHT_NODE_MODULES_RESTORED:-}" ]; then',
        "    shift",
        '    echo "[kubesight] npm ci -> npm install --no-save: node_modules was restored for'
        ' this exact lockfile, and npm ci would delete it first."',
        '    ks_nm_run npm install --no-save --prefer-offline --no-audit --no-fund "$@"',
        "    return",
        "  fi",
        '  ks_nm_run npm "$@"',
        "}",
        "ks_nm_lock || true",
        "ks_nm_prepare || true",
        "ks_nm_unlock",
        commands,
        "KS_NM_RC=$?",
        'if [ "$KS_NM_RC" -eq 0 ]; then ks_nm_lock || true; ks_nm_save || true; ks_nm_unlock; fi',
        '(exit "$KS_NM_RC")',
    ])
    return "\n".join(lines)
