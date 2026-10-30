# CHG-KS-006: Connect Hermes's gateway to Telegram

| Field | Value |
|---|---|
| ID | CHG-KS-006 |
| Modules | hermes |
| Status | Proposed. The OpenBao entry is written (2026-10-30); waiting for the firewall opening to `api.telegram.org` |
| Asked by | Elie, 2026-10-30: "configure hermes gateway from the files", Telegram as in UAT |
| Depends on | CHG-KS-003 (Hermes running); a bot of its own from @BotFather, **not UAT's** (two pollers on one token keep taking each other's messages); the firewall opening to `api.telegram.org:443` |
| Prepared by | ____ |
| Approved by | ____ (name, date) |
| Run by | ____ (name, date) |

## What

1. One OpenBao entry, `kv/kubesight/hermes-env`, fields `TELEGRAM_BOT_TOKEN`,
   `TELEGRAM_ALLOWED_USERS` (`8831815740,2081346797`) and
   `TELEGRAM_HOME_CHANNEL` (`8831815740`, where cron results and notices go).
2. A `helm upgrade` with these changes in `deploy/ai-cluster/values-ai-cluster.yaml`:
   - `externalSecrets.items`: new item `hermes-env` (from `kubesight/hermes-env`);
   - `hermes.extraEnvFromSecrets: [hermes-env]`: its fields become Hermes's
     environment, which is where the gateway reads them;
   - `hermes.config.platform_toolsets.telegram: [kubesight]`: in Telegram,
     Hermes has KubeSight's tools only, like its API server and `cli`;
   - `hermes.networkPolicy.egressFQDNs`: `api.telegram.org` added.

The bot polls Telegram (outbound only); nothing new reaches into the cluster.
Hermes restarts once (its config changes). Only the people in
`TELEGRAM_ALLOWED_USERS` get answers.

Settings go in the values file, not inside the pod: every start copies the
chart's `config.yaml` over `/opt/data/config.yaml`, so `hermes gateway setup`
or edits made in the pod are lost at the next restart.

## Why

To talk to Hermes, and get its notices, from Telegram, as in UAT.

## Before you start

1. The OpenBao entry exists (done 2026-10-30, see Evidence):
   ```bash
   bao kv get -mount=kv -format=json kubesight/hermes-env | grep -oE '"TELEGRAM_[A-Z_]+":'
   ```
2. The platform confirms the firewall lets the nodes reach
   `api.telegram.org:443`. (No pod of ours can test it before the upgrade:
   Cilium lets only Hermes out, and only to the names in its rule.)

## Steps

On the bastion: pull in `~/kubesight-ai`, copy `charts/kubesight` and
`deploy/ai-cluster` to `~/kubesight`, then in `~/kubesight`:

```bash
CHG=CHG-KS-006
TAG=$(kubectl -n kubesight get deploy kubesight-backend -o jsonpath='{.spec.template.spec.containers[0].image}' | sed 's/.*://'); echo "TAG=$TAG"
ARGS="-n kubesight -f deploy/ai-cluster/values-ai-cluster.yaml --set image.tag=$TAG --set-string commonLabels.areeba\.com/change=$CHG"

# Read only
helm template kubesight charts/kubesight $ARGS | grep -A8 'name: hermes-env$'          # the ExternalSecret
helm template kubesight charts/kubesight $ARGS | grep -B1 -A1 'name: hermes-env'       # also under envFrom
helm template kubesight charts/kubesight $ARGS | grep 'api.telegram.org'               # in the egress rule
helm upgrade kubesight charts/kubesight $ARGS --dry-run=server > /dev/null && echo "helm dry run OK"

# After a person's explicit "yes":
helm upgrade kubesight charts/kubesight $ARGS
kubectl -n kubesight get externalsecret hermes-env                 # SecretSynced, Ready True
kubectl -n kubesight rollout status deployment/kubesight-hermes
```

If the Hermes pod shows `CreateContainerConfigError`, the Secret was not
there yet when it started; it starts by itself once the ExternalSecret has
synced.

## Check

```bash
kubectl -n kubesight logs deploy/kubesight-hermes -c hermes | grep -i telegram    # connected, polling; no 401 or 409
```

Then send the bot a message from one of the two allowed accounts: Hermes
answers. From any other account: no answer.

`409 Conflict` in the log means another program polls the same token (UAT's
Hermes, or KubeSight's ticket agent): give each its own bot.

## Undo

Remove the `hermes-env` item, set `extraEnvFromSecrets: []`, drop
`platform_toolsets.telegram` and `api.telegram.org`, then the same upgrade.
The OpenBao entry can stay, or: `bao kv metadata delete -mount=kv kubesight/hermes-env`.

## Evidence

2026-10-30, the OpenBao entry, from the bastion:

```
$ printf '%s' "$tok" | bao kv put -mount=kv kubesight/hermes-env TELEGRAM_BOT_TOKEN=- TELEGRAM_ALLOWED_USERS=8831815740,2081346797 TELEGRAM_HOME_CHANNEL=8831815740
kv/data/kubesight/hermes-env   version 1   created 2026-10-30T13:49:54Z
$ bao kv get -mount=kv -format=json kubesight/hermes-env | grep -oE '"TELEGRAM_[A-Z_]+":'
"TELEGRAM_ALLOWED_USERS":
"TELEGRAM_BOT_TOKEN":
"TELEGRAM_HOME_CHANNEL":
```

Upgrade: ____
