# CHG-KS-001: Put KubeSight's secrets in OpenBao

| Field | Value |
|---|---|
| ID | CHG-KS-001 |
| Modules | kubesight-backend, kubesight-frontend, kubesight-ci |
| Status | Proposed |
| Asked by | Hussein (platform), handover of 2026-10-28 |
| Decided | Start empty: generate new keys, nothing carried over from UAT (Elie, 2026-10-28) |
| Prepared by | ____ |
| Approved by | ____ (name, date) |
| Run by | ____ (name, date) |

## What

Create four OpenBao entries. The Claude API key for Hermes is not part of this
change: there is none yet, and it comes with CHG-KS-003. No Kubernetes object is created by this change.

| Entry | Fields | Where the values come from |
|---|---|---|
| `kv/kubesight/backend` | `JWT_SECRET_KEY`, `FLASK_SECRET_KEY`, `KUBESIGHT_SECRET_KEY`, `HERMES_API_TOKEN`, `KUBESIGHT_HERMES_MCP_TOKEN`, `KUBESIGHT_INITIAL_ADMIN_PASSWORD` | generated on the bastion, never shown |
| `kv/kubesight/tls` | `tls.crt`, `tls.key` | the certificate files for `kubesight.areeba.com` |
| `kv/kubesight/registry` | `registry`, `username`, `password` | the password typed at a hidden prompt |
| `kv/kubesight-ci/registry` | the same | the same |

## Why

The rules allow secrets only in OpenBao, delivered by `ExternalSecret`. The
install (CHG-KS-002) reads only these entries, plus the platform's Secret
`postgres` for the database login. Each namespace's store reads only its own path, so the
registry login is written once per namespace that pulls KubeSight images.

`KUBESIGHT_SECRET_KEY` encrypts every credential KubeSight stores in its
database. It must **never change** once KubeSight is in use. Step 2 refuses to
overwrite an existing entry, and OpenBao is backed up (platform, 2026-10-28).

## Steps

On the bastion, as `kprodai`:

```bash
# 1. Sign in, and make sure none of the entries exists yet.
source openbao-login
bao kv list -mount=kv kubesight; bao kv list -mount=kv kubesight-ci
bao kv put -h | grep -n -- ' -$\|stdin'     # confirm "-" reads the whole entry as JSON from stdin

# 2. kubesight/backend: generated, never shown. printf is a shell builtin, so
#    the values never appear in a process list. Stops if the entry exists.
if bao kv metadata get -mount=kv kubesight/backend >/dev/null 2>&1; then
  echo "kubesight/backend already exists: stop and ask"
else
  printf '{"JWT_SECRET_KEY":"%s","FLASK_SECRET_KEY":"%s","KUBESIGHT_SECRET_KEY":"%s","HERMES_API_TOKEN":"%s","KUBESIGHT_HERMES_MCP_TOKEN":"ksa_%s","KUBESIGHT_INITIAL_ADMIN_PASSWORD":"%s"}' \
    "$(openssl rand -hex 32)" "$(openssl rand -hex 32)" "$(openssl rand -hex 32)" "$(openssl rand -hex 24)" \
    "$(openssl rand -hex 20)" "$(openssl rand -base64 24 | tr -d '/+=')" \
    | bao kv put -mount=kv kubesight/backend -
fi

# 3. kubesight/tls: copy kubesight.crt (full chain) and kubesight.key to the bastion first.
bao kv put -mount=kv kubesight/tls tls.crt=@kubesight.crt tls.key=@kubesight.key && shred -u kubesight.key kubesight.crt

# 4. The registry login, once per namespace.
read -r -p 'Registry user: ' u; read -rs -p 'Registry password: ' p; echo
for ns in kubesight kubesight-ci; do
  printf '%s' "$p" | bao kv put -mount=kv $ns/registry registry=registry.areeba.com username="$u" password=-
done; unset u p
```

If step 1 shows that `bao kv put` cannot read the entry from stdin, write the
JSON into a file made with `mktemp`, use `@file`, then run `shred -u` on it.

## Check

Field names only. The `grep` keeps the names and drops every value.

```bash
bao kv list -mount=kv kubesight; bao kv list -mount=kv kubesight-ci
bao kv get -mount=kv -format=json kubesight/backend | grep -oE '"[A-Z][A-Z_]+":'       # the 6 names
bao kv get -mount=kv -format=json kubesight/tls     | grep -oE '"tls\.(crt|key)":'
for p in kubesight/registry kubesight-ci/registry; do bao kv get -mount=kv -format=json $p | grep -oE '"(registry|username|password)":'; done
```

The change is done when every list shows all its names.

## Undo

Only before CHG-KS-002 is installed. After that, deleting `kubesight/backend`
makes the stored credentials unreadable.

```bash
for p in kubesight/backend kubesight/tls kubesight/registry kubesight-ci/registry; do
  bao kv metadata delete -mount=kv $p
done
```

## Evidence

Paste the real output of **Check** here, with the date and who ran it.

```
(to fill in after the change)
```
