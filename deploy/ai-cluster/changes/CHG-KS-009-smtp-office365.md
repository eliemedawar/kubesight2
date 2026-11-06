# CHG-KS-009: Let the backend send email through Microsoft 365

| Field | Value |
|---|---|
| ID | CHG-KS-009 |
| Modules | kubesight-backend |
| Status | Proposed. Waiting for the firewall opening to `smtp.office365.com:587` |
| Asked by | Elie, 2026-11-06: "i am trying to configure the smtp on oci but i got 504" |
| Depends on | CHG-KS-002 (backend running); the firewall opening to `smtp.office365.com:587` |
| Prepared by | ____ |
| Approved by | ____ (name, date) |
| Run by | ____ (name, date) |

## What

A `helm upgrade` with one change in `deploy/ai-cluster/values-ai-cluster.yaml`:
`networkPolicies.backendEgressFQDNs` gets `smtp.office365.com` on port 587.
The chart's Cilium rule `kubesight-backend-egress` then lets the backend
reach that name, and that name only, on 587. No image or migration change.
The backend pod does not restart (only the network rule changes).

The SMTP settings themselves (host `smtp.office365.com`, port 587, STARTTLS,
mailbox and password) are already saved in KubeSight, under Settings → Alert
Routing. They are stored in the database, encrypted, not in OpenBao.

## Why

KubeSight sends alert, sign-in, temporary-password and report emails. On this
cluster the backend's outgoing traffic is closed by default and nothing was
open for SMTP, so the connection hangs. KubeSight waits 30 seconds per address,
the gateway gives up first, and the SMTP test in the UI answers **504**.

## Before you start

The platform confirms the firewall lets the nodes reach
`smtp.office365.com:587` (TCP, outbound). The name resolves to many Microsoft
addresses that change. Open it by name if the firewall can. Otherwise use
Microsoft's published Exchange Online ranges. The addresses seen on
2026-11-06 are in `40.96.0.0/13` and `52.96.0.0/14` (see Evidence). The
backend has no IPv6 route; that is fine, it falls back to IPv4.

## Steps

On the bastion: pull in `~/kubesight-ai`, copy `charts/kubesight` and
`deploy/ai-cluster` to `~/kubesight`, then in `~/kubesight`:

```bash
CHG=CHG-KS-009
TAG=$(kubectl -n kubesight get deploy kubesight-backend -o jsonpath='{.spec.template.spec.containers[0].image}' | sed 's/.*://'); echo "TAG=$TAG"
ARGS="-n kubesight -f deploy/ai-cluster/values-ai-cluster.yaml --set image.tag=$TAG --set-string commonLabels.areeba\.com/change=$CHG"

# Read only
helm template kubesight charts/kubesight $ARGS | grep -B2 -A4 'smtp.office365.com'   # in the egress rule, port 587
helm upgrade kubesight charts/kubesight $ARGS --dry-run=server > /dev/null && echo "helm dry run OK"

# After a person's explicit "yes":
helm upgrade kubesight charts/kubesight $ARGS
kubectl -n kubesight get ciliumnetworkpolicy kubesight-backend-egress -o yaml | grep -A4 office365
```

## Check

From the backend pod, with the saved settings:

```bash
kubectl -n kubesight exec -i deploy/kubesight-backend -- python - <<'EOF'
import os, socket, psycopg2
cur = psycopg2.connect(os.environ["DATABASE_URL"]).cursor()
cur.execute("select host, port from alert_routing_smtp")
for host, port in cur.fetchall():
    for ip in sorted({a[4][0] for a in socket.getaddrinfo(host.strip(), port, socket.AF_INET, socket.SOCK_STREAM)}):
        try:
            socket.create_connection((ip, port), timeout=5).close(); print(ip, "reachable")
        except Exception as e:
            print(ip, "FAILED:", e)
EOF
```

Every address says `reachable`. Then, in KubeSight, Settings → Alert Routing →
**Send test email**: it answers "Test email sent to …" and the mail arrives.

- Still `timed out` after the upgrade: the firewall is not open yet (the
  Cilium rule is in place).
- Reachable, but the test says `Authentication unsuccessful` (535): the
  network works. Microsoft refused the login. SMTP AUTH has to be enabled
  for that mailbox in Microsoft 365. Also check that Microsoft still accepts
  password logins for SMTP on this tenant, because it has been retiring them.

## Undo

Set `backendEgressFQDNs: []` again, then the same upgrade. The SMTP settings
can stay in KubeSight; sends then fail as before.

## Evidence

2026-11-06, before the change, from the backend pod (saved settings read from
the database):

```
saved: host='smtp.office365.com' port=587 starttls=True ssl=False
  resolves to: ['2603:1046:c0c:814::2', … 8 IPv6 …, '40.99.32.114', '40.99.60.2', '40.99.68.34', '40.99.70.178', '40.99.70.210', '40.99.70.226', '52.98.61.34', '52.98.61.50']
  2603:1046:c0c:814::2 FAILED: [Errno 101] Network is unreachable      (all 8 IPv6 the same)
  40.99.32.114 FAILED: timed out                                       (all 8 IPv4 the same)
```

Upgrade: ____
