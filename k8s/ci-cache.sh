#!/bin/sh
# Native CI build cache — create it, turn it on and off, inspect it, empty it.
#
#   sh k8s/ci-cache.sh create              make the PV + PVC (NFS by default)
#   sh k8s/ci-cache.sh verify              prove a build pod can write to it
#   sh k8s/ci-cache.sh enable              point KubeSight at it (restarts backend)
#   sh k8s/ci-cache.sh disable             stop using it (volume kept intact)
#   sh k8s/ci-cache.sh status              config, binding, size per service
#   sh k8s/ci-cache.sh clean <service>     empty one service's cache
#   sh k8s/ci-cache.sh clean --all         empty every service's cache
#   sh k8s/ci-cache.sh clean --shared      empty the cache every service shares
#
# k8s/ci-cache-volume.yaml is the annotated version of what `create` applies —
# read it for why each field is the way it is.
#
# Environment (defaults in brackets):
#   MODE          nfs | local                                            [nfs]
#   NFS_SERVER    NFS server for MODE=nfs                        [10.4.27.17]
#   NFS_PATH      export directory            [/datauat/NFS-DATA/ci-cache]
#   NODE          node holding the directory, MODE=local only     [required]
#   DIR           directory on that node     [/var/lib/kubesight/ci-cache]
#   SIZE          PV capacity + claim request                           [20Gi]
#   NS            namespace builds run in — must match
#                 CI_KUBERNETES_NAMESPACE                       [kubesight-ci]
#   CLAIM         claim name                                        [ci-cache]
#   BACKEND_NS    namespace KubeSight runs in                      [kubesight]
#   CM            the CI ConfigMap            [kubesight-ci-integration]
#   PROBE_IMAGE   image for verify/status/clean pods  [CI_WORKER_IMAGE from CM]
#   FORCE         1 = let clean run even with a build in flight          [0]
#
# Nothing here needs a StorageClass, and nothing here touches a build in
# progress except `clean`, which refuses to unless FORCE=1.
set -e

MODE=${MODE:-nfs}
NFS_SERVER=${NFS_SERVER:-10.4.27.17}
NFS_PATH=${NFS_PATH:-/datauat/NFS-DATA/ci-cache}
DIR=${DIR:-/var/lib/kubesight/ci-cache}
SIZE=${SIZE:-20Gi}
NS=${NS:-kubesight-ci}
CLAIM=${CLAIM:-ci-cache}
PV=${PV:-kubesight-ci-cache}
BACKEND_NS=${BACKEND_NS:-kubesight}
CM=${CM:-kubesight-ci-integration}
BUILD_UID=65532

COMMAND=${1:-}
ARG=${2:-}

say() { printf '\n== %s\n' "$1"; }
die() { printf '%s\n' "$@" >&2; exit 1; }

usage() {
    sed -n '2,28p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
}

need_cluster() {
    kubectl version --request-timeout=10s >/dev/null 2>&1 ||
        die "kubectl cannot reach a cluster. Set KUBECONFIG and try again."
}

# The Deployment is found by the ServiceAccount it runs as, not by name: this
# cluster's backend Service is `backend-service` while its ServiceAccount is
# `kubesight-backend`, and guessing either way has a 50% failure rate.
backend_deploy() {
    kubectl -n "$BACKEND_NS" get deploy -o \
        'jsonpath={.items[?(@.spec.template.spec.serviceAccountName=="kubesight-backend")].metadata.name}' \
        2>/dev/null
}

restart_backend() {
    deploy=$(backend_deploy)
    if [ -z "$deploy" ]; then
        echo "Could not find the backend Deployment in $BACKEND_NS." >&2
        echo "Restart it yourself — the ConfigMap change only takes effect on a" >&2
        echo "new pod: kubectl -n $BACKEND_NS rollout restart deploy/<name>" >&2
        return 0
    fi
    echo "restarting deploy/$deploy"
    kubectl -n "$BACKEND_NS" rollout restart "deploy/$deploy"
    kubectl -n "$BACKEND_NS" rollout status "deploy/$deploy" --timeout=180s
}

claim_phase() {
    kubectl -n "$NS" get pvc "$CLAIM" -o jsonpath='{.status.phase}' 2>/dev/null || true
}

configured_claim() {
    kubectl -n "$BACKEND_NS" get configmap "$CM" \
        -o jsonpath='{.data.CI_CACHE_CLAIM_NAME}' 2>/dev/null || true
}

configured_class() {
    kubectl -n "$BACKEND_NS" get configmap "$CM" \
        -o jsonpath='{.data.CI_CACHE_STORAGE_CLASS}' 2>/dev/null || true
}

# Builds KubeSight is running right now, from the labels the runner puts on
# every Job. Empty output = nothing in flight.
active_jobs() {
    selector="app.kubernetes.io/name=kubesight-ci"
    [ -n "$1" ] && selector="$selector,kubesight.io/service=$1"
    kubectl -n "$NS" get jobs -l "$selector" \
        -o 'jsonpath={range .items[?(@.status.active>0)]}{.metadata.name}{" "}{end}' 2>/dev/null || true
}

probe_image() {
    if [ -z "$PROBE_IMAGE" ]; then
        PROBE_IMAGE=$(kubectl -n "$BACKEND_NS" get configmap "$CM" \
            -o jsonpath='{.data.CI_WORKER_IMAGE}' 2>/dev/null || true)
    fi
    # The worker image is the safe default: every build already pulls it, so it
    # is known to exist and to be reachable from these nodes.
    [ -z "$PROBE_IMAGE" ] && PROBE_IMAGE=busybox:1.36
    printf '%s' "$PROBE_IMAGE"
}

# Run one short-lived pod with the cache mounted, print what it said, delete it.
# Same securityContext a real stage container gets, so anything that works here
# works in a build — including under the namespace's restricted Pod Security
# Standard. $1 = pod name suffix, $2 = shell script (single-quote free).
cache_pod() {
    pod="ci-cache-$1"
    script=$2
    image=$(probe_image)
    kubectl -n "$NS" delete pod "$pod" --ignore-not-found >/dev/null
    kubectl apply -f - >/dev/null <<YAML
apiVersion: v1
kind: Pod
metadata:
  name: $pod
  namespace: $NS
  labels:
    app.kubernetes.io/name: kubesight-ci
    kubesight.io/purpose: ci-cache-maintenance
spec:
  restartPolicy: Never
  automountServiceAccountToken: false
  securityContext:
    runAsNonRoot: true
    fsGroup: $BUILD_UID
    fsGroupChangePolicy: OnRootMismatch
    seccompProfile:
      type: RuntimeDefault
  containers:
    - name: probe
      image: $image
      command: ["/bin/sh", "-c"]
      args: ['$script']
      securityContext:
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        runAsNonRoot: true
        runAsUser: $BUILD_UID
        runAsGroup: $BUILD_UID
        capabilities:
          drop: ["ALL"]
      resources:
        requests: {cpu: 50m, memory: 64Mi}
        limits: {cpu: 500m, memory: 512Mi}
      volumeMounts:
        - name: cache
          mountPath: /kubesight-cache
  volumes:
    - name: cache
      persistentVolumeClaim:
        claimName: $CLAIM
YAML
    i=0
    phase=""
    while [ "$i" -lt 90 ]; do
        phase=$(kubectl -n "$NS" get pod "$pod" -o jsonpath='{.status.phase}' 2>/dev/null || true)
        case "$phase" in Succeeded|Failed) break ;; esac
        i=$((i + 1))
        sleep 2
    done
    kubectl -n "$NS" logs "$pod" 2>&1 || true
    if [ "$phase" != "Succeeded" ]; then
        echo >&2
        echo "The maintenance pod ended as ${phase:-unknown}. If it never started," >&2
        echo "the export directory is usually missing or not writable by uid" >&2
        echo "$BUILD_UID — see the prep step in k8s/ci-cache-volume.yaml." >&2
        kubectl -n "$NS" describe pod "$pod" 2>&1 | tail -20 >&2
        kubectl -n "$NS" delete pod "$pod" --ignore-not-found >/dev/null
        return 1
    fi
    kubectl -n "$NS" delete pod "$pod" --ignore-not-found >/dev/null
}

# KubeSight's own slugification, so `clean payment-service` and the directory
# the build wrote agree even when the argument is typed with capitals.
slugify() {
    printf '%s' "$1" | tr 'A-Z' 'a-z' | sed 's/[^a-z0-9-]\{1,\}/-/g; s/^-*//; s/-*$//'
}

# ---------------------------------------------------------------------------
# create
# ---------------------------------------------------------------------------
cmd_create() {
    need_cluster
    kubectl get namespace "$NS" >/dev/null ||
        die "No such namespace: $NS — apply k8s/ci-runner.yaml first."

    # A bound PV cannot be resized or re-pathed in place, so say what exists
    # rather than failing halfway through an apply.
    if kubectl get pv "$PV" >/dev/null 2>&1; then
        say "Already there"
        kubectl get pv "$PV" -o custom-columns=NAME:.metadata.name,CAPACITY:.spec.capacity.storage,STATUS:.status.phase,CLAIM:.spec.claimRef.name
        echo
        echo "To change its size or backing path, delete both objects first."
        echo "The cache contents are NOT deleted with them:"
        echo "  kubectl -n $NS delete pvc $CLAIM && kubectl delete pv $PV"
        exit 1
    fi

    case "$MODE" in
        nfs)
            backing="  nfs:
    server: $NFS_SERVER
    path: $NFS_PATH"
            modes='["ReadWriteMany"]'
            say "Backing store"
            echo "NFS $NFS_SERVER:$NFS_PATH"
            echo
            echo "That directory must exist and belong to uid $BUILD_UID. On an"
            echo "export with root_squash, kubelet's fsGroup chown is refused, so"
            echo "this is the step that actually decides whether builds can write:"
            echo "  ssh $NFS_SERVER 'sudo mkdir -p $NFS_PATH && sudo chown $BUILD_UID:$BUILD_UID $NFS_PATH && sudo chmod 2775 $NFS_PATH'"
            ;;
        local)
            [ -n "$NODE" ] || die "MODE=local needs NODE=<node>." \
                "  kubectl get nodes -o custom-columns=NAME:.metadata.name,TAINTS:.spec.taints[*].key"
            kubectl get node "$NODE" >/dev/null || die "No such node: $NODE"
            # The PV pins every build pod to this node, and build pods carry no
            # tolerations — a NoSchedule taint here does not slow the cache
            # down, it stops builds running at all.
            taints=$(kubectl get node "$NODE" -o \
                'jsonpath={range .spec.taints[?(@.effect=="NoSchedule")]}{.key}{" "}{end}')
            if [ -n "$taints" ] && [ -z "$ALLOW_TAINTED" ]; then
                die "$NODE carries NoSchedule taints: $taints" \
                    "Build pods have no tolerations and this volume would pin them" \
                    "here, so every build would sit Pending. Pick a node that runs" \
                    "workloads, or set ALLOW_TAINTED=1 if you arranged tolerations."
            fi
            backing="  local:
    path: $DIR
  nodeAffinity:
    required:
      nodeSelectorTerms:
        - matchExpressions:
            - key: kubernetes.io/hostname
              operator: In
              values: [\"$NODE\"]"
            modes='["ReadWriteOnce"]'
            say "Backing store"
            echo "node-local directory $NODE:$DIR — every build pod will run on $NODE"
            echo "  ssh $NODE 'sudo mkdir -p $DIR && sudo chown $BUILD_UID:$BUILD_UID $DIR && sudo chmod 2775 $DIR'"
            ;;
        *) die "MODE must be nfs or local (got: $MODE)" ;;
    esac

    manifest="apiVersion: v1
kind: PersistentVolume
metadata:
  name: $PV
  labels:
    kubesight.io/purpose: ci-dependency-cache
spec:
  capacity:
    storage: $SIZE
  volumeMode: Filesystem
  accessModes: $modes
  persistentVolumeReclaimPolicy: Retain
  storageClassName: \"\"
  claimRef:
    namespace: $NS
    name: $CLAIM
$backing
---
apiVersion: v1
kind: PersistentVolumeClaim
metadata:
  name: $CLAIM
  namespace: $NS
  labels:
    app.kubernetes.io/name: kubesight-ci
    kubesight.io/purpose: ci-dependency-cache
spec:
  accessModes: $modes
  storageClassName: \"\"
  volumeName: $PV
  resources:
    requests:
      storage: $SIZE"

    say "Manifest"
    printf '%s\n' "$manifest"
    if [ -n "$DRY_RUN" ]; then
        say "DRY_RUN set — nothing applied"
        return 0
    fi

    say "Applying"
    printf '%s\n' "$manifest" | kubectl apply -f -

    say "Waiting for the claim to bind"
    i=0
    while [ "$i" -lt 30 ]; do
        [ "$(claim_phase)" = "Bound" ] && break
        i=$((i + 1))
        sleep 2
    done
    kubectl -n "$NS" get pvc "$CLAIM"
    [ "$(claim_phase)" = "Bound" ] || die \
        "Still not Bound. kubectl -n $NS describe pvc $CLAIM says why — a size" \
        "mismatch or a wrong storageClassName are the usual two."

    say "Next"
    echo "  sh $0 verify     prove a build pod can write to it"
    echo "  sh $0 enable     tell KubeSight to use it"
}

# ---------------------------------------------------------------------------
# verify / status
# ---------------------------------------------------------------------------
cmd_verify() {
    need_cluster
    [ "$(claim_phase)" = "Bound" ] || die \
        "$CLAIM in $NS is not Bound (it is: $(claim_phase)). Run: sh $0 create"
    say "Writing to the cache as uid $BUILD_UID"
    cache_pod probe 'set -e; d=/kubesight-cache/.probe; mkdir -p $d; date > $d/write-test; cat $d/write-test; rm -rf $d; echo CACHE-OK'
}

cmd_status() {
    need_cluster
    say "KubeSight configuration"
    claim=$(configured_claim)
    class=$(configured_class)
    if [ -n "$claim" ]; then
        echo "caching ENABLED — CI_CACHE_CLAIM_NAME=$claim"
        [ "$claim" = "$CLAIM" ] || echo "  (note: this script targets $CLAIM)"
    elif [ -n "$class" ]; then
        echo "caching ENABLED dynamically — CI_CACHE_STORAGE_CLASS=$class"
        echo "  KubeSight creates one PVC per service itself; this script's"
        echo "  create/clean commands are for the hand-made single-claim setup."
    else
        echo "caching DISABLED — neither CI_CACHE_CLAIM_NAME nor"
        echo "CI_CACHE_STORAGE_CLASS is set in configmap/$CM. Every build starts"
        echo "cold. Turn it on with: sh $0 enable"
    fi

    say "Volume"
    kubectl get pv "$PV" -o custom-columns=NAME:.metadata.name,CAPACITY:.spec.capacity.storage,ACCESS:.spec.accessModes,STATUS:.status.phase 2>/dev/null ||
        echo "no PersistentVolume named $PV"
    kubectl -n "$NS" get pvc "$CLAIM" 2>/dev/null || echo "no claim named $CLAIM in $NS"

    running=$(active_jobs "")
    say "Builds in flight"
    [ -n "$running" ] && echo "$running" || echo "none"

    if [ "$(claim_phase)" = "Bound" ]; then
        say "Size per service"
        # Read-only: du walks the tree, nothing is written or removed.
        cache_pod du 'du -sh /kubesight-cache/* 2>/dev/null; echo ----; du -sh /kubesight-cache 2>/dev/null'
    fi
}

# ---------------------------------------------------------------------------
# enable / disable
# ---------------------------------------------------------------------------
cmd_enable() {
    need_cluster
    phase=$(claim_phase)
    # Enabling with no volume behind it would fail every build at the first
    # stage instead of merely leaving them cold, so it is checked first.
    [ "$(claim_phase)" = "Bound" ] || die \
        "$CLAIM in $NS is not Bound (it is: ${phase:-missing})." \
        "Create the volume first: sh $0 create"

    say "Pointing KubeSight at $CLAIM"
    kubectl -n "$BACKEND_NS" patch configmap "$CM" --type merge \
        -p "{\"data\":{\"CI_CACHE_CLAIM_NAME\":\"$CLAIM\"}}"
    # envFrom is read once, when the container starts.
    restart_backend

    say "On"
    echo "Every stage now mounts $CLAIM at /kubesight-cache, with Maven, Gradle, npm,"
    echo "yarn, pnpm, pip, Go, Cargo, Composer, NuGet and XDG_CACHE_HOME"
    echo "pointed into /kubesight-cache/<service-slug>/."
    echo
    echo "This patched the live ConfigMap. Keep k8s/ci-backend-config.yaml in"
    echo "step (CI_CACHE_CLAIM_NAME: $CLAIM) or the next apply of that file"
    echo "will quietly overwrite this."
}

cmd_disable() {
    need_cluster
    say "Turning caching off"
    # Both keys: either one on its own is enough to keep the cache mounted.
    kubectl -n "$BACKEND_NS" patch configmap "$CM" --type merge \
        -p '{"data":{"CI_CACHE_CLAIM_NAME":null,"CI_CACHE_STORAGE_CLASS":null}}'
    restart_backend

    say "Off"
    echo "Builds no longer mount a cache and start cold. Nothing was deleted:"
    echo "the volume, the claim and everything in them are untouched, so"
    echo "  sh $0 enable"
    echo "picks the same warm cache back up. To reclaim the space instead:"
    echo "  sh $0 clean --all"
}

# ---------------------------------------------------------------------------
# clean
# ---------------------------------------------------------------------------
cmd_clean() {
    need_cluster
    [ -n "$ARG" ] || die "clean needs a service slug, --shared or --all:" \
        "  sh $0 clean payment-service" \
        "  sh $0 clean --all"
    [ "$(claim_phase)" = "Bound" ] || die "$CLAIM in $NS is not Bound — nothing to clean."

    if [ "$ARG" = "--all" ]; then
        target="every service"
        # -mindepth 1 empties the directory without removing /kubesight-cache itself,
        # which is the mount point; -maxdepth 1 keeps it to one pass.
        script='find /kubesight-cache -mindepth 1 -maxdepth 1 -exec rm -rf {} + ; echo CLEANED; du -sh /kubesight-cache'
        busy=$(active_jobs "")
    elif [ "$ARG" = "--shared" ]; then
        # The subtree every service shares (NVD database, npm, pip...). Any
        # running build may be reading it, so any build in flight blocks this.
        target="the shared cache"
        script="rm -rf /kubesight-cache/_shared; echo CLEANED; du -sh /kubesight-cache/* 2>/dev/null; true"
        busy=$(active_jobs "")
    else
        slug=$(slugify "$ARG")
        [ -n "$slug" ] || die "That is not a usable service slug: $ARG"
        target="$slug"
        script="rm -rf /kubesight-cache/$slug; echo CLEANED; du -sh /kubesight-cache/* 2>/dev/null; true"
        busy=$(active_jobs "$slug")
    fi

    if [ -n "$busy" ] && [ -z "$FORCE" ]; then
        die "A build is running: $busy" \
            "Deleting its cache underneath it would fail the build with errors" \
            "that look nothing like the cause. Wait for it, or set FORCE=1."
    fi

    say "Emptying the cache for $target"
    echo "This deletes downloaded dependencies only — the next build re-fetches"
    echo "them and is slow once. Artifacts, logs and builds are elsewhere."
    cache_pod clean "$script"
}

case "$COMMAND" in
    create) cmd_create ;;
    verify) cmd_verify ;;
    status) cmd_status ;;
    enable) cmd_enable ;;
    disable) cmd_disable ;;
    clean) cmd_clean ;;
    ""|-h|--help|help) usage ;;
    *) echo "unknown command: $COMMAND" >&2; usage ;;
esac
