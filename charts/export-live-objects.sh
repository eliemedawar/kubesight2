#!/usr/bin/env bash
# Export the live KubeSight objects from a cluster (UAT) so the production
# Helm chart can reproduce them exactly. Read-only: it only runs `kubectl get`
# and, inside Hermes pods, `cat`/`ls`.
#
# Per matching namespace it writes Deployments, StatefulSets, DaemonSets,
# Services, ConfigMaps, ServiceAccounts, Roles, RoleBindings, Ingresses, PVCs,
# NetworkPolicies, CronJobs, HPAs, PDBs and quotas. Plus the ClusterRoles,
# ClusterRoleBindings and PVs that belong to those namespaces, every Hermes
# pod's config.yaml, and a short description of the cluster (nodes,
# StorageClasses, ingress classes, CRDs, Helm releases).
#
# Secret VALUES are never written: only each Secret's name, type and keys.
# Hermes config.yaml is copied with key/token/secret/password values masked.
# ConfigMaps and Deployment env are written as they are, so a value set in a
# plain `value:` instead of a Secret will be in the output; the script lists
# the files that look like they hold one. Never commit the output (the
# default out-dir is in .gitignore).
#
# Usage:  bash charts/export-live-objects.sh [namespace-regex] [out-dir]
#         defaults: 'kubesight|hermes'  ./uat-export
# Works wherever kubectl reaches the cluster: Linux, macOS or Git Bash.
set -uo pipefail
# Git Bash would otherwise rewrite /opt/data into a Windows path for kubectl exec.
export MSYS_NO_PATHCONV=1

PATTERN="${1:-kubesight|hermes}"
OUT="${2:-uat-export}"

say() { printf '\n== %s\n' "$1"; }
die() { printf '%s\n' "$@" >&2; exit 1; }

command -v kubectl >/dev/null 2>&1 || die "kubectl not found."
kubectl version --request-timeout=10s >/dev/null 2>&1 ||
    die "Cannot reach the cluster (context: $(kubectl config current-context 2>/dev/null || echo none))."
[ -e "$OUT" ] && die "$OUT already exists. Move it away or pass another out-dir."
mkdir -p "$OUT/cluster" "$OUT/hermes"

{
    echo "context:  $(kubectl config current-context)"
    echo "exported: $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    echo "pattern:  $PATTERN"
} > "$OUT/README.txt"

NAMESPACED_KINDS="deployments statefulsets daemonsets services configmaps serviceaccounts
roles rolebindings ingresses persistentvolumeclaims networkpolicies cronjobs
horizontalpodautoscalers poddisruptionbudgets resourcequotas limitranges"

NAMESPACES=$(kubectl get ns -o go-template='{{range .items}}{{.metadata.name}}{{"\n"}}{{end}}' |
    grep -E "$PATTERN")
[ -n "$NAMESPACES" ] || die "No namespace matches '$PATTERN'."

say "namespaces"
for ns in $NAMESPACES; do
    echo "   $ns"
    mkdir -p "$OUT/ns/$ns"
    kubectl get ns "$ns" -o yaml > "$OUT/ns/$ns/namespace.yaml"
    for kind in $NAMESPACED_KINDS; do
        # Only write kinds that have objects, so the folder shows what exists.
        if [ -n "$(kubectl get "$kind" -n "$ns" -o name 2>/dev/null)" ]; then
            kubectl get "$kind" -n "$ns" -o yaml > "$OUT/ns/$ns/$kind.yaml"
        fi
    done
    kubectl get secrets -n "$ns" -o go-template='{{range .items}}{{.metadata.name}}  type={{.type}}  keys:{{range $k, $v := .data}} {{$k}}{{end}}{{"\n"}}{{end}}' \
        > "$OUT/ns/$ns/secrets-keys-only.txt"
    kubectl get pods -n "$ns" -o wide > "$OUT/ns/$ns/pods.txt" 2>&1
done

say "cluster-wide RBAC and volumes"
# A binding belongs to KubeSight when its name, its role or one of its
# subjects' namespaces matches the pattern.
BINDINGS_TSV=$(kubectl get clusterrolebindings -o go-template='{{range .items}}{{.metadata.name}}{{"\t"}}{{.roleRef.kind}}{{"\t"}}{{.roleRef.name}}{{"\t"}}{{range .subjects}}{{.namespace}} {{end}}{{"\n"}}{{end}}' |
    grep -E "$PATTERN")
BINDINGS=$(printf '%s\n' "$BINDINGS_TSV" | cut -f1 | grep -v '^$')
ROLES=$({
    printf '%s\n' "$BINDINGS_TSV" | awk -F'\t' '$2 == "ClusterRole" { print $3 }'
    kubectl get clusterroles -o name | sed 's|.*/||' | grep -E "$PATTERN"
} | grep -v '^$' | sort -u)
# shellcheck disable=SC2086  # word splitting is the point: one name per arg
[ -n "$BINDINGS" ] && kubectl get clusterrolebindings $BINDINGS -o yaml > "$OUT/cluster/clusterrolebindings.yaml"
# shellcheck disable=SC2086
[ -n "$ROLES" ] && kubectl get clusterroles $ROLES -o yaml > "$OUT/cluster/clusterroles.yaml"
echo "   $(printf '%s\n' "$BINDINGS" | grep -c .) ClusterRoleBindings, $(printf '%s\n' "$ROLES" | grep -c .) ClusterRoles"

PVS=$(kubectl get pv -o go-template='{{range .items}}{{.metadata.name}}{{"\t"}}{{with .spec.claimRef}}{{.namespace}}{{end}}{{"\n"}}{{end}}' |
    awk -F'\t' -v re="$PATTERN" '$2 ~ re { print $1 }')
# shellcheck disable=SC2086
[ -n "$PVS" ] && kubectl get pv $PVS -o yaml > "$OUT/cluster/persistentvolumes.yaml"
echo "   $(printf '%s\n' "$PVS" | grep -c .) PersistentVolumes"

say "cluster description"
kubectl version > "$OUT/cluster/version.txt" 2>&1
kubectl get nodes -o wide > "$OUT/cluster/nodes.txt" 2>&1
kubectl get storageclasses > "$OUT/cluster/storageclasses.txt" 2>&1
kubectl get ingressclasses > "$OUT/cluster/ingressclasses.txt" 2>&1
kubectl get svc -A 2>/dev/null | grep -iE 'NAMESPACE|ingress' > "$OUT/cluster/ingress-controllers.txt"
kubectl get crds -o name > "$OUT/cluster/crds.txt" 2>&1
if command -v helm >/dev/null 2>&1; then
    helm list -A > "$OUT/cluster/helm-releases.txt" 2>&1
fi
echo "   done"

say "Hermes"
# Every pod whose name or image mentions hermes, in any namespace: UAT may run
# more than one Hermes (Application Intelligence and the ticket/MCP agent).
kubectl get pods -A -o go-template='{{range .items}}{{.metadata.namespace}}{{"\t"}}{{.metadata.name}}{{"\t"}}{{range .spec.containers}}{{.image}} {{end}}{{"\n"}}{{end}}' |
    grep -i hermes > "$OUT/hermes/pods.txt"
# Mask any value of a key/token/secret/password field unless it is a ${VAR}
# reference or a plain number (so max_tokens survives; a Telegram bot token
# also starts with digits, so "starts with a digit" is not enough).
mask() { sed -E '/:[[:space:]]*[0-9.]+[[:space:]]*$/! s/^([[:space:]]*[^:#]*(key|token|secret|password)[^:]*:[[:space:]]*)[^[:space:]$].*$/\1<masked>/I'; }
while IFS=$'\t' read -r ns pod _images; do
    [ -n "$pod" ] || continue
    echo "   $ns/$pod"
    dir="$OUT/hermes/$ns.$pod"
    mkdir -p "$dir"
    kubectl exec -n "$ns" "$pod" -- sh -c 'cat "${HERMES_HOME:-/opt/data}/config.yaml"' 2>&1 |
        mask > "$dir/config.yaml"
    kubectl exec -n "$ns" "$pod" -- sh -c 'd="${HERMES_HOME:-/opt/data}"; ls -la "$d"; echo; ls -la "$d/skills" 2>/dev/null' \
        > "$dir/data-listing.txt" 2>&1
done < "$OUT/hermes/pods.txt"
[ -s "$OUT/hermes/pods.txt" ] || echo "   no Hermes pod found in this cluster"

say "check"
LEAKS=$(grep -rlE 'ksa_[0-9a-f]{16}|sk-[A-Za-z0-9_-]{20}|xox[abp]-|ghp_[A-Za-z0-9]{20}|AKIA[0-9A-Z]{16}|[0-9]{8,10}:AA[A-Za-z0-9_-]{30,}|-----BEGIN [A-Z ]*PRIVATE KEY' "$OUT")
if [ -n "$LEAKS" ]; then
    echo "   These files look like they hold a credential in plain text."
    echo "   Mask it before sharing, and move it into a Secret on UAT:"
    printf '     %s\n' $LEAKS
else
    echo "   no plain-text credentials spotted"
fi

tar -czf "$OUT.tar.gz" "$OUT"
say "wrote $OUT/ and $OUT.tar.gz"
