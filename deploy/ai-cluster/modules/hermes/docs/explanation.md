# Explanation: why Hermes is built this way

## OpenAI, through a ChatGPT/Codex login

The handover planned the Claude API, but there is no Claude API key. Hermes
therefore runs as it does in UAT: its `openai-codex` provider signs in with the
`auth.json` file that `codex login` writes, and calls
`https://chatgpt.com/backend-api/codex`. It is the same image and chart; only
the `model` block and the login differ.

The login lives in `kv/kubesight/hermes`, field `auth.json`. The ExternalSecret
`hermes` copies it into a Secret of the same name, mounted read-only at
`/opt/codex` (`CODEX_HOME`). It never appears in Git, a values file or a Helm
release.

A Codex login is renewed by replacing it. On first start Hermes takes the
mounted file and keeps its own copy in `/opt/data`, which it renews through
`auth.openai.com`. The mounted file is only a fallback. This is why:
- Hermes needs a login of its own: two users of one login log each other out
  when one of them renews it. The account itself can be shared (here it is
  UAT's), as long as each Hermes signs in separately;
- a new login in OpenBao reaches Hermes only after a restart, and its own copy
  on the volume may still win (see the how-to).

## Outbound traffic by name, through Cilium

Hermes runs a model with tools. Whatever it is asked, it should be able to reach
only the model's API and KubeSight. A plain Kubernetes NetworkPolicy can name
addresses but not host names, and OpenAI's hosts have addresses we do not
control. Cilium's own rule kind can allow by name (`toFQDNs`). The chart creates
`kubesight-hermes-egress` with `chatgpt.com` and `auth.openai.com` on 443,
and `models.dev`: Hermes reads model details there (context size, image
support) when it starts, and without it waits 15 seconds per lookup.

Cilium learns which addresses belong to that name by watching Hermes's DNS
answers. That is why the same rule sends Hermes's DNS through Cilium's DNS
proxy (`rules: dns`): without it, the name rule would never match. Every other
outside address is dropped. The company firewall is a second gate: a name
allowed here still needs a firewall rule.

The chart's ordinary NetworkPolicy `kubesight-hermes` adds the rest:
- in: port 8642, from `kubesight` (the backend) and `kubesight-ci` (analysis Jobs);
- out: DNS, and the backend's port 5000 for MCP.

## Two tokens, both generated

- **`HERMES_API_TOKEN`:** the backend → Hermes direction. Hermes takes it as
  `API_SERVER_KEY`.
- **`KUBESIGHT_HERMES_MCP_TOKEN`:** the Hermes → KubeSight MCP direction. The
  backend registers it for the `hermes-agent` account when it starts, so nobody
  has to mint a token in the UI. A new value retires the old one at the next start.

Both were generated straight into `kv/kubesight/backend` and were never shown
to anyone.

## Its own volume, and fresh files on every start

Hermes keeps its memory, sessions and login state in `/opt/data`. That is its
own 50Gi volume on `oci-bv-retain` (50 GiB is the smallest OCI block volume), so
it survives restarts and is kept if the claim is deleted. Only one pod may use
the volume, so the Deployment uses `Recreate`.

Hermes edits its own `config.yaml`. A read-only mount would break it, and a
copy left on the volume would drift from the chart. So two init containers run
before it starts:
- `prepare-s6-run` gives `/run` to Hermes's user (uid 10000). Hermes's process
  supervisor needs this, and it is the only step that runs as root;
- `stage-hermes-files` copies the chart's `config.yaml` and the KubeSight skill
  onto the volume, and writes the MCP token into `/opt/data/.env`, where Hermes
  resolves `${MCP_KUBESIGHT_API_KEY}`.

So every start runs the config and skill of the installed release.

## No access to the cluster

Hermes's service account mounts no token: Hermes has no Kubernetes API access.
Everything it does in a cluster goes through KubeSight's MCP tools. There,
KubeSight's own roles, approval gates and audit trail apply to it like to a
person.

## Still to improve

`prepare-s6-run` runs as root, which `baseline` allows and `restricted` warns
about. The containers do not drop all capabilities yet.
