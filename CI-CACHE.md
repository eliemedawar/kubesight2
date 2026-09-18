# Build caches in KubeSight CI

Every stage of every build gets a persistent directory of its own, on a volume
that outlives the pod. This is what turns a 555-second Gradle build into a
90-second one, and what stops OWASP Dependency-Check downloading the whole NVD
database on every run.

This page is the contract: what you get, what it is called, and what a pipeline
should say to use it.

---

## What a stage gets

One directory per service, mounted from a PersistentVolume:

```
$KUBESIGHT_CACHE_DIR = /kubesight-cache/<service-slug>
```

and inside it, five directories KubeSight creates before your commands run:

| Path | Variable | Holds |
|---|---|---|
| `$KUBESIGHT_CACHE_DIR/gradle` | `GRADLE_USER_HOME` | dependencies, wrapper distributions, Gradle's own caches |
| `$KUBESIGHT_CACHE_DIR/gradle-build-cache` | `GRADLE_BUILD_CACHE_DIR` | Gradle task outputs |
| `$KUBESIGHT_CACHE_DIR/dependency-check-data` | `DC_DATA_DIR` | the OWASP NVD database |
| `$KUBESIGHT_CACHE_DIR/semgrep` | `SEMGREP_CACHE_DIR` | downloaded Semgrep rulesets |
| `$KUBESIGHT_CACHE_DIR/buildkit` | `BUILDKIT_CACHE_DIR` | exported image layer cache |

Plus the ones the tool creates for itself on first use, already pointed at by
the variable that tool reads: `MAVEN_OPTS` (`-Dmaven.repo.local`),
`npm_config_cache`, `YARN_CACHE_FOLDER`, `npm_config_store_dir`,
`PIP_CACHE_DIR`, `GOMODCACHE`, `GOCACHE`, `CARGO_HOME`, `COMPOSER_CACHE_DIR`,
`NUGET_PACKAGES`, and `XDG_CACHE_HOME` as the catch-all.

Two more names a stage script can rely on:

- `KUBESIGHT_SERVICE_SLUG` — the slug, which is also the directory name.
- `KUBESIGHT_CACHE` — the same path as `KUBESIGHT_CACHE_DIR`, under the name it
  had before. Neither is more real than the other; new pipelines should say
  `KUBESIGHT_CACHE_DIR`.

**These are defaults, not policy.** A stage's own Environment overrides any of
them. The usual way to get this wrong is to set `MAVEN_OPTS=-Xmx2g` for the heap
and silently lose `-Dmaven.repo.local` with it — keep both:

```sh
MAVEN_OPTS=-Xmx2g -Dmaven.repo.local=$KUBESIGHT_CACHE_DIR/maven
```

### When there is no cache

`$KUBESIGHT_CACHE_DIR` is **empty** and none of the tool variables are set. A
pipeline can reference it unconditionally and simply get a cold build:

```sh
# Correct on a cluster with a cache and on one without.
if [ -n "${KUBESIGHT_CACHE_DIR:-}" ]; then ... ; fi
```

If the directory exists but cannot be written, the stage log says so —
`[kubesight] Cache directory … is not writable; this build runs cold.` — and the
build continues. A cache that is not working is a slow build, never a failed one.

---

## Pipeline recipes

### Build JAR (Gradle)

```sh
export GRADLE_OPTS="-Xmx2g -Dfile.encoding=UTF-8"

./gradlew --no-daemon --build-cache --parallel bootJar \
  -x test \
  -x checkstyleMain \
  -x checkstyleTest \
  -x compileTestJava \
  --stacktrace
```

`GRADLE_USER_HOME` and `GRADLE_BUILD_CACHE_DIR` are already exported, so the
stage does not set them. `--build-cache` is the part that matters: Gradle has no
environment variable for the build cache *directory*, so KubeSight writes an
init script to `$GRADLE_USER_HOME/init.d/kubesight-build-cache.gradle` that
reads `GRADLE_BUILD_CACHE_DIR` and points the local build cache at it. The init
script configures **where**, never **whether** — without `--build-cache` (or
`org.gradle.caching=true` in `gradle.properties`) nothing is cached and the
script is inert. Set `CI_CACHE_GRADLE_INIT=0` to stop KubeSight writing it.

**Do not run `gradle clean`.** It deletes the very outputs incremental builds
and the build cache exist to reuse. Gradle decides what to rebuild from input
hashes; a `clean` overrules that decision and throws the answer away. Stale
artifacts are not the risk here — Gradle's build cache is content-addressed, so
a cache entry is only reused when every input hashes identically.

### Dependency Check (OWASP)

```sh
mkdir -p dependency-check-report

if [ -n "${NVD_API_KEY:-}" ] && [ "${#NVD_API_KEY}" -ge 20 ]; then
  /usr/share/dependency-check/bin/dependency-check.sh \
    --project "$KUBESIGHT_SERVICE_SLUG" \
    --scan . \
    --format HTML --format JSON \
    --out dependency-check-report \
    --data "$DC_DATA_DIR" \
    --nvdApiKey "$NVD_API_KEY"
else
  env -u NVD_API_KEY /usr/share/dependency-check/bin/dependency-check.sh \
    --project "$KUBESIGHT_SERVICE_SLUG" \
    --scan . \
    --format HTML --format JSON \
    --out dependency-check-report \
    --data "$DC_DATA_DIR"
fi
```

`$DC_DATA_DIR` already exists — KubeSight creates it — but the `mkdir -p` for
the report directory is the stage's own, because that one lives in the
workspace.

The `if` is load-bearing and is what fixes this, from an earlier build of
`test123`:

```
[ERROR] Error updating the NVD Data
Invalid API Key, length of 0 too short
[ERROR] No documents exist
```

Dependency-Check reads `NVD_API_KEY` from the environment whether or not
`--nvdApiKey` is passed, and an **empty** variable is not the same as an absent
one: it sends a zero-length key, NVD rejects it, the update fails, and the scan
then has no database to scan against. `env -u NVD_API_KEY` removes the variable
for that one command. An empty `NVD_API_KEY` is usually a secret reference that
resolved to nothing — check Settings, because without a key the NVD update is
rate-limited to the point of being unusable.

With `--data` on the cache volume, the database is built once and reused. The
first run is still slow.

### Scan Source Code (Semgrep)

```sh
semgrep scan \
  --config p/owasp-top-ten \
  --config p/java \
  --config p/secrets \
  --metrics=off \
  --timeout 300 \
  --json \
  --output semgrep-report.json \
  .
```

`SEMGREP_CACHE_DIR`, `SEMGREP_VERSION_CACHE_PATH` and `XDG_CACHE_HOME` are
already exported, which is what keeps the ruleset downloads between runs.

### Build Image (BuildKit)

Nothing to write in the stage — layer caching is a runner setting, off by
default. Turn on either or both:

- `CI_BUILDKIT_LOCAL_CACHE=1` — cache to `$BUILDKIT_CACHE_DIR` on the volume.
  Cheap to read, and lost if the build lands on a node that cannot reach the
  volume. buildctl fails on an import from an empty directory, so KubeSight only
  passes `--import-cache` once `index.json` is there; the first build logs
  `No BuildKit layer cache yet; this image builds cold.`

  **This is not the thing that must not go on NFS.** `buildkitd`'s own store
  (`/var/lib/buildkit`) needs a real local filesystem, because the overlayfs
  snapshotter does not work over NFS — that store stays an `emptyDir` and this
  setting does not touch it. `--export-cache type=local` is resolved by the
  *client*, and writes ordinary blob files and an `index.json`, which NFS serves
  fine. If you would rather not find out, `CI_BUILDKIT_REGISTRY_CACHE` is the
  option this cluster already settled on.
- `CI_BUILDKIT_REGISTRY_CACHE=1` — push the cache to a `:buildcache` tag beside
  the image. Survives anything, costs a registry round trip.
  `CI_BUILDKIT_CACHE_REPO=registry.areeba.com/cache` collects every service's
  cache in one repository instead (`…/cache/<slug>:buildcache`), which is what
  you want where the image repositories are governed.

Both on at once is fine and buildctl accepts it.

---

## What is never cached

By construction, not by convention:

- **Secrets.** A secret reaches a stage as an environment variable from a
  per-build Kubernetes Secret and dies with the pod. Nothing mounts a Secret
  under the cache volume, and no injected variable points there.
- **The checkout, including `.git`.** The workspace is an `emptyDir` — the git
  credential, `$KUBESIGHT_ENV`, and the build's own output all live there and
  all go when the pod does. No cache variable points inside `/workspace`.
- **Logs.** Stage logs are masked before they are stored and are never written
  to the volume.

There is a test for each of these in
[backend/tests/test_ci_cache_paths.py](backend/tests/test_ci_cache_paths.py),
because "nothing points at the workspace" is the kind of claim that quietly
stops being true.

---

## Operating it

### Turning it on

The cluster has no StorageClass, so the cache is a PersistentVolume made by
hand and one claim shared by every service:

```sh
sh k8s/ci-cache.sh create     # applies k8s/ci-cache-volume.yaml
sh k8s/ci-cache.sh verify     # writes to it as uid 65532
sh k8s/ci-cache.sh enable     # CI_CACHE_CLAIM_NAME, restarts the backend
```

Read the comments at the top of [k8s/ci-cache-volume.yaml](k8s/ci-cache-volume.yaml)
before the first one — the NFS export has to be created and chowned on the
server, because a `root_squash` export silently refuses the `fsGroup` chown
kubelet would otherwise do.

Or from the UI: **Runners → Build cache**, which does the same things and also
measures and empties it.

Or by variable, for a cluster that *does* have a provisioner:
`CI_CACHE_STORAGE_CLASS=<class>` gives each service its own PVC. A setting saved
in the UI wins over the variables in both directions.

### Turning it off

`sh k8s/ci-cache.sh disable`, or the switch on the Runners page. Nothing is
deleted — the volume keeps its contents, builds simply stop mounting it and run
cold. Clearing both `CI_CACHE_CLAIM_NAME` and `CI_CACHE_STORAGE_CLASS` does the
same for an install with nothing saved in the UI.

### Emptying it

```sh
sh k8s/ci-cache.sh clean <service-slug>    # one service's subtree
sh k8s/ci-cache.sh clean --all
```

Refused while a build that would be reading those files is running: deleting a
Gradle cache underneath a build fails it with errors that look nothing like the
cause.

### Sizing and housekeeping

| | |
|---|---|
| **Storage class** | none — a hand-made NFS PersistentVolume, `ReadWriteMany`, `Retain` |
| **Size** | 20Gi is comfortable for a handful of Java/Node services. Budget roughly 1–2Gi per Java service for Gradle, **plus ~6–8Gi once for `dependency-check-data`** — the NVD database is shared per service and is by far the largest single item. |
| **Growth** | Gradle prunes its own caches; `removeUnusedEntriesAfterDays = 30` is set on the build cache by the init script. **BuildKit's local export prunes nothing** — if `CI_BUILDKIT_LOCAL_CACHE` is on, that directory grows until somebody empties it. |
| **Cleanup policy** | none automatic. Watch the card on the Runners page (or `sh k8s/ci-cache.sh status`) and `clean` a service when it gets large. |
| **Filling up** | NFS enforces no quota, so `capacity` on the PV is metadata only — it is the **export** that fills. Unlike a node-local volume this evicts nothing; builds just start failing to write, and the prep block reports it per stage. |
| **Permissions** | stage containers run as uid/gid 65532 with a read-only root filesystem and no `CAP_CHOWN`. The Job sets `fsGroup: 65532` with `fsGroupChangePolicy: OnRootMismatch`. On NFS that chown is done by kubelet *as root against the server*, so the export must be `chown 65532:65532` and `chmod 2775` on the server itself — the setgid bit is what keeps the group on subdirectories the build tools create. |
| **Concurrency** | one pod holds the volume at a time per service, because build tools lock their cache directories and a service's builds are serialised by its `maxConcurrentBuilds`. |

---

## Settings

| Variable | Default | Does |
|---|---|---|
| `CI_CACHE_CLAIM_NAME` | — | Mount this existing claim. One volume, every service in its own subtree. Takes precedence over the storage class; KubeSight never creates, resizes or deletes it. |
| `CI_CACHE_STORAGE_CLASS` | — | Provision one PVC per service from this class. |
| `CI_CACHE_SIZE` | `10Gi` | Size of a per-service PVC (storage-class mode only). |
| `CI_CACHE_GRADLE_INIT` | `1` | Write the Gradle init script. `0`/`off` to leave `init.d` alone. |
| `CI_BUILDKIT_LOCAL_CACHE` | `0` | Export image layers to `$BUILDKIT_CACHE_DIR`. |
| `CI_BUILDKIT_REGISTRY_CACHE` | `0` | Export image layers to a `:buildcache` tag. |
| `CI_BUILDKIT_CACHE_REPO` | — | Put those tags under one repository rather than beside each image. |

A setting saved from the Runners page wins over all of these — a switch that
cannot turn something off is not a switch. With nothing saved, the variables
decide.

---

## Where this lives in the code

| File | Holds |
|---|---|
| [backend/api/services/ci/cache_layout.py](backend/api/services/ci/cache_layout.py) | the layout: mount paths, the per-service directory rule, the variable map, the prep script, the Gradle init script |
| [backend/api/services/ci/runners/kubernetes.py](backend/api/services/ci/runners/kubernetes.py) | mounting the claim, injecting the variables, running the prep, the buildctl cache flags |
| [backend/api/services/ci/cache.py](backend/api/services/ci/cache.py) | the operator's half: status, create, on/off, measure, clean |
| [agent/kubesight-agent.py](agent/kubesight-agent.py) | the same variable names on an external agent, so one stage script runs on both runners |
| [k8s/ci-cache-volume.yaml](k8s/ci-cache-volume.yaml), [k8s/ci-cache.sh](k8s/ci-cache.sh) | the volume, and the script that applies and maintains it |

`cache_layout.py` is shared by the runner and the maintenance jobs on purpose:
if those two disagreed about a path, `clean` would empty a directory no build
ever wrote to, and the cache card would report zero on a volume that is full.

---

## A note on `/cache`

The volume used to be mounted at `/cache`, and in per-service-claim mode the
subtree was the mount root rather than `/cache/<slug>`. It is now
`/kubesight-cache/<service-slug>` in both modes.

The claim is still mounted at `/cache` as well — the same volume, so the same
bytes — so a pipeline that hardcodes `/cache/<slug>` keeps its warm cache. Two
consequences worth knowing:

- A service that was using **storage-class mode** had its caches at the mount
  root and will see one cold build while the new subtree fills. Nothing is lost;
  the old files are still on the volume, one directory up.
- Nothing KubeSight generates emits `/cache` any more. Once no pipeline mentions
  it, drop `LEGACY_MOUNT_PATH` and its mount.
