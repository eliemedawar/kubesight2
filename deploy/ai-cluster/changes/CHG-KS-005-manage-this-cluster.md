# CHG-KS-005: Let KubeSight show and manage the cluster it runs in

| Field | Value |
|---|---|
| ID | CHG-KS-005 |
| Modules | kubesight-backend |
| Status | Proposed, waiting for the platform's agreement (the first cluster-wide objects of this release) |
| Asked by | Elie, 2026-10-30: "i want it to manage this cluster", "same as uat" |
| Depends on | CHG-KS-002 (the release and the service account `kubesight:kubesight-backend`) |
| Prepared by | ____ |
| Agreed by the platform | ____ (name, date) |
| Approved by | ____ (name, date) |
| Run by | ____ (name, date) |

## What

In `deploy/ai-cluster/values-ai-cluster.yaml`: `rbac.clusterWide: false` →
`true`. The release then creates, as in UAT, two ClusterRoles and two
ClusterRoleBindings, all bound to the service account `kubesight-backend` in
`kubesight` only:

| Object | Grants |
|---|---|
| ClusterRole + binding `kubesight-backend-reader` | read only (get, list, watch): nodes, pods and their logs, services, endpoints, namespaces, events, PVCs, ConfigMaps, StorageClasses, PodDisruptionBudgets, APIServices, Ingresses and IngressClasses, Deployments, ReplicaSets, StatefulSets, DaemonSets, Jobs, CronJobs; get on `nodes/proxy` (the kubelet's disk figures); node and pod metrics |
| ClusterRole + binding `kubesight-backend-deployer` | create, update, patch, delete: pods, services, ConfigMaps, **Secrets**, service accounts, PVCs, **PersistentVolumes**, Deployments, ReplicaSets, StatefulSets, DaemonSets, Jobs, CronJobs, Ingresses, HorizontalPodAutoscalers; create **namespaces** (never delete); `pods/exec` |

The exact rules are in
[`charts/kubesight/templates/rbac.yaml`](../../../charts/kubesight/templates/rbac.yaml).
Each object carries `areeba.com/module=kubesight-backend` and
`areeba.com/change=CHG-KS-005`, and belongs to the Helm release: undoing is
flipping the switch back.

Nothing else changes. The backend already treats this cluster as
`ociduai-k8s` (`backend.inClusterContextName`) and already may reach the API
server; until now every request was refused by Kubernetes, so the cluster
showed empty. `backend.autoUpgrade` stays `false`: OKE upgrades itself.

## Why

KubeSight is to show and manage `OCIDUAI-K8S` itself (nodes, workloads, logs,
deploys, restarts, scaling, rollbacks), the same way UAT manages the cluster
it runs in.

## What the platform is asked to accept

This is the first exception to "no cluster-wide objects" in this release, and
the deployer role reaches past what the platform's rules otherwise give us:

- **Any namespace.** The deployer may change workloads in every namespace,
  the platform's included (`kube-system`, `databases`, OpenBao...).
- **Secrets.** It may read and write Secrets in any namespace. Here secrets
  are meant to come only from OpenBao through ExternalSecrets.
- **Namespaces and PersistentVolumes.** It may create them; the platform owns
  both.

What holds KubeSight back is KubeSight itself: its users' roles, and the
cluster approval gate (a change to a gated cluster waits for another person's
approval, admins and Hermes included). Turn the gate on for `ociduai-k8s`
(step 4) before anyone deploys there.

If the platform agrees to the reader but not the deployer, or to the deployer
only in named namespaces, say so here; the chart then needs a narrower switch,
to be built before this change runs.

## Before you start

On the bastion, in `~/kubesight` (pull in `~/kubesight-ai`, then copy
`charts/kubesight` and `deploy/ai-cluster` across, as for CHG-KS-004).

1. `kprodai` may create the objects. Helm creates them as `kprodai`, and
   Kubernetes lets a person grant only rights they hold themselves (or
   `escalate`/`bind`):
   ```bash
   for v in "create clusterroles" "create clusterrolebindings" "escalate clusterroles" "bind clusterroles"; do
     printf '%-28s ' "$v"; kubectl auth can-i $v
   done
   ```
   Any `no`: stop. The platform then applies the objects (see "If the
   platform applies them" below).
2. No object of these names exists yet:
   ```bash
   kubectl get clusterrole,clusterrolebinding | grep kubesight-backend   # nothing
   ```
3. Today the service account can read nothing cluster-wide (2026-10-30, see
   Evidence).

## Steps

```bash
CHG=CHG-KS-005
TAG=$(kubectl -n kubesight get deploy kubesight-backend -o jsonpath='{.spec.template.spec.containers[0].image}' | sed 's/.*://'); echo "TAG=$TAG"
ARGS="-n kubesight -f deploy/ai-cluster/values-ai-cluster.yaml --set image.tag=$TAG --set-string commonLabels.areeba\.com/change=$CHG"

# Read only
grep -n "clusterWide" deploy/ai-cluster/values-ai-cluster.yaml                        # clusterWide: true
helm template kubesight charts/kubesight $ARGS | grep -B1 -A3 '^kind: ClusterRole'     # 2 ClusterRoles, 2 bindings
helm upgrade kubesight charts/kubesight $ARGS --dry-run=server > /dev/null && echo "helm dry run OK"

# After a person's explicit "yes" and the platform's agreement:
helm upgrade kubesight charts/kubesight $ARGS
```

No pod restarts: only RBAC objects are added. If the upgrade stops on a
conflict from an earlier `kubectl edit`, run it again with `--force-conflicts`.

4. In KubeSight, as an admin: **Clusters** → `ociduai-k8s` → **Configure
   recipients**: set the approvers, so changes there wait for a second person.

### If the platform applies them

```bash
helm template kubesight charts/kubesight $ARGS \
  --show-only templates/rbac.yaml > kubesight-backend-cluster-rbac.yaml
```

Hand over the file without the `ServiceAccount` at its top (the release
already owns it). The platform applies it; `rbac.clusterWide` then stays
`false` in the values, so Helm does not try to take the objects over.

## Check

```bash
SA=system:serviceaccount:kubesight:kubesight-backend
for v in "list nodes" "get nodes/proxy" "list pods -A" "get pods/log -A" "create deployments -A" "patch deployments -A" "create pods/exec -A" "delete namespaces"; do
  printf '%-24s ' "$v"; kubectl auth can-i $v --as=$SA
done
# all yes, except "delete namespaces": no
kubectl get clusterrole,clusterrolebinding -l areeba.com/change=CHG-KS-005   # 4 objects
helm history kubesight -n kubesight | tail -1                               # deployed
```

In KubeSight: the cluster `ociduai-k8s` shows its 3 nodes with CPU, memory and
disk, and its namespaces; **Resources** lists pods in, for example,
`kube-system`; a pod's logs open.

## Undo

Put back `rbac.clusterWide: false`, then the same upgrade. Helm deletes the
four objects; KubeSight shows the cluster empty again. (If the platform
applied them: the platform deletes them.)

## Docs updated with this change

`README.md` (cluster-wide needs, mapping, changes table),
`modules/kubesight-backend/docs/explanation.md` and `reference.md`,
`modules/kubesight-ci/docs/explanation.md`.

## Evidence

Before, 2026-10-30, from the bastion:

```
$ SA=system:serviceaccount:kubesight:kubesight-backend; kubectl auth can-i <verb> --as=$SA
list nodes             no
list pods -A           no
get secrets -A         no
delete pods -A         no
create deployments -A  no
patch nodes            no
$ kubectl get clusterrolebindings -o wide | grep -E 'kubesight|system:serviceaccounts|system:authenticated'
system:basic-user, system:discovery, system:public-info-viewer, system:service-account-issuer-discovery
  (the standard discovery bindings only; nothing names kubesight)
```

After: ____
