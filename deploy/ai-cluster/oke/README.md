# OKE by workload identity: where each requirement is met

The platform's requirements, version 2 (2026-11-10). Branch `oci-workload-identity`.
Runs: CHG-KS-015 (proof), CHG-KS-016 (switch on, once the platform's state key is live).

| # | Status | Where |
|---|---|---|
| K1 | Built | `clouds/oci_wi.py`: account type with no user/fingerprint/key fields; `signer()` = `oci.auth.signers.get_oke_workload_identity_resource_principal_signer()` (SDK `oci>=2.111.0`); chart sets `OCI_RESOURCE_PRINCIPAL_VERSION=2.2`, `..._REGION`. Test button lists compartments. |
| K2 | Built | Rendered provider `{"auth": "OKEWorkloadIdentity", "region": ...}`; the Job strips every `OCI_*` key variable (`kubesight_provision.tofu_env`). |
| K3 | Built | `provisioning/remote.py`: Jobs as `kubesight-provisioner`, fixed template (`job_manifest`), image by digest only, non-root, read-only root, `ttlSecondsAfterFinished`, `backoffLimit: 0`, deadline 90 min. Image: `provisioner/Dockerfile` (OpenTofu 1.13.9 checksummed, `oracle/oci` 9.11.0 in an unpacked filesystem mirror + lock file, OCI CLI 3.96.0, kubectl v1.36.6). |
| K4 | Built (KubeSight side) / platform applies the admission rule | `api/provisioner_guard.py` refuses, on KubeSight's own cluster in `kubesight`, every kubectl/Helm change, exec and Secret read (also across all namespaces); on every cluster any manifest, kubectl's or Helm's rendered output (post-renderer `kubesight-helm-guard`), that runs as the provisioner or makes a token Secret for it. `oke/provisioner-admission.yaml`: the platform's admission rule (Jobs only from the backend's template with the backend URL pinned, pods only from those Jobs via the job controller, no exec/attach/port-forward, no token requests but the kubelet's, no legacy token Secret). The backend still decides which plan is approved: the two-person rule rests on KubeSight and its database. |
| K5 | Built | With `KUBESIGHT_OCI_AUTH_MODES=workload_identity` an Oracle account with key fields is refused, and an older key account is never used for a call. |
| K6 | Built | `type = ENHANCED_CLUSTER`. |
| K7 | Built | Endpoint, worker and load balancer subnets taken by OCID from the account; the compartment Network is never listed. |
| K8 | Built | `is_public_ip_enabled = false`; subnets that allow public IPs are refused at plan time. |
| K9 | Built | `VM.Standard.E6.Flex` is the only shape offered and accepted. |
| K10 | Built | `FLANNEL_OVERLAY`, pods `10.244.0.0/16`, services `10.96.0.0/16`; a pod subnet is refused. |
| K11 | Built | `oci_wi.nsg_rules`: two NSGs, the rules listed in K11 (node ports 30000-32767 and 10256 from the LB subnet), no 22, no `0.0.0.0/0` (refused in code). |
| K12 | Built | `KUBESIGHT_OCI_COMPARTMENT_LOCK`; another compartment is refused at account save and at build save. |
| K13 | Built | `service.approve_plan_hash`: second person, MFA enrolled, never the requester (admins included), exact sha256, plan older than `approvalHours` refused; the apply Job checks the hash again; new plan = new approval; email to `notifyEmails` on every apply and destroy. |
| K14 | **Open** | Separate piece of work: the audit findings, CSP, token storage, root containers; rotate the committed `ksa_`/Telegram tokens and purge them from history. |
| K15 | Built; on with CHG-KS-016 | `TF_ENCRYPTION` with the `openbao` key provider, `aes_gcm`, `enforced = true` for state and plan; the Job signs in to OpenBao with its own service-account token (as the platform's example; an audience-bound token would need the OpenBao role's `audience` set); plain states migrate once via `fallback`. KubeSight refuses to plan while encryption is off. |
| K16 | Built | Node cloud-init: Oracle's bootstrap, then `10.43.17.16 registry.areeba.com`. The read-only Nexus account (`kv/kubesight/oke-pull`) is put on each new cluster through its API (`kubesight-registry` in `default`), never in OpenTofu. |
| K17 | Built | `ignore_changes` on every `defined_tags` path listed; freeform `owner` and `expiry` required on every cluster. |
| K18 | Built | `KUBESIGHT_OCI_VMS=false`: Oracle VM requests refused. |
| K19 | Built | `remote.audit`: one JSON line per request, plan, approval, refusal, apply, destroy, facts, bind; never a token, key, plan or state. Check in Loki after CHG-KS-016. |
| K20 | Built | Stored kubeconfig runs `kubesight-oke-token` (no token); backend's subject from SelfSubjectReview, bound by a provisioner Job to ClusterRole `kubesight-backend` (no cluster-admin, no admission or cluster RBAC writes, no escalate/bind/impersonate). |
| K21 | Built | A ValidatingAdmissionPolicy on each created cluster refuses LoadBalancer Services without exactly the five annotations. |
| K22 | Built | `remote.record_facts`: fixed list only; Job authenticates with a projected token (audience `kubesight-backend`, 10 min) checked by TokenReview + pod-name of the waiting step; the OpenTofu HTTP backend goes through a loopback proxy in the Job that adds a fresh token per request. |
| K23 | Built | The Job removes `TF_LOG*`/`OCI_CLI_DEBUG` from every child's environment; no `set -x`. |

## The backend's role on a created cluster (K20)

`kubesight-backend`: read everything; create/update/patch/delete pods, services,
endpoints, configmaps, secrets, serviceaccounts, PVCs, namespaces, quotas,
limit ranges, events, deployments/replicasets/statefulsets/daemonsets (and
scale), jobs, cronjobs, ingresses, network policies, HPAs, PDBs, namespaced
roles and role bindings; pods/exec, port-forward and eviction; patch nodes
(cordon). Not: ClusterRoles/ClusterRoleBindings, admission objects, CRDs,
escalate, bind, impersonate. A second admission policy on each created cluster
(`kubesight-backend-limits`) refuses the backend any write in `kube-system` and
any service-account token Secret, so its rights cannot become a privileged
account's. Built-in `edit` is the fallback if the proof's
step 3.4 shows the provisioner may not create a ClusterRole
(`oke.workloadIdentity.backendRole: edit`).

## Kubernetes version (K20)

v1.36.4, the AI cluster's; KubeSight offers what OKE offers.
