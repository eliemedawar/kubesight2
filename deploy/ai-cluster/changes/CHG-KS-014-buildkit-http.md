# CHG-KS-014: Let image builds install Debian packages

| Field | Value |
|---|---|
| ID | CHG-KS-014 |
| Modules | kubesight-buildkit |
| Status | Proposed |
| Asked by | Elie, 2026-11-06: the backend image build failed at `apt-get update` |
| Depends on | CHG-KS-013 |
| Prepared by | ____ |
| Approved by | ____ (name, date) |
| Run by | ____ (name, date) |

## What

A `helm upgrade` with `buildkit.egressPorts` in
`deploy/ai-cluster/values-ai-cluster.yaml` going from
`[443, 8443, 8082, 5000, 9443, 4443]` to the same plus **80**. BuildKit's
NetworkPolicy then lets it out on TCP 80 as well. The policy changes in place;
the BuildKit pods do not restart.

## Why

A Dockerfile's `RUN` steps run on BuildKit's network, so they get BuildKit's
rule, not the build pod's. The backend image's `apt-get update` fetches
`deb.debian.org` over plain HTTP (port 80, as every Debian base image does by
default), and failed:

```
#9 68.32 Could not connect to debian.map.fastlydns.net:80 (151.101.142.132), connection timed out
#9 68.50 E: Unable to locate package curl
```

Everything else that Dockerfile downloads is HTTPS (GitHub, `dl.k8s.io`,
`get.helm.sh`, PyPI, Go and OpenTofu modules), already allowed on 443.

## Before you start

The platform confirms the firewall lets the nodes reach `deb.debian.org`
on TCP 80 (a CDN, so by name if the firewall can). If they would rather keep
port 80 closed, the alternative is a Debian proxy repository in Nexus and the
Dockerfiles pointed at it; then this change is not needed.

## Steps

On the bastion: pull in `~/kubesight-ai`, copy `charts/kubesight` and
`deploy/ai-cluster` to `~/kubesight`, then in `~/kubesight`:

```bash
CHG=CHG-KS-014
TAG=$(kubectl -n kubesight get deploy kubesight-backend -o jsonpath='{.spec.template.spec.containers[0].image}' | sed 's/.*://'); echo "TAG=$TAG"
ARGS="-n kubesight -f deploy/ai-cluster/values-ai-cluster.yaml --set image.tag=$TAG --set-string commonLabels.areeba\.com/change=$CHG"

# Read only
helm template kubesight charts/kubesight $ARGS --show-only templates/buildkit.yaml | grep 'port: 80,'
helm upgrade kubesight charts/kubesight $ARGS --dry-run=server > /dev/null && echo "helm dry run OK"

# After a person's explicit "yes":
helm upgrade kubesight charts/kubesight $ARGS
```

## Check

Run the build again: the backend image's `apt-get update` lists
`Get:1 http://deb.debian.org/debian trixie InRelease` and the packages
install. Still `connection timed out` on port 80: the firewall.

## Undo

Remove `80` from `buildkit.egressPorts`, then the same upgrade.

## Evidence

Upgrade: ____
