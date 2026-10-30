# How-to guides: Hermes

Every change below needs its own change record first, and a person's "yes".
Helm commands run from the folder that holds `charts/kubesight` and
`deploy/ai-cluster`. In them, `<tag>` is the release now installed
(`helm -n kubesight list` shows it), and `<change ID>` is your change record.
OpenBao commands need `source openbao-login` first.

## Replace the login

For a new account, or when Hermes reports that its login has expired.

1. Make a new `auth.json` with `codex login` on a computer with a browser, as in
   CHG-KS-003 ("Before you start"), then put it into OpenBao from the bastion:
   ```bash
   umask 077
   stty -echo; cat > /dev/shm/auth.json; stty echo     # paste, Enter, Ctrl-D
   bao kv put -mount=kv kubesight/hermes auth.json=@/dev/shm/auth.json
   shred -u /dev/shm/auth.json
   ```
2. Copy it into the Secret now, instead of within the hour:
   ```bash
   kubectl -n kubesight annotate externalsecret hermes force-sync="$(date +%s)" --overwrite
   kubectl -n kubesight get externalsecret hermes          # SecretSynced
   ```
3. Hermes keeps its own renewed copy on its volume, which may win over the new
   file. Look for it, remove it, and restart:
   ```bash
   kubectl -n kubesight exec deploy/kubesight-hermes -c hermes -- ls -la /opt/data
   kubectl -n kubesight exec deploy/kubesight-hermes -c hermes -- rm -f /opt/data/auth.json
   kubectl -n kubesight rollout restart deployment/kubesight-hermes
   kubectl -n kubesight rollout status deployment/kubesight-hermes
   ```
4. In KubeSight, **Integrations → Hermes → Test connection**.
5. If the old account is retired, sign it out of all devices in its ChatGPT
   security settings.

## Change the model

Set `hermes.config.model.default` in `deploy/ai-cluster/values-ai-cluster.yaml`,
to another model the ChatGPT account offers. Then:

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
   to the names already there.
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

When AI Guard runs in `ai-guard`, Hermes will call it instead of OpenAI.
Expect these changes, each written up once AI Guard's address and API are known:
- `hermes.config.model.base_url` (and `provider`, if AI Guard needs another one);
- `hermes.networkPolicy.egressFQDNs`: `chatgpt.com` and `auth.openai.com` removed;
- a rule allowing Hermes to reach AI Guard's Service in `ai-guard`;
- the login in `kv/kubesight/hermes`, replaced by AI Guard's own key if it uses one.

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
- **forwarded:** DNS, `backend-service:5000`, `chatgpt.com:443`, `auth.openai.com:443`, `models.dev:443`;
- **dropped:** any other outside address.

To prove the drop, try a host that is not allowed:

```bash
kubectl -n kubesight exec deploy/kubesight-hermes -c hermes -- \
  python3 -c "import urllib.request; urllib.request.urlopen('https://example.com', timeout=5)"
```

It should time out, and Hubble shows it as `DROPPED`.
