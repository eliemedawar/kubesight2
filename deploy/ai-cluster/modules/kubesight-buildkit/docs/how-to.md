# How-to guides: BuildKit builders

Every guide that changes the cluster needs its own change record and a
person's "yes" first. Settings change in
`deploy/ai-cluster/values-ai-cluster.yaml` (in Git), then:

```bash
helm upgrade kubesight charts/kubesight -n kubesight -f deploy/ai-cluster/values-ai-cluster.yaml \
  --set image.tag=<tag> --set-string 'commonLabels.areeba\.com/change=<change ID>'
```

## Check that the fence will accept a change, before making it

Render the release, then let the API server run every admission check without
saving anything. The checks include the fence, the quota and pod security.

```bash
helm template kubesight charts/kubesight -n kubesight -f deploy/ai-cluster/values-ai-cluster.yaml \
  --set image.tag=<tag> --set-string 'commonLabels.areeba\.com/change=<change ID>' > /tmp/kubesight.yaml
kubectl apply --dry-run=server -f /tmp/kubesight.yaml 2>&1 | grep -i -E 'kubesight-build|denied|forbidden|exceeded'
```

A refusal names the rule that failed. Paste it into the change record.

## Upgrade BuildKit

The fence accepts only `docker.io/moby/buildkit:<version>-rootless`. Change the
version and nothing else:

```yaml
buildkit:
  image: docker.io/moby/buildkit:v0.34.1-rootless
```

Then:

1. Run the dry run above.
2. Run the `helm upgrade`.
3. Watch the builders get replaced: `kubectl -n kubesight-build rollout status deploy/buildkitd`.

They are replaced one at a time, so builds keep a builder throughout. Then run
one image build from KubeSight. The client is `buildctl` inside the backend
image: when the backend image changes a lot, test a build in UAT first.

## Restart one builder

For example, when one builder misbehaves. Delete that pod: the other builder
keeps serving, and the Deployment makes a new one.

```bash
kubectl -n kubesight-build get pods -o wide
kubectl -n kubesight-build delete pod <pod name>
kubectl -n kubesight-build get pods -w
```

To restart both, one after the other, use
`kubectl -n kubesight-build rollout restart deploy/buildkitd`.

## Let builders reach a registry on another port

The builders may reach DNS and these ports out: 443, 8443, 8082, 5000, 9443
(`buildkit.egressPorts`). To add one:

```yaml
buildkit:
  egressPorts: [443, 8443, 8082, 5000, 9443, <port>]
```

Write the whole list: Helm replaces lists, it does not merge them. The
company firewall may need the same opening, so ask the platform. Build pods
need the registry's port too. That comes from the registry's settings in
KubeSight, or from `ci.extraEgressPorts`.

## A builder was OOM-killed

`kubectl -n kubesight-build get pods` shows restarts, and
`kubectl -n kubesight-build describe pod <pod>` shows `OOMKilled`. A builder may
use up to 4Gi (its limit). Large Java or Node images can need more.

The namespace allows **10Gi of memory limits**, and two builders at 4Gi use 8Gi.
Updates add no third pod (`maxSurge: 0`), so you have these choices:

- **raise the limit to 5Gi per builder** (10Gi in total, the quota's maximum);
- **run one builder** with a higher limit. You lose failover;
- **ask the platform for a bigger quota** in `kubesight-build`.

Run the dry run before any of these; an `exceeded quota` refusal means the
numbers don't fit.

## A build cannot reach the builders

The build fails with a connection timeout on 1234. Check:

- **the backend's setting:**
  `kubectl -n kubesight get configmap kubesight-config -o jsonpath='{.data.CI_BUILDKIT_NAMESPACE}'`
  must say `kubesight-build`;
- **at least one builder is ready:** `kubectl -n kubesight-build get pods`;
- **Hubble on the builder's node:**
  `hubble observe --namespace kubesight-build --verdict DROPPED`.
