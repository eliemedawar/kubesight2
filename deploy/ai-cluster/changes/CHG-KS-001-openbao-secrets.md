# CHG-KS-001: Put KubeSight's secrets in OpenBao

| Field | Value |
|---|---|
| ID | CHG-KS-001 |
| Modules | kubesight-backend, kubesight-frontend, kubesight-ci |
| Status | Done, 2026-10-28 (registry password proven at install, CHG-KS-002) |
| Asked by | Hussein (platform), handover of 2026-10-28 |
| Decided | Start empty: generate new keys, nothing carried over from UAT (Elie, 2026-10-28) |
| Prepared by | Elie Medawar |
| Approved by | ____ (Rachid,2026-10-28 ) |
| Run by | ____ (Elie, 2026-10-28) |

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
# Paste the whole block at once; its variable names are its own.
read -r -p 'Registry user: ' reguser; read -rs -p 'Registry password: ' regpass; echo
for ns in kubesight kubesight-ci; do
  printf '%s' "$regpass" | bao kv put -mount=kv $ns/registry registry=registry.areeba.com username="$reguser" password=-
done; unset reguser regpass
```

If step 1 shows that `bao kv put` cannot read the entry from stdin, write the
JSON into a file made with `mktemp`, use `@file`, then run `shred -u` on it.

## Check

Field names only. The `grep` keeps the names and drops every value.

```bash
bao kv list -mount=kv kubesight; bao kv list -mount=kv kubesight-ci
bao kv get -mount=kv -format=json kubesight/backend | grep -oE '"[A-Z][A-Z_]+":'       # the 6 names
bao kv get -mount=kv -format=json kubesight/tls     | grep -oE '"tls\.(crt|key)":'
for e in kubesight/registry kubesight-ci/registry; do bao kv get -mount=kv -format=json $e | grep -oE '"(registry|username|password)":'; done
```

The change is done when every list shows all its names.

## Undo

Only before CHG-KS-002 is installed. After that, deleting `kubesight/backend`
makes the stored credentials unreadable.

```bash
for e in kubesight/backend kubesight/tls kubesight/registry kubesight-ci/registry; do
  bao kv metadata delete -mount=kv $e
done
```

## Evidence

Paste the real output of **Check** here, with the date and who ran it.

**Step 3, `kv/kubesight/tls`.** Done 2026-10-28 by Elie. The certificate is
the `*.areeba.com` Sectigo certificate, the same one UAT uses, pasted onto the
bastion; the pair was checked before storing:

```
$ grep -c 'BEGIN CERTIFICATE' kubesight.crt
4
$ openssl x509 -in kubesight.crt -noout -subject -enddate
subject=CN = *.areeba.com
notAfter=Apr 27 23:59:59 2027 GMT
$ openssl x509 -in kubesight.crt -noout -pubkey | sha256sum
168dabb0d1f6f646e7d5f9610194d96a6d411b69fa5e313111a179c574a21192  -
$ openssl pkey -in kubesight.key -pubout | sha256sum
168dabb0d1f6f646e7d5f9610194d96a6d411b69fa5e313111a179c574a21192  -
$ bao kv put -mount=kv kubesight/tls tls.crt=@kubesight.crt tls.key=@kubesight.key && shred -u kubesight.key kubesight.crt
==== Secret Path ====
kv/data/kubesight/tls
created_time       2026-10-28T15:35:42.417415168Z
version            1
$ bao kv get -mount=kv -format=json kubesight/tls | grep -oE '"tls\.(crt|key)":'
"tls.crt":
"tls.key":
$ ls ~/kubesight.* 2>/dev/null || echo "files gone"
files gone
```

The certificate expires on **27 April 2027**. Renew it before then, then
follow the frontend how-to "Change the TLS certificate".

**Step 2, `kv/kubesight/backend`.** Done 2026-10-28 by Elie:

```
$ bao kv list -mount=kv kubesight
Keys
----
backend
postgres
tls
$ bao kv get -mount=kv -format=json kubesight/backend | grep -oE '"[A-Z][A-Z_]+":'
"FLASK_SECRET_KEY":
"HERMES_API_TOKEN":
"JWT_SECRET_KEY":
"KUBESIGHT_HERMES_MCP_TOKEN":
"KUBESIGHT_INITIAL_ADMIN_PASSWORD":
"KUBESIGHT_SECRET_KEY":
```

**Step 4, the registry login.** Done 2026-10-28 by Elie. A first attempt
wrote nothing: only the prompt line ran, and a later check loop reused the
variable `p`. It was rerun as one block with its own variable names
(`reguser`, `regpass`):

```
$ for ns in kubesight kubesight-ci; do printf '%s' "$regpass" | bao kv put -mount=kv $ns/registry registry=registry.areeba.com username="$reguser" password=-; done
======= Secret Path =======
kv/data/kubesight/registry
created_time       2026-10-28T15:41:36.549968795Z
version            1
======== Secret Path ========
kv/data/kubesight-ci/registry
created_time       2026-10-28T15:41:36.779467179Z
destroyed          false
```

Whether the password is right shows at install: the pods pull
`registry.areeba.com/kubesight-*:1.0.0` with it (CHG-KS-002).
