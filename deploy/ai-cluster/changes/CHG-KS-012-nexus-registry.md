# CHG-KS-012: Let the backend check images in Nexus

| Field | Value |
|---|---|
| ID | CHG-KS-012 |
| Modules | kubesight-backend |
| Status | Proposed |
| Asked by | Elie, 2026-11-06: the registry "Nexus OCI" (`10.43.17.16`) stays on "Testing…" in Integrations → Container registries |
| Depends on | CHG-KS-009 (same rule) |
| Prepared by | ____ |
| Approved by | ____ (name, date) |
| Run by | ____ (name, date) |

## What

A `helm upgrade` with one change in `deploy/ai-cluster/values-ai-cluster.yaml`:
`networkPolicies.backendEgressCIDRs` gets `10.43.17.16/32` on TCP 443 and 9443
(Nexus, `registry.areeba.com`; 9443 is the Docker connector builds push to). The Cilium rule `kubesight-backend-egress` then lets
the backend call the registry's Docker API (`https://10.43.17.16/v2/…`). By
address, not name, so it holds whether the registry is entered as the IP or
as `registry.areeba.com`. No image change; the backend pod does not restart.

## Why

The registry linked to `ociduai-k8s` is set to **block a deploy when the image
is missing**. KubeSight checks the image in Nexus before every deploy to that
cluster. While the backend cannot reach Nexus, that check cannot pass, so the
registry test hangs and deploys to the cluster are blocked.

## Before you start

Nothing from the platform: the nodes already pull from `registry.areeba.com`
(`10.43.17.16`, 2026-10-28), so no firewall opening is needed. If the
registry's host in KubeSight is ever given a port (`10.43.17.16:8443`, say),
add that port here as well.

Pushing on 9443 needs nothing more on our side: BuildKit, which pushes,
already allows 443, 8443, 8082, 5000 and 9443 (`buildkit.egressPorts`), and a
build pod adds its target registry's port to its own per-run rule.

## Steps

On the bastion: pull in `~/kubesight-ai`, copy `charts/kubesight` and
`deploy/ai-cluster` to `~/kubesight`, then in `~/kubesight`:

```bash
CHG=CHG-KS-012
TAG=$(kubectl -n kubesight get deploy kubesight-backend -o jsonpath='{.spec.template.spec.containers[0].image}' | sed 's/.*://'); echo "TAG=$TAG"
ARGS="-n kubesight -f deploy/ai-cluster/values-ai-cluster.yaml --set image.tag=$TAG --set-string commonLabels.areeba\.com/change=$CHG"

# Read only
helm template kubesight charts/kubesight $ARGS | grep -A5 '10.43.17.16'      # toCIDR, ports 443 and 9443
helm upgrade kubesight charts/kubesight $ARGS --dry-run=server > /dev/null && echo "helm dry run OK"

# After a person's explicit "yes":
helm upgrade kubesight charts/kubesight $ARGS
```

## Check

```bash
kubectl -n kubesight exec -i deploy/kubesight-backend -- python - <<'EOF'
import socket
for port in (443, 9443):
    socket.create_connection(("10.43.17.16", port), timeout=5).close(); print(port, "reachable")
EOF
```

Then Integrations → Container registries → **Test** on "Nexus OCI": it
answers instead of staying on "Testing…", and Last test shows a time.

- `reachable`, but the test reports a certificate error: the registry's
  certificate is for `registry.areeba.com`, not the IP. Set the registry's
  host to `registry.areeba.com` (same address, so this rule still covers
  it), or give KubeSight the CA that signed it.
- `reachable`, but 401: the registry's username or password.

## Undo

Set `backendEgressCIDRs: []`, then the same upgrade.

## Evidence

Upgrade: ____
