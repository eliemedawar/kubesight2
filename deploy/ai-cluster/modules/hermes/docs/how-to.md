# How-to guides: Hermes

Every change below needs its own change record first, and a person's "yes".
Helm commands run from the folder that holds `charts/kubesight` and
`deploy/ai-cluster`. In them, `<tag>` is the release now installed
(`helm -n kubesight list` shows it), and `<change ID>` is your change record.
OpenBao commands need `source openbao-login` first.

## Rotate the Claude API key

1. Type the new key at a hidden prompt. It goes straight into OpenBao:
   ```bash
   read -rs -p 'Anthropic API key: ' v; echo; printf '%s' "$v" | bao kv put -mount=kv kubesight/hermes ANTHROPIC_API_KEY=-; unset v
   ```
2. Copy it into the Secret now, instead of within the hour:
   ```bash
   kubectl -n kubesight annotate externalsecret hermes force-sync="$(date +%s)" --overwrite
   kubectl -n kubesight get externalsecret hermes          # SecretSynced
   ```
3. Hermes reads the key when it starts, so restart it:
   ```bash
   kubectl -n kubesight rollout restart deployment/kubesight-hermes
   kubectl -n kubesight rollout status deployment/kubesight-hermes
   ```
4. In KubeSight, **Integrations → Hermes → Test connection**.
5. Revoke the old key in the Anthropic console.

## Change the model

Set `hermes.config.model.default` in `deploy/ai-cluster/values-ai-cluster.yaml`,
for example to `claude-sonnet-5-5`. Then:

```bash
helm upgrade kubesight charts/kubesight -n kubesight -f deploy/ai-cluster/values-ai-cluster.yaml \
  --set image.tag=<tag> --set-string 'commonLabels.areeba\.com/change=<change ID>'
kubectl -n kubesight rollout status deployment/kubesight-hermes
```

The config's checksum is on the pod, so a changed config restarts Hermes by
itself. Check the result with **Test connection**.

## Let Hermes reach another outside host

For example, `api.telegram.org` for approvals by Telegram.

1. Add the name to `hermes.networkPolicy.egressFQDNs` in the values file, next
   to `api.anthropic.com`.
2. Ask the platform to open the company firewall for that name on 443. Cilium's
   rule alone is not enough.
3. Run `helm upgrade` as above.
4. Check with Hubble, in the Cilium pod on Hermes's node:
   ```bash
   kubectl -n kubesight get pods -l app.kubernetes.io/name=kubesight-hermes -o wide
   kubectl -n kube-system get pods -l k8s-app=cilium -o wide
   kubectl -n kube-system exec <cilium pod on that node> -c cilium-agent -- \
     hubble observe --namespace kubesight --to-fqdn api.telegram.org --last 20
   ```

## Move Hermes onto AI Guard (planned)

When AI Guard runs in `ai-guard`, Hermes will call it instead of the Claude API.
Expect these changes, each written up once AI Guard's address and API are known:
- `hermes.config.model.base_url` (and `provider`, if AI Guard needs another one);
- `hermes.networkPolicy.egressFQDNs`: `api.anthropic.com` removed;
- a rule allowing Hermes to reach AI Guard's Service in `ai-guard`;
- the key in `kv/kubesight/hermes`, if AI Guard uses its own.

Ask the platform for the details before writing that change record.

## Reissue the MCP token

Hermes calls KubeSight's MCP server with `KUBESIGHT_HERMES_MCP_TOKEN`. The
backend registers it at start-up and retires the previous one.

1. Write a new token into OpenBao without showing it:
   ```bash
   printf '%s' "ksa_$(openssl rand -hex 20)" | bao kv patch -mount=kv kubesight/backend KUBESIGHT_HERMES_MCP_TOKEN=-
   ```
   If `kv patch` is refused, add `-method=rw`.
2. Copy it into the Secret, then restart the backend first, then Hermes:
   ```bash
   kubectl -n kubesight annotate externalsecret kubesight-secrets force-sync="$(date +%s)" --overwrite
   kubectl -n kubesight rollout restart deployment/kubesight-backend
   kubectl -n kubesight rollout status deployment/kubesight-backend --timeout=5m
   kubectl -n kubesight rollout restart deployment/kubesight-hermes
   ```
3. In KubeSight, **API Tokens** shows the new "Hermes MCP (set by the deployment)"
   token; **Test connection** still works.

## Check Hermes's network rules with Hubble

```bash
kubectl -n kube-system exec <cilium pod on Hermes's node> -c cilium-agent -- \
  hubble observe --namespace kubesight --label app.kubernetes.io/name=kubesight-hermes --last 50
kubectl -n kube-system exec <cilium pod on Hermes's node> -c cilium-agent -- \
  hubble observe --namespace kubesight --label app.kubernetes.io/name=kubesight-hermes --verdict DROPPED --last 50
```

Expected:
- **forwarded:** DNS, `backend-service:5000`, `api.anthropic.com:443`;
- **dropped:** any other outside address.

To prove the drop, try a host that is not allowed:

```bash
kubectl -n kubesight exec deploy/kubesight-hermes -c hermes -- \
  python3 -c "import urllib.request; urllib.request.urlopen('https://example.com', timeout=5)"
```

It should time out, and Hubble shows it as `DROPPED`.
