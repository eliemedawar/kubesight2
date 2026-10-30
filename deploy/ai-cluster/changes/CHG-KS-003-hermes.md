# CHG-KS-003: Add Hermes, on OpenAI through a ChatGPT/Codex login

| Field | Value |
|---|---|
| ID | CHG-KS-003 |
| Modules | hermes |
| Status | Waiting: the platform's agreement to OpenAI instead of the Claude API, and the firewall opening below |
| Asked by | Elie, 2026-10-30: "install Hermes on OpenAI", with the ChatGPT/Codex login, as in UAT. This replaces the handover's plan (2026-10-28: the Claude API, `api.anthropic.com` only). |
| Depends on | CHG-KS-002 done, with its evidence; KubeSight `1.0.3` running; a ChatGPT account used by this Hermes only; the firewall opening to `chatgpt.com:443` and `auth.openai.com:443` |
| Prepared by | ____ |
| Approved by | ____ (name, date) |
| Run by | ____ (name, date) |

## What

1. One OpenBao entry, `kv/kubesight/hermes`, with the field `auth.json`: the
   Codex login file of the account Hermes uses.
2. A `helm upgrade` of the `kubesight` release that turns Hermes on. In
   `deploy/ai-cluster/values-ai-cluster.yaml`, change exactly two lines:
   - under `hermes:`, `enabled: false` → `enabled: true`;
   - under `externalSecrets.items`, the `hermes` item's `enabled: false` → `enabled: true`.

   Leave every other `enabled:` as it is: `ingress.enabled` must stay `false`.

The upgrade adds, in `kubesight` only:

| Kind | Objects |
|---|---|
| Deployment, Service, ServiceAccount | `kubesight-hermes` |
| PersistentVolumeClaim | `kubesight-hermes-data` (50Gi, `oci-bv-retain`) |
| ConfigMap | `kubesight-hermes-config`, `kubesight-hermes-skill` |
| ExternalSecret | `hermes` (from `kv/kubesight/hermes`), mounted read-only at `/opt/codex` |
| NetworkPolicy | `kubesight-hermes` (callers: `kubesight` and `kubesight-ci`, port 8642; out: DNS and the backend's 5000) |
| CiliumNetworkPolicy | `kubesight-hermes-egress` (HTTPS to `chatgpt.com` and `auth.openai.com` only) |

It also changes the ConfigMap `kubesight-config` (adds `HERMES_API_URL`,
`TICKET_AGENT_HERMES_URL` and `HERMES_SERVICE_*`), so the backend restarts once.
Model: `gpt-5.6-sol`, provider `openai-codex` (`hermes.config.model`), the same
as UAT.

## Why

The ticket agent, AI source analysis and CI assist all run through Hermes.
CHG-KS-002 installed KubeSight without it. There is still no Claude API key;
the ChatGPT/Codex login is what UAT already runs on.

## Before you start: the login file

The login must belong to **this Hermes only**. Codex renews its login by
replacing it: if UAT's Hermes or a laptop uses the same login, whichever renews
first logs the other out.

On a computer with a browser, signed in to the ChatGPT account meant for this
Hermes:

```bash
npm install -g @openai/codex     # if Codex is not installed yet
codex login                      # opens the browser; choose "Sign in with ChatGPT"
```

That writes `~/.codex/auth.json` (on Windows `%USERPROFILE%\.codex\auth.json`).
Keep it open for the paste below. Then delete that file. Do not run
`codex logout`, which may end the session Hermes is about to use.

## Steps

On the bastion, in `~/kubesight`, with the two lines changed:

```bash
# 1. The login, pasted with the screen echo off, into memory only.
source openbao-login
umask 077
stty -echo; cat > /dev/shm/auth.json; stty echo     # paste auth.json, Enter, then Ctrl-D
python3 -m json.tool /dev/shm/auth.json > /dev/null && echo "auth.json is valid JSON"
bao kv put -mount=kv kubesight/hermes auth.json=@/dev/shm/auth.json
shred -u /dev/shm/auth.json

# 2. Read only: what changes, and the API server's judgement.
CHG=CHG-KS-003; TAG=1.0.3
ARGS="-n kubesight -f deploy/ai-cluster/values-ai-cluster.yaml --set image.tag=$TAG --set-string commonLabels.areeba\.com/change=$CHG"
helm template kubesight charts/kubesight $ARGS > /tmp/kubesight-$CHG.yaml
grep -E '^kind:' /tmp/kubesight-$CHG.yaml | sort | uniq -c        # 1 more Deployment, 5 ExternalSecrets, no Ingress
grep -A3 'toFQDNs' /tmp/kubesight-$CHG.yaml                        # chatgpt.com, auth.openai.com
kubectl apply --dry-run=server -f /tmp/kubesight-$CHG.yaml
helm upgrade kubesight charts/kubesight $ARGS --dry-run=server > /dev/null && echo "helm dry run OK"

# 3. After a person's explicit "yes":
helm upgrade kubesight charts/kubesight $ARGS
```

## Check

```bash
bao kv get -mount=kv -format=json kubesight/hermes | python3 -c "import json,sys; print(list(json.load(sys.stdin)['data']['data']))"   # ['auth.json']
kubectl -n kubesight get externalsecret hermes                                   # SecretSynced
kubectl -n kubesight get secret hermes -o go-template='{{range $k,$v := .data}}{{$k}}{{"\n"}}{{end}}'   # auth.json
kubectl -n kubesight rollout status deploy/kubesight-hermes --timeout=5m
kubectl -n kubesight rollout status deploy/kubesight-backend --timeout=5m
kubectl -n kubesight logs deploy/kubesight-hermes -c hermes --tail=50
```

In KubeSight: **Integrations → Hermes → Test connection** answers. If it
reports an unknown model, set `hermes.config.model.default` to a model the
account offers and upgrade again. If it reports a login error, look for drops
to `auth.openai.com` in Hubble first.

**Hubble**, in the Cilium pod on Hermes's node:

```bash
kubectl -n kube-system exec <cilium pod> -c cilium-agent -- hubble observe --namespace kubesight --pod kubesight-hermes --last 100
kubectl -n kubesight exec deploy/kubesight-hermes -c hermes -- python3 -c "import urllib.request;urllib.request.urlopen('https://example.com',timeout=5)"   # must time out
```

**Must be allowed:**
- Hermes → `chatgpt.com` 443 and `auth.openai.com` 443;
- Hermes ↔ backend.

**Must be dropped:** Hermes → any other host.

## Undo

Set both lines back to `enabled: false` and run the same `helm upgrade`. The
claim `kubesight-hermes-data` stays (kept on purpose), and so does Hermes's own
copy of the login on it. The login stays in OpenBao until
`bao kv metadata delete -mount=kv kubesight/hermes`. To end the session itself,
sign out of all devices from the ChatGPT account's security settings.

## Evidence

```
(to fill in after the change)
```
