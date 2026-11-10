# The OKE proof, step by step

The platform's requirements (version 2), section 5. It checks the platform's
side, not KubeSight's code. Due **2026-11-17**, all four steps. Run CHG-KS-015
first (the service account and the two pods).

Everything runs inside the two proof pods, as their own service accounts, by
OKE workload identity: there is no key, no stored token and no kubeconfig with a
token anywhere. The scripts and the OpenTofu configuration are in the image at
`/opt/kubesight/proof`. Every output goes to `/work/evidence` in the pod.

```bash
P="kubectl -n kubesight exec -it kubesight-proof-provisioner --"
B="kubectl -n kubesight exec -it kubesight-proof-backend --"
```

## Step 1: read

```bash
$P /opt/kubesight/proof/step1-read.sh
```

It lists the compartments and reads the VCN and the three subnets by OCID. A
refusal is printed word for word and kept in `/work/evidence/step1-*.err`.

## Step 2: create

Email the platform owner first. Then:

```bash
$P /opt/kubesight/proof/step2-create.sh emedawer@areeba.com 2026-11-21
```

(Owner = a work email, expiry = `YYYY-MM-DD`.) It runs `tofu init` and
`tofu plan`, shows the plan, and creates only after you type `yes`: an ENHANCED
cluster `kubesight-proof` v1.36.4 in K8s-UAT, endpoint/workers/load balancers
in their three subnets, flannel, two NSGs with no SSH and no `0.0.0.0/0`, one
node pool of 2 × E6.Flex (2 OCPUs, 16 GB), the Oracle Linux 9 OKE image, the
cloud-init that adds `10.43.17.16 registry.areeba.com`, and `ignore_changes` on
every `defined_tags`. Allow 15 to 25 minutes.

The cluster's OCID is printed and kept in `/work/proof/cluster-id` as soon as it
exists. On any refusal the `NotAuthorizedOrNotFound` lines are collected in
`/work/evidence/step2-refusals.txt`: send them to the platform owner word for
word and stop.

## Step 3: short-lived tokens (in this order)

```bash
ID=$($P cat /work/proof/cluster-id | tr -d '\r')

# 3.1 backend pod: works with no binding; prints the backend's subject
$B env CLUSTER_ID=$ID /opt/kubesight/proof/step3.sh whoami
# 3.2 provisioner pod
$P /opt/kubesight/proof/step3.sh whoami
$P /opt/kubesight/proof/step3.sh can-i
# 3.3 provisioner pod: bind the subject from 3.1 to the built-in view
$P /opt/kubesight/proof/step3.sh bind '<username from 3.1>'
# 3.4 provisioner pod: try a custom ClusterRole (may be refused; we need to know)
$P /opt/kubesight/proof/step3.sh role
# 3.5 backend pod
$B env CLUSTER_ID=$ID /opt/kubesight/proof/step3.sh can-i
```

Each pod writes its own kubeconfig with `oci ce cluster create-kubeconfig
--token-version 2.0.0 --kube-endpoint PRIVATE_ENDPOINT`; the file holds no
token, and every token is made by `oci ce cluster generate-token` with the pod's
own identity. A timeout in 3.1 is the firewall: send the platform owner the
endpoint address (`$P cat /work/evidence/step2-outputs.txt`) and the time.

**If OKE refuses the workload-identity token, stop here.** Tell the platform
owner, keep the cluster for up to two working days so they can look at it with
you, then run step 5.

## Step 4: use it

```bash
$P /opt/kubesight/proof/step4-use.sh registry.areeba.com/<repository>/nginx:alpine
```

It asks for the read-only Nexus account (the password is not shown, not
logged, and goes to the created cluster only as a pull Secret), then creates a
Deployment from that image, a 50 GiB `oci-bv` claim it mounts, and an internal
LoadBalancer Service with the K21 annotations. It waits for the load
balancer's address (expected in `10.43.20.0/25`) and checks, from a pod inside
the created cluster, that the address answers and that the volume is mounted
and writable. Then:

```bash
$P /opt/kubesight/proof/step4-use.sh cleanup
```

(Service first, so its load balancer goes; then the claim and the namespace.)

## Step 5: delete the cluster

```bash
$P /opt/kubesight/proof/step5-destroy.sh
```

If the pod was lost, delete by OCID: `oci ce cluster delete --cluster-id <ocid> --force`
from a new provisioner pod. Email the platform owner when it is gone.

## Send the platform owner

```bash
mkdir -p ~/proof-evidence
kubectl -n kubesight cp kubesight-proof-provisioner:/work/evidence ~/proof-evidence/provisioner
kubectl -n kubesight cp kubesight-proof-backend:/work/evidence ~/proof-evidence/backend
```

- every refusal word for word (`*.err`, `step2-refusals.txt`);
- the five outputs of step 3 (`step3-*`);
- step 4's `step4-service.yaml`, `step4-lb-probe.txt`, `step4-volume.txt`, `step4-images.txt`;
- `notes.txt` from both pods (times of every step).

Then delete the two pods (CHG-KS-015, Undo).
