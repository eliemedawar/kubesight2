# Explanation: why the backend is built this way

## One release for everything

The whole of KubeSight is one Helm release of `charts/kubesight`. The same
chart runs elsewhere. On this cluster its switches keep it inside the
platform's rules: `createNamespaces: false`, and
`externalSecrets.items`, `networkPolicies.*` and the module labels set. One
release means one upgrade, one history and one rollback for all the parts.

## Cluster-wide rights, as in UAT

The backend starts Jobs in `kubesight-ci` through two namespaced Roles there
(`kubesight-ci-launcher`, `kubesight-analysis-launcher`). It manages other
clusters through the kubeconfigs people save in it.

The first install stopped there (`rbac.clusterWide: false`), so this cluster
showed empty. CHG-KS-005 turns the switch on: the backend gets the same two
ClusterRoles as in UAT, `kubesight-backend-reader` (read everything) and
`kubesight-backend-deployer` (deploy, restart, scale, roll back, exec, in any
namespace), so KubeSight shows and manages this cluster too. They are the one
exception to the platform's "no cluster-wide objects". What keeps changes in
check is KubeSight's own roles and its cluster approval gate on `ociduai-k8s`.

## Secrets from OpenBao, and the one key that must never change

The chart can generate its own keys, but then they would be rendered by Helm
and kept in Helm's release history. Instead the keys are generated once on the
bastion, straight into `kv/kubesight/backend` (CHG-KS-001). The ExternalSecret
`kubesight-secrets` delivers them.

`KUBESIGHT_SECRET_KEY` encrypts every credential stored in the database:
cluster kubeconfigs, registry, Bitbucket, Zoho/Jira and vCenter logins. Lose
it, or change it, and all of them must be entered again. That is why OpenBao's
backups matter, and why it is never rotated.

## The database through the pooler

The backend's connection string is put together in the pod
(`database.fromLogin`). The user name and password come from the platform's
Secret `postgres`, which the platform keeps in step with OpenBao. Kubernetes
expands them into `DATABASE_URL`, with `sslmode=verify-full` and the CA from
the ConfigMap `postgres-ca`, mounted at `/etc/postgres-ca`. Nobody copies the
password, and a rotation reaches the backend at its next restart. The guide
says the generated password needs no URL escaping.

- **Session mode.** The pooler must be in session mode, which it is: the
  scheduler and the CI engine elect a leader with PostgreSQL advisory locks,
  and those belong to a session.
- **Connection pool.** The login may hold 40 connections. The backend's pool
  is cut from 16 + 20 to 12 + 12 (`DB_POOL_SIZE`, `DB_MAX_OVERFLOW`), which
  leaves room for a check or a restore pod.
- **Migrations.** The backend creates and migrates its own schema when it
  starts, serialised by an advisory lock. The platform accepted this in place
  of a separate migration Job.

## One replica, Recreate, a block volume

The read caches live in the process, so a second replica would show stale
lists. One replica, updated with `Recreate`, also suits the data volume. An
OCI block volume can be attached to one node at a time.

The volume holds files that must survive a restart:
- cluster kubeconfigs;
- CI artifacts and mobile binaries;
- analysis artifacts;
- OpenTofu work dirs.

It is 50Gi, the smallest OCI volume, on `oci-bv-retain`, so deleting the claim
by mistake keeps the disk.

## Closed by default

Every KubeSight namespace starts from `default-deny`. In `kubesight` the
backend may:
- talk to its neighbours (frontend, Hermes);
- reach its pooler;
- reach the Kubernetes API (a Cilium rule, since the API server is not a pod);
- receive callbacks on 5000 from `kubesight-ci`.

Each outside system it needs is added by name or address, with a firewall
opening, when that integration is turned on. A new integration is therefore
always a visible change, never a surprise.

## Not yet as strict as it could be

- The backend image runs as root. `baseline` allows it; `restricted` would
  warn. Moving to a non-root user needs an image change and testing.
- Objects the backend creates while running carry KubeSight's own labels, not
  `areeba.com/*`: build Jobs, their Secrets and their NetworkPolicies.
- `KUBESIGHT_AUTO_UPGRADE` is off. OKE upgrades its own control plane, and
  KubeSight's kubeadm-based upgrades do not apply here.
