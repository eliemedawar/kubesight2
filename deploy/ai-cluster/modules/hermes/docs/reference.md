# Reference: Hermes

As rendered by `charts/kubesight` with `deploy/ai-cluster/values-ai-cluster.yaml`,
release `kubesight`, namespace `kubesight`.

## Objects

| Kind | Name | Namespace |
|---|---|---|
| Deployment | `kubesight-hermes` | `kubesight` |
| Service (ClusterIP) | `kubesight-hermes` | `kubesight` |
| ServiceAccount | `kubesight-hermes` (no token mounted) | `kubesight` |
| ConfigMap | `kubesight-hermes-config` (key `config.yaml`) | `kubesight` |
| ConfigMap | `kubesight-hermes-skill` (the KubeSight skill) | `kubesight` |
| PersistentVolumeClaim | `kubesight-hermes-data` | `kubesight` |
| ExternalSecret → Secret | `hermes` | `kubesight` |
| ExternalSecret → Secret | `hermes-env` (the Telegram bot, CHG-KS-006) | `kubesight` |
| NetworkPolicy | `kubesight-hermes` | `kubesight` |
| CiliumNetworkPolicy | `kubesight-hermes-egress` | `kubesight` |

Shared with the rest of `kubesight`:
- NetworkPolicies `default-deny` and `allow-dns-and-same-namespace`;
- ExternalSecrets `kubesight-secrets` (its two tokens) and `regcred`.

## Image, ports and containers

| | |
|---|---|
| Image | `registry.areeba.com/kubesight-hermes:v2026.7.20`, pulled with `regcred` |
| Port | `http` 8642: the OpenAI-style API, `/v1/chat/completions`, health at `/health` |
| Service URL | `http://kubesight-hermes.kubesight.svc.cluster.local:8642/v1/chat/completions` |
| Init containers | `prepare-s6-run` (root: owns `/run` for uid 10000), `stage-hermes-files` (uid 10000: copies config, skill, MCP token) |
| Main container | `hermes`, args `gateway run`, uid/gid 10000, no privilege escalation |
| Pod | `fsGroup: 10000`, `seccompProfile: RuntimeDefault`, strategy `Recreate`, 1 replica |
| Probes | readiness and liveness `GET /health` on `http` |

## Settings

| Values key | Value here |
|---|---|
| `hermes.enabled` | `true` |
| `hermes.image` | `kubesight-hermes:v2026.7.20` (registry `registry.areeba.com`) |
| `hermes.codexAuth.existingSecret` | `hermes` |
| `hermes.extraEnvFromSecrets` | `[hermes-env]` |
| `hermes.config.model` | `provider: openai-codex`, `default: gpt-5.6-sol`, `base_url: https://chatgpt.com/backend-api/codex`, `max_tokens: 8192` |
| `hermes.config.agent` | `max_turns: 20`, `gateway_timeout: 900`, `api_max_retries: 1`, `reasoning_effort: low` |
| `hermes.maxIterations` | `20` |
| `hermes.installSkill` | `true` |
| `hermes.persistence` | `50Gi`, `ReadWriteOnce`, class `oci-bv-retain` |
| `hermes.config.platform_toolsets` | `api_server`, `cli` and `telegram`: `[kubesight]` |
| `hermes.networkPolicy.egressFQDNs` | `[chatgpt.com, auth.openai.com, models.dev, api.telegram.org]` |
| `hermes.resources` | requests 250m CPU / 512Mi / 256Mi disk; limits 2 CPU / 2Gi / 2Gi disk |
| `hermes.initResources` | requests 10m CPU / 32Mi; limits 500m CPU / 128Mi |

## Environment

| Variable | Value |
|---|---|
| `HERMES_HOME`, `HOME` | `/opt/data` |
| `HERMES_UID`, `HERMES_GID` | `10000` |
| `API_SERVER_ENABLED`, `API_SERVER_HOST`, `API_SERVER_PORT` | `true`, `0.0.0.0`, `8642` |
| `API_SERVER_MODEL_NAME` | `hermes-agent` |
| `HERMES_MAX_ITERATIONS` | `20` |
| `API_SERVER_KEY` | Secret `kubesight-secrets`, key `HERMES_API_TOKEN` |
| `MCP_KUBESIGHT_API_KEY` | Secret `kubesight-secrets`, key `KUBESIGHT_HERMES_MCP_TOKEN` |
| `CODEX_HOME` | `/opt/codex`, where the Secret `hermes` is mounted |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_ALLOWED_USERS`, `TELEGRAM_HOME_CHANNEL` | Secret `hermes-env` (from `kv/kubesight/hermes-env`), all its fields |

`config.yaml` also names the MCP server `kubesight` at
`http://backend-service.kubesight.svc.cluster.local:5000/api/mcp`, with a timeout
of 120 s.

## Volumes

| Name | From | Mounted at |
|---|---|---|
| `data` | PVC `kubesight-hermes-data` (50Gi, `oci-bv-retain`, kept on uninstall) | `/opt/data` |
| `s6-run` | emptyDir in memory, 64Mi | `/run` |
| `tmp` | emptyDir, 256Mi | `/tmp` |
| `config` | ConfigMap `kubesight-hermes-config` | `/config` (init only) |
| `skill` | ConfigMap `kubesight-hermes-skill` | `/skill` (init only) → `/opt/data/skills/kubesight` |
| `codex-auth` | Secret `hermes` (mode 0440) | `/opt/codex`, read-only |

## Secrets

| Secret | Keys | From OpenBao |
|---|---|---|
| `hermes` | `auth.json` | `kv/kubesight/hermes` |
| `kubesight-secrets` | `HERMES_API_TOKEN`, `KUBESIGHT_HERMES_MCP_TOKEN` (and the backend's keys) | `kv/kubesight/backend` |
| `regcred` | `.dockerconfigjson` | `kv/kubesight/registry` |

All refresh every hour. A changed key reaches Hermes only after a restart. Hermes
keeps its own renewed copy of the login in `/opt/data`.

## Network rules

| Rule | Effect on Hermes |
|---|---|
| `default-deny` | nothing in or out unless allowed below |
| `allow-dns-and-same-namespace` | DNS to `kube-dns`; traffic to and from pods in `kubesight` |
| `kubesight-hermes` | in: TCP 8642 from namespaces `kubesight`, `kubesight-ci`; out: DNS (kube-system, 53), TCP 5000 to `kubesight` |
| `kubesight-hermes-egress` (Cilium) | out: DNS through Cilium's DNS proxy; TCP 443 to `chatgpt.com`, `auth.openai.com` and `models.dev` only |

Firewall: `chatgpt.com:443`, `auth.openai.com:443` and `models.dev:443` must be opened for the
cluster (asked with CHG-KS-003). The opening agreed on 2026-10-28 was for
`api.anthropic.com`.

## Labels

| Where | Labels |
|---|---|
| Every object | `app.kubernetes.io/part-of: kubesight`, `app.kubernetes.io/instance: kubesight`, `app.kubernetes.io/managed-by: Helm`, `helm.sh/chart: kubesight-0.1.0`, `areeba.com/module: hermes`, `areeba.com/change: <change ID>` |
| Pod | `app.kubernetes.io/name: kubesight-hermes`, `areeba.com/module: hermes` |

## Quota use (namespace `kubesight`)

The pod counts as 250m CPU and 512Mi of the 3 CPU / 8Gi requests, 2Gi of the
16Gi memory limits, one of the 30 pods, and one of the 6 claims (50Gi of 300Gi).
