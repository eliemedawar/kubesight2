# KubeSight CI and analysis runs

KubeSight builds code itself, without Jenkins. For each build, the backend
starts one Kubernetes Job in `kubesight-ci`. The Job checks out the source,
runs each stage in its own container and reports back. Application
Intelligence's source analyses run in the same namespace, also as one Job
each. This module covers the namespace's accounts, permissions and rules, not
the backend that starts the Jobs.

| | |
|---|---|
| Owner | DevOps team |
| Purpose | Run CI builds and source analyses away from the backend, with no Kubernetes API access |
| Version | 1.0.0 (the backend image, which the checkout and collector steps also use) |
| Status | Planned: installed by CHG-KS-002 |
| Namespace | `kubesight-ci` (pod security `baseline`) |
| Runs as | uid 65532, non-root, no service account token; stage containers run the stage's own image |
| Depends on | `kubesight-backend` (starts the Jobs, receives callbacks), `kubesight-buildkit` (container image stages) |
| Used by | KubeSight's Service Catalog, Pipelines, Merge Checks and Application Intelligence |
| Secrets | `regcred` (registry pull secret, from OpenBao `kv/kubesight-ci/registry`); one short-lived Secret per build or analysis, made by the backend |
| Change records | [CHG-KS-001: secrets in OpenBao](../../../changes/CHG-KS-001-openbao-secrets.md), [CHG-KS-002: install](../../../changes/CHG-KS-002-install.md) |
| Signed | ____ (fill in your name) |

## Documents

- [Tutorial](tutorial.md): watch a build run, end to end.
- [How-to guides](how-to.md): one task each.
- [Explanation](explanation.md): why it is built this way.
- [Reference](reference.md): objects, settings, rules and limits.
