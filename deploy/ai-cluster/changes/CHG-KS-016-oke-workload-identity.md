# CHG-KS-016: KubeSight creates OKE clusters by workload identity

| Field | Value |
|---|---|
| ID | CHG-KS-016 |
| Modules | kubesight-backend, kubesight-provisioner |
| Status | Proposed |
| Asked by | Platform owner, requirements version 2 (2026-11-10), section 4 |
| Depends on | CHG-KS-015 (proof passed, proof pods deleted); the platform's state key live (Transit `kubesight-tofu-state`, role `kubesight-provisioner-state`, egress to OpenBao 8200) |
| Prepared by | ____ |
| Approved by | ____ (platform owner, name, date) |
| Run by | ____ (name, date) |

## What

A KubeSight release (backend and `kubesight-provisioner` images, one tag) and a
`helm upgrade` that switches on Oracle Cloud by OKE workload identity:

- **The backend** gets `OCI_RESOURCE_PRINCIPAL_VERSION=2.2` and
  `OCI_RESOURCE_PRINCIPAL_REGION=me-dubai-1` and signs its own OCI calls with
  Oracle's SDK signer as `kubesight-backend` (K1). API keys for Oracle are
  refused (K5), Oracle VMs are refused (K18), every cluster goes to K8s-UAT
  (K12).
- **OpenTofu runs only in Jobs** in `kubesight` as `kubesight-provisioner`,
  from the provisioner image by digest, a fixed template, non-root, read-only
  root, `ttlSecondsAfterFinished: 600`, `activeDeadlineSeconds: 5400` (K3).
- **Every create, node-pool change and destroy** waits for a second person,
  signed in with MFA and never the requester, to approve the plan's sha256;
  the Job applies only that file; approvals expire after 24 hours; the
  platform owner is emailed on every apply and destroy (K13).
- **State and plans encrypted** (K15): OpenTofu's `openbao` key provider on
  the Transit key, `enforced = true`; the Job signs in to OpenBao with its own
  token. KubeSight refuses to plan an OKE cluster while this is off.
- **Facts, not state** (K22): the Job reports the cluster's OCID, endpoint,
  node pools and NSGs; the backend never opens the state.
- **Created clusters** are reached with short-lived tokens only (K20): the
  stored kubeconfig runs `kubesight-oke-token` (no token in it). A
  provisioner Job binds the backend's subject to the ClusterRole
  `kubesight-backend` (never `cluster-admin`) and installs the admission
  policy that refuses any LoadBalancer Service without the K21 annotations.
- **Nothing reaches the provisioner through KubeSight** (K4): on this cluster,
  in `kubesight`, KubeSight refuses every kubectl and Helm change, exec, and
  Secret read; any manifest naming `kubesight-provisioner` is refused.

New Kubernetes objects from the chart:

| Object | Scope |
|---|---|
| Role + RoleBinding `kubesight-backend-provisioner-launcher` (jobs create/get/list/watch/delete, pods + logs read) | `kubesight` |
| ClusterRole + ClusterRoleBinding `kubesight-backend-tokenreview` (tokenreviews create) | cluster: the platform's grant of section 11, item 2 |
| ExternalSecret `kubesight-oke-pull` from `kv/kubesight/oke-pull` | `kubesight` |
| ConfigMap `kubesight-config`: the settings above | `kubesight` |

For the platform to apply (cluster-wide, theirs): the admission rule of K4,
`deploy/ai-cluster/oke/provisioner-admission.yaml`. It refuses provisioner Jobs
not made by the backend from the template, provisioner pods not from those
Jobs, any exec/attach/port-forward into them, and any token request for
`kubesight-provisioner` except the kubelet's.

## Before you start

1. CHG-KS-015 done: the proof passed (all four steps) and both proof pods are
   deleted.
2. A release tag built by the Bitbucket tag pipeline (backend, frontend and
   provisioner). Note the provisioner's digest from the pipeline log.
3. OpenBao: write `kv/kubesight/oke-pull` with fields
   `KUBESIGHT_OKE_PULL_USERNAME` and `KUBESIGHT_OKE_PULL_PASSWORD` (the
   read-only Nexus account).
4. At least two people who can run cluster builds have MFA on.

## Steps

On the bastion, as for the other changes (pull in `~/kubesight-ai`, copy
`charts/kubesight` and `deploy/ai-cluster` to `~/kubesight`), then in
`~/kubesight`. In `deploy/ai-cluster/values-ai-cluster.yaml`:

- `oke.provisioner.image: registry.areeba.com/kubesight-provisioner@sha256:<digest>`
- `oke.workloadIdentity.enabled: true`
- `oke.stateEncryption.enabled: true`
- the `kubesight-oke-pull` item: `enabled: true`
- `backend.extraEnvFromSecrets: [kubesight-oke-pull]`

```bash
CHG=CHG-KS-016
TAG=<the new release tag>
ARGS="-n kubesight -f deploy/ai-cluster/values-ai-cluster.yaml --set image.tag=$TAG --set-string commonLabels.areeba\.com/change=$CHG"

# Read only
helm template kubesight charts/kubesight $ARGS --show-only templates/backend-config.yaml | grep -E 'OCI_|KUBESIGHT_(OCI|PROVISIONER|TOFU_RUNNER)'
helm template kubesight charts/kubesight $ARGS --show-only templates/rbac.yaml | grep -A3 'kind: Role\|kind: ClusterRole'
helm upgrade kubesight charts/kubesight $ARGS --dry-run=server > /dev/null && echo "helm dry run OK"

# After the platform owner's explicit "yes":
helm upgrade kubesight charts/kubesight $ARGS
kubectl -n kubesight rollout status deploy/kubesight-backend
```

Then in KubeSight, Cluster Builder → Sources → Cloud accounts → Add, Oracle
Cloud (the form has no key fields now):

| Field | Value |
|---|---|
| Region | me-dubai-1 |
| Tenancy OCID | `ocid1.tenancy.oc1..aaaaaaaaz2g7phoft6kww53xootx4iwuhvwa4oaropoghofhaai4dgdjviua` |
| Compartment OCID | `ocid1.compartment.oc1..aaaaaaaajihvv42vja5ofypuvvhhxujlknsqwaolivymekmitqzhxxqesqgq` |
| VCN OCID | `ocid1.vcn.oc1.me-dubai-1.amaaaaaabqxyblyabcseiwzpcvapendzs3ai4a2xky642sbmpuv6xotvdhla` |
| API endpoint subnet | `ocid1.subnet.oc1.me-dubai-1.aaaaaaaa5qni34xzwbmw6zxji54smkwrg5qqa6kogpwiz2ablkmevfjj3m4a` |
| Worker subnet | `ocid1.subnet.oc1.me-dubai-1.aaaaaaaab7ia3gj4zpaavo4dgjnsnwqmbvj5ecmgkrwjnre4cwmcucr47hsq` |
| Load balancer subnet | `ocid1.subnet.oc1.me-dubai-1.aaaaaaaanjvtfokxg7fknvj33mbvio32p5pbbue5bito7xyxwiriueeuikvq` |
| KubeSight's network | 10.43.11.64/26 |
| Availability domain | wchx:ME-DUBAI-1-AD-1 |
| Image registry host / address | registry.areeba.com / 10.43.17.16 |

Press **Test**: it lists the compartments by workload identity (K1 "done when").

## Check

- `kubectl -n kubesight auth can-i --list --as=system:serviceaccount:kubesight:<each>` for
  every service account, and `--as=<user> --as-group=<group>` for every person
  and group bound in `kubesight` (K4): send to the platform owner.
- The acceptance tests of section 6, together with the platform owner.

## Undo

`oke.workloadIdentity.enabled: false` (and `oke.stateEncryption.enabled: false`) and the same upgrade (the Role, the
TokenReview grant and the settings go; the service account stays). Builds in
progress stop at their next step; clusters already created keep running.

## Evidence

- Upgrade: ____
- Account test: ____
- `can-i --list` outputs sent on ____
- Admission rule applied by the platform on ____
