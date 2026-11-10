# CHG-KS-015: The OKE workload-identity proof

| Field | Value |
|---|---|
| ID | CHG-KS-015 |
| Modules | kubesight-provisioner (new), kubesight-backend |
| Status | Proposed |
| Asked by | Platform owner, requirements version 2 (2026-11-10), section 5; due 2026-11-17 |
| Depends on | CHG-KS-014; a release tag whose pipeline built `kubesight-provisioner` |
| Prepared by | ____ |
| Approved by | ____ (platform owner, name, date) |
| Run by | ____ (name, date) |

## What

1. A `helm upgrade` that adds one object: the service account
   **`kubesight-provisioner`** in `kubesight` (`oke.provisioner.serviceAccount: true`
   in `deploy/ai-cluster/values-ai-cluster.yaml`). It has no Kubernetes rights
   and does not mount a token unless a pod asks for one. The OCI policy
   `kubesight-provisioning-workload` matches this exact name in this namespace.
2. Two plain pods from `deploy/ai-cluster/proof/pods.yaml`, both from the
   provisioner image (pulled by digest from `registry.areeba.com` with
   `regcred`), both sleeping, non-root (uid 10001), read-only root, no
   capabilities:
   - `kubesight-proof-provisioner`, running as `kubesight-provisioner`;
   - `kubesight-proof-backend`, running as `kubesight-backend` (the live
     backend has no OCI CLI, so it is not used).

   They end by themselves after 7 days at most (`activeDeadlineSeconds`). Using
   `kubectl exec` on them is the one exception to K4, for these two pods during
   the proof only. Write down who used them below.
3. The proof itself (section 5 of the requirements), run from those pods with
   the scripts baked into the image (`/opt/kubesight/proof`): read, create a tiny
   enhanced cluster, reach it with short-lived tokens, use it, delete it. State
   is local in the pod's `emptyDir`, with no encryption and no OpenBao (as
   section 5 says).

Nothing else changes: the backend, its configuration and its rights are as
before. `oke.workloadIdentity.enabled` stays `false` (that is CHG-KS-016, after
the proof).

## Why

To prove the platform's side before KubeSight's changes are built: that the
policy's grants work by OKE workload identity, the hub firewall's 6443 opening,
whether OKE accepts workload-identity tokens for a cluster's API, and that an
internal load balancer and a block volume work in K8s-UAT.

## Before you start

- The image: a release tag (e.g. `1.0.9`) built by the Bitbucket tag pipeline
  pushes `registry.areeba.com/kubesight-provisioner:<tag>` and prints its digest
  (`docker inspect` line at the end of "Build + push Docker images"). Put that
  digest into both pods in `deploy/ai-cluster/proof/pods.yaml`.
- The platform owner said OK to this record.
- Email the platform owner before step 2 (cluster creation) and after step 5.
- A read-only Nexus account for step 4, and an image in Nexus that has `sh` and
  `wget` (for example a mirrored `nginx:alpine`).

## Steps

On the bastion: pull in `~/kubesight-ai`, copy `charts/kubesight` and
`deploy/ai-cluster` to `~/kubesight`, then in `~/kubesight`:

```bash
CHG=CHG-KS-015
TAG=$(kubectl -n kubesight get deploy kubesight-backend -o jsonpath='{.spec.template.spec.containers[0].image}' | sed 's/.*://'); echo "TAG=$TAG"
ARGS="-n kubesight -f deploy/ai-cluster/values-ai-cluster.yaml --set image.tag=$TAG --set-string commonLabels.areeba\.com/change=$CHG"

# Read only: the only new object is the service account.
helm template kubesight charts/kubesight $ARGS --show-only templates/rbac.yaml | grep -B2 -A8 'name: kubesight-provisioner'
helm upgrade kubesight charts/kubesight $ARGS --dry-run=server > /dev/null && echo "helm dry run OK"

# After the platform owner's explicit "yes":
helm upgrade kubesight charts/kubesight $ARGS
kubectl -n kubesight get serviceaccount kubesight-provisioner

# The proof pods (digest filled in first):
grep -c 'sha256:<digest>' deploy/ai-cluster/proof/pods.yaml   # must print 0
kubectl apply -f deploy/ai-cluster/proof/pods.yaml
kubectl -n kubesight wait pod -l app.kubernetes.io/name=kubesight-proof --for=condition=Ready --timeout=5m
```

Then the proof, step by step: `deploy/ai-cluster/proof/README.md`.

## Check

- `kubectl -n kubesight get pod kubesight-proof-provisioner -o jsonpath='{.spec.serviceAccountName}'`
  prints `kubesight-provisioner`.
- Proof step 1 lists the compartments and reads the VCN and the three subnets.

## Undo

```bash
kubectl -n kubesight delete pod kubesight-proof-provisioner kubesight-proof-backend
```
Then `oke.provisioner.serviceAccount: false` and the same `helm upgrade` (keep
the service account if CHG-KS-016 follows).

## Evidence

- Upgrade: ____
- Who used the pods (name, date, what): ____
- Proof outputs: `/work/evidence` from both pods (`kubectl cp`, see the README),
  sent to the platform owner on ____
- Pods deleted on ____
