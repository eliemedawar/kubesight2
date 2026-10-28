# CHG-KS-003: Add Hermes, on the Claude API

| Field | Value |
|---|---|
| ID | CHG-KS-003 |
| Modules | hermes |
| Status | Waiting: there is no Claude API key yet |
| Asked by | Hussein (platform), handover of 2026-10-28: "Hermes calls the Claude API directly, allowed out to api.anthropic.com only" |
| Depends on | CHG-KS-002 done, with its evidence; a Claude API key; the firewall opening to `api.anthropic.com:443` (agreed by the platform, 2026-10-28) |
| Prepared by | ____ |
| Approved by | ____ (name, date) |
| Run by | ____ (name, date) |

## What

1. One OpenBao entry, `kv/kubesight/hermes`, with the field `ANTHROPIC_API_KEY`.
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
| ExternalSecret | `hermes` (from `kv/kubesight/hermes`) |
| NetworkPolicy | `kubesight-hermes` (callers: `kubesight` and `kubesight-ci`, port 8642; out: DNS and the backend's 5000) |
| CiliumNetworkPolicy | `kubesight-hermes-egress` (HTTPS to `api.anthropic.com` only) |

It also changes the ConfigMap `kubesight-config` (adds `HERMES_API_URL`,
`TICKET_AGENT_HERMES_URL` and `HERMES_SERVICE_*`), so the backend restarts once.
Model: `claude-opus-5-5` (`hermes.config.model.default`).

## Why

The ticket agent, AI source analysis and CI assist all run through Hermes.
CHG-KS-002 installed KubeSight without it, because there was no key yet.

## Steps

On the bastion, in `~/kubesight`, with the two lines changed:

```bash
# 1. The key, at a hidden prompt.
source openbao-login
read -rs -p 'Anthropic API key: ' v; echo
printf '%s' "$v" | bao kv put -mount=kv kubesight/hermes ANTHROPIC_API_KEY=-; unset v

# 2. Read only: what changes, and the API server's judgement.
CHG=CHG-KS-003; TAG=1.0.0
ARGS="-n kubesight -f deploy/ai-cluster/values-ai-cluster.yaml --set image.tag=$TAG --set-string commonLabels.areeba\.com/change=$CHG"
helm template kubesight charts/kubesight $ARGS > /tmp/kubesight-$CHG.yaml
grep -E '^kind:' /tmp/kubesight-$CHG.yaml | sort | uniq -c        # 1 more Deployment, 5 ExternalSecrets, no Ingress
kubectl apply --dry-run=server -f /tmp/kubesight-$CHG.yaml
helm upgrade kubesight charts/kubesight $ARGS --dry-run=server > /dev/null && echo "helm dry run OK"

# 3. After a person's explicit "yes":
helm upgrade kubesight charts/kubesight $ARGS
```

## Check

```bash
bao kv get -mount=kv -format=json kubesight/hermes | grep -oE '"[A-Z][A-Z_]+":'   # "ANTHROPIC_API_KEY":
kubectl -n kubesight get externalsecret hermes                                   # SecretSynced
kubectl -n kubesight rollout status deploy/kubesight-hermes --timeout=5m
kubectl -n kubesight rollout status deploy/kubesight-backend --timeout=5m
kubectl -n kubesight logs deploy/kubesight-hermes -c hermes --tail=50
```

In KubeSight: **Integrations → Hermes → Test connection** answers. If it
reports an unknown model, set `hermes.config.model.default` to one Hermes
lists (for example `claude-opus-4-8`) and upgrade again.

**Hubble**, in the Cilium pod on Hermes's node:

```bash
kubectl -n kube-system exec <cilium pod> -c cilium-agent -- hubble observe --namespace kubesight --pod kubesight-hermes --last 100
kubectl -n kubesight exec deploy/kubesight-hermes -c hermes -- python3 -c "import urllib.request;urllib.request.urlopen('https://example.com',timeout=5)"   # must time out
```

**Must be allowed:**
- Hermes → `api.anthropic.com` 443;
- Hermes ↔ backend.

**Must be dropped:** Hermes → any other host.

## Undo

Set both lines back to `enabled: false` and run the same `helm upgrade`. The
claim `kubesight-hermes-data` stays (kept on purpose). The key stays in
OpenBao until `bao kv metadata delete -mount=kv kubesight/hermes`.

## Evidence

```
(to fill in after the change)
```
