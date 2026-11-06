# CHG-KS-011: Let KubeSight reach Bitbucket Cloud

| Field | Value |
|---|---|
| ID | CHG-KS-011 |
| Modules | kubesight-backend (rule); kubesight-ci (firewall only) |
| Status | Proposed. Waiting for the firewall opening to Bitbucket Cloud |
| Asked by | Elie, 2026-11-06: "fix the connection to bitbucket too" |
| Depends on | CHG-KS-009 (same rule); the firewall opening below |
| Prepared by | ____ |
| Approved by | ____ (name, date) |
| Run by | ____ (name, date) |

## What

A `helm upgrade` with one change in `deploy/ai-cluster/values-ai-cluster.yaml`:
`networkPolicies.backendEgressFQDNs` gets `api.bitbucket.org` and
`bitbucket.org`, both on 443. The Cilium rule `kubesight-backend-egress` then
lets the backend reach those two names. No image change; the backend pod does
not restart.

Who talks to Bitbucket, and what each needs:

| From | To | For | Our rule | Firewall |
|---|---|---|---|---|
| backend (`kubesight`) | `api.bitbucket.org:443` | checking a credential, listing branches and tags, build statuses, pull-request comments, merge checks | **this change** | needed |
| backend (`kubesight`) | `bitbucket.org:443` | Git over HTTPS (Helm chart import from Git) | **this change** | needed |
| build pods, analysis Jobs (`kubesight-ci`) | `bitbucket.org:443` | `git clone` | already open: their per-run rule allows 443 | needed |

Bitbucket calling KubeSight (push and pull-request webhooks) is not part of
this. KubeSight is reachable only on the internal load balancer, so those
webhooks cannot arrive from Bitbucket Cloud. That is a separate decision.

## Why

Without it, everything in KubeSight that reaches Bitbucket hangs and fails
(as SMTP did before CHG-KS-009): the branch list in Register service, Hermes
analysis, builds cloning the repository, merge checks.

## Before you start

The platform confirms the firewall lets the nodes reach Bitbucket Cloud on
TCP 443: `bitbucket.org` and `api.bitbucket.org`. Bitbucket's addresses are
in Atlassian's published list (`https://ip-ranges.atlassian.com`, product
`bitbucket`), if the firewall cannot allow by name.

## Steps

On the bastion: pull in `~/kubesight-ai`, copy `charts/kubesight` and
`deploy/ai-cluster` to `~/kubesight`, then in `~/kubesight`:

```bash
CHG=CHG-KS-011
TAG=$(kubectl -n kubesight get deploy kubesight-backend -o jsonpath='{.spec.template.spec.containers[0].image}' | sed 's/.*://'); echo "TAG=$TAG"
ARGS="-n kubesight -f deploy/ai-cluster/values-ai-cluster.yaml --set image.tag=$TAG --set-string commonLabels.areeba\.com/change=$CHG"

# Read only
helm template kubesight charts/kubesight $ARGS | grep -A4 'bitbucket.org'           # both names, port 443
helm upgrade kubesight charts/kubesight $ARGS --dry-run=server > /dev/null && echo "helm dry run OK"

# After a person's explicit "yes":
helm upgrade kubesight charts/kubesight $ARGS
kubectl -n kubesight get ciliumnetworkpolicy kubesight-backend-egress -o yaml | grep -A4 bitbucket
```

## Check

From the backend pod:

```bash
kubectl -n kubesight exec -i deploy/kubesight-backend -- python - <<'EOF'
import socket
for host in ("api.bitbucket.org", "bitbucket.org"):
    for ip in sorted({a[4][0] for a in socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)}):
        try:
            socket.create_connection((ip, 443), timeout=5).close(); print(host, ip, "reachable")
        except Exception as e:
            print(host, ip, "FAILED:", e)
EOF
```

Every line says `reachable`. Then, in KubeSight, Build Center → Register
service: with a repository URL and a credential profile, the branch list
fills in. A build of that service gets past its clone step.

- `timed out` after the upgrade: the firewall is not open yet.
- Reachable, but the branch list says the credential was refused: the token
  (its type, scopes, or the Atlassian email for an API token).
- Backend fine, but the build's clone times out: the firewall does not let
  the nodes out to `bitbucket.org` (build pods use the same nodes).

## Undo

Remove the two Bitbucket lines from `backendEgressFQDNs`, then the same
upgrade.

## Evidence

Upgrade: ____
