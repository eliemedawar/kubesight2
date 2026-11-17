# CHG-KS-019: Let KubeSight read network policies

| Field | Value |
|---|---|
| ID | CHG-KS-019 |
| Modules | kubesight-backend |
| Status | Proposed. CHG-KS-020 (release `main-40`) carries the same rule: if 020 runs, skip this one |
| Asked by | Elie, 2026-11-17: Resources › Network policies shows none for `ai-guard`, "why no network policies in oci even if we have" |
| Depends on | CHG-KS-005 (the ClusterRole `kubesight-backend-reader` this change adds one rule to) |
| Prepared by | ____ |
| Agreed by the platform | ____ (name, date) |
| Approved by | ____ (name, date) |
| Run by | ____ (name, date) |

## What

Add one read-only rule to the existing ClusterRole `kubesight-backend-reader`:
get, list and watch on `networkpolicies` (`networking.k8s.io`). The rule now
reads:

```yaml
- apiGroups: [networking.k8s.io]
  resources: [ingresses, ingressclasses, networkpolicies]
  verbs: [get, list, watch]
```

Nothing else changes: no new object, no write right, no pod restart, the same
binding to `kubesight:kubesight-backend`. The rule is in
[`charts/kubesight/templates/rbac.yaml`](../../../charts/kubesight/templates/rbac.yaml).

## Why

The Resources page now lists network policies (with volume claims, volumes,
storage classes, autoscalers and events). On this cluster the reader role from
CHG-KS-005 grants `networking.k8s.io` only Ingresses and IngressClasses, so
Kubernetes refuses `kubectl get networkpolicies` and the list stays empty,
though `ai-guard` and `kubesight` have policies. The other new lists are
already covered: claims, storage classes and events by the reader role,
volumes and autoscalers by the deployer role.

Releases from 2026-11-17 on show "KubeSight is not allowed to list
networkpolicies on this cluster" instead of an empty list. Earlier ones show
"No network policies".

## Know before approving

- **Read only, every namespace.** KubeSight can then see every
  NetworkPolicy, the platform's too. A policy names pods, namespaces, CIDRs and
  ports; it holds no secret. KubeSight shows a non-admin only the policies of
  namespaces their KubeSight role reaches.
- **Cilium policies are not part of this change.** `CiliumNetworkPolicy` and
  `CiliumClusterwideNetworkPolicy` (`cilium.io`) are other kinds. KubeSight
  does not list them yet, so a rule that exists only as a Cilium policy does
  not appear on the page after this change either.

## Before you start

On the bastion, in `~/kubesight` (pull in `~/kubesight-ai`, then copy
`charts/kubesight` and `deploy/ai-cluster` across, as for CHG-KS-018).

1. `kprodai` may change the role (granting needs the right itself, or
   `escalate`):
   ```bash
   kubectl auth can-i update clusterroles; kubectl auth can-i list networkpolicies -A
   ```
   Either one `no`: stop; the platform applies the file from step 2 instead.
2. Today the service account is refused (see Evidence):
   ```bash
   kubectl auth can-i list networkpolicies -n ai-guard --as=system:serviceaccount:kubesight:kubesight-backend   # no
   ```

## Steps

This updates only the ClusterRole. A full `helm upgrade` with today's chart
would also roll out any other change still waiting (for example CHG-KS-018), so
the role is rendered from the chart and applied alone. The next upgrade of the
release carries the same rule, so the two never disagree.

```bash
CHG=CHG-KS-019
TAG=$(kubectl -n kubesight get deploy kubesight-backend -o jsonpath='{.spec.template.spec.containers[0].image}' | sed 's/.*://'); echo "TAG=$TAG"
ARGS="-n kubesight -f deploy/ai-cluster/values-ai-cluster.yaml --set image.tag=$TAG --set-string commonLabels.areeba\.com/change=$CHG"

# Read only: the reader role alone, and what would change
helm template kubesight charts/kubesight $ARGS --show-only templates/rbac.yaml \
  | awk 'BEGIN{RS="---\n"} /kind: ClusterRole\n/ && /-backend-reader\n/ {print "---"; print}' > reader-role.yaml
grep -n "name:\|networkpolicies" reader-role.yaml          # kubesight-backend-reader, networkpolicies  (awk: GNU awk, the RHEL default)
kubectl diff -f reader-role.yaml                            # only the networking.k8s.io rule (+ the change label)

# After a person's explicit "yes" and the platform's agreement:
kubectl apply -f reader-role.yaml
```

`kubectl apply` may warn that the object lacks the
`kubectl.kubernetes.io/last-applied-configuration` annotation; that is
expected for an object Helm created, and kubectl adds it.

## Check

```bash
SA=system:serviceaccount:kubesight:kubesight-backend
kubectl auth can-i list networkpolicies -n ai-guard --as=$SA     # yes
kubectl auth can-i delete networkpolicies -n ai-guard --as=$SA   # no
kubectl get networkpolicies -n ai-guard                          # the policies KubeSight should show
```

In KubeSight: **Resources** › cluster `ociduai-k8s` › namespace `ai-guard` ›
**Network policies** lists the same policies (refresh the list once; the
earlier empty answer is cached for a few seconds).

## Undo

Apply the previous rule (`resources: [ingresses, ingressclasses]`), or run
the same steps with a chart from before this change. KubeSight's list then
shows "not allowed" (or empty, on older releases) again.

## Evidence

Before, 2026-11-17: Resources › Network policies on `ociduai-k8s` / `ai-guard`
shows "No network policies in the ai-guard namespace", while the namespace has
policies.

After: ____
