# How-to guides: KubeSight frontend

Every change below needs its own change record first, and a person's "yes".
Helm commands run from the folder that holds `charts/kubesight` and
`deploy/ai-cluster`. In them, `<tag>` is the release now installed
(`helm -n kubesight list` shows it), and `<change ID>` is your change record.

## Change the TLS certificate

1. Copy the new certificate and key to the bastion, then write them to OpenBao
   and destroy the files:
   ```bash
   source openbao-login
   bao kv put -mount=kv kubesight/tls tls.crt=@kubesight.crt tls.key=@kubesight.key
   shred -u kubesight.key kubesight.crt
   ```
2. Copy it into the Secret now, instead of within the hour:
   ```bash
   kubectl -n kubesight annotate externalsecret kubesight-tls force-sync="$(date +%s)" --overwrite
   kubectl -n kubesight get externalsecret kubesight-tls        # SecretSynced
   ```
3. nginx reads the certificate when it starts, so restart the pods:
   ```bash
   kubectl -n kubesight rollout restart deployment/kubesight-frontend
   kubectl -n kubesight rollout status deployment/kubesight-frontend
   ```
4. Check the dates people now see:
   ```bash
   echo | openssl s_client -connect kubesight.areeba.com:443 -servername kubesight.areeba.com 2>/dev/null | openssl x509 -noout -subject -dates
   ```

## Find the load balancer's address, and ask for DNS and the firewall

```bash
kubectl -n kubesight get service frontend-service -o jsonpath='{.status.loadBalancer.ingress[0].ip}'; echo
```

Send the platform:
- the address and port 443;
- the DNS name `kubesight.areeba.com`;
- who must reach it (the office networks).

They open the company firewall rule. The address stays the same while the
Service exists; deleting the Service gives a new one.

## Change the number of replicas

`frontend.replicas` is 2 in `deploy/ai-cluster/values-ai-cluster.yaml`. Edit it,
then:

```bash
helm upgrade kubesight charts/kubesight -n kubesight -f deploy/ai-cluster/values-ai-cluster.yaml \
  --set image.tag=<tag> --set-string 'commonLabels.areeba\.com/change=<change ID>'
kubectl -n kubesight get pods -l app=kubesight-frontend
```

Each replica requests 50m CPU and 64Mi, with a 128Mi memory limit. The
namespace quota (3 CPU / 8Gi requests, 16Gi memory limits, 30 pods) is shared
with the backend and Hermes.

## Check that /api reaches the backend

```bash
kubectl -n kubesight exec deploy/kubesight-frontend -- wget -qO- http://localhost/health; echo
kubectl -n kubesight exec deploy/kubesight-frontend -- wget -qO- http://backend-service:5000/health; echo
```

If the first fails and the second answers, the problem is nginx's configuration
(`kubectl -n kubesight get configmap kubesight-frontend-nginx -o yaml`). If both
fail, look at the backend (`kubectl -n kubesight logs deploy/kubesight-backend --tail=50`).

Requests that hang or are cut off may be network rules. Look for drops with
Hubble, in the Cilium pod on the frontend pod's node:

```bash
kubectl -n kubesight get pods -l app=kubesight-frontend -o wide
kubectl -n kube-system get pods -l k8s-app=cilium -o wide
kubectl -n kube-system exec <cilium pod on that node> -c cilium-agent -- \
  hubble observe --namespace kubesight --label app=kubesight-frontend --verdict DROPPED --last 50
```

## Roll back a release

```bash
helm -n kubesight history kubesight
helm -n kubesight rollback kubesight <revision>
kubectl -n kubesight rollout status deployment/kubesight-frontend
```

A rollback moves the whole release (backend, Hermes, CI) to that revision, not
only the frontend. It does not undo the backend's database migrations. Write the
rollback in a change record like any other change.
