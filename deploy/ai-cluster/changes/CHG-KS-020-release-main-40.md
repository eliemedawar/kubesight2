# CHG-KS-020: KubeSight `main-40` (Resources redesign, Alt shortcuts)

| Field | Value |
|---|---|
| ID | CHG-KS-020 |
| Modules | kubesight-backend, kubesight-frontend (chart: kubesight-backend RBAC, network rules) |
| Status | Proposed |
| Asked by | Elie, 2026-11-17: "tag is main-40" (after the Resources redesign and the Alt shortcuts) |
| Depends on | CHG-KS-018 done, or skipped for this run (see "Before you start", 3) |
| Prepared by | ____ |
| Agreed by the platform | ____ (name, date): needed for the RBAC rule in What, 2 |
| Approved by | ____ (name, date) |
| Run by | ____ (name, date) |

## What

A `helm upgrade` of the release `kubesight` to the backend and frontend
images `main-40`, built by KubeSight CI from Bitbucket `main` (`f8f5938b` or
later). The provisioner stays pinned by digest in the values (`main-21`).

1. **New in the app** since `main-17`:
   - **Resources**: kinds grouped in a side rail (Activity, Workloads,
     Network, Config, Storage) with counts and problem badges, a "Needs
     attention" strip, and six new lists: Events, Autoscalers, Network
     policies, Volume claims, Volumes, Storage classes (View YAML and
     Describe only). When the cluster refuses KubeSight a list (RBAC), the
     page says so instead of showing it empty.
   - **Shortcuts** are Alt+letter (⌥ on a Mac): Alt+K jump palette, Alt+G
     Hermes chat (were Ctrl+K and Ctrl+G), Alt+N namespace picker, Alt+C
     cluster picker.
   - CI: a Deploy stage can take its Helm chart from the service's own
     repository, a "Chart check" stage, a registry value of its own; Semgrep
     can scan chosen directories; one ticket deploying several images starts
     one build.
   - OKE (Cluster Builder): node pools (sizes, labels, taints, fault domains,
     image), day-two node pools, Hermes creates OKE clusters and node pools
     from tickets with one approval, withdraw your own plan or destroy.
   - No typed confirmation phrase for applying YAML, Helm deploys, adding an
     app, upgrades and rollbacks (the server checks are unchanged).
2. **Chart and values** that come with this upgrade if they are not on the
   cluster yet (step 2 shows which are):
   - ClusterRole `kubesight-backend-reader`: + get/list/watch
     `networkpolicies` (CHG-KS-019). Read only, every namespace.
   - Egress from the backend to the UAT Kubernetes API load balancer
     `10.4.27.15:6443`, so UAT can be managed from here with its kubeconfig
     (`58f11ea4`). The values file named it CHG-KS-019 by mistake; this
     record covers it.
   - `oke.workloadIdentity.backendRole: cluster-admin`: KubeSight gets full
     rights on the OKE clusters it creates (Hussein, 2026-11-13). Existing
     clusters keep their role until "Give KubeSight full rights" on their
     build page.
   - BuildKit's claim template carries fixed labels only (`0416c579`); this
     matters only once BuildKit is a StatefulSet (CHG-KS-018).
3. **Database**: no migration. Two lists of allowed values grow (a CI stage
   type `chart_check`, a merge-check tool `chart`); no column or table
   changes.

## Before you start

On the bastion:

```bash
cd ~/kubesight-ai && git pull && git log --oneline -1        # f8f5938b or later
cp -r ~/kubesight-ai/charts/kubesight ~/kubesight/charts/
cp -r ~/kubesight-ai/deploy/ai-cluster ~/kubesight/deploy/
cd ~/kubesight
```

1. **The images exist**: in KubeSight CI, the build `main-40` shows the
   commit it built (`f8f5938b` or later) and pushed
   `registry.areeba.com/kubesight-backend:main-40` and
   `kubesight-frontend:main-40`.
2. **What runs now** (write it in Evidence; it is the way back):
   ```bash
   kubectl -n kubesight get deploy kubesight-backend kubesight-frontend \
     -o jsonpath='{range .items[*]}{.metadata.name}{"  "}{.spec.template.spec.containers[0].image}{"\n"}{end}'
   helm history kubesight -n kubesight | tail -3
   ```
3. **CHG-KS-018** (BuildKit on volumes):
   ```bash
   kubectl -n kubesight-build get statefulset,deploy buildkitd
   ```
   - StatefulSet: CHG-KS-018 is done, go on.
   - Deployment: either run CHG-KS-018 first (it scales the old Deployment
     to 0 first, for the quota), or add
     `--set buildkit.persistence.enabled=false` to `ARGS` below so this
     upgrade leaves BuildKit as it is.

## Steps

```bash
CHG=CHG-KS-020
TAG=main-40
ARGS="-n kubesight -f deploy/ai-cluster/values-ai-cluster.yaml --set image.tag=$TAG --set-string commonLabels.areeba\.com/change=$CHG"
# + --set buildkit.persistence.enabled=false   (only if CHG-KS-018 has not run)

# Read only: exactly what the upgrade changes
helm get manifest kubesight -n kubesight > now.yaml
helm template kubesight charts/kubesight $ARGS > next.yaml
diff <(grep -v 'areeba.com/change' now.yaml) <(grep -v 'areeba.com/change' next.yaml) | less
#   expected: image tags -> main-40, chart version/annotations, and whichever
#   items of What, 2 are not on the cluster yet. Anything else: stop and ask.
helm upgrade kubesight charts/kubesight $ARGS --dry-run=server > /dev/null && echo "helm dry run OK"

# After a person's explicit "yes" (and the platform's agreement for What, 2):
helm upgrade kubesight charts/kubesight $ARGS
kubectl -n kubesight rollout status deploy/kubesight-backend --timeout=10m
kubectl -n kubesight rollout status deploy/kubesight-frontend --timeout=5m
```

The backend restarts (rolling); KubeSight is briefly read-only while the new
pod starts. If the upgrade stops on a field conflict from an earlier
`kubectl apply` (for example CHG-KS-019's ClusterRole), run it again with
`--force-conflicts`.

## Check

```bash
helm history kubesight -n kubesight | tail -1                                   # deployed, CHG-KS-020
kubectl -n kubesight get deploy kubesight-backend kubesight-frontend -o wide    # main-40, ready
SA=system:serviceaccount:kubesight:kubesight-backend
kubectl auth can-i list networkpolicies -n ai-guard --as=$SA                    # yes
```

In KubeSight (`https://kubesight.areeba.com`, hard refresh):

1. **Resources** shows the side rail; cluster `ociduai-k8s`, namespace
   `ai-guard` › **Network policies** lists the namespace's policies (the same
   as `kubectl get networkpolicies -n ai-guard`).
2. **Volume claims**, **Storage classes** and **Events** show rows;
   **Volumes** opens on the volumes claimed from `ai-guard`.
3. Alt+K opens the jump palette, Alt+N the namespace picker.

## Undo

```bash
helm history kubesight -n kubesight                 # the revision before CHG-KS-020
helm rollback kubesight <that revision> -n kubesight
```

No migration ran, so the previous images work with the database as it is.
The rollback also takes back the chart items of What, 2 that this upgrade
added.

## Evidence

Before (images and `helm history` from "Before you start", 2): ____

After: ____
