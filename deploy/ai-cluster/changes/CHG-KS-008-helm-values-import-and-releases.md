# CHG-KS-008: Helm values import, and upgrade or delete installed releases

| Field | Value |
|---|---|
| ID | CHG-KS-008 |
| Modules | kubesight-backend, kubesight-frontend |
| Status | Proposed |
| Asked by | Elie, 2026-10-30: "import a yaml for the values of helm … even the secrets", "the option to delete a helm or upgrade a helm in the helm inv (the installed ones)", "deploy it also on oci" |
| Depends on | CHG-KS-007; images `kubesight-backend:1.0.7` and `kubesight-frontend:1.0.7` built by the tag pipeline |
| Prepared by | ____ |
| Approved by | ____ (name, date) |
| Run by | ____ (name, date) |

## What

A new release, `1.0.7`, with two features (commits `1722a2e2` and `7beb3d0e`).
No chart value, values file or Kubernetes object changes: only the image tag.
No database migration.

1. **Import a values.yaml into a Helm deploy.** Inventory → Helm Charts →
   Deploy: a file or pasted values.yaml fills every field, passwords included,
   and the whole file is applied. It can be kept on the chart as
   `values-<name>.yaml`, with its passwords blanked. The Cluster Builder's Apps
   step and app profiles get an Import button too.
2. **Installed Releases.** A new Inventory tab lists the Helm releases on a
   cluster, with Upgrade and Delete on each:
   - Upgrade moves a release onto a stored Inventory chart version and keeps
     the release's current values. The backend reads them with
     `helm get values` when the upgrade runs, so passwords never reach the
     browser or the approval queue.
   - Delete asks for the release name, then runs `helm uninstall`.
   - Both follow the cluster's approval rule and the `helm:upgrade` /
     `helm:uninstall` permissions.

## What this gives KubeSight on this cluster

Through CHG-KS-005 the backend's `kubesight-backend-deployer` ClusterRole can
read and delete Secrets in every namespace, which is where Helm keeps its
releases. So on `ociduai-k8s` the new tab lists **every** Helm release,
including:

- `kubesight` in `kubesight`: KubeSight itself;
- any release the platform installed in its own namespaces.

An admin, and Hermes since CHG-KS-007, could upgrade or delete any of them
from KubeSight. Two ways to keep that safe, before or after this change:

- Clusters → `ociduai-k8s` → Configure recipients: set at least one approver,
  so every Helm upgrade or delete waits for another person's approval.
- Agree with the platform team which releases KubeSight may touch. The rest is
  theirs, and they may want those namespaces left out of the deployer
  ClusterRole.

KubeSight's own release stays managed from the bastion with `helm upgrade`, as
in every change so far. Never upgrade or delete `kubesight` from the tab.

## Why

Elie wants the Helm work done from KubeSight on the AI cluster too, as in UAT.

## Steps

The images first, from a workstation: tag `1.0.7` on `7beb3d0e`, pushed to
Bitbucket. That starts the pipeline: tests, then `kubesight-backend:1.0.7` and
`kubesight-frontend:1.0.7`. Wait for it to finish.

```bash
git fetch origin && git tag -a 1.0.7 7beb3d0e -m "1.0.7: Helm values import, upgrade and delete installed releases (CHG-KS-008)"
git push origin 1.0.7 && git push bitbucket 1.0.7
```

On the bastion: pull in `~/kubesight-ai`, copy `charts/kubesight` and
`deploy/ai-cluster` to `~/kubesight`, then in `~/kubesight`:

```bash
cd ~/kubesight-ai && git pull && cd ~
cp -r ~/kubesight-ai/charts/kubesight ~/kubesight/charts/
cp -r ~/kubesight-ai/deploy/ai-cluster ~/kubesight/deploy/
cd ~/kubesight

CHG=CHG-KS-008
TAG=1.0.7
ARGS="-n kubesight -f deploy/ai-cluster/values-ai-cluster.yaml --set image.tag=$TAG --set-string commonLabels.areeba\.com/change=$CHG"

# Read only
helm template kubesight charts/kubesight $ARGS | grep -E 'image: .*kubesight-(backend|frontend):' | sort -u   # all :1.0.7
helm get values kubesight -n kubesight -o yaml > /tmp/before-values.yaml && echo "current values saved"
helm upgrade kubesight charts/kubesight $ARGS --dry-run=server > /dev/null && echo "helm dry run OK"

# After a person's explicit "yes":
helm upgrade kubesight charts/kubesight $ARGS
kubectl -n kubesight rollout status deploy/kubesight-backend
kubectl -n kubesight rollout status deploy/kubesight-frontend
kubectl -n kubesight rollout status deploy/kubesight-hermes
```

If Helm refuses with a field-manager conflict (seen in CHG-KS-004), add
`--force-conflicts` to the same `helm upgrade`.

## Check

```bash
kubectl -n kubesight get deploy -o custom-columns=NAME:.metadata.name,IMAGE:.spec.template.spec.containers[0].image
kubectl -n kubesight get pods
kubectl -n kubesight logs deploy/kubesight-backend --since=5m | grep -i -E "error|traceback" | head
# Helm is in the backend image and sees the releases the tab will list:
kubectl -n kubesight exec deploy/kubesight-backend -- helm version --short
kubectl -n kubesight exec deploy/kubesight-backend -- helm list -A
```

In KubeSight:

- Inventory shows three tabs; Installed Releases with the cluster
  `ociduai-k8s` lists the same releases as `helm list -A`.
- Inventory → Helm Charts → Deploy on a stored chart: "Have a values.yaml?"
  imports a file and fills the fields.
- Optional, on a test release only, never `kubesight`: Upgrade keeps its
  values. Afterwards `helm get values <release> -n <namespace>` still shows its
  passwords.

## Undo

The same upgrade with `TAG=1.0.6`. Nothing was stored that `1.0.6` cannot
read: a values file saved on a chart is a plain chart file, and `1.0.6` lists
it under Environment values like any other.

## Evidence

Before: ____

After: ____
