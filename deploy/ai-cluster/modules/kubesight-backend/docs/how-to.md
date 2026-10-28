# How-to guides: the backend

Every change below needs its own change record and a person's "yes" before it
runs. Commands are for the bastion, in the folder holding `charts/` and
`deploy/`.

## Upgrade to a new release

1. Push the release tag to Bitbucket and wait for its pipeline to push
   `kubesight-backend:<tag>` and `kubesight-frontend:<tag>`.
2. Note the time: the platform's backups allow a restore to any moment. A
   rollback does not undo database migrations.
3. Check, then apply:

```bash
ARGS="-n kubesight -f deploy/ai-cluster/values-ai-cluster.yaml --set image.tag=<tag> --set-string commonLabels.areeba\.com/change=<change ID>"
helm upgrade kubesight charts/kubesight $ARGS --dry-run=server > /dev/null && echo OK
helm upgrade kubesight charts/kubesight $ARGS
kubectl -n kubesight rollout status deploy/kubesight-backend --timeout=5m
```

The CI build pods and analysis Jobs use the backend image at the same tag, so
they move with it.

## Roll back a release

```bash
helm history kubesight -n kubesight
helm rollback kubesight <revision> -n kubesight
```

If the newer release migrated the database, ask the platform for a restore to
the time noted before the upgrade.

## Change a setting

Plain settings go under `backend.config` in `values-ai-cluster.yaml`, for
example `LOG_LEVEL: "DEBUG"`, then run the upgrade above with the same tag.
The pod restarts by itself, because the ConfigMap's checksum is on it.

## Let the backend reach an outside system

When an integration is turned on (Bitbucket, Zoho, Jira, SMTP, a managed
cluster's API server, vCenter, ...):

1. Add it to `values-ai-cluster.yaml`:
   ```yaml
   networkPolicies:
     backendEgressFQDNs:
       - {name: bitbucket.org}                 # 443 unless ports are given
       - {name: smtp.areeba.com, ports: [587]}
     backendEgressCIDRs:
       - {cidr: 10.4.20.10/32, ports: [6443]}  # an address with no name
   ```
2. Ask the platform for the firewall opening (host, port).
3. Upgrade with the same tag, then check with Hubble that the traffic is allowed:
   ```bash
   kubectl -n kube-system exec <cilium pod on the backend's node> -c cilium-agent -- \
     hubble observe --namespace kubesight --pod kubesight-backend --last 50
   ```

## Rotate a secret

`JWT_SECRET_KEY` and `FLASK_SECRET_KEY` can be rotated; everyone is signed out.
**Never rotate `KUBESIGHT_SECRET_KEY`.** It decrypts every credential stored in
the database.

```bash
source openbao-login
printf '%s' "$(openssl rand -hex 32)" | bao kv patch -mount=kv kubesight/backend JWT_SECRET_KEY=-
kubectl -n kubesight annotate externalsecret kubesight-secrets force-sync="$(date +%s)" --overwrite
kubectl -n kubesight rollout restart deploy/kubesight-backend
```

The database password belongs to the platform. When they rotate it,
their Secret `postgres` follows. Then restart the backend.

## Restart it

```bash
kubectl -n kubesight rollout restart deploy/kubesight-backend
kubectl -n kubesight rollout status deploy/kubesight-backend --timeout=5m
```

The strategy is `Recreate`: the old pod stops before the new one starts, so
the data volume is never used by two pods. Expect a minute without the API.

## Read its logs, and find a failing request

```bash
kubectl -n kubesight logs deploy/kubesight-backend --since=15m | grep -iE 'error|traceback' -A5
kubectl -n kubesight logs deploy/kubesight-backend --previous     # after a crash
```

## See why it cannot reach something

```bash
kubectl -n kube-system exec <cilium pod on the backend's node> -c cilium-agent -- \
  hubble observe --namespace kubesight --pod kubesight-backend --verdict DROPPED --last 50
```

A drop here is a missing network rule (see above). A timeout with no drop is
usually the company firewall: ask the platform.
