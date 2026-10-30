# Tutorial: your first look at Hermes

About fifteen minutes. Every step only reads. You need a session on the bastion
(`ssh kprodai@10.43.17.34`).

## 1. See it running

```bash
kubectl -n kubesight get deployment kubesight-hermes
kubectl -n kubesight get pods -l app.kubernetes.io/name=kubesight-hermes -o wide
kubectl -n kubesight get pvc kubesight-hermes-data
```

One pod, `Running` and `1/1` ready. Its data volume is `Bound`, 50Gi, class
`oci-bv-retain`.

## 2. Look at its configuration

Hermes's `config.yaml` comes from a ConfigMap and is copied into its data
volume on every start:

```bash
kubectl -n kubesight get configmap kubesight-hermes-config -o jsonpath='{.data.config\.yaml}'
```

Find these two parts:
- `model`: provider `openai-codex`, model `gpt-5.6-sol`, `base_url` `https://chatgpt.com/backend-api/codex`;
- `mcp_servers.kubesight`: KubeSight's MCP server, with the token read from
  `${MCP_KUBESIGHT_API_KEY}` rather than written in the file.

See which Secrets it reads. This shows key names only, never values:

```bash
kubectl -n kubesight get secret hermes -o go-template='{{range $k,$v := .data}}{{$k}}{{"\n"}}{{end}}'
kubectl -n kubesight get externalsecret hermes          # SecretSynced
```

## 3. Look inside its data volume

```bash
kubectl -n kubesight exec deploy/kubesight-hermes -c hermes -- ls -la /opt/data /opt/data/skills/kubesight
```

You see:
- `config.yaml`;
- the KubeSight skill (`SKILL.md` and `references/`), copied from the chart on every start;
- `.env`, which holds the MCP token. Never print `.env`.

## 4. Check that it answers

```bash
kubectl -n kubesight exec deploy/kubesight-hermes -c hermes -- \
  python3 -c "import urllib.request; print(urllib.request.urlopen('http://localhost:8642/health').read().decode())"
```

Then check the whole path in KubeSight, signed in as an admin:
- **Integrations → Hermes → Test connection** sends a request from the backend
  to Hermes, and from Hermes to OpenAI;
- **API Tokens** lists "Hermes MCP (set by the deployment)", the token Hermes
  uses to call KubeSight.

## 5. Read its logs

```bash
kubectl -n kubesight logs deploy/kubesight-hermes -c hermes --tail=50
kubectl -n kubesight logs deploy/kubesight-hermes -c stage-hermes-files
```

The first shows requests and tool calls. The second is the step that copies the
config and skill before Hermes starts. It prints nothing when all went well.

## 6. See where it may connect

```bash
kubectl -n kubesight get networkpolicy kubesight-hermes -o yaml
kubectl -n kubesight get ciliumnetworkpolicy kubesight-hermes-egress -o yaml
```

- **Into Hermes:** port 8642, from `kubesight` and `kubesight-ci` only.
- **Out of Hermes:** DNS, the backend on port 5000 (with the namespace's
  same-namespace rule, any pod in `kubesight`), and `chatgpt.com`, `auth.openai.com` and `models.dev` on 443.
  Nothing else.

You have seen Hermes end to end. The [how-to guides](how-to.md) cover the
changes you may need to make.
